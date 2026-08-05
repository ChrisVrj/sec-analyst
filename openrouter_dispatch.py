#!/usr/bin/env python3
"""
LLM dispatcher for the OpenClaw sec-analyst pipeline.

NOTE ON THE FILENAME: this module is no longer OpenRouter-only. It calls
NVIDIA NIM (build.nvidia.com) first and falls back to OpenRouter. The name is
kept because .github/workflows/poll.yml, README.md and AGENTS.md all
reference it; treat "openrouter" in the filename as historical.

Pipeline:
  1. Read all *.json filing payloads from filings-inbox/
  2. Pre-filter each filing against prefilter.should_skip (drops Form 3/4/5
     from non-activists and unlisted/structured 424B & FWP offerings)
  3. For surviving filings, call the LLM with the sec-analyst prompt
  4. Post the structured summary to Discord via webhook
  5. Move processed filings to filings-inbox/processed/
  6. Send a Discord alert if a call fails or returns no content
  7. Persist dispatched accessions so restarts never duplicate posts

Providers (tried in order, first one with a key configured wins):
  - NVIDIA NIM   — https://integrate.api.nvidia.com/v1, key NVIDIA_API_KEY
                   (nvapi-...). Free tier ≈ 40 req/min per model, no
                   published daily cap. Keys expire after ~6 months and must
                   be regenerated at build.nvidia.com/settings/api-keys.
  - OpenRouter   — fallback only, key OPENROUTER_API_KEY (sk-or-v1-...).
                   Free tier ≈ 20 req/min AND ≈ 200 req/day.

Every attempt (provider, model) in ATTEMPTS is tried in order until one
returns content. Missing keys simply drop that provider from the chain, so
deleting OPENROUTER_API_KEY from GitHub Secrets cleanly disables the
fallback without a code change.

Nemotron 3 is a REASONING model. We send chat_template_kwargs
{"enable_thinking": false} to suppress the chain-of-thought, and
strip_reasoning() defensively removes any <think>...</think> block that
leaks through anyway — an unstripped one would blow the 1900-char Discord
cap and dump the model's scratchpad into #sec-filings.

Rate limiting:
  - SLEEP_BETWEEN_CALLS is per-provider (see PROVIDERS[...]["sleep"]) and is
    applied based on which provider actually served the call
  - Pre-filtered (skipped) filings DO NOT count against the rate limit
"""

import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, UTC
from pathlib import Path

from prefilter import should_skip

# ---------------------------------------------------------------------------
# Config — all secrets come from environment variables (GitHub Secrets)
# ---------------------------------------------------------------------------

