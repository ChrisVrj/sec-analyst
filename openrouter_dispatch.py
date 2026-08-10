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

# ── Priority routing (optional) ────────────────────────────────────────────
# A redemption of a security you hold is worth interrupting your day for; a
# routine NAV update is not. Both are indistinguishable in a single busy
# channel, so priority-1..4 events can be split off.
#
#   DISCORD_WEBHOOK_URGENT  second webhook (e.g. #sec-urgent). When set,
#                           urgent filings go there INSTEAD of the main
#                           channel, so that channel stays pure signal and
#                           can carry push notifications. Unset = everything
#                           goes to the main webhook, exactly as before.
#   DISCORD_URGENT_MENTION  prepended to urgent posts, e.g. "@here" or
#                           "<@&1234567890>" for a role. Unset = no ping.
DISCORD_WEBHOOK_URGENT = os.environ.get("DISCORD_WEBHOOK_URGENT", "").strip()
DISCORD_URGENT_MENTION = os.environ.get("DISCORD_URGENT_MENTION", "").strip()


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

== THE READER'S TRADEABLE UNIVERSE (decides everything below) ==
He trades EXCHANGE-LISTED INCOME SECURITIES at RETAIL denomination:
  ✅ preferred stock and depositary shares (typically $25 par, sometimes $10/$20/$50)
  ✅ baby bonds and exchange-traded debt (typically $25 par, trade under their own ticker)
  ✅ CEF and BDC shares
He does NOT trade, and must NOT be alerted about:
  ❌ COMMON STOCK of any kind — ATM programmes, follow-ons, shelf takedowns
  ❌ $1,000-par institutional paper — senior notes, global notes, medium-term notes.
     A $1,000 denomination is the single clearest tell. These are sold to
     institutions and are not what he trades, even when NYSE-listed.
  ❌ unlisted or structured products

⚠️ TICKER DISCIPLINE. An exchange-traded preferred or baby bond has its OWN
ticker, distinct from the issuer's common (e.g. common "SQFT" vs preferred
"SQFTP"; common "MBIN" vs its depositary shares). NEVER present the issuer's
common-stock ticker as the symbol of the security being offered. If the filing
does not state a symbol for the offered security, write "n/d". Reporting
AT&T's $1,000-par notes as 'NYSE SYMBOL "T"' is wrong — "T" is the common.

== PRIORITY ORDER ==
Lead with the highest-priority event the filing actually discloses. Use it to pick the [EMOJI] for line 1 and to decide which highlight block (if any) to include.
1. Redemption / call of an existing publicly traded security
2. New issuance of an EXCHANGE-TRADED INCOME SECURITY from the ✅ list above,
   to be listed on NYSE or NASDAQ, at retail denomination. A common-stock
   offering is NEVER priority 2 no matter how clearly it is listed, and
   neither is a $1,000-par note. Both are priority 7.
3. M&A or change-of-control affecting publicly traded preferreds / baby bonds
4. Tender or exchange offer for a publicly traded security
5. Distribution change on a publicly traded security
6. CEF / BDC NAV or financial update
7. Other material event — including all common-stock offerings, all $1,000-par
   institutional notes, and all shelf registrations where nothing is priced yet

== OUTPUT TEMPLATE ==

Line 1: [EMOJI] **TICKER | FORM | YYYY-MM-DD** — one-sentence headline (≤120 chars)
Line 2: Company: <legal name>

[HIGHLIGHT BLOCK — include ONLY if a priority-1 to priority-4 trigger is LITERALLY stated in the filing. If unsure, omit the block. Never fabricate.]

For redemption / call of publicly traded security:
## 🚨 REDEMPTION OF PUBLICLY TRADED SECURITY
> "verbatim quote naming the series, redemption price, redemption date, and accrued-dividend treatment"

For new publicly listed issuance — ALL FOUR must hold, else omit this block entirely:
  (a) the security is a preferred / depositary share / baby bond / exchange-traded debt
  (b) it is NOT common stock and NOT $1,000-par institutional paper
  (c) listing on NYSE or NASDAQ is literally stated for THAT security
  (d) the symbol is the offered security's own symbol, not the issuer's common
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

