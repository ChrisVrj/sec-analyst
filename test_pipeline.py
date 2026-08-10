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
# build_footer — posts must land on the document, not EDGAR's table of
# contents. primary_doc_url was added to the payload Aug 2026; payloads
# written before that (or where extraction failed) must still get a link.
# ---------------------------------------------------------------------------
print("\nbuild_footer / direct document links")

WITH_DOC = dict(FILING, primary_doc_url="https://www.sec.gov/Archives/edgar/data/1176334/d123456d8k.htm")
foot = dispatch.build_footer(WITH_DOC)
check("document link is present and labelled", "Document: <" in foot)
check("document link comes before the index link",
      foot.index("Document:") < foot.index("Index:"))
check("index page is retained as a secondary link", "Index: <" in foot)
check("embeds are suppressed with angle brackets", foot.count("<http") == 2)

legacy = dispatch.build_footer(FILING)          # no primary_doc_url key at all
check("payload without primary_doc_url still gets a link", "Link: <" in legacy)
check("legacy footer keeps the accession", FILING["accession"] in legacy)

same = dispatch.build_footer(dict(FILING, primary_doc_url=FILING["filing_url"]))
check("identical doc and index links are not duplicated", same.count("http") == 1)


# ---------------------------------------------------------------------------
# classify_priority — a redemption is worth interrupting a trading day for;
# a NAV update is not. Prose mentioning a keyword must NOT trigger.
# ---------------------------------------------------------------------------
print("\nclassify_priority / routing")

REDEMPTION = (
    "\U0001F6A8 **ABR | 8-K | 2026-08-05** — Calls Series F.\n"
    "## \U0001F6A8 REDEMPTION OF PUBLICLY TRADED SECURITY\n"
    '> "will redeem all Series F at $25.00 plus accrued"'
)
NAV_ROUTINE = (
    "\U0001F4CA **BGT | N-CSR | 2026-08-05** — NAV update.\n"
    "**NAV:** $11.84 per share\n"
    "The fund may permit redemption of shares at net asset value."
)

check("redemption classifies tier 1", dispatch.classify_priority(REDEMPTION) == (1, "redemption"))
check("NAV report stays routine despite the word 'redemption'",
      dispatch.classify_priority(NAV_ROUTINE)[0] == 0,
      "prose must not trigger routing")
check("lead emoji alone is enough",
      dispatch.classify_priority("\U0001F501 **X | SC TO-I | 2026-08-05** — offer")[0] == 4)
check("listing headline classifies tier 2 when the security is tradeable",
      dispatch.classify_priority(
          "\U0001F4E2 **X | 424B5 | 2026-08-05** — new pfd\n## \U0001F4E2 LISTING: PUBLIC — NYSE\n"
          "**Product:** preferred stock\n**Par:** $25.00")[0] == 2)
check("bare listing headline with no tradeable evidence does NOT ping",
      dispatch.classify_priority(
          "\U0001F4E2 **X | 424B5 | 2026-08-05** — new pfd\n## \U0001F4E2 LISTING: PUBLIC — NYSE")[0] == 0,
      "tier 2 fails safe to routine; he still sees it in #sec-filings")
check("M&A headline classifies tier 3",
      dispatch.classify_priority(
          "**X | 8-K | 2026-08-05** — merger\n## ⚠ M&A — CHANGE OF CONTROL")[0] == 3)
check("plain housekeeping is routine",
      dispatch.classify_priority("\U0001F4CB **X | 8-K** — bylaw amendment.")[0] == 0)
check("redemption outranks a co-occurring M&A block",
      dispatch.classify_priority(
          "**X | 8-K** — merger\n## ⚠ M&A — CHANGE OF CONTROL\n"
          "## \U0001F6A8 REDEMPTION OF PUBLICLY TRADED SECURITY") == (1, "redemption"))


