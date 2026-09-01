"""
prefilter.py — drop noise filings before the LLM call.

Three filters:
  1. Form 3/4/5 ownership filings: skip unless the filer matches a tracked
     activist (Saba, Bulldog, Karpus, RiverNorth, etc.).
  2. 424B / FWP offerings: skip retail structured notes (autocallable,
     contingent coupon, market-linked, etc.) AND unlisted bank senior notes
     ($1k denomination, no exchange listing — Citi/JPM/BAC/RBC etc.).
     Keep only offerings that explicitly mention listing on NYSE / NASDAQ.
  3. Form N-PX proxy voting records: skip unconditionally. See below.

Edit the lists below to tune. Matching is substring, case-insensitive.

⚠ THE COST OF A FALSE SKIP IS SILENCE. A dropped filing produces no post, no
log line in Discord, and no way to notice it went missing — it looks exactly
like a quiet day. So every skip signal here must describe THE SECURITY BEING
OFFERED, never a risk factor, an index definition, or a portfolio holding.
See is_rights_offering() for the case that proved the point.
"""

import re

# ---------------------------------------------------------------------------
# Activist filers — tracked names for Form 3/4/5 surveillance.
# Match runs against entity_name + filing_text (lowercased).
# ---------------------------------------------------------------------------
ACTIVIST_FILERS: list[str] = [
    "saba capital",
    "boaz weinstein",
    "bulldog investors",
    "phillip goldstein",
    "phil goldstein",
    "karpus management",
    "karpus investment",
    "1607 capital",
    "city of london investment",
    "sit investment associates",
    "relative value partners",
    "western investment",
    "rivernorth capital",
    "rivernorth funds",
    "almitas capital",
    "ancora holdings",
    "ancora advisors",
    "wynnefield capital",
    "180 degree capital",
    "source capital",
    "bandera partners",
    "lazard asset management",
    "matisse capital",
    # add tickers/CIKs of activist funds here as you discover them
]

# ---------------------------------------------------------------------------
# Retail structured-note signals — strong indicators a 424B / FWP is for a
# product you don't trade (autocallable, contingent coupon, etc.).
# ---------------------------------------------------------------------------
STRUCTURED_NOTE_SIGNALS: list[str] = [
    "autocallable",
    "auto-callable",
    "auto callable",
    "contingent coupon",
    "contingent income",
    "market-linked",
    "market linked",
    "buffer note",
    "buffered note",
    "principal-protected note",
    "principal protected note",
    "barrier note",
    "leveraged upside",
    "dual directional",
    "review notes",
    "callable yield",
    "trigger jump",
    "trigger autocallable",
    "linked to the worst",
    "linked to the least performing",
    "least performing underlying",
    "worst-performing underlying",
    "worst performing underlying",
    "linked to the lowest",
    "step-up callable",
    # Added Aug 2026 after an RBC Nasdaq-100 buffer note (0000950103-26-011945)
    # reached Discord. None of the signals above appear in it — its payoff
    # vocabulary is "participation rate" / "buffer" / "underlier" instead.
    "participation rate",
    "upside participation",
    "buffer amount",
    "buffer level",
    "underlier",              # near-universal in payoff-linked notes, ~never in preferreds
    "initial underlier level",
    "final underlier level",
    # ⚠ "structured note" was here from Aug 2026 until Sep 2026 and was
    # REMOVED. It is a topic word, not an offering term: it appears in the
    # boilerplate "Structured Notes Risks. The Underlying Funds may invest in
    # structured notes." risk factor carried by every fund-of-funds CEF
    # prospectus. That one sentence silently killed RiverNorth's RIV rights
    # offering (0001398344-26-016049) — a 9.1M-share offer with transferable
    # rights trading as RIV.RT. The RBC note that motivated the addition hits
    # seven other signals here plus two UNLISTED_SIGNALS, so nothing regressed.
    # Do not reinstate it.
]

