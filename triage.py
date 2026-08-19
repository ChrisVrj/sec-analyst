"""
triage.py — decide urgency from the filing, before the model sees it.

Everything in `classify_priority` reads the model's summary: the "## " block
it chose to write, the emoji it put on line 1. That works right up until the
model describes the event correctly and words it differently, and then the
filing routes to the main channel with a perfect summary nobody reads in time.

Both misses in August 2026 were that failure:

  · CLM N-2, 2026-08-14 23:45. A 1-for-3 rights offering at 104% of NAV on a
    fund trading at a 14.7% premium. The summary was accurate and complete.
    It carried no highlight block, and 📋 is not an urgent emoji, so tier 0.
    The deeper cause: no rule in the ladder covers a rights offering at all.
    Over the next three sessions CLM fell 6.6% and CRF 6.8%.

  · SAR 424B2, 2026-08-18. A $25-par baby bond listing as SAX, with proceeds
    earmarked to redeem the 6.00% and 8.00% 2027 Notes. The LISTING block did
    fire tier 2 — and `_is_tradeable_new_issue` demoted it for "no preferred /
    depositary / baby-bond / $25-par signal", because the model wrote "$25
    par" in prose and the gate only recognised a `**Par:**` field.

So this module classifies from `form_type` and `filing_text` — the EDGAR
payload, which is the same every time regardless of how the model phrases
things. It returns a tier on the same scale as URGENT_RULES, and the
dispatcher takes the more urgent of the two. It can only ever promote: if
the model spots something this misses, the model still wins.

Two design rules carried over from the misses:

  · Nothing computed gates the alert. Form type alone is enough to reach a
    tier. Coupons, ratios and prices are extracted where they parse and
    omitted where they do not, and a parse failure is logged, not raised.

  · Base forms outrank their own amendments. N-2 is the announcement, N-2/A
    is housekeeping filed weeks later. Across CLM and CRF's last three
    offerings the three sessions after the base N-2 were negative in 6 of 6
    cases (median -4.4%, -6.7pp of premium); after the N-2/A, positive in 4
    of 6. An amendment therefore does not page on form type alone — but it
    still pages if it carries a redemption, since that is a fact about a
    security rather than a stage in a process.

There is no issuer-universe check here. The poller only fetches filings whose
CIK is in cik_map.json, so everything reaching this module is already a name
being watched.
"""

import logging
import re

log = logging.getLogger("DISPATCH")

# Tiers match URGENT_RULES in openrouter_dispatch.py. Lower is more urgent;
# 0 means nothing structural was found.
TIER_REDEMPTION = 1
TIER_NEW_ISSUE = 2
TIER_NONE = 0

# --- Forms that register or price new securities ---------------------------
# Only a registered closed-end fund files an N-2 and only a BDC files an
# N-54A, so those are self-identifying. The rest are filed by every operating
# company in the market — harmless here, because the poller has already
# matched the CIK against the watchlist.

CAPITAL_RAISE_FORMS = frozenset({
    "N-2", "N-2ASR", "N-2MEF", "N-54A", "N-54C",
    "S-1", "S-3", "S-3ASR", "S-11", "POS AM", "POSASR",
    "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8",
})

# Shelf housekeeping and Rule 482 advertising. Mostly annual updates and
# marketing, so these reach a tier only when the text names an offering.
SOFT_FORMS = frozenset({"486APOS", "486BPOS", "497", "497K", "497AD", "FWP"})

# --- What the text has to say ----------------------------------------------

_RIGHTS_OFFERING_RE = re.compile(
    r"\brights?\s+offering\b"
    r"|non-?transferable\s+rights?\b"
    r"|subscription\s+price[^.]{0,160}?%\s*of\s*(?:the\s*)?(?:NAV|net\s+asset\s+value)"
    r"|for\s+every\s+\w+\s*\(\d+\)\s*rights",
    re.IGNORECASE,
)

_OFFERING_RE = re.compile(
    r"\b(?:public|registered|underwritten)\s+offering\b"
    r"|\bat-the-market\b"
    r"|\bwe\s+are\s+offering\b"
    r"|\baggregate\s+principal\s+amount\b",
    re.IGNORECASE,
)

# Redemption of something already outstanding. The prose form matters: this
# has to fire on a use-of-proceeds sentence, which is where SAR named SAT and
# SAJ, not only on a formal notice of redemption.
_REDEMPTION_RE = re.compile(
    r"\b(?:redeem|repurchase|retire)\b[^.]{0,80}?"
    r"\b(?:the\s+)?(?:outstanding|existing|all|any)\b"
    r"|\bnotice\s+of\s+redemption\b"
    r"|\bproceeds[^.]{0,120}?\bto\s+redeem\b"
    r"|\bredemption\s+of\s+the\s+(?:outstanding|existing)\b",
    re.IGNORECASE,
)