# ---------------------------------------------------------------------------
# Calendar-form gate — verbatim from #sec-urgent on 2026-08-10. Both pinged
# Chris on the lead emoji alone; neither filing carries a highlight block,
# because neither discloses anything tradeable. A proxy and an annual report
# land every year for every issuer, so this is the one place a stray emoji
# turns into a recurring ping.
# ---------------------------------------------------------------------------
print("\ncalendar-form gate (real #sec-urgent posts, 2026-08-10)")

EQH_ARS = (
    "\U0001F6A8 **EQH | ARS | 2026-08-10** — Annual report to stockholders "
    "for fiscal year ended December 31, 2025.\n"
    "Company: Equitable Holdings, Inc."
)
EQH_PROXY = (
    "⚠ **EQH | DEF 14A | 2026-08-10** — Annual meeting of stockholders to "
    "vote on director elections, auditor ratification, and say-on-pay on "
    "September 23, 2026.\n"
    "Company: Equitable Holdings, Inc."
)

check("EQH annual report does NOT ping on a stray siren emoji",
      dispatch.classify_priority(EQH_ARS, "ARS")[0] == 0,
      "an ARS discloses nothing; the emoji is the model's whim")
check("EQH annual-meeting proxy does NOT ping on a stray warning emoji",
      dispatch.classify_priority(EQH_PROXY, "DEF 14A")[0] == 0,
      "director elections and say-on-pay are not a change of control")
check("the same two still reach the main channel",
      dispatch.classify_priority(EQH_ARS, "ARS")[0] == 0
      and "Document: <" in dispatch.finalize_message(EQH_ARS, WITH_DOC))

check("a merger-vote proxy that quotes COC terms still pings",
      dispatch.classify_priority(
          "⚠ **X | DEF 14A | 2026-08-10** — merger vote\n"
          "## ⚠ M&A — CHANGE OF CONTROL\n"
          '> "each Series A share converts into one share of Parent\'s '
          'Series D preferred"', "DEF 14A")[0] == 3,
      "the header is evidence; only the bare emoji is disarmed")
check("DEFM14A is a merger proxy, never gated",
      dispatch.classify_priority("⚠ **X | DEFM14A | 2026-08-10** — merger vote",
                                 "DEFM14A")[0] == 3)
check("an amendment inherits its parent form's gate",
      dispatch.classify_priority("\U0001F6A8 **X | 10-K/A | 2026-08-10** — restated",
                                 "10-K/A")[0] == 0)
check("an 8-K redemption is untouched by the gate",
      dispatch.classify_priority(REDEMPTION, "8-K") == (1, "redemption"))
check("an unknown form stays permissive",
      dispatch.classify_priority("\U0001F501 **X | SC TO-I** — offer", "SC TO-I")[0] == 4)
check("a weak hit on one tier cannot suppress a header hit on another",
      dispatch.classify_priority(
          "\U0001F6A8 **X | 10-K | 2026-08-10** — annual report\n"
          "## ⚠ M&A — CHANGE OF CONTROL", "10-K")[0] == 3)

mentioned = dispatch.finalize_message(REDEMPTION, WITH_DOC, prefix="@here")
check("mention prefix is prepended", mentioned.startswith("@here "))
check("prefixed message still fits the cap", len(mentioned) <= dispatch.MAX_DISCORD_CHARS)
check("prefixed message keeps its footer", "Document: <" in mentioned)

huge = dispatch.finalize_message("\U0001F6A8 x" + ("y" * 4000), WITH_DOC, prefix="@here")
check("prefix is accounted for when the body must be truncated",
      len(huge) <= dispatch.MAX_DISCORD_CHARS, f"got {len(huge)}")
check("footer survives a prefixed truncation", "Accession:" in huge)


# ---------------------------------------------------------------------------
# Tradeable-universe gate — verbatim from the #sec-urgent channel on
# 2026-08-07. Six of these eight pinged Chris and should not have. Every one
# of the six was a tier-2 "LISTING" block; neither genuine alert was tier 2.
#
# The reader trades $25-par exchange-listed preferreds, depositary shares and
# baby bonds. Not common stock. Not $1,000-par institutional notes.
# ---------------------------------------------------------------------------
print("\ntradeable-universe gate (real #sec-urgent posts)")

