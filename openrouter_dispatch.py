#!/usr/bin/env python3
"""
OpenRouter dispatcher for the OpenClaw sec-analyst pipeline.

What this does:
  - Reads all *.json filing payloads from filings-inbox/
  - For each filing, calls OpenRouter with the sec-analyst prompt
  - Posts the returned summary to Discord via webhook
  - Moves processed filings to filings-inbox/processed/
  - Sends a Discord alert if a call fails or returns no content
  - Persists dispatched accessions so restarts never duplicate posts

Rate limiting:
  - Most OpenRouter free models: 20 req/min, 200 req/day
  - SLEEP_BETWEEN_CALLS (default 4s) keeps us at ~15 req/min → safe margin
  - If you're hitting 200/day, reduce MAX_DAILY_DISPATCHES or upgrade model
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, UTC
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — all secrets come from environment variables (GitHub Secrets)
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DISCORD_WEBHOOK    = os.environ.get("DISCORD_WEBHOOK", "").strip()

# Primary model — override via GitHub variable OPENROUTER_MODEL.
# Falls back through FALLBACK_MODELS if the primary returns HTTP 404
# (model removed / no endpoints). Add/remove entries freely.
MODEL = os.environ.get(
    "OPENROUTER_MODEL",
    "meta-llama/llama-3.3-70b-instruct:free",
)

FALLBACK_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
    "openai/gpt-oss-120b:free",
    "openai/gpt-oss-20b:free",
    "meta-llama/llama-3.2-3b-instruct:free",
]

# Build the final ordered list: primary first, then fallbacks (no duplicates)
_seen: set[str] = set()
MODEL_LIST: list[str] = []
for _m in [MODEL] + FALLBACK_MODELS:
    if _m not in _seen:
        MODEL_LIST.append(_m)
        _seen.add(_m)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

BASE_DIR   = Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).parent))
INBOX_DIR  = BASE_DIR / "filings-inbox"
PROCESSED  = INBOX_DIR / "processed"
LOG_FILE   = BASE_DIR / "dispatch.log"
DISPATCHED_FILE = BASE_DIR / "dispatched_accessions.json"

MAX_TOKENS           = 700     # ~500 words — enough for a tight summary under 2000 chars
MAX_TEXT_CHARS       = 400_000 # send essentially the full filing text
MAX_DISCORD_CHARS    = 1_900   # Discord hard limit is 2000; each chunk stays under
SLEEP_BETWEEN_CALLS  = 4       # seconds between OpenRouter calls (stay under 20 req/min)
MAX_RETRIES          = 1       # retry once on transient errors, then try next model
RETRY_DELAY          = 3       # seconds between retries
REQUEST_TIMEOUT      = 90      # seconds to wait for LLM response

# ---------------------------------------------------------------------------
# System prompt (IDENTITY.md embedded)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a fixed-income trading analyst monitoring SEC filings for market-moving events.

Read the full filing text provided. Extract ONLY information that is actionable or relevant to a professional fixed-income trader — specifically someone trading preferred stocks, baby bonds, exchange-traded notes, CEF/BDC debt, and other retail fixed-income instruments.

WHAT MATTERS (cover any of these found in the filing):
- New preferred stock or baby bond issuance: ticker, exchange listing, coupon/rate, par value, maturity, call date/price, issue size, use of proceeds (especially if retiring existing securities)
- Redemption or call of existing publicly traded preferred/baby bond: which series, call price, call/redemption date
- Tender offer or exchange offer affecting fixed-income securities
- Distribution change (increase, cut, suspension, omission): amount, frequency, effective date
- M&A event: acquirer/target, implications for existing preferred/baby bonds (change of control put, rating impact, successor obligor)
- For CEFs and BDCs: NII per share vs distribution (coverage ratio), NAV per share, discount/premium to NAV, managed distribution policy changes, leverage ratio changes, asset coverage
- Credit rating change or watch/review status
- Any event that could move the price or sentiment of publicly traded fixed-income instruments

WHAT TO IGNORE:
- Common stock operations, equity compensation, share buybacks (unless they affect capital structure relevant to fixed-income)
- Routine earnings beats/misses unless they directly affect distribution coverage or credit quality
- SEC website boilerplate, navigation text, legal disclaimers without substance
- If the filing contains no fixed-income relevant information, respond with only: "⚪ TICKER | FORM | Date — No fixed-income impact."

OUTPUT RULES:
- Plain text only. No markdown code fences. No commentary or sign-offs.
- Must fit in one Discord message: stay under 1800 characters total.
- Be direct and specific. Use exact numbers from the filing — do not round or estimate.
- Never fabricate data. If a specific figure is not stated, omit it rather than writing "Not disclosed".
- Format:

[EMOJI] TICKER | FORM | Date
Company: Name
[2-4 lines covering only the fixed-income relevant facts with exact figures]
Accession: XXXXXXXXXX-XX-XXXXXX

Emoji guide: 📄 new issuance | 🔔 redemption/call | ✂️ distribution cut/suspension | 💰 distribution raise | 📊 CEF/BDC financials | ⚠️ M&A/restructuring | 🔁 tender/exchange offer | 🔔 other material event"""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DISPATCH] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

