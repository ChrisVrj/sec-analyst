#!/usr/bin/env python3
"""
EDGAR poller for the OpenClaw sec-analyst pipeline.

Modes:
  --once     Poll EDGAR once, write matched filings to filings-inbox/, exit.
             Used by GitHub Actions inside a trading window.
  --catchup  Sweep back through the feed to a time horizon and queue anything
             on the watchlist that was never seen. Used at the start of every
             Actions run, in or out of a window, so a gap in GitHub's cron
             costs latency rather than the filing.
  (default)  Poll continuously (original local dev behavior).

Matched filings are written as JSON files to INBOX_DIR.
openrouter_dispatch.py reads them in the same GitHub Actions job.
"""

import argparse
import datetime
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

USER_AGENT = "OpenClaw SEC Monitor chrisdoesdocu@gmail.com"
POLL_INTERVAL = 60
ET_ZONE = ZoneInfo("America/New_York")

# Respect GITHUB_WORKSPACE when running in Actions; fall back to script dir.
BASE_DIR = Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).parent))

WATCHLIST_FILE = BASE_DIR / "cik_map.json"
SEEN_FILE      = BASE_DIR / "seen_accessions.json"
INBOX_DIR      = BASE_DIR / "filings-inbox"
LOG_FILE       = BASE_DIR / "edgar_poller.log"

MAX_SEEN = 10_000   # cap memory of seen accessions

# EDGAR's current-filings feed caps `count` at 100 ENTRIES per page — asking
# for 200 or 400 silently returns 100 — but `start` pages backwards without
# limit, at least to the previous session's open. Two facts about that page,
# both measured on 2026-09-01, drive the numbers below:
#
#   · 100 entries is NOT 100 filings. Ownership and beneficial-ownership forms
#     emit one entry per role (Reporting + Issuer, Filed by + Subject), so a
#     page holds ~50 unique accessions on a quiet evening.
#   · At the 17:20-17:30 ET deadline rush a page spans FIVE AND A HALF
#     MINUTES. Page 0 alone is not a safety margin; it is barely a buffer
#     against one slow dispatch cycle.
#
# So a live poll reads a few pages, and the catch-up sweep reads as many as it
# needs to reach its horizon. At 3 pages per 15s poll that is 0.2 req/s
# against SEC's 10 req/s fair-access limit.
FEED_PAGE_SIZE   = 100
FEED_PAGE_SLEEP  = 0.25   # between pages, well inside SEC fair access
LIVE_PAGES       = 3      # ~17 min of depth at peak, ~7 h on a quiet evening
CATCHUP_PAGES    = 40     # ceiling; the horizon normally stops it far sooner
CATCHUP_HOURS    = 8      # covers the longest observed cron gap plus slack
CATCHUP_MAX_QUEUE = 40    # cold-start guard, see poll_once()

# ---------------------------------------------------------------------------
# Federal holidays (EDGAR closed)
# ---------------------------------------------------------------------------

FEDERAL_HOLIDAYS = {
    datetime.date(2026, 1, 1),
    datetime.date(2026, 1, 19),
    datetime.date(2026, 2, 16),
    datetime.date(2026, 5, 25),
    datetime.date(2026, 6, 19),
    datetime.date(2026, 7, 3),
    datetime.date(2026, 9, 7),
    datetime.date(2026, 11, 11),
    datetime.date(2026, 11, 26),
    datetime.date(2026, 12, 25),
    datetime.date(2027, 1, 1),
    datetime.date(2027, 1, 18),
    datetime.date(2027, 2, 15),
    datetime.date(2027, 5, 31),
    datetime.date(2027, 6, 18),
    datetime.date(2027, 7, 5),
    datetime.date(2027, 9, 6),
    datetime.date(2027, 11, 11),
    datetime.date(2027, 11, 25),
    datetime.date(2027, 12, 24),
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EDGAR-POLLER] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        # Explicit UTF-8: entity names and filing text carry non-ASCII, and
        # FileHandler otherwise uses the locale codepage (cp1252 on Windows).
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