# ---------------------------------------------------------------------------
# Unlisted signals — explicit statements that the offering will not trade on
# a US exchange. Catches both bank senior notes and structured products.
# ---------------------------------------------------------------------------
UNLISTED_SIGNALS: list[str] = [
    "will not be listed on any securities exchange",
    "will not be listed on any exchange",
    "are not listed on any securities exchange",
    "are not listed on any exchange",
    "no exchange listing",
    "no listed exchange",
    "the notes will not be listed",
    "we do not intend to list",
    "will not apply to list",
    "do not intend to apply to list",
    "are not intended to be listed",
    "unlisted (otc)",
    # Term-sheet shorthand. Goldman's fixed-rate MTN pricing supplements
    # (e.g. 0001193125-26-375702) never write a sentence about listing — the
    # cover page carries a bare "Listing: None" field, and nothing else in the
    # document says the notes are unlisted. Anchored on the colon so prose
    # like "the listing: none of the exchanges..." can't match by accident.
    "listing: none",
    "listing: not applicable",
    "listing: n/a",
    "exchange listing: none",
]

# ---------------------------------------------------------------------------
# Listed signals — explicit statements that THIS offering will list on a US
# exchange. Overrides STRUCTURED_NOTE_SIGNALS (a genuine exchange-listed baby
# bond can share payoff vocabulary with a structured note), but NOT
# UNLISTED_SIGNALS — see is_unlisted_offering() for the precedence rule.
#
# ⚠ Every entry must describe the SECURITY BEING OFFERED, not merely mention
# an exchange. Bare "listed on the nasdaq" was removed Aug 2026: it matched
# the Nasdaq-100 Index's own definition ("100 of the largest non-financial
# companies listed on The Nasdaq Stock Market") inside an RBC structured note
# and forced a keep on a filing that twice said it would NOT be listed.
# Prefer phrasings containing "apply to list" / "approved for listing" /
# "under the symbol", which have no benign index-description reading.
# ---------------------------------------------------------------------------
LISTED_SIGNALS: list[str] = [
    "nyse:",
    "nasdaq:",
    "application has been made to list",
    "we have applied to list",
    "we intend to apply to list",
    "intend to apply to list the",
    "approved for listing on the new york",
    "approved for listing on the nasdaq",
    "approved for listing on the nyse",
    "expected to be listed on the new york",
    "expected to be listed on the nasdaq",
    "will be listed on the new york stock exchange",
    "will be listed on the nasdaq",
    "trade on the new york stock exchange under the symbol",
    "trade on the nasdaq stock market under the symbol",
]

# ---------------------------------------------------------------------------
# Proxy voting records — Aug 2026: five N-PX filings reached #sec-filings
# inside twenty minutes on the night of the 21st (PMM, PMO, PPT, DBRG and
# others), each one an LLM call and a post, and none of them about the filer.
#
# An N-PX reports how a fund voted OTHER issuers' proxies over the twelve
# months to 30 June, filed by 31 August. Everything in it was public long
# before: the proposal in that issuer's DEF 14A weeks ahead of the meeting,
# the outcome in an 8-K Item 5.07 within four business days of it. What N-PX
# adds is which way this one holder voted, as much as fourteen months late.
#
# It is dropped unconditionally rather than demoted, because the body is a
# list of other companies' corporate actions and nothing downstream can tell
# them from the filer's own. PPT's filing of 2026-08-21 is a bankruptcy ballot
# on Wolfspeed debt; a ballot line reading "Approve Reverse Stock Split" is
# some portfolio company's split, not the fund's. Sending that to the model
# only buys an accurate summary of an event that cannot move the ticker it is
# filed under.
#
# 686 CIKs are tracked and the deadline is the same date for all of them, so
# this is a concentrated annual burst, not a trickle.
#
# Volume, not judgement, is the reason this is in prefilter and not in
# triage.py: skipping here is what avoids the token spend.
PROXY_VOTING_FORMS: set[str] = {"N-PX", "N-PX/A"}

