"""
prefilter.py — drop noise filings before the LLM call.

Two filters:
  1. Form 3/4/5 ownership filings: skip unless the filer matches a tracked
     activist (Saba, Bulldog, Karpus, RiverNorth, etc.).
  2. 424B / FWP offerings: skip retail structured notes (autocallable,
     contingent coupon, market-linked, etc.) AND unlisted bank senior notes
     ($1k denomination, no exchange listing — Citi/JPM/BAC/RBC etc.).
     Keep only offerings that explicitly mention listing on NYSE / NASDAQ.

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
# Listed signals — explicit listing language. If any of these appear, KEEP
# the filing even if other unlisted/structured signals also appear.
# ---------------------------------------------------------------------------
LISTED_SIGNALS: list[str] = [
    "nyse:",
    "nasdaq:",
    "listed on the new york stock exchange",
    "listed on the nasdaq",
    "expected to be listed on the new york",
    "expected to be listed on the nasdaq",
    "application has been made to list",
    "we have applied to list",
    "we intend to apply to list",
    "intend to apply to list the",
    "approved for listing on the new york",
    "approved for listing on the nasdaq",
    "trade on the new york stock exchange under the symbol",
    "trade on the nasdaq",
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


def is_unlisted_offering(filing: dict) -> tuple[bool, str]:
    """
    Returns (skip, reason). True means the offering is unlisted retail/wholesale
    product not relevant to a public-securities trader.
    """
    text = (filing.get("filing_text", "") or "").lower()

    # If the filing explicitly says it will be listed on a US exchange, keep it
    # regardless of any other signals.
    if any(s in text for s in LISTED_SIGNALS):
        return False, ""

    # Strong: structured-note language → drop.
    if any(s in text for s in STRUCTURED_NOTE_SIGNALS):
        return True, "structured note (unlisted)"

    # Explicit unlisted statement → drop (catches bank senior notes too).
    if any(s in text for s in UNLISTED_SIGNALS):
        return True, "unlisted offering"

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