NVIDIA_API_KEY     = os.environ.get("NVIDIA_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
DISCORD_WEBHOOK    = os.environ.get("DISCORD_WEBHOOK", "").strip()


def _model_list(env_var: str, default: list[str]) -> list[str]:
    """Comma-separated env override, else the built-in chain. Order preserved,
    duplicates dropped so an override that repeats a default costs nothing."""
    raw = os.environ.get(env_var, "").strip()
    models = [m.strip() for m in raw.split(",") if m.strip()] if raw else list(default)
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        if m not in seen:
            out.append(m)
            seen.add(m)
    return out


# NVIDIA NIM model chain. Verify these IDs against your own key with:
#   curl -H "Authorization: Bearer $NVIDIA_API_KEY" \
#        https://integrate.api.nvidia.com/v1/models
# NVIDIA renames/retires model IDs without notice; a 404 just falls through to
# the next entry, so a stale ID degrades rather than breaks.
NVIDIA_MODELS = _model_list("NVIDIA_MODELS", [
    "nvidia/nemotron-3-super-120b-a12b",   # 120B MoE, 1M ctx — the workhorse
    "nvidia/nemotron-3-ultra-550b-a55b",   # 550B MoE — slower, use if Super is busy
    "nvidia/nemotron-3-nano-30b-a3b",      # fast/cheap last resort
])

# OpenRouter chain — fallback only. OPENROUTER_MODEL still honoured as the
# primary of this chain for backwards compatibility with the existing repo var.
_OR_PRIMARY = os.environ.get("OPENROUTER_MODEL", "").strip()
OPENROUTER_MODELS = _model_list("OPENROUTER_MODELS", [
    m for m in [
        _OR_PRIMARY,
        "openai/gpt-oss-120b:free",           # empirically the most available
        "meta-llama/llama-3.3-70b-instruct:free",
        "google/gemma-4-31b-it:free",
        "openai/gpt-oss-20b:free",
        "meta-llama/llama-3.2-3b-instruct:free",
    ] if m
])

PROVIDERS = [
    {
        "name":    "nvidia",
        "url":     "https://integrate.api.nvidia.com/v1/chat/completions",
        "key":     NVIDIA_API_KEY,
        "models":  NVIDIA_MODELS,
        "headers": {"Accept": "application/json"},
        # Suppress Nemotron's chain-of-thought. Sent top-level, which is where
        # the OpenAI SDK's extra_body= puts it.
        "extra":   {"chat_template_kwargs": {"enable_thinking": False}},
        "sleep":   2,   # free tier ~40 req/min → 30/min leaves margin
    },
    {
        "name":    "openrouter",
        "url":     "https://openrouter.ai/api/v1/chat/completions",
        "key":     OPENROUTER_API_KEY,
        "models":  OPENROUTER_MODELS,
        "headers": {
            "HTTP-Referer": "https://github.com/openclaw/sec-poller",
            "X-Title":      "OpenClaw SEC Analyst",
        },
        "extra":   {},
        "sleep":   4,   # free tier ~20 req/min → 15/min leaves margin
    },
]

# Flattened (provider, model) attempt order. Providers without a key are
# dropped entirely, so removing a secret disables that provider cleanly.
ATTEMPTS = [
    (p, m)
    for p in PROVIDERS
    if p["key"] and p["models"]
    for m in p["models"]
]

BASE_DIR        = Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).parent))
INBOX_DIR       = BASE_DIR / "filings-inbox"
PROCESSED       = INBOX_DIR / "processed"
LOG_FILE        = BASE_DIR / "dispatch.log"
DISPATCHED_FILE = BASE_DIR / "dispatched_accessions.json"

MAX_TOKENS          = 900
MAX_TEXT_CHARS      = 400_000
MAX_DISCORD_CHARS   = 1_900
MAX_FIELD_LINE      = 240    # a "**Label:** value" line
MAX_PROSE_LINE      = 420    # an OTHER-template paragraph
DEFAULT_SLEEP       = 4
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

== HOW TO WRITE BODY FIELDS (read before choosing a section) ==
- Every **Field:** line is a SHORT EXTRACTED VALUE — a number, a date, a name, a ticker. Target under 15 words. Never longer than one line.
- NEVER copy sentences out of the filing into the body. If the value you want isn't a clean figure, write "n/d" and move on. A field padded with prospectus boilerplate is worse than no field.
- If a section's fields are almost all "n/d", you picked the wrong section — use OTHER instead.

— CEF / BDC NAV (N-CSR, N-CSRS, N-PORT, N-2, 10-Q, 10-K, 8-K with NAV) —
USE THIS SECTION ONLY IF BOTH ARE TRUE:
  (a) the issuer is a closed-end fund, BDC, or registered investment company — its name usually contains Fund / Trust / Income / Capital Corp, or the form is an N- form; AND
  (b) the filing actually states a net asset value per share.
An operating company (a manufacturer, bank, REIT, utility, insurer) has NO NAV. If you are looking at one, use a different section — inventing a NAV for an operating company is a serious error.
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
2 to 4 sentences of plain prose, in your own words, covering the material content. This is the correct fallback whenever no other section fits — including for operating companies, and for any filing whose figures you cannot cleanly extract.