# ---------------------------------------------------------------------------
# Rights-offering signals — a closed-end fund issuing transferable
# subscription rights to its own holders.
#
# This is the one offering type that must NEVER be dropped. It is dilutive,
# it is dated (the rights expire), and the rights themselves list and trade
# under their own ticker (RIV.RT, "NMCO RT"), so it is directly actionable in
# the reader's universe.
#
# It also happens to be the offering type most exposed to a false skip: a CEF
# rights prospectus runs 250-300k characters of fund boilerplate, and
# somewhere in that mass sits a risk factor about structured notes, a line
# about preferred shares that "may not be listed" during their first 30 days,
# and an expense table denominated in $1,000. Any of those can look like a
# bank note to a substring matcher. So this check runs FIRST and wins
# outright — see is_unlisted_offering().
#
# The safety of running first rests entirely on precision, so evidence is
# required from BOTH lists below: the topic AND the mechanics of a live offer.
#
# The two-list split is not decoration. A first attempt required any two
# markers from one flat list and matched Gladstone Capital's and Saratoga's
# ordinary shelf takedowns, because a BDC base prospectus lists "subscription
# rights" among the securities it registers and carries a boilerplate "Rights
# Offerings" section describing what one would look like. Neither filing was
# offering rights — Saratoga's was $85m of 8.00% notes due 2031. Topic words
# are free; over-subscription privileges and record-date stockholders are not.
# ---------------------------------------------------------------------------
RIGHTS_OFFERING_SIGNALS: list[str] = [
    "subscription rights",
    "rights offering",
    "rights offer",
]

# Mechanics of an actual offer. A shelf that merely registers rights never
# describes these, because there is no offer yet to describe.
RIGHTS_OFFER_TERMS: list[str] = [
    "over-subscription",
    "primary subscription",
    "record date stockholders",
    "record date shareholders",
    "rights are transferable",
    "transferable subscription rights",
    "expiration of the offer",
]

# ...and the topic has to be on the COVER, not buried in the base prospectus.
# A BDC base prospectus describes rights-offering mechanics in real detail —
# the below-NAV rules require it — so Oxford Square's $150m COMMON STOCK ATM
# clears the terms list on boilerplate alone. What it cannot fake is where the
# words appear. Measured first-occurrence offsets:
#
#   RIV 424B2   350      OXSQ 424B2 (common ATM)     9,632
#   NMCO 497AD  158      SAR 424B2 (8.00% notes)    11,630
#   NMCO 424B2  525      HTGC 424B2 (6.30% notes)   25,135
#                        GLAD 424B2 (7.00% notes)  166,347
#
# A real rights offering names itself on the cover page because that is what
# is being sold. 4,000 leaves ~8x headroom over the largest real offset and
# ~2.4x clearance below the nearest false one.
RIGHTS_COVER_CHARS = 4_000

# ---------------------------------------------------------------------------
# Shelf hedges — phrasing that marks an UNLISTED_SIGNAL as base-prospectus
# boilerplate about a security class rather than a statement about the thing
# being offered here.
#
# AGNC's $2bn COMMON STOCK at-the-market supplement (0001104659-26-xxxxx) and
# Rithm's January 2026 424B5 both carry, from the base prospectus:
#
#   "Unless we inform you otherwise in the applicable prospectus supplement,
#    the debt securities will not be listed on any securities exchange."
#
# Neither filing offers debt securities. Skipping them happens to be right for
# other reasons, but the same sentence sits in the base prospectus of every
# shelf those issuers file from — including the one a $25-par preferred would
# be taken down under. That is the RIV failure mode again: a sentence about a
# different security silently killing the filing.
#
# The discriminator is the hedge. A pricing supplement that means it writes
# "No listing: the notes will not be listed on any securities exchange" with
# nothing conditional in front (RBC, verbatim). If ANY unhedged occurrence
# exists the filing is still skipped, so a document that hedges once and
# states it plainly elsewhere is unaffected.
# ---------------------------------------------------------------------------
SHELF_HEDGES: list[str] = [
    "unless we inform you otherwise",
    "in the applicable prospectus supplement",
    "unless otherwise specified in the applicable",
    "unless otherwise indicated in the applicable",
    "unless otherwise provided in the applicable",
]
HEDGE_WINDOW = 160   # characters of lead-in searched for a hedge
#                      (AGNC's hedge opens 88 characters before its signal;
#                       keep this tight so a hedge elsewhere in a long
#                       document cannot immunise an unrelated statement)


