"""
prefilter.py — drop noise filings before the LLM call.

Five filters:
  1. Form 3/4/5 ownership filings: skip unless the filer matches a tracked
     activist (Saba, Bulldog, Karpus, RiverNorth, etc.).
  2. Form 144 proposed-sale notices: same activist gate. See below.
  3. 424B / FWP document-maintenance supplements: index supplements and
     prospectus addenda, which are filed on offering forms but offer
     nothing. See is_document_maintenance().
  4. 424B / FWP offerings: skip retail structured notes (autocallable,
     contingent coupon, market-linked, etc.), unlisted bank senior notes,
     and anything denominated outside the reader's universe — $1,000-and-up
     paper, $1,000 bank depositary shares, and non-USD notes. See
     offered_denomination().
  5. Form N-PX proxy voting records: skip unconditionally. See below.

Edit the lists below to tune. Matching is substring, case-insensitive, on
text with its HTML entities decoded — see plain_text().

⚠ THE COST OF A FALSE SKIP IS SILENCE. A dropped filing produces no post, no
log line in Discord, and no way to notice it went missing — it looks exactly
like a quiet day. So every skip signal here must describe THE SECURITY BEING
OFFERED, never a risk factor, an index definition, or a portfolio holding.
See is_rights_offering() for the case that proved the point.
"""

import html
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

# The same statement with its negation ahead of the subject, which no fixed
# phrase can carry. BNY's Series O preferred (0001193125-26-424725) says only
# "Neither the depositary shares nor shares of the Series O Preferred Stock
# will be listed on any securities exchange or automated quotation system" —
# and pinged #sec-urgent on 2026-10-19.
UNLISTED_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bneither\b[^.;]{0,160}?\bnor\b[^.;]{0,160}?"
               r"\bwill\s+be\s+listed\s+on\s+any\b", re.I),
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
# Denomination — the single clearest tell that an offering is not in the
# reader's universe, and the one signal the phrase lists missed.
#
# Goldman's fixed-rate MTNs and Prudential's InterNotes are plain vanilla
# senior notes: no structured-product vocabulary to match, and Prudential's
# term sheet makes no listing statement at all. What they do state, on the
# cover, is the denomination — $1,000, sometimes with no exchange listing
# anywhere in the document. Three of them reached #sec-urgent on 2026-08-31.
#
# Oct 2026: four more kinds pinged #sec-urgent in one week, every one of them
# stating its denomination in a shape the first version could not read.
#
#   · NRUC's MTN term sheets (0000070502-26-000738 / 740 / 743) write
#     "Denominations&#58; $2,000 x $1,000". The colon was an HTML entity —
#     the poller decoded four named entities and nothing else — and the old
#     pattern allowed no digits between the word and the amount.
#   · Citi's euro notes (0001193125-26-427834, FWPs -426008 / -426010):
#     "in denominations of €100,000 and integral multiples of €1,000", the
#     euro sign arriving as "&#128;". The old pattern knew only "$", and its
#     separator excluded letters, so it could not read "denominations OF"
#     at all.
#   · Walmart's Maple bonds (0001193125-26-425645) state no denomination;
#     what they state is "Aggregate Principal Amount: C$750,000,000".
#   · BNY's Series O and BofA's Series WW are bank preferreds sold at "$1,000
#     per depositary share" — unlisted, institutional — and the bare words
#     "depositary share" were a retail veto, so BNY's term sheet sailed
#     through. A retail veto now has to name an amount.
#
# Still anchored on the word "denomination(s)" (or "per depositary share",
# or "principal amount" for a foreign currency) so the many incidental
# $1,000s in a real prospectus can't match: a CEF expense table ("expenses
# you would pay on a $1,000 investment"), an asset-coverage-per-$1,000 ratio,
# or a liquidation preference of $1,000 per share that is $25 per depositary
# share.
#
# ⚠ Prose "denominations of $1,000" deliberately does NOT count. It is the
# indenture default every base prospectus restates — "the Debt Securities of
# each series will be issued only ... in denominations of $1,000 and integral
# multiples thereof (Section 302)" (BNY), "in denominations of $1,000 and
# whole multiples of $1,000" (Citi), often with no hedge to recognise it by —
# and a base prospectus rides along behind every $25-par takedown. What an
# actual institutional issue states is a $2,000 minimum, a term-sheet field,
# a foreign currency, or $1,000 per depositary share.
# ---------------------------------------------------------------------------

