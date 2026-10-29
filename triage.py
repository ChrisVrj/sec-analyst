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

    One exception since Oct 2026: a prospectus supplement or term sheet
    (424B*, FWP) prices a single security, and pages only if that security
    shows a retail size — see SUPPLEMENT_FORMS. Scored on form type alone,
    every bank's euro notes and MTNs paged. Registration statements, rights
    offerings, redemptions and tenders are untouched; a merger prospectus
    pages only when the deal disposes of a preferred or note.

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

from prefilter import (
    income_security_treatment,
    is_document_maintenance,
    plain_text,
    retail_signal,
    unlisted_statement,
)

log = logging.getLogger("DISPATCH")

# Tiers match URGENT_RULES in openrouter_dispatch.py. Lower is more urgent;
# 0 means nothing structural was found.
TIER_REDEMPTION = 1
TIER_NEW_ISSUE = 2
TIER_MERGER = 3
TIER_TENDER = 4
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

# A post-effective amendment is also how a shelf is taken DOWN. After a merger
# or reorganisation the registrant files one per old shelf to "remove from
# registration" whatever went unsold — nothing is offered, and outstanding
# securities are untouched. Navient LLC's six POSASRs on 2026-10-29
# (0001193125-26-436981 and siblings) each paged as a new issue after its
# holding-company reorganisation.
#
# Matched on the deregistration heading or the operative "hereby terminates /
# removes" — never on "remove from registration" alone, because that phrase
# is the standard Item 512 undertaking in Part II of EVERY registration
# statement, live shelves included.
POST_EFFECTIVE_FORMS = frozenset({"POS AM", "POSASR"})
_DEREGISTRATION_RE = re.compile(
    r"\bderegistration\s+of\s+(?:unsold\s+)?securities\b"
    r"|\bhereby\s+(?:terminates?|removes?|deregisters?)\b",
    re.IGNORECASE,
)

# Shelf housekeeping and Rule 482 advertising. Mostly annual updates and
# marketing, so these reach a tier only when the text names an offering.
SOFT_FORMS = frozenset({"486APOS", "486BPOS", "497", "497K", "497AD", "FWP"})

# --- A takedown pages only when it is a security he trades ----------------
# The registration forms above page on form type alone: a new shelf is wanted
# loud (see test_pipeline.py, "shelf registrations — louder on purpose").
# A prospectus supplement is different. It prices ONE security, and the
# question is whether that security is in his universe — which the form type
# cannot answer, because every bank, utility and REIT files 424Bs.
#
# Scored on form type alone they produced 18 of the 71 pings in the week to
# 2026-10-23: Citi's euro notes, NRUC's and Walmart's MTNs, Goldman's index
# supplements, BNY's $1,000 depositary shares, a common-stock ATM, Wheeler's
# proxy supplements — and on the 26th Strive's $100-stated-amount SATS
# preferred ATM. The genuine retail takedowns that week (OXLC's term
# preferred, PINE's preferred ATM) state their $25 size on the first page.
#
# So a 424B or FWP now pages only on a retail signal — a $25 amount, a $25
# denomination, a 1/1,000th depositary fraction (prefilter.retail_signal) —
# read near the front, where the security being offered is described. This
# is NOT the summary-side gate that silenced CLM: it reads the filing, not the
# model's words, and a rights offering is exempt before it is ever consulted.
#
# How near the front was measured on 333 EDGAR full-text hits for $25 paper:
# most state it in the first 4,000 characters, but a preferred ATM's cover
# lists the series and their tickers and leaves the $25 to the description
# (NYMT 28,345, MFA 25,813), and a BDC baby bond can spend the cover on what
# a BDC is (CION 11,099). No institutional or non-traded supplement in the
# sample showed a retail signal before 60,000 (Phoenix Energy One's
# non-traded notes) — those are base-prospectus mentions of OTHER series. A
# common-stock cover gets only its first page, since the description of some
# existing preferred can sit much nearer the front (CareCloud's common ATM
# 27,748; Alpine's 133,828).
SUPPLEMENT_FORMS = frozenset({
    "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8", "FWP",
})
RETAIL_EVIDENCE_CHARS = 30_000
COMMON_COVER_CHARS = 4_000
_OFFERED_SECURITY_RE = re.compile(
    r"common\s+(?:stock|shares)|preferred|depositary|depository"
    r"|\bnotes?\b|debentures|\bunits\b",
    re.IGNORECASE,
)