INBOX_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# EDGAR schedule
# ---------------------------------------------------------------------------

def edgar_is_open() -> bool:
    now_et = datetime.datetime.now(ET_ZONE)
    if now_et.weekday() >= 5:
        return False
    if now_et.date() in FEDERAL_HOLIDAYS:
        return False
    return 6 <= now_et.hour < 22


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception as e:
            log.warning(f"Could not load seen_accessions.json: {e}")
    return set()


def save_seen(seen: set[str]) -> None:
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen)))
    except Exception as e:
        log.warning(f"Could not save seen_accessions.json: {e}")


def load_watchlist() -> dict[str, str]:
    """
    Returns {cik_no_leading_zeros: TICKER}.
    Supports both formats:
      {"TICKER": "0001234567"}           (cik_map.json format)
      {"TICKER": {"cik": "0001234567"}}  (legacy dict format)
    """
    if not WATCHLIST_FILE.exists():
        log.warning("cik_map.json not found — matching ALL filings (probably not what you want)")
        return {}

    raw = json.loads(WATCHLIST_FILE.read_text())
    cik_to_ticker: dict[str, str] = {}

    for ticker, value in raw.items():
        if isinstance(value, dict):
            cik = str(value.get("cik") or value.get("CIK") or "").strip()
        else:
            cik = str(value).strip()

        cik = cik.lstrip("0")
        if cik:
            cik_to_ticker[cik] = ticker.upper()

    return cik_to_ticker


# ---------------------------------------------------------------------------
# EDGAR feed fetch
# ---------------------------------------------------------------------------

def _parse_feed_entry(entry, ns: dict) -> dict:
    link    = entry.find("a:link", ns)
    title   = entry.find("a:title", ns)
    updated = entry.find("a:updated", ns)

    href       = link.attrib.get("href", "") if link is not None else ""
    title_text = title.text or ""
    updated_at = (updated.text or "") if updated is not None else ""

    accession = ""
    cik       = ""

    if "-index.htm" in href:
        parts     = href.rstrip("/").split("/")
        accession = parts[-1].replace("-index.htm", "")
        cik       = parts[-3] if len(parts) >= 3 else ""

    form_type   = title_text.split(" - ")[0].strip() if " - " in title_text else ""
    entity_part = title_text.split(" - ", 1)[1]      if " - " in title_text else title_text
    entity_name = entity_part.rsplit("(", 1)[0].strip()

    return {
        "accession":   accession,
        "cik":         cik.lstrip("0"),
        "entity_name": entity_name.upper(),
        "form_type":   form_type,
        "file_date":   updated_at[:10],
        "index_url":   href,
        "updated":     updated_at,
    }


def _fetch_feed_page(start: int) -> list[dict]:
    """One page of the global EDGAR current-filings Atom feed."""
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=&dateb=&owner=include&output=atom"
        f"&count={FEED_PAGE_SIZE}&start={start}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        log.error(f"EDGAR RSS fetch failed at start={start}: {e}")
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}
    return [_parse_feed_entry(e, ns) for e in root.findall("a:entry", ns)]


