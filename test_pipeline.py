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

import datetime
import os
import sys

os.environ.setdefault("NVIDIA_API_KEY", "test")
os.environ.setdefault("DISCORD_WEBHOOK", "https://example.invalid/webhook")

import edgar_poller                   # noqa: E402
import prefilter                      # noqa: E402
import triage                        # noqa: E402
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
# prefilter / N-PX — Aug 2026: five proxy voting records reached #sec-filings
# in twenty minutes on the night of the 21st. Each one reports how a fund
# voted OTHER issuers' proxies through the previous 30 June; the proposals
# were in those issuers' DEF 14As and the outcomes in an 8-K Item 5.07 months
# earlier. Real accession: 0001193125-26-361204 (PPT).
# ---------------------------------------------------------------------------

check(
    "N-PX is skipped",
    prefilter.should_skip({
        "form_type": "N-PX", "entity_name": "Franklin Premier Income Trust",
        "filing_text": "Annual proxy voting report for the period ended June 30, 2026.",
    })[0],
)

check(
    "N-PX/A is skipped too",
    prefilter.should_skip({"form_type": "N-PX/A", "entity_name": "Franklin Managed Municipal",
                           "filing_text": "Amended proxy voting report."})[0],
)

check(
    "a third party's reverse split in a ballot line does not rescue an N-PX",
    prefilter.should_skip({
        "form_type": "N-PX", "entity_name": "Franklin Managed Municipal Income Trust",
        "filing_text": ("Item 3: Approve Reverse Stock Split. Vote cast: FOR. "
                        "Item 4: Approve Merger Agreement. Vote cast: FOR."),
    })[0],
    "the split belongs to a portfolio company, not the filer",
)

check(
    "a bankruptcy ballot in an N-PX is still an N-PX",
    prefilter.should_skip({
        "form_type": "N-PX", "entity_name": "Franklin Premier Income Trust",
        "filing_text": ("Voting instructions on three bondholder matters concerning "
                        "Wolfspeed, Inc. (CUSIP 977852AD4): acceptance of a "
                        "restructuring plan, opting out of releases, and eligible "
                        "holder certification. 182,000 ballots voted FOR."),
    })[0],
    "stale by filing date, and NPORT-P carries the holding more directly",
)

check(
    "the skip reason names the form, for dispatch.log",
    "N-PX" in prefilter.should_skip({"form_type": "N-PX", "filing_text": ""})[1],
)

check(
    "N-CSR is not caught by the N-PX rule",
    not prefilter.should_skip({"form_type": "N-CSR", "entity_name": "BGT",
                               "filing_text": "Annual report to shareholders."})[0],
    "only proxy voting records are dropped, not fund periodics",
)


# ---------------------------------------------------------------------------
# prefilter — Sep 2026. Four filings, four different ways the offering filter
# was reading the wrong part of the document.
#
# Missed (silently dropped, never posted):
#   RIV  424B2 0001398344-26-016049 — 9.1M-share rights offer, rights trade
#                                     as RIV.RT. Killed by a risk factor.
# Delivered to #sec-urgent, and should never have been fetched at all:
#   GS   424B2 0001193125-26-375702 — $1,000-par callable MTN, "Listing: None"
#   PRU  424B2 0001193125-26-376736 — $1,000 InterNotes, no listing statement
# ---------------------------------------------------------------------------
print("\nprefilter — rights offerings and $1,000-par paper (Sep 2026)")

# Verbatim from the RIV prospectus. This one sentence, about what the
# UNDERLYING FUNDS may hold, was the entire reason a rights offering never
# reached Discord.
RIV_RIGHTS = (
    "RiverNorth Opportunities Fund, Inc. is issuing transferable subscription "
    "rights to its stockholders of record as of August 31, 2026. For every "
    "three Rights held, a Record Date Stockholder is entitled to purchase one "
    "Common Share of the Fund (the Primary Subscription). Stockholders who "
    "fully exercise their Rights are entitled to the Over-Subscription "
    "Privilege. The Rights are transferable and will be admitted for trading "
    "on the New York Stock Exchange under the symbol RIV.RT. "
    "Structured Notes Risks. The Underlying Funds may invest in structured "
    "notes. Structured notes are subject to a number of fixed income risks."
)
check(
    "RIV rights offering survives a structured-note RISK FACTOR",
    not prefilter.should_skip({"form_type": "424B2",
                               "entity_name": "RiverNorth Opportunities Fund, Inc.",
                               "filing_text": RIV_RIGHTS})[0],
    "the fund's holdings are not the security being offered",
)

