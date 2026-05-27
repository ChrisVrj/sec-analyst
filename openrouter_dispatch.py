#!/usr/bin/env python3
"""
OpenRouter dispatcher for the OpenClaw sec-analyst pipeline.

Pipeline:
  1. Read all *.json filing payloads from filings-inbox/
  2. Pre-filter each filing against prefilter.should_skip (drops Form 3/4/5
     from non-activists and unlisted/structured 424B & FWP offerings)
  3. For surviving filings, call OpenRouter with the sec-analyst prompt
  4. Post the structured summary to Discord via webhook
  5. Move processed filings to filings-inbox/processed/
  6. Send a Discord alert if a call fails or returns no content
  7. Persist dispatched accessions so restarts never duplicate posts

Rate limiting:
  - OpenRouter free tier ≈ 20 req/min, 200 req/day
  - SLEEP_BETWEEN_CALLS (default 4s) keeps us at ~15 req/min — safe margin
  - Pre-filtered (skipped) filings DO NOT count against the rate limit
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, UTC
from pathlib import Path

from prefilter import should_skip

# ---------------------------------------------------------------------------
# Config — all secrets come from environment variables (GitHub Secrets)
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DISCORD_WEBHOOK    = os.environ.get("DISCORD_WEBHOOK", "").strip()

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

_seen: set[str] = set()
MODEL_LIST: list[str] = []
for _m in [MODEL] + FALLBACK_MODELS:
    if _m not in _seen:
        MODEL_LIST.append(_m)
        _seen.add(_m)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

BASE_DIR        = Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).parent))
INBOX_DIR       = BASE_DIR / "filings-inbox"
PROCESSED       = INBOX_DIR / "processed"
LOG_FILE        = BASE_DIR / "dispatch.log"
DISPATCHED_FILE = BASE_DIR / "dispatched_accessions.json"

MAX_TOKENS          = 900
MAX_TEXT_CHARS      = 400_000
MAX_DISCORD_CHARS   = 1_900
SLEEP_BETWEEN_CALLS = 4
MAX_RETRIES         = 1
RETRY_DELAY         = 3
REQUEST_TIMEOUT     = 90

# ---------------------------------------------------------------------------
# System prompt — structured Discord output with priority highlight block.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a fixed-income trading analyst. Your reader is a professional trader of PUBLICLY TRADED preferred stocks, baby bonds, exchange-traded debt, CEFs, and BDCs. You read one SEC filing at a time and write a structured Discord summary.

== PRIORITY ORDER ==
Lead with the highest-priority event the filing actually discloses. Use it to pick the [EMOJI] for line 1 and to decide which highlight block (if any) to include.
1. Redemption / call of an existing publicly traded security
2. New issuance that WILL BE LISTED on NYSE or NASDAQ
3. M&A or change-of-control affecting publicly traded preferreds / baby bonds
4. Tender or exchange offer for a publicly traded security
5. Distribution change on a publicly traded security
6. CEF / BDC NAV or financial update
7. Other material event

== OUTPUT TEMPLATE ==

Line 1: [EMOJI] **TICKER | FORM | YYYY-MM-DD** — one-sentence headline (≤120 chars)
Line 2: Company: <legal name>

[HIGHLIGHT BLOCK — include ONLY if a priority-1 to priority-4 trigger is LITERALLY stated in the filing. If unsure, omit the block. Never fabricate.]

For redemption / call of publicly traded security:
## 🚨 REDEMPTION OF PUBLICLY TRADED SECURITY
> "verbatim quote naming the series, redemption price, redemption date, and accrued-dividend treatment"

For new publicly listed issuance (only when listing on NYSE or NASDAQ is stated):
## 📢 LISTING: PUBLIC — <NYSE | NASDAQ> SYMBOL "<X>"
> "verbatim quote on listing application or expected listing"

For use of proceeds that names existing publicly traded securities to be redeemed:
## 💸 PROCEEDS WILL REDEEM EXISTING SECURITIES: <ticker(s)>
> "verbatim quote from use-of-proceeds section"

For M&A / change-of-control affecting preferreds or baby bonds:
## ⚠️ M&A — CHANGE OF CONTROL
> "verbatim quote on change-of-control terms for preferred / baby bond holders"

For tender or exchange offer on a publicly traded security:
## 🔁 TENDER / EXCHANGE OFFER
> "verbatim quote naming the security, offer price, expiration"

[BODY — pick ONE section by filing type. Use "n/d" for figures the filing doesn't disclose. Drop lines that don't apply.]

— CEF / BDC NAV (N-CSR, N-CSRS, N-PORT, N-2, 10-Q, 10-K, 8-K with NAV) —
**NAV:** $X.XX per share (prior $X.XX, ±X.X%)
**Market price:** $X.XX (X.X% discount / premium)
**Total net assets:** $X.XXbn (prior $X.XXbn)
**Total assets:** $X.XXbn
**Total liabilities:** $X.XXm
**Shares outstanding:** X.XM
**Distribution:** $X.XX [monthly / quarterly] (prior $X.XX, ±X.X%)
**Coverage (NII / distribution):** X.X%
**NII:** $X.XXm ($X.XX per share)
**Realized + unrealized P&L:** $X.XXm
**Asset coverage / leverage:** X%
**Preferreds outstanding:** ticker / series / par / coupon / call date
**Debt outstanding:** aggregate / weighted-avg coupon / maturity ladder

— NEW ISSUANCE (424B*, S-1, prospectus supplement) —
**Product:** preferred stock | baby bond | senior note | structured note
**Listing:** **PUBLIC (NYSE / NASDAQ)** symbol "X" — or — **UNLISTED**
**Coupon:** X.XX% [fixed | floating | fixed-to-floating reset DATE]
**Par:** $XX.XX
**Maturity:** <date> or perpetual
**First call:** <date> at <price>
**Size:** $XXm
**Use of proceeds:** paraphrase; if it names existing publicly traded securities to redeem, quote verbatim in the highlight block above
**Change of control:** yes <terms> | no

— REDEMPTION / CALL —
**Series:** ticker / name
**Redemption price:** $XX.XX [+ accrued]
**Redemption date:** <date>
**Notice date:** <date>
**Source of funds:** if disclosed

— DISTRIBUTION —
**Security:** common | preferred series | baby bond
**Declared:** $X.XX per share
**Prior:** $X.XX per share (±X.X%)
**Frequency:** monthly | quarterly
**Ex-date:** <date>
**Pay date:** <date>
**Coverage (CEF / BDC):** NII / distribution X.X%

— M&A —
**Acquirer:** ...
**Target:** ...
**Deal price:** $XX.XX per share
**Treatment of preferreds:** redeemed at par + accrued | assumed by successor | COC put at $X.XX
**Treatment of baby bonds:** same scheme
**Closing conditions / expected close:** ...

— OTHER —
2 to 4 sentences of plain prose covering the material content.

Always end with:
Link: <EDGAR URL>
Accession: XXXXXXXXXX-XX-XXXXXX

== CONSTRAINTS ==
- Total message ≤ 1800 characters
- Discord markdown: ## for highlight headers, ** for bold, > for blockquote
- Verbatim quotes (with double quotes inside a > blockquote) ONLY inside the highlight block
- The body uses paraphrase + key-value lines, NO blockquotes
- Never invent figures, dates, or ticker symbols. If a field is not disclosed, write "n/d" or drop the line entirely
- Include the highlight block ONLY when the trigger event is literally stated in the filing — when in doubt, omit
- For CEFs and BDCs, prior-period comparisons matter — include them when disclosed

== EMOJI GUIDE (the [EMOJI] at line 1) ==
🚨 redemption / call of publicly traded security
📢 new publicly listed issuance
⚠️ M&A / change of control / restructuring
🔁 tender / exchange offer
💰 distribution raise
✂️ distribution cut or suspension
📊 CEF / BDC NAV or financials
🏦 structured product (rare — pre-filter usually drops these)
📄 other prospectus / new issuance
📋 other / housekeeping
👤 insider activity from tracked activist"""

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