INBOX_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED.mkdir(parents=True, exist_ok=True)

log.info(f"DISCORD_WEBHOOK len={len(DISCORD_WEBHOOK)} prefix={DISCORD_WEBHOOK[:50]!r}")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_dispatched() -> set[str]:
    if DISPATCHED_FILE.exists():
        try:
            return set(json.loads(DISPATCHED_FILE.read_text()))
        except Exception as e:
            log.warning(f"Could not load dispatched_accessions.json: {e}")
    return set()


def save_dispatched(dispatched: set[str]) -> None:
    try:
        DISPATCHED_FILE.write_text(json.dumps(sorted(dispatched)))
    except Exception as e:
        log.warning(f"Could not save dispatched_accessions.json: {e}")


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def post_discord(content: str) -> None:
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/ChrisVrj/sec-analyst, 1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Discord returned HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read(300).decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord HTTP {e.code}: {body}")


def send_discord(content: str, label: str = "") -> None:
    try:
        post_discord(content)
        if label:
            log.info(f"Posted to Discord: {label}")
    except Exception as e:
        log.error(f"Discord post failed ({label}): {e}")


def send_discord_alert(content: str) -> None:
    try:
        post_discord(content)
    except Exception as e:
        log.warning(f"Discord alert failed: {e}")


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------

def build_user_message(filing: dict) -> str:
    text = filing.get("filing_text", "") or ""
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n...(truncated)..."

    return (
        f"Ticker:      {filing.get('ticker', 'UNKNOWN')}\n"
        f"Form type:   {filing.get('form_type', '')}\n"
        f"Filed:       {filing.get('file_date', '')}\n"
        f"Entity:      {filing.get('entity_name', '')}\n"
        f"Accession:   {filing.get('accession', '')}\n"
        f"CIK:         {filing.get('cik', '')}\n"
        f"EDGAR URL:   {filing.get('filing_url', '')}\n\n"
        f"Filing text:\n{text}"
    )