REAL_POSTS = [
    # (label, form_type, expected_urgent, summary)
    # The form type is real, so these also prove the calendar-form gate above
    # doesn't cost anything: MBIN's redemption was disclosed inside a 10-Q —
    # a gated form — and must still route urgent on the strength of its header.
    ("T FWP — $1,000-par senior note, 'NYSE symbol T' is the COMMON ticker", "FWP", False,
     '\U0001F4E2 **T | FWP | 2026-08-07** — AT&T intends to list its $1.2B Floating Rate Global Notes due 2028 on the NYSE.\n'
     'Company: AT&T Inc.\n## \U0001F4E2 LISTING: PUBLIC — NYSE SYMBOL "T"\n'
     '> "AT&T intends to apply to list the Notes on the New York Stock Exchange."\n'
     '**Product:** senior note\n**Listing:** PUBLIC (NYSE) symbol "T"\n'
     '**Coupon:** EURIBOR + 40 bps [floating]\n**Par:** $1,000.00\n**Size:** $1.2bn'),

    ("OCFC 10-Q — body says UNLISTED outright", "10-Q", False,
     '\U0001F4E2 **OCFC | 10-Q | 2026-08-07** — Completed Flushing acquisition.\n'
     'Company: OCEANFIRST FINANCIAL CORP\n## \U0001F4E2 LISTING: PUBLIC — NASDAQ SYMBOL "OCFC"\n'
     '> "Common stock, $0.01 par value per share OCFC NASDAQ"\n'
     '**Product:** NVCE Stock\n**Listing:** UNLISTED\n**Par:** $0.00001'),

    ("INN 424B5 — common stock ATM", "424B5", False,
     '\U0001F4E2 **INN | 424B5 | 2026-08-07** — Summit Hotel files for up to $200m ATM common stock offering.\n'
     'Company: Summit Hotel Properties, Inc.\n## \U0001F4E2 LISTING: PUBLIC — NYSE SYMBOL "INN"\n'
     '> "Our common stock is traded on the New York Stock Exchange under the symbol \'INN\'"\n'
     '**Product:** common stock\n**Listing:** PUBLIC (NYSE) symbol "INN"\n**Par:** $0.01\n**Size:** $200m'),

    ("BNY 424B2 — $1,000-par senior note", "424B2", False,
     '\U0001F4E2 **BNY | 424B2 | 2026-08-07** — BNY Mellon prices $300M floating-rate senior notes due 2030.\n'
     'Company: Bank of New York Mellon Corp\n## \U0001F4E2 LISTING: PUBLIC — NYSE SYMBOL "BNY"\n'
     '> "The Notes are not intended to be offered to any retail investor in the United Kingdom..."\n'
     '**Product:** senior note\n**Listing:** PUBLIC (NYSE) symbol "BNY"\n'
     '**Coupon:** Compounded SOFR + 69 bps [floating]\n**Par:** $1,000.00\n**Size:** $300m'),

    ("AOD N-2ASR — shelf, nothing priced", "N-2ASR", False,
     '\U0001F4E2 **AOD | N-2ASR | 2026-08-07** — Shelf registration for up to $250 million.\n'
     'Company: ABRDN TOTAL DYNAMIC DIVIDEND FUND\n'
     '**Product:** common shares | preferred shares | notes | subscription rights\n'
     '**Listing:** PUBLIC (NYSE) symbol "AOD"\n**Size:** $250m'),

    ("CSWC 424B3 — common stock ATM", "424B3", False,
     '\U0001F4E2 **CSWC | 424B3 | 2026-08-07** — Capital Southwest adds RBC as sales agent to its ATM programme.\n'
     'Company: Capital Southwest Corporation\n**NEW ISSUANCE**\n'
     '**Product:** common stock\n**Listing:** PUBLIC (NASDAQ) symbol "CSWC"\n**Par:** n/d\n**Size:** up to $2.0B'),

    ("MBIN 10-Q — redemption of a listed preferred at $25/depositary share", "10-Q", True,
     '\U0001F6A8 **MBIN | 10-Q | 2026-08-07** — Redeemed all outstanding Series B Preferred.\n'
     'Company: MERCHANTS BANCORP\n## \U0001F6A8 REDEMPTION OF PUBLICLY TRADED SECURITY\n'
     '> "redeemed all outstanding shares of the Series B Preferred Stock ... at a price equal to the '
     'liquidation preference of $1,000 per share (equivalent to $25 per depositary share), or $125.0 million."'),

    ("SQFT SC TO-I — exchange offer on a listed preferred", "SC TO-I", True,
     '\U0001F501 **SQFT | SC TO-I | 2026-08-07** — Presidio offers 5.5 common shares per Series D Preferred.\n'
     'Company: Presidio Property Trust, Inc.\n## \U0001F501 TENDER / EXCHANGE OFFER\n'
     '> "offer to exchange ... each outstanding share of its 9.375% Series D Cumulative Redeemable '
     'Perpetual Preferred Stock ... five and one half shares (5.5) of its Series A Common Stock"\n'
     '**Security:** Series D Preferred Stock\n**Listing:** Nasdaq (SQFTP)\n**Par value:** $0.01 per share'),
]