def _entry_time(hit: dict) -> datetime.datetime | None:
    raw = hit.get("updated") or ""
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def fetch_recent_filings(
    max_pages: int = 1,
    since: datetime.datetime | None = None,
) -> list[dict]:
    """Walk the current-filings feed, newest first, deduplicated by accession.

    Stops at whichever comes first: `max_pages`, an empty page, or a page
    whose oldest entry predates `since`. The dedupe is not cosmetic — the feed
    emits one entry per ROLE, so a page of 100 entries carries roughly half
    that many filings and the old single-page read had half the depth it
    appeared to have.
    """
    hits: list[dict] = []
    seen_accessions: set[str] = set()
    entries_read = 0

    for page in range(max_pages):
        entries = _fetch_feed_page(page * FEED_PAGE_SIZE)
        if not entries:
            break
        entries_read += len(entries)

        oldest = None
        for hit in entries:
            acc = hit["accession"]
            if acc and acc not in seen_accessions:
                seen_accessions.add(acc)
                hits.append(hit)
            ts = _entry_time(hit)
            if ts and (oldest is None or ts < oldest):
                oldest = ts

        if since and oldest and oldest < since:
            break
        if page + 1 < max_pages:
            time.sleep(FEED_PAGE_SLEEP)

    if max_pages > 1:
        span = ""
        if hits:
            newest, tail = _entry_time(hits[0]), _entry_time(hits[-1])
            if newest and tail:
                span = f" spanning {newest:%H:%M} back to {tail:%H:%M} ET"
        log.info(
            f"Feed: {entries_read} entries over {min(max_pages, (entries_read + 99) // 100)} "
            f"page(s) -> {len(hits)} unique filing(s){span}"
        )

    return hits


# ---------------------------------------------------------------------------
# Filing text extraction
# ---------------------------------------------------------------------------

def strip_html(html: str, max_chars: int = 300_000) -> str:
    text = re.sub(r"<script.*?>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style.*?>.*?</style>",   " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text
            .replace("&nbsp;", " ")
            .replace("&amp;",  "&")
            .replace("&lt;",   "<")
            .replace("&gt;",   ">"))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _is_valid_doc_href(href: str) -> bool:
    """Return True if this href points to a real filing document (not SEC nav)."""
    lower = href.lower()
    if "-index.htm" in lower:
        return False
    if not lower.endswith((".htm", ".html", ".txt")):
        return False
    # Skip XBRL/graphic files
    if re.search(r'\.(xsd|xml|jpg|png|gif|css|js|cal|lab|pre|def)$', lower):
        return False
    # Skip SEC website navigation — only allow relative paths or /Archives/ absolute paths
    if lower.startswith("/") and "/archives/edgar/data/" not in lower:
        return False
    if lower.startswith("http") and "/archives/edgar/data/" not in lower:
        return False
    return True


def extract_document_urls(index_html: str, index_url: str) -> list[str]:
    """
    Parse the EDGAR filing index page and return document URLs in priority order:
    1. Primary document (Seq 1 in the filing table)
    2. EX-99.x exhibits (press releases, supplements — the actual content for 8-Ks)
    Falls back to first valid href if table parsing fails.
    """
    primary_url = ""
    exhibit_urls: list[str] = []

    # Parse the EDGAR document table rows
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', index_html, flags=re.I | re.S)
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, flags=re.I | re.S)
        if len(cells) < 3:
            continue

        href_match = re.search(r'href="([^"]+)"', row, flags=re.I)
        if not href_match:
            continue

        href = href_match.group(1).strip()
        if "/ix?doc=" in href:
            href = href.split("/ix?doc=", 1)[1]

        if not _is_valid_doc_href(href):
            continue

        full_url = urllib.parse.urljoin(index_url, href)
        row_text = re.sub(r'<[^>]+>', ' ', row)

        # Seq 1 = primary document
        seq_text = re.sub(r'<[^>]+>', '', cells[0]).strip()
        if seq_text == "1" and not primary_url:
            primary_url = full_url
        # EX-99.x = press releases / exhibits with real content
        elif re.search(r'EX-99', row_text, re.I):
            exhibit_urls.append(full_url)

    # Fallback: first valid href in the whole page
    if not primary_url:
        for href in re.findall(r'href="([^"]+)"', index_html, flags=re.I):
            href = href.strip()
            if "/ix?doc=" in href:
                href = href.split("/ix?doc=", 1)[1]
            if _is_valid_doc_href(href):
                primary_url = urllib.parse.urljoin(index_url, href)
                break

    result = []
    if primary_url:
        result.append(primary_url)
    result.extend(exhibit_urls[:2])  # up to 2 exhibits (EX-99.1, EX-99.2)
    return result


def _fetch_url_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return strip_html(raw)
    except Exception as e:
        log.warning(f"Could not fetch {url}: {e}")
        return ""