def call_openrouter_model(filing: dict, model: str) -> str:
    """
    Call OpenRouter with a specific model.
    Returns the model's text response.
    Raises RuntimeError on unrecoverable error.
    Raises ModelUnavailableError (subclass) on HTTP 404 so the caller can try the next model.
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    body = json.dumps({
        "model":      model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": build_user_message(filing)},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://github.com/openclaw/sec-poller",
            "X-Title":       "OpenClaw SEC Analyst",
        },
        method="POST",
    )

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read())

            choices = data.get("choices") or []
            if not choices:
                raise ValueError(f"OpenRouter returned no choices: {data}")

            content = (choices[0].get("message") or {}).get("content", "").strip()
            if not content:
                raise ValueError("OpenRouter returned empty content")

            return content

        except _ModelUnavailableError:
            raise  # propagate immediately so fallback logic kicks in

        except urllib.error.HTTPError as e:
            body_snippet = e.read(300).decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {e.code}: {body_snippet}")
            if e.code in (404, 400):
                # 404 = no endpoints; 400 = invalid model ID — both mean try next model
                raise _ModelUnavailableError(f"Model unavailable ({model}): {body_snippet}")
            if e.code in (429, 500, 502, 503, 504):
                log.warning(f"OpenRouter HTTP {e.code} on {model} (attempt {attempt}), retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                raise last_error  # other 4xx — won't recover

        except Exception as e:
            last_error = e
            if attempt <= MAX_RETRIES:
                log.warning(f"OpenRouter error on {model} (attempt {attempt}): {e}, retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)

    # All retries exhausted — treat as unavailable so outer loop tries next model
    raise _ModelUnavailableError(f"Model {model} failed after all attempts: {last_error}")


class _ModelUnavailableError(RuntimeError):
    """Raised when a model returns 404 — triggers fallback to next model."""


def call_openrouter(filing: dict) -> str:
    """
    Try each model in MODEL_LIST in order.
    Moves to the next model on 404 (model gone / no endpoints).
    Raises RuntimeError only if every model fails.
    """
    last_error: Exception | None = None

    for model in MODEL_LIST:
        try:
            result = call_openrouter_model(filing, model)
            if model != MODEL_LIST[0]:
                log.info(f"Fallback succeeded with model: {model}")
            return result
        except _ModelUnavailableError as e:
            log.warning(f"Model unavailable, trying next fallback. ({e})")
            last_error = e
            continue
        except Exception as e:
            # Non-404 failure — don't try other models, surface the error
            raise

    raise last_error or RuntimeError("All models in fallback list exhausted")


# ---------------------------------------------------------------------------
# Move to processed
# ---------------------------------------------------------------------------

def move_to_processed(filing_path: Path, prefix: str = "") -> None:
    ts   = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    dest = PROCESSED / f"{prefix}{ts}_{filing_path.name}"
    try:
        filing_path.rename(dest)
        log.info(f"Moved → processed/{dest.name}")
    except Exception as e:
        log.error(f"Could not move {filing_path.name}: {e}")


# ---------------------------------------------------------------------------
# Dispatch one filing
# ---------------------------------------------------------------------------

def dispatch(filing_path: Path) -> None:
    try:
        filing = json.loads(filing_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"Could not read {filing_path.name}: {e}")
        move_to_processed(filing_path, prefix="err_")
        return

    ticker     = filing.get("ticker", "UNKNOWN")
    accession  = filing.get("accession", filing_path.stem)
    form_type  = filing.get("form_type", "")
    file_date  = filing.get("file_date", "")
    edgar_url  = filing.get("filing_url", "")

    log.info(f"Dispatching {ticker:10s} | {form_type:12s} | {accession}")

    try:
        summary = call_openrouter(filing)
    except Exception as e:
        log.error(f"OpenRouter failed for {accession}: {e}")
        send_discord_alert(
            f"❌ **{ticker}** | {form_type} | {file_date}\n"
            f"OpenRouter error: {str(e)[:200]}\n"
            f"Manual review: <{edgar_url}>\n"
            f"`{accession}`"
        )
        move_to_processed(filing_path, prefix="err_")
        return

    if not summary:
        log.error(f"Empty summary for {accession}")
        send_discord_alert(
            f"⚠️ **{ticker}** | {form_type} | {file_date}\n"
            f"Model returned no content.\n"
            f"Manual review: <{edgar_url}>\n"
            f"`{accession}`"
        )
        move_to_processed(filing_path, prefix="err_")
        return

    if len(summary) > MAX_DISCORD_CHARS:
        summary = summary[:MAX_DISCORD_CHARS]

    send_discord(summary, label=f"{ticker} / {accession}")
    log.info(f"Summary preview: {summary[:300]}")
    move_to_processed(filing_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not OPENROUTER_API_KEY:
        log.error("OPENROUTER_API_KEY environment variable is not set. Exiting.")
        raise SystemExit(1)

    if not DISCORD_WEBHOOK:
        log.error("DISCORD_WEBHOOK environment variable is not set. Exiting.")
        raise SystemExit(1)

    dispatched = load_dispatched()
    log.info(f"Loaded {len(dispatched)} previously dispatched accessions.")

    pending = sorted(INBOX_DIR.glob("*.json"))

    if not pending:
        log.info("No filings in inbox — nothing to dispatch.")
        return

    log.info(f"Found {len(pending)} filing(s) to dispatch. Primary model: {MODEL_LIST[0]} ({len(MODEL_LIST)} fallbacks configured)")
    changed = False

    for fp in pending:
        # Read accession without fully loading the file
        try:
            raw_acc = json.loads(fp.read_text(encoding="utf-8")).get("accession", fp.stem)
        except Exception:
            raw_acc = fp.stem

        if raw_acc in dispatched:
            log.warning(f"Duplicate in inbox: {raw_acc} — moving out")
            move_to_processed(fp, prefix="dup_")
            continue

        dispatch(fp)
        dispatched.add(raw_acc)
        changed = True

        # Rate limiting: stay well under OpenRouter's 20 req/min free limit
        time.sleep(SLEEP_BETWEEN_CALLS)

    if changed:
        # Trim to last 10,000 to prevent unbounded growth
        if len(dispatched) > 10_000:
            dispatched = set(sorted(dispatched)[-10_000:])
        save_dispatched(dispatched)

    log.info("Dispatch run complete.")


if __name__ == "__main__":
    main()