# ---------------------------------------------------------------------------
# Institutional denomination — the single clearest tell that an offering is
# not in the reader's universe, and the one signal the phrase lists missed.
#
# Goldman's fixed-rate MTNs and Prudential's InterNotes are plain vanilla
# senior notes: no structured-product vocabulary to match, and Prudential's
# term sheet makes no listing statement at all. What they do state, on the
# cover, is the denomination — $1,000, sometimes with no exchange listing
# anywhere in the document. Three of them reached #sec-urgent on 2026-08-31.
#
# Anchored on the word "denomination(s)" so the many incidental $1,000s in a
# real prospectus can't match: a CEF expense table ("expenses you would pay
# on a $1,000 investment"), an asset-coverage-per-$1,000 ratio, or a
# liquidation preference of $1,000 per share that is $25 per depositary share.
# ---------------------------------------------------------------------------
INSTITUTIONAL_DENOM_RE = re.compile(
    r"(?:minimum\s+)?denominations?"          # Denominations: / Minimum Denomination
    r"(?:\s*/\s*increments?)?"                # /Increments
    r"[^A-Za-z0-9]{0,12}"                      # ": ", " of ", "&#160;" etc.
    r"\$\s?(?:1[,.]000|[2-9][,.]?\d{3}|\d{2,3}[,.]\d{3})",   # $1,000 and up
    re.I,
)

# Positive evidence of a retail-denominated income security. Present in every
# $25-par preferred, depositary share and baby bond; absent from bank paper.
RETAIL_PAR_SIGNALS: list[str] = [
    "liquidation preference of $25",
    "$25.00 per share",
    "$25 per share",
    "$25 per depositary share",
    "depositary share",
    "depositary receipt",
    "baby bond",
    "per depositary share",
]