# Nuveen's 497AD press release — a rights offering announced in 9k characters,
# filed 13:51 ET into the hole the old two-window schedule left open.
NMCO_497AD = (
    "FILING BY CERTAIN INVESTMENT COMPANIES OF SECURITIES ACT RULE 482. "
    "Nuveen Municipal Credit Opportunities Fund Announces Terms of Rights "
    "Offering. Rights are transferable and are expected to be admitted for "
    "trading on the NYSE under the symbol NMCO RTWI. The Rights offering will "
    "expire at 5:00 p.m., Eastern time, on October 7, 2026."
)
check(
    "NMCO rights announcement is kept",
    not prefilter.should_skip({"form_type": "497AD",
                               "entity_name": "Nuveen Municipal Credit Opportunities Fund",
                               "filing_text": NMCO_497AD})[0],
)

# Goldman's pricing supplements never write a sentence about listing. The
# cover page carries a bare field, and that is the only statement in 36k
# characters. Everything else about the note reads like ordinary debt.
GS_MTN = (
    "The Goldman Sachs Group, Inc. Principal amount: $8,000,000. "
    "Type of Notes: Fixed rate notes. Denominations: $1,000 and integral "
    "multiples of $1,000 in excess thereof. Interest rate: 6.00% per annum. "
    "Maturity date: August 19, 2041. Listing: None. "
    "The notes are a new issue of securities with no established trading market."
)
check(
    "GS $1,000-par MTN is skipped on 'Listing: None'",
    prefilter.should_skip({"form_type": "424B2",
                           "entity_name": "GOLDMAN SACHS GROUP INC",
                           "filing_text": GS_MTN})[0],
    "a term-sheet field is a listing statement",
)

# Prudential's InterNotes state no listing at all — the denomination is the
# only thing that says this is not for him.
PRU_INTERNOTES = (
    "Prudential Financial InterNotes. Settle Date: Friday, September 11, 2026. "
    "Minimum Denomination/Increments: $1,000.00/$1,000.00. "
    "Coupon 4.700% due 09/15/2029. Initial trades settle flat and clear SDFS."
)
check(
    "PRU InterNotes is skipped on denomination alone",
    prefilter.should_skip({"form_type": "424B2",
                           "entity_name": "PRUDENTIAL FINANCIAL INC",
                           "filing_text": PRU_INTERNOTES})[0],
    "no structured vocabulary and no listing sentence to match on",
)

check(
    "a $25 depositary share is not mistaken for institutional paper",
    not prefilter.should_skip({
        "form_type": "424B5",
        "entity_name": "Merchants Bancorp",
        "filing_text": (
            "Depositary Shares each representing a 1/1,000th interest in a "
            "share of Series D Preferred Stock. Denominations: $1,000 per "
            "share, equivalent to $25.00 per share of the depositary shares, "
            "to be listed on the Nasdaq Stock Market."
        ),
    })[0],
    "the retail-par veto must beat the denomination rule",
)

check(
    "a shelf that merely registers subscription rights is not a rights offering",
    not prefilter.is_rights_offering({
        "filing_text": "We may offer common shares, preferred shares, notes "
                       "or subscription rights from time to time."
    }),
    "topic vocabulary alone is not evidence; the keep must stay narrow",
)

# Oxford Square's $150m COMMON STOCK ATM (424B2, 2026-05-05). A BDC base
# prospectus has to describe rights-offering mechanics in detail because of
# the below-NAV rules, so this clears the terms list on boilerplate alone —
# 9,632 characters in. The cover says what is actually being sold.
OXSQ_ATM = (
    "PROSPECTUS SUPPLEMENT Oxford Square Capital Corp. $150,000,000 Common "
    "Stock. We operate as a closed-end management investment company and have "
    "elected to be regulated as a business development company."
    + (" filler." * 900) +
    " Rights Offerings. We may issue subscription rights to our stockholders "
    "to purchase common stock. Record date stockholders would receive one "
    "right per share, and the rights are transferable."
)
check(
    "a BDC common-stock ATM is not a rights offering, boilerplate and all",
    not prefilter.is_rights_offering({"filing_text": OXSQ_ATM}),
    "mechanics deep in the base prospectus, nothing on the cover",
)
check(
    "…and RIV still is, on a cover mention alone",
    prefilter.is_rights_offering({"filing_text": RIV_RIGHTS}),
)