# "the 6.00% 2027 Notes", "our 8.125% Notes due 2027". Coupon plus year is
# what makes the reference resolvable to a ticker.
_TARGET_RE = re.compile(
    r"(\d{1,2}(?:\.\d{1,3})?)\s*%\s+"
    r"(?:(20\d{2})\s+)?"
    r"(Notes|Debentures|Bonds|Preferred(?:\s+Stock)?)"
    r"(?:\s+due\s+(20\d{2}))?",
    re.IGNORECASE,
)

# A commitment, as opposed to a list of candidates. Saratoga's supplement
# names the 6.00% Notes, the 8.00% Notes and a credit facility joined by
# "and/or", against a deal whose size was still blank — so at most some of
# them get taken out. A notice of redemption has no such discretion in it.
_COMMITTED_RE = re.compile(
    r"\bnotice\s+of\s+redemption\b|\bhas\s+(?:called|redeemed)\b|\birrevocabl",
    re.IGNORECASE,
)
_CONDITIONAL_RE = re.compile(
    r"\band/or\b|\bmay\s+(?:redeem|elect)\b|\bexpects?\s+to\s+use\b"
    r"|\bintends?\s+to\s+use\b|\ba\s+portion\s+of\b",
    re.IGNORECASE,
)


def split_form(form_type):
    """'N-2/A' -> ('N-2', True)."""
    form = (form_type or "").strip().upper()
    if form.endswith("/A"):
        return form[:-2].strip(), True
    return form, False


def _sentence_around(text, start, end, max_span=600):
    """The sentence containing a match, for attributing tense and commitment.

    A fixed character window straddles section boundaries and reads the
    neighbouring paragraph's wording onto the match. Over-splitting on "Inc."
    only narrows the window, which is the safe direction.
    """
    before = text[max(0, start - max_span):start]
    after = text[end:min(len(text), end + max_span)]
    cut = max(before.rfind(". "), before.rfind(".\n"), before.rfind("; "))
    if cut != -1:
        before = before[cut + 1:]
    stop = re.search(r"[.;](?:\s|$)", after)
    if stop:
        after = after[:stop.start()]
    return before + text[start:end] + after


def find_redemption_targets(filing_text):
    """Securities the filing says it intends to retire.

    Returns a list of dicts: coupon, year, label, committed. Resolving these
    to tickers needs an instrument table the poller does not carry, so the
    coupon and year are reported as written and the reader does the last hop.
    """
    if not filing_text:
        return []

    targets, seen = [], set()
    for match in _REDEMPTION_RE.finditer(filing_text):
        sentence = _sentence_around(filing_text, match.start(), match.end())
        committed = bool(_COMMITTED_RE.search(sentence)
                         and not _CONDITIONAL_RE.search(sentence))
        for m in _TARGET_RE.finditer(sentence):
            try:
                coupon = float(m.group(1))
            except (TypeError, ValueError):
                continue
            year = m.group(2) or m.group(4)
            key = (coupon, year)
            if key in seen:
                continue
            seen.add(key)
            targets.append({
                "coupon": coupon,
                "year": int(year) if year else None,
                "label": m.group(3).strip(),
                "committed": committed,
            })
    return targets


def describe_targets(targets):
    """One short line naming what is being retired, for the Discord post."""
    if not targets:
        return ""
    parts = []
    for t in targets:
        year = f" {t['year']}" if t["year"] else ""
        parts.append(f"{t['coupon']:g}%{year} {t['label']}")
    verb = "CALLED" if all(t["committed"] for t in targets) else "NAMED"
    line = f"REDEMPTION {verb}: " + ", ".join(parts)
    if verb == "NAMED":
        line += "  (use of proceeds — some or all may not be redeemed)"
    return line


def triage_filing(form_type, filing_text="", entity_name=""):
    """Classify from the filing itself. Returns (tier, label, notes).

    `notes` is a list of short strings explaining the tier, for the log.
    """
    base_form, is_amendment = split_form(form_type)
    text = filing_text or ""
    notes = []

    # A redemption is a fact about a security that already trades, so it
    # outranks everything and is not suppressed by amendment status.
    targets = find_redemption_targets(text)
    if targets:
        notes.append(describe_targets(targets))
        return TIER_REDEMPTION, "redemption (filing text)", notes

    is_rights = bool(_RIGHTS_OFFERING_RE.search(text))
    if is_rights:
        notes.append("rights offering language in the filing")

    if base_form in CAPITAL_RAISE_FORMS:
        if is_amendment and not is_rights:
            # The announcement already went out weeks ago; this sets a date.
            notes.append(f"{base_form}/A amendment — announcement already made")
            return TIER_NONE, "", notes
        notes.append(f"{base_form} registers or prices new securities")
        return TIER_NEW_ISSUE, "new issuance (form type)", notes

    if base_form in SOFT_FORMS and (is_rights or _OFFERING_RE.search(text)):
        notes.append(f"{base_form} naming an offering")
        return TIER_NEW_ISSUE, "new issuance (soft form, offering named)", notes

    if is_rights:
        # A rights offering can be disclosed on a form nobody listed.
        return TIER_NEW_ISSUE, "rights offering (filing text)", notes

    return TIER_NONE, "", notes