# Foreign alternatives come first, so "C$750,000,000" is never read as "$".
_FOREIGN_CCY = (r"€|£|¥|C\$|CA\$|A\$|AU\$|NZ\$|HK\$|S\$"
                r"|\bCHF|\bEUR|\bGBP|\bJPY|\bCAD|\bAUD|\bNZD|\bSEK|\bNOK|\bDKK")
_USD_CCY = r"US\$|U\.S\.\s?\$|\$"
_AMOUNT = r"(?P<amt>\d[\d,]*(?:\.\d+)?)"

# "Denominations: $2,000 x $1,000", "Minimum Denomination/Increments:
# $1,000.00/$1,000.00", "Minimum Denominations/Multiples: €100,000",
# "in denominations of €100,000".
DENOMINATION_RE = re.compile(
    r"(?:minimum\s+)?denominations?"
    r"(?:\s*/\s*(?:increments?|multiples?))?"
    r"\s*(?P<field>:)?\s*(?P<of>of\s+)?"
    rf"(?P<ccy>{_FOREIGN_CCY}|{_USD_CCY})\s?{_AMOUNT}",
    re.I,
)

# "Liquidation Preference: $1,000 per depositary share", "Public Offering
# Price: $1,000 per depositary share", "(equivalent to $1,000 per depositary
# share)".
DEPOSITARY_AMOUNT_RE = re.compile(
    rf"(?P<ccy>{_FOREIGN_CCY}|{_USD_CCY})\s?{_AMOUNT}"
    r"\s+per\s+(?:depositary|depository)\s+share",
    re.I,
)

# "Aggregate Principal Amount: C$750,000,000", "aggregate principal amount
# of €1,250,000,000". Foreign currencies only: a USD principal amount says
# nothing about who the paper is sold to.
FOREIGN_PRINCIPAL_RE = re.compile(
    rf"principal\s+amount\s*(?::|of)?\s*(?P<ccy>{_FOREIGN_CCY})\s?{_AMOUNT}",
    re.I,
)

# A denomination sentence that is about a CLASS of security on the shelf,
# not the one being offered. Same idea as SHELF_HEDGES, but the boilerplate
# is worded differently, and it hedges after the amount as often as before.
DENOMINATION_HEDGES: list[str] = [
    "if other than",
    "unless",
    "otherwise specified",
    "specified otherwise",
    "specify otherwise",
    "otherwise indicated",
    "otherwise provided",
    "applicable prospectus",
    "applicable supplement",
    "applicable pricing supplement",
    "each series",
]
DENOMINATION_HEDGE_WINDOW = 400   # cut at the sentence boundary either side

# Positive evidence of a retail-denominated income security. Each one names
# a retail AMOUNT for the thing being offered, because a bare noun proved
# worthless: "depositary share" describes BNY's $1,000 Series O exactly as
# well as BofA's $25 Series SS.
#
# The amount may not run on into more digits, so "$25,000 per share" — the
# liquidation preference behind every 1/1,000th depositary share, and the
# whole of BofA's Series WW 8-K — is not "$25".
_RETAIL_AMT = r"\$\s?(?:10|20|25|50)(?:\.00)?(?![\d,]|\.\d)"
RETAIL_AMOUNT_RE = re.compile(
    rf"liquidation\s+(?:preference|amount|value)\s*(?::|of|equal\s+to)?\s*{_RETAIL_AMT}"
    rf"|{_RETAIL_AMT}\s+(?:liquidation\s+(?:preference|amount)|stated\s+amount)"
    rf"|\$\s?25(?:\.00)?(?![\d,]|\.\d)\s+(?:per\s+(?:depositary\s+|depository\s+)?share"
    r"|per\s+note|principal\s+amount)"
    rf"|denominations?\s*:?\s*(?:of\s+)?{_RETAIL_AMT}"
    rf"|{_RETAIL_AMT}\s+(?:and|or)\s+(?:any\s+)?integral\s+multiples?"
    r"|\bbaby\s+bonds?\b",
    re.I,
)