# AGNC's $2bn COMMON STOCK ATM and Rithm's Jan 2026 424B5 both carry this from
# the base prospectus. Neither offers debt securities — and the same sentence
# sits in the shelf a $25-par preferred would be taken down under.
SHELF_BOILERPLATE = (
    "PROSPECTUS SUPPLEMENT $2,000,000,000 Common Stock. We have entered into "
    "an equity distribution agreement. The terms of any series of debt "
    "securities will be described in the applicable prospectus supplement. "
    "Unless we inform you otherwise in the applicable prospectus supplement, "
    "the debt securities will not be listed on any securities exchange."
)
check(
    "hedged shelf boilerplate is not an unlisted statement",
    prefilter.unlisted_statement(SHELF_BOILERPLATE) is None,
    "the sentence is about a class of security this filing is not offering",
)
check(
    "an unhedged statement elsewhere in the same document still counts",
    prefilter.unlisted_statement(
        SHELF_BOILERPLATE + (" filler." * 60) +
        " No listing: the notes will not be listed on any securities exchange."
    ) is not None,
    "one hedge must not immunise the whole filing",
)
check(
    "RBC's plain statement is still an unlisted statement",
    prefilter.unlisted_statement(" ".join(RBC_BUFFER_NOTE.split()).lower()) is not None,
    "the fixture is line-wrapped; EDGAR text is whitespace-collapsed",
)

check(
    "the RBC buffer note is still dropped after 'structured note' was removed",
    prefilter.should_skip({
        "form_type": "424B2", "entity_name": "Royal Bank of Canada",
        "filing_text": RBC_BUFFER_NOTE,
    })[0],
    "seven other structured signals and two unlisted signals remain",
)


# ---------------------------------------------------------------------------
# edgar_poller feed reading — Sep 2026.
#
# Two properties of EDGAR's current-filings feed that the single-page read got
# wrong, both measured against the live endpoint on 2026-09-01:
#
#   · a page of 100 ENTRIES is ~50 FILINGS, because ownership and beneficial-
#     ownership forms emit one entry per role
#   · at the 17:20-17:30 ET rush a page spans 5m38s, so page 0 is not a
#     recovery mechanism for anything
#
# Together those are why nothing filed during a cron blackout was ever
# recoverable. The paging walk is the fix; these check it walks correctly.
# ---------------------------------------------------------------------------
print("\nedgar_poller feed paging")


def _fake_entry(acc: str, cik: str, form: str, when: str, role: str) -> dict:
    """One feed entry as _parse_feed_entry would return it."""
    return {
        "accession": acc, "cik": cik, "entity_name": f"CO {cik} ({role})",
        "form_type": form, "file_date": when[:10],
        "index_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}-index.htm",
        "updated": when,
    }


# Page 0 is the Form 4 pattern: every filing twice, once per role.
PAGE_0 = []
for i in range(3):
    acc = f"000000000{i}-26-00000{i}"
    PAGE_0.append(_fake_entry(acc, "111", "4", f"2026-09-01T20:0{i}:00-04:00", "Reporting"))
    PAGE_0.append(_fake_entry(acc, "222", "4", f"2026-09-01T20:0{i}:00-04:00", "Issuer"))
PAGE_1 = [_fake_entry("0000000099-26-000099", "333", "424B2",
                      "2026-09-01T14:00:00-04:00", "Filer")]
PAGE_2 = [_fake_entry("0000000098-26-000098", "444", "8-K",
                      "2026-09-01T09:00:00-04:00", "Filer")]

_PAGES = {0: PAGE_0, 100: PAGE_1, 200: PAGE_2}
_requested: list[int] = []


def _stub_page(start: int) -> list:
    _requested.append(start)
    return _PAGES.get(start, [])