OWNERSHIP_FORMS: set[str] = {"3", "4", "5", "3/A", "4/A", "5/A"}
OFFERING_FORMS: set[str] = {
    "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8", "FWP",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _haystack(filing: dict) -> str:
    """Lowercased entity name + filing text for substring matching."""
    return " ".join([
        filing.get("entity_name", "") or "",
        filing.get("filing_text", "") or "",
    ]).lower()


def is_activist_filer(filing: dict) -> bool:
    h = _haystack(filing)
    return any(name in h for name in ACTIVIST_FILERS)


def unlisted_statement(text: str) -> str | None:
    """The first UNLISTED_SIGNAL stated about THIS offering, or None.

    Occurrences introduced by a shelf hedge are skipped — see SHELF_HEDGES.
    """
    lower = text.lower()
    for sig in UNLISTED_SIGNALS:
        start = 0
        while True:
            i = lower.find(sig, start)
            if i < 0:
                break
            lead = lower[max(0, i - HEDGE_WINDOW):i]
            if not any(h in lead for h in SHELF_HEDGES):
                return sig
            start = i + 1
    return None


def is_rights_offering(filing: dict) -> bool:
    """True when the filing is a subscription-rights offer to existing holders.

    Three things must hold together, because this check wins outright and
    precision is the only thing making that safe:
      · the topic appears on the cover (first RIGHTS_COVER_CHARS characters)
      · at least one mechanic of a live offer appears anywhere
    RIV's 424B2 and Nuveen's NMCO 497AD clear both. Gladstone's and Saratoga's
    note offerings clear neither on the cover; Oxford Square's common-stock ATM
    clears the mechanics on base-prospectus boilerplate but not the cover; the
    Goldman / Prudential / RBC note supplements clear nothing at all.
    """
    text = (filing.get("filing_text", "") or "").lower()
    cover = text[:RIGHTS_COVER_CHARS]
    return (any(sig in cover for sig in RIGHTS_OFFERING_SIGNALS)
            and any(term in text for term in RIGHTS_OFFER_TERMS))


def is_institutional_denomination(filing: dict) -> tuple[bool, str]:
    """Returns (skip, reason) for $1,000-and-up denominated paper.

    Reads the denomination the filing states for the security being offered.
    A retail signal ($25 par, depositary share, baby bond) vetoes it, so a
    preferred whose liquidation preference is quoted as "$1,000 per share
    (equivalent to $25 per depositary share)" survives.
    """
    text = (filing.get("filing_text", "") or "")
    m = INSTITUTIONAL_DENOM_RE.search(text)
    if not m:
        return False, ""
    lower = text.lower()
    if any(sig in lower for sig in RETAIL_PAR_SIGNALS):
        return False, ""
    return True, f"institutional denomination ({m.group(0).strip()!r})"


def is_unlisted_offering(filing: dict) -> tuple[bool, str]:
    """
    Returns (skip, reason). True means the offering is an unlisted
    retail/wholesale product not relevant to a public-securities trader.

    PRECEDENCE (order matters — every step of it is a bug that shipped):

      0. A RIGHTS OFFERING is kept, unconditionally. Added Sep 2026 after
         RiverNorth's RIV rights offering was dropped by a structured-note
         risk factor buried in the prospectus. A 300k-character CEF document
         will eventually contain a sentence that looks like bank-note
         boilerplate to a substring matcher; two independent rights markers
         are far better evidence than one stray phrase.
      1. An explicit UNLISTED_SIGNAL wins over everything below it. "The
         notes will not be listed on any securities exchange" is an
         unambiguous statement about the security being offered; there is no
         benign reading of it. It is NOT overridable by a listing phrase,
         because listing phrases turned out to be the fragile ones. Shelf
         boilerplate hedged with "unless we inform you otherwise in the
         applicable prospectus supplement" does not count — see
         unlisted_statement().
      2. A stated denomination of $1,000 or more, with no retail-par signal
         anywhere, drops the filing. This is what catches plain vanilla bank
         MTNs — no payoff vocabulary to match, sometimes no listing statement
         at all, just a $1,000 cover-page denomination.
      3. Otherwise, structured-note vocabulary drops the filing — unless a
         LISTED_SIGNAL says this particular offering will list, which is the
         escape hatch for exchange-traded baby bonds that happen to share
         payoff vocabulary.

    Step 1 used to run after the listing check, so one loose phrase matching
    an index description ("...companies listed on The Nasdaq Stock Market")
    beat two explicit "will not be listed" statements and pushed an RBC buffer
    note to Discord. Do not reinstate that ordering.
    """
    if is_rights_offering(filing):
        return False, ""

    text = (filing.get("filing_text", "") or "").lower()

    unlisted = unlisted_statement(text)
    if unlisted:
        return True, f"unlisted offering ({unlisted!r})"

    inst, why = is_institutional_denomination(filing)
    if inst:
        return True, why

    structured = next((s for s in STRUCTURED_NOTE_SIGNALS if s in text), None)
    if structured:
        listed = next((s for s in LISTED_SIGNALS if s in text), None)
        if listed:
            return False, ""
        return True, f"structured note ({structured!r})"

    return False, ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def should_skip(filing: dict) -> tuple[bool, str]:
    """
    Returns (skip, reason).

    skip=True means the dispatcher should move the filing to processed/ without
    calling the LLM. reason is a short string for the dispatch.log.
    """
    form_type = (filing.get("form_type", "") or "").strip().upper()

    # 0) N-PX — a record of votes cast on other issuers' securities, never an
    #    event in the filer's own. Checked first and without reading the text,
    #    since the text is what misleads every reader downstream.
    if form_type in PROXY_VOTING_FORMS:
        return True, (f"{form_type} proxy voting record — votes on other "
                      f"issuers, through 30 June, already public via their "
                      f"DEF 14A and 8-K Item 5.07")

    # 1) Form 3 / 4 / 5 — skip unless filed by a tracked activist
    if form_type in OWNERSHIP_FORMS:
        if not is_activist_filer(filing):
            return True, f"Form {form_type} not from tracked activist"
        return False, ""

    # 2) 424B / FWP — skip unlisted structured products and unlisted senior notes
    if form_type in OFFERING_FORMS:
        skip, why = is_unlisted_offering(filing)
        if skip:
            return True, f"{form_type} {why}"
        return False, ""

    return False, ""
