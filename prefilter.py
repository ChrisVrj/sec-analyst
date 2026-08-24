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
"""

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
    "structured note",
    "underlier",              # near-universal in payoff-linked notes, ~never in preferreds
    "initial underlier level",
    "final underlier level",
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


def is_unlisted_offering(filing: dict) -> tuple[bool, str]:
    """
    Returns (skip, reason). True means the offering is an unlisted
    retail/wholesale product not relevant to a public-securities trader.

    PRECEDENCE (order matters — this was the Aug 2026 bug):

      1. An explicit UNLISTED_SIGNAL wins outright. "The notes will not be
         listed on any securities exchange" is an unambiguous statement about
         the security being offered; there is no benign reading of it. It is
         NOT overridable by a listing phrase, because listing phrases turned
         out to be the fragile ones.
      2. Otherwise, structured-note vocabulary drops the filing — unless a
         LISTED_SIGNAL says this particular offering will list, which is the
         escape hatch for exchange-traded baby bonds that happen to share
         payoff vocabulary.

    The previous order checked LISTED_SIGNALS first and returned early, so one
    loose phrase matching an index description ("...companies listed on The
    Nasdaq Stock Market") beat two explicit "will not be listed" statements
    and pushed an RBC buffer note to Discord. Do not reinstate that ordering.
    """
    text = (filing.get("filing_text", "") or "").lower()

    unlisted = next((s for s in UNLISTED_SIGNALS if s in text), None)
    if unlisted:
        return True, f"unlisted offering ({unlisted!r})"

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