_real_fetch_page = edgar_poller._fetch_feed_page
_real_sleep = edgar_poller.time.sleep
edgar_poller._fetch_feed_page = _stub_page
edgar_poller.time.sleep = lambda *_: None
try:
    _requested.clear()
    hits = edgar_poller.fetch_recent_filings(max_pages=1)
    check("one page of 6 role-entries is 3 filings", len(hits) == 3, f"got {len(hits)}")
    check("only page 0 was requested", _requested == [0], f"got {_requested}")

    _requested.clear()
    hits = edgar_poller.fetch_recent_filings(max_pages=5)
    check("paging continues past page 0", len(hits) == 5, f"got {len(hits)}")
    check("an empty page stops the walk", _requested == [0, 100, 200, 300],
          f"got {_requested}")
    check("newest first is preserved", hits[0]["accession"] == "0000000000-26-000000")

    # Page 2 reaches back past 13:00, so the walk stops there — and keeps it.
    # Stopping ON the horizon page rather than before it is deliberate: the
    # page that straddles the boundary holds the oldest filings still in range.
    _requested.clear()
    horizon = datetime.datetime.fromisoformat("2026-09-01T13:00:00-04:00")
    hits = edgar_poller.fetch_recent_filings(max_pages=40, since=horizon)
    check("the horizon stops the walk well before the 40-page ceiling",
          _requested == [0, 100, 200], f"got {_requested}")
    check("the page that crosses the horizon is still kept",
          len(hits) == 5, f"got {len(hits)}")

    # Page 0 spans 20:00-20:02, so a 20:01 horizon is reached on that page.
    _requested.clear()
    recent = datetime.datetime.fromisoformat("2026-09-01T20:01:00-04:00")
    edgar_poller.fetch_recent_filings(max_pages=40, since=recent)
    check("a horizon reached on page 0 stops there",
          _requested == [0], f"got {_requested}")
finally:
    edgar_poller._fetch_feed_page = _real_fetch_page
    edgar_poller.time.sleep = _real_sleep


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
# Route on the posted body — Sep 2026.
#
# Three Goldman / Prudential note supplements pinged #sec-urgent carrying a
# body that classify_priority() scores as routine: 📋 lead emoji, no highlight
# block, "Listing: UNLISTED", "Par: $1,000". Both facts were true because
# routing ran on the RAW completion while render_body() then dropped a second
# summary copy, stripped the deliberation and truncated the tail. The evidence
# that justified the ping was in the part nobody ever saw.
#
# The invariant these lock in: whatever routes a message must be visible in it.
# ---------------------------------------------------------------------------
print("\nroute on the posted body")

# The model wrote the template-conforming summary, second-guessed itself, and
# started again with a redemption header. strip_meta_commentary keeps copy 1.
SELF_CORRECTED = (
    '\U0001F4CB **GS | 424B2 | 2026-08-31** — Goldman prices $8M of 6.00% callable notes due 2041.\n'
    'Company: The Goldman Sachs Group, Inc.\n'
    '**Product:** senior note\n**Listing:** UNLISTED\n**Par:** $1,000\n'
    '**First call:** February 28, 2029 at 100%\n\n'
    '(Note: the call feature may read as a redemption — reconsidering.)\n'
    '\U0001F6A8 **GS | 424B2 | 2026-08-31** — Goldman will redeem notes.\n'
    '## \U0001F6A8 REDEMPTION OF PUBLICLY TRADED SECURITY\n'
    '> "the notes are callable at par on February 28, 2029"'
)

FILING_GS = {
    "primary_doc_url": "https://www.sec.gov/Archives/edgar/data/886982/gs-20260828.htm",
    "filing_url": "https://www.sec.gov/Archives/edgar/data/886982/x-index.htm",
    "accession": "0001193125-26-375702",
}

_raw_tier = dispatch.classify_priority(SELF_CORRECTED, "424B2")[0]
_body = dispatch.render_body(SELF_CORRECTED, dispatch.body_budget(FILING_GS, "@here"))
_body_tier = dispatch.classify_priority(_body, "424B2")[0]

check("the raw completion really does score urgent", _raw_tier > 0,
      "if this stops being true the fixture no longer reproduces the bug")
check("the posted body does not", _body_tier == 0, f"tier={_body_tier}")
check("the discarded header is genuinely absent from the post",
      "REDEMPTION" not in _body)

check("a header that SURVIVES into the post still routes urgent",
      dispatch.classify_priority(
          dispatch.render_body(REDEMPTION, dispatch.body_budget(WITH_DOC, "@here")),
          "8-K")[0] == 1,
      "routing on the body must not cost real alerts")

# Reserving the mention budget before the tier is known is what keeps the
# classified body and the posted body the same string.
_long = "\U0001F6A8 **X | 8-K | 2026-09-01** — call.\n" + ("word " * 900)
check("body length does not depend on the routing decision",
      dispatch.render_body(_long, dispatch.body_budget(FILING_GS, "@here"))
      == dispatch.render_body(_long, dispatch.body_budget(FILING_GS, "@here")))