def fetch_filing_documents(accession_raw: str, cik: str) -> tuple[str, list[str]]:
    """
    Returns (concatenated_text, document_urls).

    document_urls[0] is the primary document — the thing worth linking to.
    The EDGAR *index* page that filing_url points at is only a table of
    contents, so a Discord post carrying just that costs a second click to
    reach any actual content.
    """
    accession_clean = accession_raw.replace("-", "")
    cik_padded      = str(cik).zfill(10)
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_padded}/"
        f"{accession_clean}/{accession_raw}-index.htm"
    )
    req = urllib.request.Request(index_url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            index_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"Could not fetch index for {accession_raw}: {e}")
        return "", []

    doc_urls = extract_document_urls(index_html, index_url)
    if not doc_urls:
        log.warning(f"No document URLs found for {accession_raw}")
        return "", []

    parts = []
    for i, url in enumerate(doc_urls):
        text = _fetch_url_text(url)
        if text:
            parts.append(text)
        if i < len(doc_urls) - 1:
            time.sleep(0.3)  # respect SEC rate limits between doc fetches

    return "\n\n---\n\n".join(parts), doc_urls


def fetch_filing_text(accession_raw: str, cik: str) -> str:
    """Text-only wrapper, kept for local/ad-hoc use."""
    return fetch_filing_documents(accession_raw, cik)[0]


# ---------------------------------------------------------------------------
# Write to inbox
# ---------------------------------------------------------------------------