== OUTPUT DISCIPLINE ==
Write the summary EXACTLY ONCE. Then stop.
Never show your reasoning. Do not write notes to yourself, do not discuss these instructions, do not second-guess a decision in the output, and never write anything like "(Note: ... per guidance ...)" or "Re-evaluating: ...". Decide silently, then write the one final summary. If you conclude a highlight block does not apply, simply omit it — do not explain that you omitted it.

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
        # encoding is explicit because summaries carry emoji (🚨, 📊, —) and
        # FileHandler otherwise falls back to the locale codepage — cp1252 on
        # Windows, where every log line with an emoji raises
        # UnicodeEncodeError. Harmless on the Ubuntu runner, fatal for local
        # debugging on Chris's machine.
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
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

def post_discord(content: str, webhook: str = "") -> None:
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook or DISCORD_WEBHOOK,
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


def send_discord(content: str, label: str = "", webhook: str = "") -> None:
    try:
        post_discord(content, webhook)
        if label:
            log.info(f"Posted to Discord: {label}")
    except Exception as e:
        log.error(f"Discord post failed ({label}): {e}")
        # A dedicated urgent channel must never swallow an urgent filing. If
        # its webhook is broken, fall back to the main channel rather than
        # losing the one post that actually mattered.
        if webhook and webhook != DISCORD_WEBHOOK and DISCORD_WEBHOOK:
            log.warning(f"Retrying {label} on the main webhook")
            try:
                post_discord(content, DISCORD_WEBHOOK)
                log.info(f"Posted to Discord (main, after urgent failed): {label}")
            except Exception as e2:
                log.error(f"Main-webhook fallback also failed ({label}): {e2}")


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
# Priority classification
#
# Mirrors the PRIORITY ORDER in SYSTEM_PROMPT. Tiers 1-4 are exactly the
# events the prompt allows a "## " highlight block for, and exactly the ones
# worth interrupting a trading day: something is being called, listed, taken
# over, or tendered for. Tiers 5+ (distributions, NAV updates, housekeeping)
# are reading material, not action items.
#
# Detection order matters — a merger that also redeems preferreds should
# classify as a redemption, which is why tier 1 is checked first.
# ---------------------------------------------------------------------------

URGENT_RULES: list[tuple[int, str, str, tuple[str, ...]]] = [
    (1, "redemption",              "\U0001F6A8", ("REDEMPTION",)),
    (2, "public listing",          "\U0001F4E2", ("LISTING:", "PROCEEDS WILL REDEEM")),
    (3, "M&A / change of control", "⚠",     ("M&A", "CHANGE OF CONTROL")),
    (4, "tender / exchange offer", "\U0001F501", ("TENDER", "EXCHANGE OFFER")),
]

# ── Tradeable-universe gate for tier 2 ─────────────────────────────────────
# Chris trades EXCHANGE-LISTED INCOME SECURITIES at retail denomination:
# $25-par preferreds, depositary shares, baby bonds, exchange-traded debt.
# He does NOT trade common stock, and does NOT trade $1,000-par institutional
# paper (senior notes, global notes, medium-term notes).
#
# Every false ping observed on 2026-08-07 was a tier-2 "LISTING" block, and
# every one failed on a field the model had already written correctly in the
# body — so gate on the body, not on the model's own priority judgement:
#   T    senior note, Par $1,000        (and "NYSE symbol T" is AT&T's COMMON ticker)
#   BNY  senior note, Par $1,000        (same — "BNY" is the common)
#   INN  common stock ATM
#   CSWC common stock ATM
#   OCFC Listing: UNLISTED, stated outright
#   AOD  shelf listing common | preferred | notes | rights, nothing priced
# Neither genuine alert was tier 2: MBIN was a redemption, SQFT a tender.

_FIELD = r"\*\*{}:\*\*[^\n]*"
_PRODUCT_COMMON = re.compile(_FIELD.format("Product") + r"common\s+(stock|share)", re.I)
_LISTING_UNLISTED = re.compile(_FIELD.format("Listing") + r"unlisted", re.I)
# $1,000 / $1000 par is the institutional denomination. Retail income
# securities price at $25 (occasionally $10/$20/$50).
_PAR_INSTITUTIONAL = re.compile(r"\*\*Par:\*\*\s*\$?\s*1[,.]?000", re.I)
# Positive evidence that the thing being offered is actually in his universe.
_RETAIL_SECURITY = re.compile(
    r"preferred\s+(stock|share)|depositary\s+(share|receipt)|baby\s+bond"
    r"|exchange[- ]traded\s+(debt|note)|\bpar:\*\*\s*\$?2[05]\b",
    re.I,
)