check("a message routed urgent still fits Discord's cap",
      len(dispatch.assemble_message(
          dispatch.render_body(_long, dispatch.body_budget(FILING_GS, "@here")),
          FILING_GS, prefix="@here")) <= dispatch.MAX_DISCORD_CHARS)

# The PRU post ended "...Omit highlight block — no priority-1 to -4 trigger is
# literally stated. Proceeds, size, and agent ..." — prompt vocabulary, mid
# paragraph, where the line-anchored meta rule could not reach it.
PRU_ECHO = (
    '\U0001F4CB **PRU | 424B2 | 2026-08-31** — Prudential InterNotes pricing supplement.\n'
    'Company: Prudential Financial Inc\n'
    '**Product:** senior note\n**Listing:** UNLISTED\n**Par:** $1,000.00\n\n'
    'These are $1,000-par InterNotes issued via an automated shelf facility. '
    'No NYSE/NASDAQ listing is stated. Omit highlight block — no priority-1 to -4 '
    'trigger is literally stated. Proceeds and agent are disclosed in the pricing table.'
)
_cleaned = dispatch.strip_meta_commentary(PRU_ECHO)
check("mid-paragraph prompt echo is scrubbed", "highlight block" not in _cleaned)
check("the analysis around it survives",
      "InterNotes" in _cleaned and "pricing table" in _cleaned)
check("field lines are never touched by the scrubber",
      "**Listing:** UNLISTED" in _cleaned)


# ---------------------------------------------------------------------------
# Rights offerings route urgent.
#
# A CEF rights offering is the one COMMON-share event in the tradeable
# universe: shares outstanding rise on a fixed date at a price struck off NAV,
# and a fund trading at a premium gives that premium up. It would fail
# _is_tradeable_new_issue by construction, so the rule is ungated —
# _TRADEABLE_GATED_KEYWORDS holds "LISTING:" and nothing else.
#
# Two independent things have to keep working: the header must route it, and
# triage.py must route it even when the model writes no header at all, which
# is how CLM's N-2 reached the main channel on 2026-08-14.
# ---------------------------------------------------------------------------
print("\nrights offerings")

RIV_POST = (
    '\U0001F9E8 **RIV | 424B2 | 2026-08-28** — RiverNorth offers 9,124,000 shares '
    'via transferable rights, one for every three held.\n'
    'Company: RiverNorth Opportunities Fund, Inc.\n'
    '## \U0001F9E8 RIGHTS OFFERING — DILUTION\n'
    '> "For every three Rights held, a Record Date Stockholder is entitled to purchase '
    'one Common Share at 90% of NAV or 95% of the five-day average market price, '
    'whichever is higher."\n'
    '**Ratio:** one new share for every three rights held\n'
    '**Rights symbol:** "RIV.RT" on NYSE\n**Transferable:** yes\n'
    '**Subscription price:** 90% of NAV or 95% of the 5-day average market price, whichever is higher\n'
    '**Estimated price:** $11.00 per share\n**Record date:** 2026-08-31\n'
    '**Expiration:** 2026-09-23\n**Shares offered:** 9,124,000'
)
check("RIV rights offering routes urgent on its header",
      dispatch.classify_priority(RIV_POST, "424B2")[0] == 2)
check("the AOD shelf that only registers rights stays routine",
      dispatch.classify_priority(REAL_POSTS[4][3], REAL_POSTS[4][1])[0] == 0,
      "registering rights among four security types is not an offer")
check("a rights offering is NOT demoted for saying UNLISTED",
      dispatch.classify_priority(
          RIV_POST.replace('**Rights symbol:** "RIV.RT" on NYSE',
                           '**Listing:** UNLISTED'), "424B2")[0] == 2,
      "the dilution is the event; whether the rights themselves list is beside the point")
check("…and it survives the trip through render_body",
      dispatch.classify_priority(
          dispatch.render_body(RIV_POST, dispatch.body_budget(WITH_DOC, "@here")),
          "424B2")[0] == 2,
      "routing on the posted body must not cost the rights alert")

# CLM's N-2 on 2026-08-14 carried an accurate summary, no highlight block, and
# a 📋 lead emoji. classify_priority can only score what the model wrote.
CLM_NO_HEADER = (
    '\U0001F4CB **CLM | N-2 | 2026-08-14** — Cornerstone Strategic Value files a '
    'registration statement for a rights offering.\n'
    'Company: Cornerstone Strategic Value Fund, Inc.'
)
check("a rights offering with no header scores routine on the summary alone",
      dispatch.classify_priority(CLM_NO_HEADER, "N-2")[0] == 0,
      "this is the miss triage.py exists to catch")