# The fraction on the title of a retail depositary share, which is often the
# only place the retail size shows: Goldman's Series D (0000950123-06-006614)
# says "1/1,000 th Interest" and "$25,000 liquidation preference per share"
# and never states $25. 1/1,000th of $25,000, 1/40th of $1,000, 1/400th of
# $10,000, 1/4,000th of $100,000 and 1/1,200th of $30,000 are all $25; the
# institutional fractions (1/25th of $25,000, 1/100th of $100,000) are not
# listed. EDGAR often sets the suffix apart: "1/100 th".
RETAIL_FRACTION_RE = re.compile(
    r"\b1\s?/\s?(?:1,?000|4,?000|1,?200|400|40)\s?th\b", re.I,
)


# ---------------------------------------------------------------------------
# Document-maintenance supplements — Sep 2026: six JPMorgan 424B3s reached
# #sec-urgent inside four minutes on the 10th, each one an LLM call, a ping
# and three paragraphs of the model explaining why it had nothing to say.
#
# They are not offerings. A 424B3 is the form a bank uses for a supplement
# that prices nothing, and two kinds of it arrive in bulk:
#
#   · INDEX SUPPLEMENT. A monthly republication of an index's backtested and
#     actual return table — "MerQube US Gold Vol Advantage Index", "J.P.
#     Morgan Multi-Asset Index". The subject is an INDEX, not a security. No
#     issuance, no coupon, no listing, no par; the body is a grid of monthly
#     percentages back to 1999.
#
#   · PROSPECTUS ADDENDUM. A notice that some earlier document's references
#     have been re-pointed at a refreshed shelf — "all references in the
#     prospectus supplement to the prospectus dated November 16, 2020 should
#     refer instead to the accompanying prospectus dated April 8, 2026". It
#     exists so dealers can keep using old pricing supplements in
#     market-making. It creates, retires and reprices nothing.
#
# Neither can be caught by the checks above, and that is not an oversight in
# them: every signal in this module describes the SECURITY BEING OFFERED, and
# these documents offer no security. All six went through untouched.
#
# The volume is the reason this is worth its own rule. JPM alone has 23,744
# 424B filings in EDGAR's recent window, MS 16,581, BAC 10,547, RY 3,577. In
# a 67-filing sample across those four issuers the existing filters caught
# every 424B2 pricing supplement — 40 of 40 — and none of the 424B3s: 20
# leaked, 18 of them these two classes.
#
# ⚠ Matched on the COVER, and on the document's own title for itself, not on
# a passing mention. A pricing supplement's cover stack routinely lists "the
# index supplement dated April 17, 2026" among its accompanying documents;
# what it never does is call ITSELF one. Measured first-occurrence offsets in
# the sample: index supplement 42 in all eight JPM filings, prospectus
# addendum 113-129 (MS) and 558-613 (BAC). Across 75 documents — 40 real bank
# pricing supplements, 18 of these, RIV's and NMCO's rights offerings, SAR's
# baby bond, and Gladstone / Hercules / Oxford Square note and ATM
# supplements — these two patterns matched all 18 and nothing else.
#
# Oct 2026: Goldman's versions read "MOBU Focus ER Index Supplement No. 16
# to the Prospectus" (0001193125-26-427988, a 424B2) and "BlackRock® Dynamic
# Factor Index Supplement Addendum to the BlackRock® Dynamic Factor Index
# Supplement, the Prospectus Supplement and the Prospectus" (-427963, a
# 424B3). Same document, numbered or amended; both pinged #sec-urgent. A
# pricing supplement that merely cites one says "the index supplement
# dated ...", which still does not match.
# ---------------------------------------------------------------------------
INDEX_SUPPLEMENT_RE = re.compile(
    r"index\s+supplement(?:\s+addendum)?(?:\s+no\.?\s*\d+)?\s+to\s+the\b", re.I,
)
PROSPECTUS_ADDENDUM_RE = re.compile(
    r"(?:this\s+)?prospectus\s+addendum\s+(?:to\s+the|supplements)", re.I,
)