Do NOT write a "Link:" or "Accession:" line. The system appends the verified EDGAR URL and accession number automatically after your text. Anything you write there is discarded.

== CONSTRAINTS ==
- Total message ≤ 1600 characters. Going over gets your ending cut off, so stop early rather than padding.
- Discord markdown: ## for highlight headers, ** for bold, > for blockquote
- Verbatim quotes (with double quotes inside a > blockquote) ONLY inside the highlight block, and at most 2 sentences long
- The body uses paraphrase + key-value lines, NO blockquotes, NO copied filing sentences
- Never invent figures, dates, ticker symbols, or NAVs. If a field is not disclosed, write "n/d" or drop the line entirely
- Ignore boilerplate: risk factors, tax discussion, "past performance is not an indication of future results", distribution-rate footnotes, and legal disclaimers are never worth reporting
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
# LLM providers
# ---------------------------------------------------------------------------

class _ModelUnavailableError(RuntimeError):
    """Raised on 400/404 — triggers fallback to the next model."""


class _ProviderUnavailableError(RuntimeError):
    """Raised on 401/403 — skip every remaining model on that provider."""


# Nemotron emits <think>...</think> when thinking isn't fully suppressed.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(content: str) -> str:
    """Remove chain-of-thought that leaked into the message content.

    enable_thinking=false should prevent this, but a model that ignores the
    flag would otherwise dump its scratchpad into #sec-filings and eat the
    1900-char budget. Handles the unterminated case too (thinking that ran
    past max_tokens leaves an opening <think> with no closer).
    """
    content = _THINK_RE.sub("", content)
    if "<think>" in content.lower():
        idx = content.lower().rindex("<think>")
        # Unclosed tag: keep whatever came before it, drop the rest.
        content = content[:idx]
    return content.replace("</think>", "").strip()


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