check("…and triage promotes it from the filing text",
      triage.triage_filing(
          "N-2",
          "The Fund is offering transferable Rights to subscribe for Common Shares. "
          "For every three (3) rights held, a shareholder may purchase one share. "
          "The subscription price will be 104% of NAV per share.",
          "Cornerstone Strategic Value Fund, Inc.")[0] == 2)


# ---------------------------------------------------------------------------
# Never publish the model's working-out — 2026-09-01, #sec-urgent.
#
# A USB 424B3 post that was the model thinking out loud, verbatim: "We need to
# produce a Discord summary... What emoji? ... We must not include highlight
# block". It pinged, because triage.py promotes every 424B* to P2 on form type
# and nothing downstream asked whether the text was a summary at all.
#
# strip_reasoning() could not help — none of it was inside <think> tags. It
# arrived in the completion, most likely salvaged out of reasoning_content by
# the empty-content fallback in _post_chat().
# ---------------------------------------------------------------------------
print("\nscratchpad leak")

# Reconstructed from the post. Note the well-formed headline in the middle of
# the fourth paragraph — the model quoting the template back at itself is
# exactly why the headline check is anchored to the start of a line.
LEAKED_SCRATCHPAD = (
    "We need to produce a Discord summary. This is a 424B3 pricing supplement "
    "for US Bancorp Senior Medium-Term Notes, Series EE. These are senior notes, "
    "$1,000 denomination, unsecured, callable fixed rate. This is institutional "
    "paper ($1,000 par), not retail.\n\n"
    "We must not include highlight block because not priority 1-4. Use OTHER "
    "section: 2-4 sentences plain prose.\n\n"
    "We need to stay under 1600 chars, easy.\n\n"
    "We need to include ticker? Since no ticker for the offered security, we "
    'write "n/d". So line 1: [EMOJI] **n/d | 424B3 | 2026-09-01** \u2014 ...\n\n'
    "What emoji? Since it's other prospectus/new issuance (priority 7), emoji: "
    "\U0001F4C4 other prospectus / new issuance."
)

REAL_USB = (
    '\U0001F4C4 **USB | 424B3 | 2026-09-01** \u2014 U.S. Bancorp prices $1,000-par '
    'senior medium-term notes at 5.00% due 2030, not exchange-listed.\n'
    'Company: US BANCORP \\DE\\\n'
    '**Product:** senior note\n**Listing:** UNLISTED\n**Coupon:** 5.00% fixed\n'
    '**Par:** $1,000\n**Maturity:** 2030-03-10'
)

check("the leaked scratchpad is not a summary",
      not dispatch.looks_like_summary(LEAKED_SCRATCHPAD))
check("a headline quoted mid-sentence does not rescue it",
      "**n/d | 424B3 | 2026-09-01**" in LEAKED_SCRATCHPAD
      and not dispatch.looks_like_summary(LEAKED_SCRATCHPAD),
      "the start-of-line anchor is the whole defence here")
check("a real summary is still a summary", dispatch.looks_like_summary(REAL_USB))
check("it survives render_body",
      dispatch.looks_like_summary(
          dispatch.render_body(REAL_USB, dispatch.body_budget(WITH_DOC, "@here"))))
check("so does a redemption", dispatch.looks_like_summary(REDEMPTION))
check("and a two-field headline with no date",
      dispatch.looks_like_summary("\U0001F501 **X | SC TO-I** \u2014 offer"),
      "the check must not become a template validator")
check("an empty completion is not a summary", not dispatch.looks_like_summary(""))

