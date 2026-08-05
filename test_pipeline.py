#!/usr/bin/env python3
"""
Regression tests for the sec-analyst pipeline.

Run with plain Python, no pytest, no network:

    python test_pipeline.py

Every case here corresponds to a bug that actually reached Discord. The
project's failure mode is silence — a broken extraction or filter looks
identical to "nothing was filed" — so these exist to make regressions loud.

Add a case whenever a bad post gets through. A 20-line fixture beats another
week of squinting at #sec-filings.
"""

import os
import sys

os.environ.setdefault("NVIDIA_API_KEY", "test")
os.environ.setdefault("DISCORD_WEBHOOK", "https://example.invalid/webhook")

import prefilter                      # noqa: E402
import openrouter_dispatch as dispatch  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")
        failures.append(name)


# ---------------------------------------------------------------------------
# prefilter — Aug 2026: an RBC Nasdaq-100 buffer note reached Discord.
#
# The filing says twice that it will not be listed, but a loose LISTED_SIGNAL
# matched the Nasdaq-100 Index's own description ("100 of the largest
# non-financial companies listed on The Nasdaq Stock Market") and the
# listed-override ran first, forcing a keep.
# Real accession: 0000950103-26-011945
# ---------------------------------------------------------------------------
print("prefilter")

RBC_BUFFER_NOTE = """
Preliminary Pricing Supplement. Royal Bank of Canada. Notes linked to the
Nasdaq-100 Index. The Nasdaq-100 Index is a modified capitalization-weighted
index that is designed to measure the performance of 100 of the largest
non-financial companies listed on The Nasdaq Stock Market. The notes will not
be listed on any securities exchange. Upside participation rate: 150%.
Buffer level: 90%. Initial underlier level to be determined on the trade date.
"""

check(
    "RBC buffer note is skipped",
    prefilter.should_skip({
        "form_type": "424B2",
        "entity_name": "Royal Bank of Canada",
        "filing_text": RBC_BUFFER_NOTE,
    })[0],
    "index description must not override an explicit 'will not be listed'",
)

check(
    "exchange-listed preferred survives structured vocabulary",
    not prefilter.should_skip({
        "form_type": "424B5",
        "entity_name": "Example REIT Inc",
        "filing_text": (
            "We intend to apply to list the Series D Cumulative Redeemable "
            "Preferred Stock on the New York Stock Exchange under the symbol "
            "EXR PrD. Participation rate is not applicable to this offering."
        ),
    })[0],
    "the listed-override must still protect genuine listed offerings",
)

check(
    "explicit unlisted beats a listing phrase in the same document",
    prefilter.should_skip({
        "form_type": "424B2",
        "entity_name": "Big Bank NA",
        "filing_text": (
            "The notes will not be listed on any securities exchange. "
            "We intend to apply to list nothing further."
        ),
    })[0],
)

check(
    "Form 4 from an untracked filer is skipped",
    prefilter.should_skip({"form_type": "4", "entity_name": "Some Exec",
                           "filing_text": "routine grant"})[0],
)

check(
    "Form 4 from a tracked activist is kept",
    not prefilter.should_skip({"form_type": "4", "entity_name": "Saba Capital Management",
                               "filing_text": "acquired shares"})[0],
)

check(
    "8-K is untouched by the offering filters",
    not prefilter.should_skip({"form_type": "8-K", "entity_name": "Brunswick Corp",
                               "filing_text": "quarterly results"})[0],
)


# ---------------------------------------------------------------------------
# finalize_message — Aug 2026: a BGT N-CSR/A post was cut mid-word at
# "Does not reflect deri" and lost its Link/Accession footer entirely,
# because summary[:1900] truncated blindly. The footer is the one thing the
# post must always carry.
# ---------------------------------------------------------------------------
print("\nfinalize_message")

FILING = {
    "ticker": "BGT",
    "form_type": "N-CSR/A",
    "accession": "0001193125-26-123456",
    "filing_url": "https://www.sec.gov/Archives/edgar/data/1176334/x-index.htm",
}

overlong = (
    "\U0001F4CA **BGT | N-CSR/A | 2026-08-05** — NAV update.\n"
    "Company: BlackRock Floating Rate Income Trust\n"
    "**NAV:** $11.84 per share (prior $12.57, -5.81%)\n"
    "**Total net assets " + ("blah boilerplate " * 200) + "\n"
    "Link: https://a-url-the-model-invented.example.com\n"
    "Accession: 9999999999-99-999999"
)
out = dispatch.finalize_message(overlong, FILING)

check("stays within the Discord cap", len(out) <= dispatch.MAX_DISCORD_CHARS,
      f"got {len(out)}")
check("real EDGAR link survives truncation", FILING["filing_url"] in out)
check("real accession survives truncation", FILING["accession"] in out)
check("model-invented URL is discarded", "a-url-the-model-invented" not in out)
check("model-invented accession is discarded", "9999999999" not in out)
check("runaway field line is trimmed", out.count("blah") < 60)
check("figures before the runaway line are kept", "$11.84" in out)

check(
    "short summaries still get a footer",
    FILING["filing_url"] in dispatch.finalize_message("\U0001F6A8 **ABR** call", FILING),
)

quote = (
    '\U0001F6A8 **X | 8-K | 2026-08-05** — Redemption.\n'
    '## \U0001F6A8 REDEMPTION\n'
    '> "the Company will redeem all outstanding shares of its 6.375% Series F '
    'Cumulative Redeemable Preferred Stock at $25.00 per share plus accrued '
    'and unpaid dividends to the redemption date"'
)
check(
    "verbatim highlight quotes are never trimmed",
    "accrued and unpaid dividends to the redemption date"
    in dispatch.finalize_message(quote, FILING),
    "blockquote lines must be exempt from line-length trimming",
)


# ---------------------------------------------------------------------------
# strip_reasoning — Nemotron 3 is a reasoning model. enable_thinking=false
# should prevent chain-of-thought, but a leak would dump the model's
# scratchpad into #sec-filings and eat the character budget.
# ---------------------------------------------------------------------------
print("\nstrip_reasoning")

check("closed <think> block is removed",
      dispatch.strip_reasoning("<think>scratchpad</think>\nSUMMARY") == "SUMMARY")
check("unterminated <think> truncates rather than leaking",
      dispatch.strip_reasoning("SUMMARY\n<think>ran past max_tokens...") == "SUMMARY")
check("plain content is untouched",
      dispatch.strip_reasoning("  SUMMARY  ") == "SUMMARY")


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")