for label, form, want_urgent, text in REAL_POSTS:
    tier = dispatch.classify_priority(text, form)[0]
    check(("routes urgent   " if want_urgent else "stays routine   ") + label,
          (tier > 0) == want_urgent,
          f"tier={tier}")

check("a genuine $25-par baby bond IPO still routes urgent",
      dispatch.classify_priority(
          '\U0001F4E2 **XYZ | 424B5 | 2026-08-07** — new baby bond.\n'
          '## \U0001F4E2 LISTING: PUBLIC — NYSE SYMBOL "XYZL"\n'
          '**Product:** baby bond\n**Listing:** PUBLIC (NYSE) symbol "XYZL"\n**Par:** $25.00')[0] == 2,
      "gate must not block the issues he actually wants")

check("a genuine $25-par preferred IPO still routes urgent",
      dispatch.classify_priority(
          '\U0001F4E2 **XYZ | 424B5 | 2026-08-07** — new pfd.\n'
          '## \U0001F4E2 LISTING: PUBLIC — NASDAQ SYMBOL "XYZP"\n'
          '**Product:** preferred stock\n**Listing:** PUBLIC (NASDAQ) symbol "XYZP"\n**Par:** $25.00')[0] == 2)


# ---------------------------------------------------------------------------
# strip_meta_commentary — the BNY post carried the model's own deliberation
# and then repeated the whole summary with a different conclusion.
# ---------------------------------------------------------------------------
print("\nstrip_meta_commentary")

DOUBLED = (
    '\U0001F4E2 **BNY | 424B2 | 2026-08-07** — prices $300M notes.\n'
    '**Product:** senior note\n**Par:** $1,000.00\n\n-------\n'
    '(Note: Filing does not state NYSE/NASDAQ listing explicitly — highlight block added '
    'conditionally per guidance; if listing is not literally stated, block should be omitted. '
    'Re-evaluating: filing contains no literal listing statement — therefore ...\n'
    '\U0001F4E2 **BNY | 424B2 | 2026-08-07** — prices $300M notes.\n'
    '**Listing:** UNLISTED (424B2 for medium-term notes)\n'
)
cleaned = dispatch.strip_meta_commentary(DOUBLED)
check("duplicate summary is dropped", cleaned.count("**BNY | 424B2") == 1)
check("model's deliberation is removed", "Re-evaluating" not in cleaned and "per guidance" not in cleaned)
check("the real summary survives", "$300M notes" in cleaned and "senior note" in cleaned)
check("a normal summary is left untouched",
      dispatch.strip_meta_commentary(REDEMPTION).strip() == REDEMPTION.strip())


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