# The "n/d" ticker is what most bank paper carries, and the old headline
# pattern ([A-Z0-9.\-]) never matched it — so the duplicate-summary guard was
# silently a no-op on exactly those posts.
ND_DOUBLED = (
    '\U0001F4CB **n/d | 424B2 | 2026-08-31** \u2014 Goldman prices $4.6M callable MTNs.\n'
    '**Product:** senior note\n\n(Note: reconsidering the listing.)\n'
    '\U0001F4CB **n/d | 424B2 | 2026-08-31** \u2014 Goldman prices $4.6M callable MTNs.\n'
    '**Listing:** UNLISTED'
)
check("a duplicated n/d summary is collapsed like any other",
      dispatch.strip_meta_commentary(ND_DOUBLED).count("**n/d | 424B2") == 1,
      "an 'n/d' ticker used to slip past _HEADLINE_RE entirely")


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
# triage — Aug 2026: two filings routed to the main channel that should have
# paged, both because urgency was read off the model's wording.
#
#   CLM  N-2   2026-08-14 23:45 — 1-for-3 rights offering at 104% of NAV on a
#              fund at a 14.7% premium. Accurate summary, no highlight block,
#              📋 on line 1. Tier 0. CLM -6.6% and CRF -6.8% over the next
#              three sessions.
#   SAR  424B2 2026-08-18 — $25-par baby bond listing as SAX, proceeds
#              earmarked to redeem the 6.00% and 8.00% 2027 Notes. The LISTING
#              block fired tier 2, then the tradeable gate demoted it: the
#              model wrote "$25 par" as prose and the pattern only matched a
#              **Par:** field.
#
# triage_filing reads form_type and filing_text, which do not change with
# phrasing, and can only promote.
# ---------------------------------------------------------------------------
print("\ntriage — urgency from the filing, not the wording")

from triage import triage_filing, find_redemption_targets, split_form  # noqa: E402

CLM_N2_TEXT = (
    "Cornerstone Strategic Investment Fund, Inc. is issuing non-transferable "
    "rights to its holders of record of shares of common stock. For every "
    "three (3) Rights a Stockholder receives, such Stockholder will be "
    "entitled to buy one (1) new Share. The subscription price per Share will "
    "be 104% of NAV per Share as calculated at the close of trading on the "
    "date of expiration of the Offering."
)

SAX_TEXT = (
    "We are offering $ in aggregate principal amount of % notes due 2031. We "
    "intend to list the Notes on the New York Stock Exchange under the trading "
    "symbol 'SAX'. We expect to use the net proceeds from this offering to "
    "redeem the outstanding 6.00% 2027 Notes, redeem the outstanding 8.00% "
    "2027 Notes, and/or repay a portion of the outstanding indebtedness under "
    "the Valley Credit Facility."
)

tier, label, _ = triage_filing("N-2", CLM_N2_TEXT)
check("CLM rights offering reaches an urgent tier from the filing alone",
      tier == 2, f"got tier={tier}")

tier, label, notes = triage_filing("424B2", SAX_TEXT)
check("SAR use-of-proceeds redemption is tier 1, above the new listing",
      tier == 1, f"got tier={tier}")
check("the redeemed series are named, both of them",
      any("6%" in n and "8%" in n for n in notes), f"notes={notes}")

targets = find_redemption_targets(SAX_TEXT)
check("both series are extracted with coupon and year",
      [(t["coupon"], t["year"]) for t in targets] == [(6.0, 2027), (8.0, 2027)],
      str(targets))
check("an 'and/or' use of proceeds is not reported as committed",
      not any(t["committed"] for t in targets))
check("a notice of redemption is reported as committed",
      find_redemption_targets(
          "The Company has issued a notice of redemption for all of the "
          "outstanding 6.00% 2027 Notes.")[0]["committed"])

# The 8.125% 2027 Notes (SAY) are NOT named in the SAX filing. Rounding the
# coupon or matching the year alone would call a bond nobody is calling.
check("a series the filing does not name is not extracted",
      all(t["coupon"] != 8.125 for t in targets))

check("a capital-structure mention is not a redemption",
      find_redemption_targets(
          "Our capital structure includes the 6.00% 2027 Notes, which remain "
          "outstanding.") == [])

# Base form versus amendment. The announcement moves price; the amendment
# sets a date. Across CLM/CRF's last three offerings the three sessions after
# the base N-2 were negative 6 of 6, and positive 4 of 6 after the N-2/A.
check("an N-2/A does not page on form type alone",
      triage_filing("N-2/A", "Amendment to the registration statement.")[0] == 0)
check("an amendment carrying a redemption still pages",
      triage_filing("N-2/A", SAX_TEXT)[0] == 1)
check("an N-2/A that restates rights terms still pages",
      triage_filing("N-2/A", CLM_N2_TEXT)[0] == 2)

check("split_form strips the amendment suffix", split_form("N-2/A") == ("N-2", True))
check("a routine fund periodic stays routine",
      triage_filing("NPORT-P", "Monthly portfolio holdings report.")[0] == 0)