def _is_tradeable_new_issue(summary: str) -> tuple[bool, str]:
    """Tier-2 gate. Returns (keep_urgent, reason_if_demoted)."""
    if _LISTING_UNLISTED.search(summary):
        return False, "listing states UNLISTED"
    if _PAR_INSTITUTIONAL.search(summary):
        return False, "$1,000 par — institutional, not exchange-traded retail"
    if _PRODUCT_COMMON.search(summary):
        return False, "common stock, not an income security"
    if not _RETAIL_SECURITY.search(summary):
        return False, "no preferred / depositary / baby-bond / $25-par signal"
    return True, ""


# ── Calendar-form gate for the lead-emoji signal ───────────────────────────
# Observed 2026-08-10: two Equitable Holdings posts pinged Chris off the line-1
# emoji alone, with no highlight block anywhere in the body —
#   🚨 EQH | ARS     — "Annual report to stockholders for fiscal year 2025"
#   ⚠  EQH | DEF 14A — "Annual meeting to vote on director elections, auditor
#                       ratification, and say-on-pay on September 23, 2026"
# Neither filing discloses a tradeable event. The model simply reached for a
# dramatic emoji on a document that has none, and 🚨/⚠ are tiers 1 and 3.
#
# These forms are the worst possible carriers for that mistake: every issuer
# files a proxy and an annual report every single year, so an occasional emoji
# slip becomes a steady drip of proxy-season pings on a channel whose whole
# value is that it only fires when something is tradeable.
#
# So the WEAK signal (line-1 emoji) is switched off for forms that exist to
# satisfy a filing calendar rather than to disclose an event. The STRONG signal
# is untouched everywhere: a merger-vote proxy that quotes change-of-control
# terms writes "## ⚠️ M&A — CHANGE OF CONTROL" and still routes urgent. The
# merger-proxy forms (DEFM14A / PREM14A) are deliberately absent from the set.
#
# It is a denylist, not an allowlist, so an unrecognised form keeps today's
# behaviour — a missed redemption still costs far more than a stray ping.
_CALENDAR_FORMS: frozenset[str] = frozenset({
    # Annual-meeting proxies and information statements.
    "DEF 14A", "DEFA14A", "DEFR14A", "PRE 14A", "PRER14A",
    "DEF 14C", "DEFA14C", "PRE 14C",
    # Reports to shareholders and periodic reports.
    "ARS", "10-K", "10-KT", "10-Q", "10-QT", "20-F", "40-F", "11-K",
    # Notices that a periodic report will be late — a date, nothing more.
    "NT 10-K", "NT 10-Q", "NT 20-F", "NT-NCSR",
    # Fund periodics.
    "N-CSR", "N-CSRS", "N-PORT", "N-CEN", "N-Q", "N-30D", "N-30B-2",
    "N-MFP", "N-MFP2", "N-MFP3", "24F-2NT",
    # Ownership / holdings snapshots.
    "3", "4", "5", "13F-HR", "13F-NT", "SC 13G",
})


def _is_calendar_form(form_type: str) -> bool:
    """True for forms filed to satisfy a calendar, not to disclose an event."""
    form = (form_type or "").strip().upper()
    if form.endswith("/A"):          # an amendment is the same kind of document
        form = form[:-2].strip()
    return form in _CALENDAR_FORMS