# A merger is registered on the same forms. The acquirer files its proxy
# statement/prospectus as a 424B3, and what it registers is the acquirer's
# common stock, so the takedown rule above would read it as nothing he
# trades. Until Oct 2026 these paged as "new issuance (form type)"; they now
# page as what they are — but only, like every M&A ping, when the deal does
# something to a preferred, depositary share or note
# (prefilter.income_security_treatment). UWM's for Two Harbors
# (0001140361-26-004996) converts TWO's three preferred series into new UWMC
# preferred. Matched on the cover's own heading.
_MERGER_PROSPECTUS_RE = re.compile(
    r"merger\s+proposed|proxy\s+statement\s*/\s*prospectus", re.IGNORECASE,
)

# --- Tender and exchange offers -------------------------------------------
# A Schedule TO is a standing bid for a security that already trades, at a
# stated price and with an expiry. There is nothing to interpret: the form
# type alone is the event, which is what makes it safe to page on.
#
# Added 2026-09-01 after NFJ's SC TO-I (0001193125-26-377910, a Virtus CEF
# self-tender) went unposted. Nothing here would have rescued that particular
# miss — it never reached the model — but the routing gap it exposed is real:
# tender offers are tier 4 in URGENT_RULES and this module scored them 0, so
# they paged only when the model happened to write a "## 🔁 TENDER" block.
# That is the same dependence on the model's wording that lost CLM.
#
# Amendments page too, unlike the capital-raise forms above. An amended
# Schedule TO is usually a price increase or an extension of the expiry —
# both are terms of a live offer, not an announcement already made.
TENDER_FORMS = frozenset({
    "SC TO-I", "SC TO-T", "SC TO-C",
    "SC 13E3", "SC 13E1",
    "SC 14D9",
})

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


def offered_retail_security(text):
    """The retail signal for the security a supplement offers, or None.

    Reads the first RETAIL_EVIDENCE_CHARS — or only the first
    COMMON_COVER_CHARS when the first security the cover names is common
    stock. Expects plain_text().
    """
    first = _OFFERED_SECURITY_RE.search(text[:COMMON_COVER_CHARS])
    common = bool(first) and first.group(0).lower().startswith("common")
    return retail_signal(text[:COMMON_COVER_CHARS if common else RETAIL_EVIDENCE_CHARS])