check("an 8-K with nothing structural stays routine",
      triage_filing("8-K", "The Company announced quarterly results.")[0] == 0)


# ---------------------------------------------------------------------------
# classify_priority — the two fixes that let the summary path work as well.
# ---------------------------------------------------------------------------
print("\nclassify_priority — redemption tier and the $25-par prose")

check("'$25 par' in prose clears the tradeable gate",
      dispatch._is_tradeable_new_issue(
          '\U0001F4E2 **SAX | 424B2** — offering $SAX notes due 2031 at $25 par\n'
          '## \U0001F4E2 LISTING: PUBLIC — NYSE SYMBOL "SAX"')[0])
check("'par value of $25' clears it too",
      dispatch._is_tradeable_new_issue("baby note with a par value of $25")[0])
check("$1,000 par is still institutional",
      not dispatch._is_tradeable_new_issue(
          'Senior note\n**Par:** $1,000.00\n## LISTING: PUBLIC — NYSE')[0])

# PROCEEDS WILL REDEEM moved to tier 1. While it sat in tier 2 it was gated by
# a test about the NEW security, so a redemption of listed bonds could be
# demoted because the bond being issued did not look retail.
tier, label = dispatch.classify_priority(
    '\U0001F4E2 **SAX | 424B2 | 2026-08-18** — new notes\nCompany: Saratoga\n'
    '## \U0001F4B8 PROCEEDS WILL REDEEM EXISTING SECURITIES: 6.00% 2027 Notes\n'
    '> "We expect to use the net proceeds to redeem the outstanding 6.00% 2027 Notes."\n'
    '**Product:** senior note\n**Par:** $1,000.00', "424B2")
check("PROCEEDS WILL REDEEM is tier 1 and not gated by the new-issue test",
      tier == 1, f"got tier={tier} label={label!r}")

tier, _ = dispatch.classify_priority(
    '\U0001F9E8 **CLM | N-2 | 2026-08-14** — rights offering\nCompany: Cornerstone\n'
    '## \U0001F9E8 RIGHTS OFFERING — DILUTION\n'
    '> "For every three (3) Rights a Stockholder receives..."\n'
    '**Product:** common stock', "N-2")
check("a rights-offering block routes urgent despite being common stock",
      tier == 2, f"got tier={tier}")

# A genuine LISTING block is still gated — that protection is unchanged.
tier, _ = dispatch.classify_priority(
    '\U0001F4E2 **INN | 424B5 | 2026-08-07** — ATM programme\nCompany: Summit\n'
    '## \U0001F4E2 LISTING: PUBLIC — NYSE SYMBOL "INN"\n'
    '**Product:** common stock\n**Par:** n/d', "424B5")
check("a common-stock ATM is still demoted", tier == 0, f"got tier={tier}")


# ---------------------------------------------------------------------------
# DELIBERATE BEHAVIOUR CHANGE, Aug 2026 — read this before "fixing" it.
#
# "AOD N-2ASR — shelf, nothing priced" is in the tradeable-gate table above as
# a filing that must STAY ROUTINE, tuned away on 2026-08-07 as a false ping.
# It still stays routine on the summary path, which is what that table tests.
#
# But triage_filing now promotes shelf registrations on form type, because the
# instruction changed: shelf registrations are wanted in #sec-urgent. So an
# N-2ASR pages via the deterministic path even though the summary path demotes
# it. That is intended, and it is the one place where this work makes the
# channel louder rather than quieter. If shelf noise becomes the problem,
# remove the *ASR forms from CAPITAL_RAISE_FORMS in triage.py — do not
# reinstate a gate, which is what silenced CLM.
# ---------------------------------------------------------------------------
print("\nshelf registrations — louder on purpose")

aod_summary = ('\U0001F4E2 **AOD | N-2ASR | 2026-08-07** — Shelf registration.\n'
               '**Product:** common shares | preferred shares | notes\n'
               '**Listing:** PUBLIC (NYSE) symbol "AOD"\n**Size:** $250m')
check("summary path still demotes an unpriced shelf",
      dispatch.classify_priority(aod_summary, "N-2ASR")[0] == 0)
check("filing path promotes it, as now requested",
      triage_filing("N-2ASR", "Shelf registration statement for up to "
                              "$250 million of common shares, preferred "
                              "shares, notes and subscription rights.")[0] == 2)


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("All checks passed.")