def _post_chat(provider: dict, model: str, filing: dict, extra: dict) -> str:
    """One HTTP round-trip. Raises on any failure."""
    payload = {
        "model":      model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": build_user_message(filing)},
        ],
        **extra,
    }

    req = urllib.request.Request(
        provider["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {provider['key']}",
            "Content-Type":  "application/json",
            **provider["headers"],
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read())

    choices = data.get("choices") or []
    if not choices:
        raise ValueError(f"{provider['name']} returned no choices: {data}")

    message = choices[0].get("message") or {}
    content = strip_reasoning(message.get("content") or "")

    # Reasoning models may return the whole answer in reasoning_content when
    # content comes back empty — better to salvage it than alert on nothing.
    if not content:
        content = strip_reasoning(message.get("reasoning_content") or "")
        if content:
            log.warning(f"{provider['name']}/{model}: answer arrived in reasoning_content")

    if not content:
        raise ValueError(f"{provider['name']} returned empty content")

    return content


def call_model(filing: dict, provider: dict, model: str) -> str:
    label      = f"{provider['name']}/{model}"
    extra      = dict(provider["extra"])
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            return _post_chat(provider, model, filing, extra)

        except urllib.error.HTTPError as e:
            body_snippet = e.read(300).decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {e.code}: {body_snippet}")

            # A 400 caused by our chat_template_kwargs (model doesn't accept
            # the thinking toggle) is recoverable — retry the same model plain
            # rather than burning the whole chain on a param the model rejects.
            if e.code == 400 and extra and _rejects_extra(body_snippet):
                log.warning(f"{label}: rejected {list(extra)}, retrying without it")
                extra = {}
                continue

            if e.code in (400, 404):
                raise _ModelUnavailableError(f"Model unavailable ({label}): {body_snippet}")
            if e.code in (401, 403):
                # Bad/expired key — every model on this provider will fail the
                # same way, so skip straight to the next provider.
                raise _ProviderUnavailableError(f"Auth failed for {provider['name']}: {body_snippet}")
            if e.code in (429, 500, 502, 503, 504):
                log.warning(f"{label} HTTP {e.code} (attempt {attempt}), retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                raise last_error

        except Exception as e:
            last_error = e
            if attempt <= MAX_RETRIES:
                log.warning(f"{label} error (attempt {attempt}): {e}, retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)

    raise _ModelUnavailableError(f"{label} failed after all attempts: {last_error}")


def _rejects_extra(body_snippet: str) -> bool:
    low = body_snippet.lower()
    return any(s in low for s in (
        "chat_template_kwargs", "enable_thinking",
        "extra_forbidden", "unrecognized", "unknown field",
        "additional properties", "unexpected keyword",
    ))


def call_llm(filing: dict) -> tuple[str, dict]:
    """Try every (provider, model) in order. Returns (summary, provider)."""
    if not ATTEMPTS:
        raise RuntimeError("No LLM provider configured — set NVIDIA_API_KEY or OPENROUTER_API_KEY")

    last_error: Exception | None = None
    dead_providers: set[str] = set()

    for provider, model in ATTEMPTS:
        if provider["name"] in dead_providers:
            continue
        try:
            result = call_model(filing, provider, model)
            if (provider, model) != ATTEMPTS[0]:
                log.info(f"Fallback succeeded with {provider['name']}/{model}")
            return result, provider

        except _ProviderUnavailableError as e:
            log.error(f"Provider {provider['name']} unusable, skipping its models. ({e})")
            dead_providers.add(provider["name"])
            last_error = e

        except _ModelUnavailableError as e:
            log.warning(f"Model unavailable, trying next fallback. ({e})")
            last_error = e

        except Exception as e:
            log.warning(f"{provider['name']}/{model} failed: {e}")
            last_error = e

    raise last_error or RuntimeError("All provider/model combinations exhausted")


# ---------------------------------------------------------------------------
# Message assembly
#
# The Link/Accession footer is built from the filing payload, NOT from the
# model. Two reasons, both observed in production:
#   1. A blind summary[:1900] truncation cut a long N-CSR/A summary off
#      mid-word and took the footer with it — leaving a post with no way to
#      reach the filing, which is the one thing the post must always carry.
#   2. A model-written URL can be wrong. The one in the payload came from
#      EDGAR itself and is correct by construction.
# So: strip any footer the model wrote, fit the body to the remaining budget,
# then append the real footer last.
# ---------------------------------------------------------------------------

_MODEL_FOOTER_RE = re.compile(r"^[ \t]*(link|accession)[ \t]*:.*$", re.IGNORECASE | re.MULTILINE)


def build_footer(filing: dict) -> str:
    """Verified EDGAR link + accession. Angle brackets suppress Discord's
    link-preview embed, which keeps #sec-filings scannable."""
    lines = []
    url = (filing.get("filing_url", "") or "").strip()
    acc = (filing.get("accession", "") or "").strip()
    if url:
        lines.append(f"Link: <{url}>")
    if acc:
        lines.append(f"Accession: {acc}")
    return "\n".join(lines)


def trim_long_lines(body: str) -> str:
    """Cap runaway lines.

    A CEF N-CSR/A once produced a single 1,400-char "field" that was raw
    prospectus boilerplate copied out of the filing — it consumed the whole
    Discord budget on its own. Blockquote lines are left alone: those are the
    verbatim highlight quotes, which are supposed to be long.
    """
    out = []
    for line in body.split("\n"):
        if line.lstrip().startswith(">"):
            out.append(line)
            continue
        limit = MAX_FIELD_LINE if line.lstrip().startswith("**") else MAX_PROSE_LINE
        if len(line) > limit:
            line = line[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-") + " …"
        out.append(line)
    return "\n".join(out)


def fit_to_budget(body: str, budget: int) -> str:
    """Truncate on a line boundary where possible, else a word boundary —
    never mid-word, which is how the BGT post ended on 'Does not reflect deri'."""
    if len(body) <= budget:
        return body
    cut = body[:budget]
    nl = cut.rfind("\n")
    if nl > budget * 0.6:          # a clean line break reasonably near the end
        return cut[:nl].rstrip() + "\n…"
    return cut.rsplit(" ", 1)[0].rstrip(" ,;:-") + " …"


def finalize_message(summary: str, filing: dict) -> str:
    body = _MODEL_FOOTER_RE.sub("", summary).strip()
    body = trim_long_lines(body)

    footer = build_footer(filing)
    if not footer:
        return fit_to_budget(body, MAX_DISCORD_CHARS)

    budget = MAX_DISCORD_CHARS - len(footer) - 2   # 2 for the joining newlines
    return f"{fit_to_budget(body, budget)}\n\n{footer}"


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

def dispatch(filing_path: Path) -> int:
    """
    Returns the number of seconds the caller should sleep before the next
    filing — the serving provider's rate-limit spacing, or 0 when no LLM call
    was made (pre-filtered, or unreadable before the call).
    """
    try:
        filing = json.loads(filing_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"Could not read {filing_path.name}: {e}")
        move_to_processed(filing_path, prefix="err_")
        return 0

    # Pre-filter — drop obvious noise before spending tokens.
    skip, reason = should_skip(filing)
    if skip:
        log.info(
            f"SKIP {filing.get('ticker','UNKNOWN'):10s} | "
            f"{filing.get('form_type',''):12s} | "
            f"{filing.get('accession','')} — {reason}"
        )
        move_to_processed(filing_path, prefix="skip_")
        return 0

    ticker     = filing.get("ticker", "UNKNOWN")
    accession  = filing.get("accession", filing_path.stem)
    form_type  = filing.get("form_type", "")
    file_date  = filing.get("file_date", "")
    edgar_url  = filing.get("filing_url", "")

    log.info(f"Dispatching {ticker:10s} | {form_type:12s} | {accession}")

    try:
        summary, provider = call_llm(filing)
    except Exception as e:
        log.error(f"All LLM providers failed for {accession}: {e}")
        send_discord_alert(
            f"❌ **{ticker}** | {form_type} | {file_date}\n"
            f"LLM error: {str(e)[:200]}\n"
            f"Manual review: <{edgar_url}>\n"
            f"`{accession}`"
        )
        move_to_processed(filing_path, prefix="err_")
        return DEFAULT_SLEEP  # attempts were spent; rate-limit anyway

    message = finalize_message(summary, filing)

    send_discord(message, label=f"{ticker} / {accession} via {provider['name']}")
    log.info(f"Summary preview: {message[:300]}")
    move_to_processed(filing_path)
    return provider["sleep"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not ATTEMPTS:
        log.error(
            "No LLM provider configured. Set NVIDIA_API_KEY (preferred) "
            "and/or OPENROUTER_API_KEY. Exiting."
        )
        raise SystemExit(1)

    if not NVIDIA_API_KEY:
        log.warning("NVIDIA_API_KEY not set — running on OpenRouter only.")
    if not OPENROUTER_API_KEY:
        log.warning("OPENROUTER_API_KEY not set — no fallback provider.")

    if not DISCORD_WEBHOOK:
        log.error("DISCORD_WEBHOOK environment variable is not set. Exiting.")
        raise SystemExit(1)

    dispatched = load_dispatched()
    log.info(f"Loaded {len(dispatched)} previously dispatched accessions.")

    pending = sorted(INBOX_DIR.glob("*.json"))

    if not pending:
        log.info("No filings in inbox — nothing to dispatch.")
        return

    first_provider, first_model = ATTEMPTS[0]
    log.info(
        f"Found {len(pending)} filing(s). Primary: {first_provider['name']}/{first_model} "
        f"({len(ATTEMPTS)} provider/model combinations configured)"
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

        sleep_for = dispatch(fp)
        dispatched.add(raw_acc)
        changed = True

        if sleep_for:
            sent_count += 1
            time.sleep(sleep_for)
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