def classify_priority(summary: str, form_type: str = "") -> tuple[int, str]:
    """Returns (tier, label); tier 0 means routine.

    Two independent signals, both anchored so prose can't trigger them:
      · a '## ' highlight header containing the keyword — the strongest, since
        the prompt only emits one when the trigger is literally in the filing
      · the lead emoji on line 1 — the weakest, since the model picks it
        freely and has been seen putting 🚨 on an annual report

    Deliberately NOT a substring search over the whole summary: a NAV report
    mentioning "redemption of shares at NAV" in passing must stay routine.

    `form_type` comes from the EDGAR payload, not from the model, and is what
    disarms the emoji signal on calendar-driven forms. It defaults to "" —
    unknown forms stay permissive.

    Tier 2 additionally has to clear _is_tradeable_new_issue(). Tiers 1, 3 and
    4 are NOT gated: a redemption or tender on something he holds is the whole
    point of the channel, and a false negative there costs far more than a
    false positive.

    A demoted rule falls through to the next tier rather than returning, so a
    weak hit on one tier can never suppress a header-backed hit on another.
    """
    lines       = summary.splitlines()
    headers     = " ".join(l.upper() for l in lines if l.lstrip().startswith("#"))
    first_line  = lines[0] if lines else ""

    for tier, label, emoji, keywords in URGENT_RULES:
        header_hit = any(k in headers for k in keywords)
        emoji_hit  = bool(emoji) and emoji in first_line
        if not (header_hit or emoji_hit):
            continue
        if not header_hit and _is_calendar_form(form_type):
            log.info(
                f"Demoting {form_type} to routine: lead emoji reads as "
                f"{label}, but the summary carries no highlight block"
            )
            continue
        if tier == 2:
            keep, why = _is_tradeable_new_issue(summary)
            if not keep:
                log.info(f"Demoting new issuance to routine: {why}")
                continue
        return tier, label
    return 0, ""


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
    """Verified EDGAR links + accession.

    `primary_doc_url` goes first because it lands directly on the filing text;
    `filing_url` is EDGAR's index page, which is only a table of contents and
    costs a second click. The index link is kept as a secondary "Index:" line
    since it's the route to exhibits the poller didn't fetch.

    Angle brackets suppress Discord's link-preview embeds, keeping the channel
    scannable.
    """
    lines = []
    doc   = (filing.get("primary_doc_url", "") or "").strip()
    index = (filing.get("filing_url", "") or "").strip()
    acc   = (filing.get("accession", "") or "").strip()

    if doc:
        lines.append(f"Document: <{doc}>")
        if index and index != doc:
            lines.append(f"Index: <{index}>")
    elif index:
        # Payload predates primary_doc_url, or extraction failed.
        lines.append(f"Link: <{index}>")

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


# Meta-commentary the model sometimes emits instead of just answering. The
# BNY 424B2 post on 2026-08-07 carried "(Note: Filing does not state
# NYSE/NASDAQ listing explicitly — highlight block added conditionally per
# guidance; ... Re-evaluating: ...)" and then repeated the entire summary with
# a different conclusion.
_META_LINE_RE = re.compile(
    r"^\s*(\(?Note:|Re-evaluating|Per strict interpretation|However, as\b|-{3,}\s*$)",
    re.I,
)
# Line 1 of the template: [emoji] **TICKER | FORM | YYYY-MM-DD** — headline
_HEADLINE_RE = re.compile(r"^.{0,4}\*\*[A-Z0-9.\-]{1,8}\s*\|", re.M)


def strip_meta_commentary(body: str) -> str:
    """Drop the model's deliberation, and keep only the first summary if it
    wrote more than one.

    Keeping the FIRST is a deliberate choice: it is the one that follows the
    template. When the model self-corrects in a second copy the two disagree,
    and there is no reliable way to tell which is right — but routing no
    longer depends on it, because classify_priority() gates tier 2 on the
    Product/Par/Listing fields rather than on the model's own verdict.
    """
    heads = list(_HEADLINE_RE.finditer(body))
    if len(heads) > 1:
        body = body[:heads[1].start()].rstrip()
    return "\n".join(l for l in body.split("\n") if not _META_LINE_RE.match(l)).strip()


def finalize_message(summary: str, filing: dict, prefix: str = "") -> str:
    body = _MODEL_FOOTER_RE.sub("", summary).strip()
    body = strip_meta_commentary(body)
    body = trim_long_lines(body)

    head   = f"{prefix} " if prefix else ""
    footer = build_footer(filing)

    # The prefix and footer are both fixed costs; only the body flexes.
    budget = MAX_DISCORD_CHARS - len(head) - (len(footer) + 2 if footer else 0)
    fitted = fit_to_budget(body, budget)

    return f"{head}{fitted}\n\n{footer}" if footer else f"{head}{fitted}"


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

    tier, priority_label = classify_priority(summary, form_type)
    is_urgent = tier > 0

    prefix  = DISCORD_URGENT_MENTION if (is_urgent and DISCORD_URGENT_MENTION) else ""
    webhook = DISCORD_WEBHOOK_URGENT if (is_urgent and DISCORD_WEBHOOK_URGENT) else ""
    message = finalize_message(summary, filing, prefix=prefix)

    if is_urgent:
        log.info(
            f"PRIORITY {tier} ({priority_label}) — {ticker} {accession} "
            f"→ {'urgent webhook' if webhook else 'main webhook'}"
            f"{' with mention' if prefix else ''}"
        )

    send_discord(
        message,
        label=f"{ticker} / {accession} via {provider['name']}"
              f"{f' [P{tier} {priority_label}]' if is_urgent else ''}",
        webhook=webhook,
    )
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