# Same 4,000-character cover window as RIGHTS_COVER_CHARS, for the same
# reason: a document says what it is on its first page.
MAINTENANCE_COVER_CHARS = 4_000

# A maintenance document has no event in it by construction, so any of these
# appearing means the classification is wrong and the filing must survive.
# Cheap insurance against the two losses this module has already taken.
MAINTENANCE_VETO: list[str] = [
    "notice of redemption",
    "rights offering",
    "subscription rights",
    "will be redeemed",
    "has been called for redemption",
]


OWNERSHIP_FORMS: set[str] = {"3", "4", "5", "3/A", "4/A", "5/A"}

# ---------------------------------------------------------------------------
# Proposed-sale notices — Sep 2026: 144s started arriving in #sec-filings in
# bulk (AEP, PG&E, Equitable, Bridgewater Bancshares in a single afternoon).
#
# Nothing changed at EDGAR. The role fix in da7a5a6 is what opened the tap:
# the feed emits a 144 as two entries, "(Reporting)" for the insider and
# "(Subject)" for the issuer, and the watchlist CIK is on the SECOND one.
# Until that commit the first role missed the watchlist and took the whole
# accession down with it, so these were being dropped by accident. They are
# now dropped on purpose.
#
# A Form 144 is a NOTICE OF INTENT, filed by an affiliate when a proposed sale
# of restricted or control stock will exceed 5,000 shares or $50,000 in any
# three months. It is not a transaction. The sale may be partial, may be
# priced anywhere, may never happen — and when it does happen, a Section 16
# insider (which is nearly every 144 filer) reports it on a Form 4 within two
# business days. So a 144 is a weaker, earlier draft of a filing this module
# already gates behind ACTIVIST_FILERS. Keeping the 144 while skipping the
# Form 4 is incoherent.
#
# It is also always the wrong asset class. A 144 covers common stock sold
# under Rule 144; the mandate in openrouter_dispatch.py is exchange-traded
# income securities. The posts that prompted this said so themselves, in the
# body, unprompted: "No preferred stock, baby bonds, or exchange-traded debt
# are referenced in this filing."
#
# Gated rather than dropped outright, unlike N-PX. An activist that is an
# affiliate must file the 144 BEFORE it sells, which makes it the earliest
# obtainable warning of an unwind — ahead of the Form 4 that §3 exists to
# catch. is_activist_filer() reads entity_name + filing_text, and the filer's
# name is in the body of the 144 even when the matched role is the issuer.
PROPOSED_SALE_FORMS: set[str] = {"144", "144/A"}

