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
DISCORD_WEBHOOK    = os.environ.get("DISCORD_WEBHOOK", "")

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
    "google/gemma-3-27b-it:free",
    "qwen/qwen3-coder-480b-a35b:free",
    "nvidia/nemotron-3-super-49b:free",
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

MAX_TOKENS           = 700
MAX_TEXT_CHARS       = 8_000   # chars of filing_text sent to LLM (keep tokens manageable)
MAX_DISCORD_CHARS    = 1_900
SLEEP_BETWEEN_CALLS  = 4       # seconds between OpenRouter calls (stay under 20 req/min)
MAX_RETRIES          = 2       # retry on transient HTTP errors
RETRY_DELAY          = 10      # seconds between retries
REQUEST_TIMEOUT      = 90      # seconds to wait for LLM response

# ---------------------------------------------------------------------------
# System prompt (IDENTITY.md embedded)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You monitor a queue of SEC filings sourced directly from EDGAR.
Analyze the filing JSON provided. Return ONLY the formatted Discord summary.
No markdown code fences, no curl commands, no commentary, no sign-offs. Plain text only.

One filing = one message. Never combine multiple filings.
Keep each message under 1800 characters.

Rules:
- Return plain text only.
- Never invent data. If a field is not explicitly stated, write "Not disclosed".
- Never include raw HTML, CSS, or JavaScript.
- Prefer explicit values from the filing payload.
- Do not fabricate proceeds, pricing, coupon, maturity, exchange ratio, offering size, or use of proceeds.

Output format by form type:

8-K (Current Report):
🔔 TICKER | 8-K | Date
Company: Entity Name
Item(s): [e.g. Item 2.02 — Results of Operations]
Summary: 2-3 sentence plain-English summary of what happened
Key figures: Revenue / EPS / dividend changes / M&A details / preferred stock calls or redemptions
Accession: XXXXXXXXXX-XX-XXXXXX

424B2 / 424B3 / 424B5 (Prospectus Supplement):
📄 TICKER | 424Bx | Date
Company: Entity Name
Offering: Type of security (notes, warrants, units, preferred, baby bonds, etc.)
Key terms: Principal / rate / maturity / offering price
Notable: Public listing (which exchange)? Annual yield %? Issue size ($)? Par value? Call features? Use of proceeds (will it retire existing preferred/baby bonds)? Change of control clauses?
Accession: XXXXXXXXXX-XX-XXXXXX

10-Q / 10-K (Quarterly / Annual Report):
📊 TICKER | 10-Q/10-K | Period
Company: Entity Name
Revenue: $X (±Y% YoY if available)
Net Income: $X
EPS: $X
Key risk or highlight: 1 sentence
Accession: XXXXXXXXXX-XX-XXXXXX

S-3 / S-11 (Shelf Registration):
🗂 TICKER | S-3 | Date
Company: Entity Name
Shelf size: $X or share count
Securities registered: [common / preferred / baby bonds / debt / warrants]
Notable: Stated use of proceeds or ATM program
Accession: XXXXXXXXXX-XX-XXXXXX

SC 13D / 13G (Beneficial Ownership):
👤 TICKER | SC 13D/G | Date
Filer: Name of reporting person / entity
Ownership: X% of outstanding shares
Shares held: X shares
Purpose: [investment / control / passive]
Accession: XXXXXXXXXX-XX-XXXXXX

DEF 14A (Proxy Statement):
🗳 TICKER | DEF 14A | Date
Company: Entity Name
Meeting date: [if stated]
Key proposals: [director elections / say-on-pay / shareholder proposals]
Notable: Contested vote or unusual proposal
Accession: XXXXXXXXXX-XX-XXXXXX

Unknown / Other:
📎 TICKER | FORM-TYPE | Date
Company: Entity Name
Summary: 2-3 sentence plain-English description of the filing
Accession: XXXXXXXXXX-XX-XXXXXX

If filing_text is empty or boilerplate only, post a minimal alert with ticker, form type, date, and accession. Do not fabricate content."""

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
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {resp.status}")


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

        except urllib.error.HTTPError as e:
            body_snippet = e.read(300).decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {e.code}: {body_snippet}")
            if e.code == 404:
                # Model not available — signal caller to try next fallback
                raise _ModelUnavailableError(f"Model unavailable ({model}): {body_snippet}")
            if e.code in (429, 500, 502, 503, 504):
                log.warning(f"OpenRouter HTTP {e.code} on {model} (attempt {attempt}), retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
                continue
            raise last_error  # other 4xx — won't recover

        except _ModelUnavailableError:
            raise  # propagate immediately so fallback logic kicks in

        except Exception as e:
            last_error = e
            if attempt <= MAX_RETRIES:
                log.warning(f"OpenRouter error on {model} (attempt {attempt}): {e}, retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            continue

    raise last_error or RuntimeError(f"OpenRouter call failed after retries (model: {model})")


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

    # Trim to Discord limit
    if len(summary) > MAX_DISCORD_CHARS:
        summary = summary[:MAX_DISCORD_CHARS] + "\n...(truncated)"

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