def write_filing_payload(
    hit: dict, ticker: str, filing_text: str, doc_urls: list[str] | None = None
) -> None:
    doc_urls = doc_urls or []
    payload = {
        "ticker":       ticker,
        "accession":    hit["accession"],
        "cik":          hit["cik"],
        "entity_name":  hit["entity_name"],
        "form_type":    hit["form_type"],
        "file_date":    hit["file_date"],
        "filing_url":   hit["index_url"],
        # Direct link to the actual document, so a Discord post lands on the
        # filing text rather than EDGAR's table of contents.
        "primary_doc_url": doc_urls[0] if doc_urls else "",
        "exhibit_urls":    doc_urls[1:],
        "filing_text":  filing_text,
        "detected_at_utc": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    out_path = INBOX_DIR / f"{hit['accession']}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    log.info(f"Queued  {ticker:10s} | {hit['form_type']:12s} | {hit['accession']}")


# ---------------------------------------------------------------------------
# One poll cycle
# ---------------------------------------------------------------------------

def poll_once(
    seen: set[str],
    watchlist: dict[str, str],
    max_pages: int = 1,
    since: datetime.datetime | None = None,
    max_queue: int | None = None,
) -> tuple[set[str], int]:
    """
    Run one poll cycle.
    Returns (updated_seen, number_of_new_filings_queued).

    `max_queue` is a cold-start guard for the catch-up sweep. If the cache was
    lost, `seen` is empty and an 8-hour sweep would otherwise hand the
    dispatcher every watchlist filing of the day at once. The newest
    `max_queue` are kept — they are the ones still worth acting on — and the
    rest are marked seen so the next cycle starts clean.
    """
    hits = fetch_recent_filings(max_pages=max_pages, since=since)
    if not hits:
        log.warning("Empty response from EDGAR feed")
        return seen, 0

    queued = 0
    over_cap: list[str] = []

    for hit in hits:
        accession = hit["accession"]
        cik       = hit["cik"]

        if not accession or accession in seen:
            continue

        if watchlist and cik not in watchlist:
            seen.add(accession)   # mark as seen so we don't re-check next cycle
            continue

        ticker = watchlist.get(cik, "UNKNOWN")

        if max_queue is not None and queued >= max_queue:
            seen.add(accession)
            over_cap.append(f"{ticker} {hit['form_type']} {accession}")
            continue

        filing_text, doc_urls = fetch_filing_documents(accession, cik)

        seen.add(accession)

        if not filing_text:
            log.warning(f"No text for {ticker} {accession} — skipping dispatch")
            save_seen(seen)
            continue

        write_filing_payload(hit, ticker, filing_text, doc_urls)
        queued += 1

        # Respect SEC rate guidance: max 10 req/sec, we stay well below.
        time.sleep(0.5)

        if len(seen) > MAX_SEEN:
            seen = set(list(sorted(seen))[-MAX_SEEN:])

        save_seen(seen)

    if over_cap:
        # Named, not counted. This module's failure mode is silence, and a
        # filing dropped by the cap is exactly the shape of thing that goes
        # unnoticed for a week — so put every one of them in the log where a
        # search for a ticker will find it.
        log.warning(
            f"Queue cap ({max_queue}) reached — {len(over_cap)} older watchlist "
            f"filing(s) marked seen WITHOUT dispatch:"
        )
        for item in over_cap:
            log.warning(f"    not dispatched: {item}")
        log.warning("Raise --max-queue if this was not a cold start.")

    save_seen(seen)
    return seen, queued


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="EDGAR filing poller")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit (GitHub Actions mode)",
    )
    parser.add_argument(
        "--catchup",
        action="store_true",
        help="Sweep back to --catchup-hours and queue anything never seen, "
             "then exit. Runs whether or not EDGAR is open.",
    )
    parser.add_argument("--catchup-hours", type=float, default=CATCHUP_HOURS,
                        help=f"catch-up horizon in hours (default {CATCHUP_HOURS})")
    parser.add_argument("--max-pages", type=int, default=CATCHUP_PAGES,
                        help=f"page ceiling for a catch-up sweep (default {CATCHUP_PAGES})")
    parser.add_argument("--pages", type=int, default=LIVE_PAGES,
                        help=f"feed pages per live poll (default {LIVE_PAGES})")
    parser.add_argument("--max-queue", type=int, default=CATCHUP_MAX_QUEUE,
                        help=f"cold-start queue cap for a catch-up sweep "
                             f"(default {CATCHUP_MAX_QUEUE})")
    args = parser.parse_args()

    seen      = load_seen()
    watchlist = load_watchlist()
    log.info(f"Loaded {len(seen)} seen accessions | {len(watchlist)} watchlist CIKs")

    if args.catchup:
        # Deliberately NOT gated on edgar_is_open(). The whole point is to run
        # after a blackout, and the most valuable moment to run is the first
        # trigger after EDGAR closes — that is when the day's tail is still
        # unrecovered. Nothing new appears while EDGAR is shut, so a sweep
        # outside filing hours is cheap and occasionally decisive.
        horizon = datetime.datetime.now(ET_ZONE) - datetime.timedelta(
            hours=args.catchup_hours
        )
        log.info(
            f"Catch-up sweep: back to {horizon:%Y-%m-%d %H:%M} ET "
            f"(max {args.max_pages} pages, queue cap {args.max_queue})"
        )
        seen, queued = poll_once(
            seen, watchlist,
            max_pages=args.max_pages,
            since=horizon,
            max_queue=args.max_queue,
        )
        log.info(f"Catch-up complete. {queued} missed filing(s) queued.")
        return

    if args.once:
        # GitHub Actions mode: single pass.
        if not edgar_is_open():
            log.info("EDGAR is closed right now — nothing to poll.")
            return

        log.info("Running single poll cycle...")
        seen, queued = poll_once(seen, watchlist, max_pages=args.pages)
        log.info(f"Poll complete. {queued} filing(s) queued for dispatch.")
        return

    # Continuous mode (local dev / always-on server).
    log.info(f"Continuous mode. Polling every {POLL_INTERVAL}s.")
    while True:
        try:
            if not edgar_is_open():
                time.sleep(POLL_INTERVAL)
                continue
            seen, queued = poll_once(seen, watchlist, max_pages=args.pages)
            if queued:
                log.info(f"{queued} filing(s) queued this cycle.")
        except Exception as e:
            log.exception(f"Unexpected poller error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