def triage_filing(form_type, filing_text="", entity_name=""):
    """Classify from the filing itself. Returns (tier, label, notes).

    `notes` is a list of short strings explaining the tier, for the log.
    """
    base_form, is_amendment = split_form(form_type)
    text = plain_text(filing_text)
    notes = []

    # A redemption is a fact about a security that already trades, so it
    # outranks everything and is not suppressed by amendment status.
    targets = find_redemption_targets(text)
    if targets:
        notes.append(describe_targets(targets))
        return TIER_REDEMPTION, "redemption (filing text)", notes

    # A supplement that discloses no event cannot be urgent on any form.
    # prefilter drops these before the LLM call, but only on 424B / FWP —
    # this is the routing layer saying the same thing independently, so a
    # maintenance supplement filed on some other form cannot page either.
    #
    # Placed below the redemption check on purpose. is_document_maintenance()
    # vetoes itself on redemption language, but the module's precedence is
    # that a redemption outranks everything, and it should not depend on
    # another module's veto list staying as broad as this one's.
    maintenance, why = is_document_maintenance(
        {"filing_text": text, "entity_name": entity_name})
    if maintenance:
        notes.append(f"{base_form} {why}")
        return TIER_NONE, "", notes

    if base_form in TENDER_FORMS:
        notes.append(
            f"{base_form} is a standing bid for an outstanding security"
            + (" (amendment — terms of a live offer)" if is_amendment else "")
        )
        return TIER_TENDER, "tender / exchange offer (form type)", notes

    if (base_form in SUPPLEMENT_FORMS
            and _MERGER_PROSPECTUS_RE.search(text[:COMMON_COVER_CHARS])):
        treated = income_security_treatment(text)
        if not treated:
            notes.append(f"{base_form} merger prospectus disposes of no "
                         f"preferred or note — not urgent")
            return TIER_NONE, "", notes
        notes.append(f"{base_form} merger prospectus: {treated[:120]!r}")
        return TIER_MERGER, "merger proxy / prospectus (cover)", notes

    is_rights = bool(_RIGHTS_OFFERING_RE.search(text))
    if is_rights:
        notes.append("rights offering language in the filing")

    if base_form in CAPITAL_RAISE_FORMS:
        if is_amendment and not is_rights:
            # The announcement already went out weeks ago; this sets a date.
            notes.append(f"{base_form}/A amendment — announcement already made")
            return TIER_NONE, "", notes
        if base_form in POST_EFFECTIVE_FORMS:
            dereg = _DEREGISTRATION_RE.search(text)
            if dereg:
                notes.append(f"{base_form} deregisters unsold securities "
                             f"({dereg.group(0)!r}) — nothing offered")
                return TIER_NONE, "", notes
        # An offering that says outright it will never trade is not urgent,
        # whatever form it arrived on.
        #
        # Sep 2026: six JPMorgan 424B3s pinged #sec-urgent carrying bodies
        # that read "Listing: UNLISTED". Both halves of the router agreed
        # they were routine — classify_priority() demotes an unlisted tier 2
        # by design — and they paged anyway, because this function scores
        # every 424B* a tier 2 on form type and can only promote. The
        # summary-side gate has therefore been dead on 424B filings for as
        # long as both have existed.
        #
        # This is NOT the gate that silenced CLM, and the difference is the
        # whole reason it is safe. That one demanded POSITIVE evidence of a
        # retail income security, which a rights offering — common stock —
        # can never produce. This demands the issuer's own unhedged statement
        # that the thing will not be listed, the same signal prefilter treats
        # as decisive, and it does not run on a rights offering at all.
        # CLM's N-2 contains no such sentence and is caught by is_rights
        # first; SAR's 424B2 says it will list as SAX and never reaches here,
        # because a redemption already returned tier 1 above.
        if not is_rights:
            unlisted = unlisted_statement(text)
            if unlisted:
                notes.append(f"{base_form} states {unlisted!r} — nothing to "
                             f"trade, so not urgent on form type alone")
                return TIER_NONE, "", notes
            # A takedown has to be a security he trades — see SUPPLEMENT_FORMS.
            if base_form in SUPPLEMENT_FORMS:
                retail = offered_retail_security(text)
                if not retail:
                    notes.append(f"{base_form} offers nothing at retail "
                                 f"denomination — not urgent on form type alone")
                    return TIER_NONE, "", notes
                notes.append(f"{base_form} offers a retail-denominated "
                             f"security ({retail!r})")
        notes.append(f"{base_form} registers or prices new securities")
        return TIER_NEW_ISSUE, "new issuance (form type)", notes

    # A term sheet pages on what it prices. Until Oct 2026 an FWP needed only
    # to name an offering — and NRUC's MTN term sheets, Walmart's notes and
    # Citi's euro notes all name an "aggregate principal amount" — while the
    # $25 term sheets he wanted never did: M&T's Series J (0001193125-24-
    # 132472) says "Offering Price: $25 per Depositary Share", not "public
    # offering", and stayed silent.
    if base_form in SOFT_FORMS and base_form in SUPPLEMENT_FORMS and not is_rights:
        retail = offered_retail_security(text)
        if not retail:
            notes.append(f"{base_form} prices nothing at retail denomination")
            return TIER_NONE, "", notes
        unlisted = unlisted_statement(text)
        if unlisted:
            notes.append(f"{base_form} states {unlisted!r} — nothing to trade")
            return TIER_NONE, "", notes
        notes.append(f"{base_form} prices a retail-denominated security "
                     f"({retail!r})")
        return TIER_NEW_ISSUE, "new issuance (retail term sheet)", notes

    if base_form in SOFT_FORMS and (is_rights or _OFFERING_RE.search(text)):
        notes.append(f"{base_form} naming an offering")
        return TIER_NEW_ISSUE, "new issuance (soft form, offering named)", notes

    if is_rights:
        # A rights offering can be disclosed on a form nobody listed.
        return TIER_NEW_ISSUE, "rights offering (filing text)", notes

    return TIER_NONE, "", notes