class _ModelUnavailableError(RuntimeError):
    """Raised when a model returns 404 — triggers fallback to next model."""


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
            raise

        except urllib.error.HTTPError as e:
            body_snippet = e.read(300).decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {e.code}: {body_snippet}")
            if e.code in (404, 400):
                raise _ModelUnavailableError(f"Model unavailable ({model}): {body_snippet}")
            if e.code in (429, 500, 502, 503, 504):
                log.warning(f"OpenRouter HTTP {e.code} on {model} (attempt {attempt}), retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                raise last_error

        except Exception as e:
            last_error = e
            if attempt <= MAX_RETRIES:
                log.warning(f"OpenRouter error on {model} (attempt {attempt}): {e}, retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)

    raise _ModelUnavailableError(f"Model {model} failed after all attempts: {last_error}")


def call_openrouter(filing: dict) -> str:
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
        except Exception:
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

def dispatch(filing_path: Path) -> bool:
    """
    Returns True if an LLM call was made (so the caller should rate-limit),
    False if the filing was skipped by the pre-filter or errored before the call.
    """
    try:
        filing = json.loads(filing_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"Could not read {filing_path.name}: {e}")
        move_to_processed(filing_path, prefix="err_")
        return False

    # Pre-filter — drop obvious noise before spending tokens.
    skip, reason = should_skip(filing)
    if skip:
        log.info(
            f"SKIP {filing.get('ticker','UNKNOWN'):10s} | "
            f"{filing.get('form_type',''):12s} | "
            f"{filing.get('accession','')} — {reason}"
        )
        move_to_processed(filing_path, prefix="skip_")
        return False

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
        return True  # we did spend an LLM call attempt; rate-limit anyway

    if not summary:
        log.error(f"Empty summary for {accession}")
        send_discord_alert(
            f"⚠️ **{ticker}** | {form_type} | {file_date}\n"
            f"Model returned no content.\n"
            f"Manual review: <{edgar_url}>\n"
            f"`{accession}`"
        )
        move_to_processed(filing_path, prefix="err_")
        return True

    if len(summary) > MAX_DISCORD_CHARS:
        summary = summary[:MAX_DISCORD_CHARS]

    send_discord(summary, label=f"{ticker} / {accession}")
    log.info(f"Summary preview: {summary[:300]}")
    move_to_processed(filing_path)
    return True


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

    log.info(
        f"Found {len(pending)} filing(s). Primary model: {MODEL_LIST[0]} "
        f"({len(MODEL_LIST)} fallbacks configured)"
    )
    changed = False
    sent_count = 0
    skipped_count = 0

    for fp in pending:
        try:
            raw_acc = json.loads(fp.read_text(encoding="utf-8")).get("accession", fp.stem)
        except Exception:
            raw_acc = fp.stem

        if raw_acc in dispatched:
            log.warning(f"Duplicate in inbox: {raw_acc} — moving out")
            move_to_processed(fp, prefix="dup_")
            continue

        called_llm = dispatch(fp)
        dispatched.add(raw_acc)
        changed = True

        if called_llm:
            sent_count += 1
            time.sleep(SLEEP_BETWEEN_CALLS)
        else:
            skipped_count += 1

    if changed:
        if len(dispatched) > 10_000:
            dispatched = set(sorted(dispatched)[-10_000:])
        save_dispatched(dispatched)

    log.info(
        f"Dispatch run complete. Dispatched={sent_count}, skipped={skipped_count}."
    )


if __name__ == "__main__":
    main()