OFFERING_FORMS: set[str] = {
    "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8", "FWP",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def plain_text(text: str) -> str:
    """Filing text with HTML entities decoded and whitespace collapsed.

    EDGAR documents are HTML, and until Oct 2026 the poller decoded only
    &nbsp; &amp; &lt; &gt;. Everything else arrived verbatim — "&#58;" for a
    colon, "&#128;" for the euro sign, "&#8201;" between "of" and "$25.00" —
    and every phrase in this module silently failed to match across one.
    edgar_poller.strip_html() decodes them now; this repeats it so a payload
    from anywhere else reads the same. html.unescape maps "&#128;" to "€",
    as a browser does.
    """
    return re.sub(r"\s+", " ", html.unescape(text or ""))


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
    for pattern in UNLISTED_PATTERNS:
        for m in pattern.finditer(lower):
            lead = lower[max(0, m.start() - HEDGE_WINDOW):m.start()]
            if not any(h in lead for h in SHELF_HEDGES):
                return m.group(0)
    return None


def _denomination_hedged(text: str, start: int, end: int) -> bool:
    """True if the sentence around a stated amount is shelf boilerplate.

    Reads the sentence on both sides, because base prospectuses hedge after
    the amount as often as before it ("...in minimum denominations of $2,000
    ..., unless otherwise specified in the applicable prospectus
    supplement"). A term sheet has no sentences, so its window runs into the
    neighbouring fields — which can only hide a statement, never invent one.

    Wider than HEDGE_WINDOW because these sentences run long: NRUC's shelf
    (0001144204-19-022409, the 5.500% notes due 2064 — $25 paper) reads
    "Unless specified otherwise in the prospectus supplement or pricing
    supplement, all senior debt securities will be denominated in U.S.
    dollars, registered senior debt securities will be issued in
    denominations of $1,000 ... and bearer senior debt securities will be
    issued in denominations of $5,000", 250 characters from hedge to amount.
    """
    lead = text[max(0, start - DENOMINATION_HEDGE_WINDOW):start]
    cut = lead.rfind(". ")
    if cut != -1:
        lead = lead[cut + 2:]
    tail = text[end:end + DENOMINATION_HEDGE_WINDOW]
    stop = tail.find(". ")
    if stop != -1:
        tail = tail[:stop]
    window = f"{lead} {tail}".lower()
    return any(h in window for h in DENOMINATION_HEDGES)


def _amount(digits: str) -> float:
    try:
        return float(digits.replace(",", ""))
    except ValueError:
        return 0.0


def _is_foreign(ccy: str) -> bool:
    return not re.fullmatch(_USD_CCY, ccy.strip(), re.I)


def offered_denomination(text: str) -> tuple[str, str]:
    """('foreign' | 'institutional' | '', evidence) for the security offered.

    'foreign'       a denomination, principal amount or per-share amount in a
                    currency other than USD
    'institutional' a USD denomination of $2,000 and up, a $1,000 term-sheet
                    field, or $1,000-and-up per depositary share
    ''              nothing stated that rules the offering out

    Statements inside a hedged shelf sentence are skipped — see
    _denomination_hedged(). Expects plain_text().
    """
    for m in DENOMINATION_RE.finditer(text):
        amount = _amount(m.group("amt"))
        if _is_foreign(m.group("ccy")):
            kind = "foreign"
        elif amount >= 2_000 or (amount >= 1_000 and not m.group("of")):
            kind = "institutional"
        else:
            continue
        if not _denomination_hedged(text, m.start(), m.end()):
            return kind, m.group(0)

    for m in DEPOSITARY_AMOUNT_RE.finditer(text):
        foreign = _is_foreign(m.group("ccy"))
        if not foreign and _amount(m.group("amt")) < 1_000:
            continue
        if not _denomination_hedged(text, m.start(), m.end()):
            return ("foreign" if foreign else "institutional"), m.group(0)

    for m in FOREIGN_PRINCIPAL_RE.finditer(text):
        if not _denomination_hedged(text, m.start(), m.end()):
            return "foreign", m.group(0)

    return "", ""


def retail_amount(text: str) -> str | None:
    """A stated retail amount — "$25 per depositary share", "denominations
    of $25", "liquidation preference of $25.00" — or None. Expects
    plain_text()."""
    m = RETAIL_AMOUNT_RE.search(text)
    return m.group(0) if m else None


def retail_signal(text: str) -> str | None:
    """The first sign that the offering is retail-denominated, or None.

    A stated retail amount always counts. A retail fraction counts unless
    the document states a $1,000-and-up amount per depositary share, which
    is the offered security's own size and outranks a fraction that may
    belong to some other series named in passing. Expects plain_text().
    """
    amount = retail_amount(text)
    if amount:
        return amount
    for d in DEPOSITARY_AMOUNT_RE.finditer(text):
        if _is_foreign(d.group("ccy")) or _amount(d.group("amt")) >= 1_000:
            return None
    m = RETAIL_FRACTION_RE.search(text)
    return m.group(0) if m else None


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


def is_document_maintenance(filing: dict) -> tuple[bool, str]:
    """Returns (skip, reason) for a supplement that discloses no event.

    An index supplement republishes an index's return table; a prospectus
    addendum re-points an older document's references at a refreshed shelf.
    Both are filed on offering forms and neither offers, prices, retires or
    reprices anything.

    The test is what the document calls ITSELF, on its cover. That matters:
    a real pricing supplement lists the index supplement among its
    accompanying documents, so a bare mention of the phrase is worthless as a
    signal and only its self-description will do.

    Any redemption or rights language vetoes the whole classification. A
    maintenance document cannot contain either, so their presence means this
    read is wrong — and those are precisely the two events whose loss this
    module has paid for before.
    """
    text  = filing.get("filing_text", "") or ""
    cover = text[:MAINTENANCE_COVER_CHARS]

    if INDEX_SUPPLEMENT_RE.search(cover):
        kind = "index supplement — an index return table, no security offered"
    elif PROSPECTUS_ADDENDUM_RE.search(cover):
        kind = ("prospectus addendum — re-points an earlier document at a "
                "refreshed shelf, nothing offered or repriced")
    else:
        return False, ""

    lower = text.lower()
    veto = next((v for v in MAINTENANCE_VETO if v in lower), None)
    if veto:
        return False, ""

    return True, kind


def is_institutional_denomination(filing: dict) -> tuple[bool, str]:
    """Returns (skip, reason) for paper denominated outside his universe.

    Reads the denomination the filing states for the security being offered
    — see offered_denomination(). Any retail signal vetoes it, so a preferred
    whose liquidation preference is quoted as "$1,000 per share (equivalent
    to $25 per depositary share)" survives, and so does a $25 baby bond whose
    base prospectus restates some other shelf class at $2,000.
    """
    text = plain_text(filing.get("filing_text", ""))
    kind, stated = offered_denomination(text)
    if not kind or retail_signal(text):
        return False, ""
    if kind == "foreign":
        return True, f"non-USD offering ({stated.strip()!r})"
    return True, f"institutional denomination ({stated.strip()!r})"


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
      2. A stated denomination outside his universe — $2,000-and-up, a $1,000
         term-sheet field, $1,000 per depositary share, or any non-USD amount
         — with no retail signal anywhere, drops the filing. This is what
         catches plain vanilla bank MTNs, euro notes and bank preferreds — no
         payoff vocabulary to match, sometimes no listing statement at all.
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
    filing = {**filing, "filing_text": plain_text(filing.get("filing_text", ""))}

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

    # 2) Form 144 — a notice that an affiliate intends to sell common stock.
    #    Same activist gate as Form 4, because it is the same event seen
    #    earlier and less reliably: the sale reaches Form 4 within two
    #    business days if it happens at all.
    if form_type in PROPOSED_SALE_FORMS:
        if not is_activist_filer(filing):
            return True, (f"Form {form_type} proposed common-stock sale, not "
                          f"from a tracked activist — the executed sale "
                          f"arrives on a Form 4")
        return False, ""

    # 3) 424B / FWP — skip unlisted structured products and unlisted senior notes
    if form_type in OFFERING_FORMS:
        # A supplement that discloses no event is dropped before anything
        # tries to read an offering out of it. Its own veto list covers the
        # rights and redemption language that would mean this read is wrong.
        maint, why = is_document_maintenance(filing)
        if maint:
            return True, f"{form_type} {why}"

        skip, why = is_unlisted_offering(filing)
        if skip:
            return True, f"{form_type} {why}"
        return False, ""

    return False, ""
