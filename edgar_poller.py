#!/usr/bin/env python3
"""
EDGAR poller for the OpenClaw sec-analyst pipeline.

Modes:
  --once    Poll EDGAR once, write matched filings to filings-inbox/, exit.
            Used by GitHub Actions.
  (default) Poll continuously (original local dev behavior).

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
        logging.FileHandler(LOG_FILE),
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

def fetch_recent_filings() -> list[dict]:
    """Fetch the 100-entry global EDGAR current filings Atom feed."""
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=&dateb=&owner=include&count=100&output=atom"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        log.error(f"EDGAR RSS fetch failed: {e}")
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}
    hits = []

    for entry in root.findall("a:entry", ns):
        link    = entry.find("a:link", ns)
        title   = entry.find("a:title", ns)
        updated = entry.find("a:updated", ns)

        href       = link.attrib.get("href", "") if link is not None else ""
        title_text = title.text or ""

        accession = ""
        cik       = ""

        if "-index.htm" in href:
            parts     = href.rstrip("/").split("/")
            accession = parts[-1].replace("-index.htm", "")
            cik       = parts[-3] if len(parts) >= 3 else ""

        form_type   = title_text.split(" - ")[0].strip() if " - " in title_text else ""
        entity_part = title_text.split(" - ", 1)[1]      if " - " in title_text else title_text
        entity_name = entity_part.rsplit("(", 1)[0].strip()

        hits.append({
            "accession":  accession,
            "cik":        cik.lstrip("0"),
            "entity_name": entity_name.upper(),
            "form_type":  form_type,
            "file_date":  (updated.text or "")[:10] if updated is not None else "",
            "index_url":  href,
        })

    return hits


# ---------------------------------------------------------------------------
# Filing text extraction
# ---------------------------------------------------------------------------

def strip_html(html: str) -> str:
    text = re.sub(r"<script.*?>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style.*?>.*?</style>",   " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text
            .replace("&nbsp;", " ")
            .replace("&amp;",  "&")
            .replace("&lt;",   "<")
            .replace("&gt;",   ">"))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80_000]


def extract_document_url(index_html: str, index_url: str) -> str:
    candidates = re.findall(r'href="([^"]+)"', index_html, flags=re.I)

    for href in candidates:
        href = href.strip()

        if "/ix?doc=" in href:
            href = href.split("/ix?doc=", 1)[1]

        lower = href.lower()
        if "-index.htm" in lower:
            continue
        if not lower.endswith((".htm", ".html", ".txt")):
            continue

        return urllib.parse.urljoin(index_url, href)

    return ""


def fetch_filing_text(accession_raw: str, cik: str) -> str:
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
        return ""

    doc_url = extract_document_url(index_html, index_url)
    if not doc_url:
        log.warning(f"No primary document URL found for {accession_raw}")
        return ""

    doc_req = urllib.request.Request(doc_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(doc_req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return strip_html(raw)
    except Exception as e:
        log.warning(f"Could not fetch filing body for {accession_raw}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Write to inbox
# ---------------------------------------------------------------------------

def write_filing_payload(hit: dict, ticker: str, filing_text: str) -> None:
    payload = {
        "ticker":       ticker,
        "accession":    hit["accession"],
        "cik":          hit["cik"],
        "entity_name":  hit["entity_name"],
        "form_type":    hit["form_type"],
        "file_date":    hit["file_date"],
        "filing_url":   hit["index_url"],
        "filing_text":  filing_text,
        "detected_at_utc": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    out_path = INBOX_DIR / f"{hit['accession']}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    log.info(f"Queued  {ticker:10s} | {hit['form_type']:12s} | {hit['accession']}")


# ---------------------------------------------------------------------------
# One poll cycle
# ---------------------------------------------------------------------------

def poll_once(seen: set[str], watchlist: dict[str, str]) -> tuple[set[str], int]:
    """
    Run one poll cycle.
    Returns (updated_seen, number_of_new_filings_queued).
    """
    hits = fetch_recent_filings()
    if not hits:
        log.warning("Empty response from EDGAR feed")
        return seen, 0

    queued = 0

    for hit in hits:
        accession = hit["accession"]
        cik       = hit["cik"]

        if not accession or accession in seen:
            continue

        if watchlist and cik not in watchlist:
            seen.add(accession)   # mark as seen so we don't re-check next cycle
            continue

        ticker       = watchlist.get(cik, "UNKNOWN")
        filing_text  = fetch_filing_text(accession, cik)

        seen.add(accession)

        if not filing_text:
            log.warning(f"No text for {ticker} {accession} — skipping dispatch")
            save_seen(seen)
            continue

        write_filing_payload(hit, ticker, filing_text)
        queued += 1

        # Respect SEC rate guidance: max 10 req/sec, we stay well below.
        time.sleep(0.5)

        if len(seen) > MAX_SEEN:
            seen = set(list(sorted(seen))[-MAX_SEEN:])

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
    args = parser.parse_args()

    seen      = load_seen()
    watchlist = load_watchlist()
    log.info(f"Loaded {len(seen)} seen accessions | {len(watchlist)} watchlist CIKs")

    if args.once:
        # GitHub Actions mode: single pass.
        if not edgar_is_open():
            log.info("EDGAR is closed right now — nothing to poll.")
            return

        log.info("Running single poll cycle...")
        seen, queued = poll_once(seen, watchlist)
        log.info(f"Poll complete. {queued} filing(s) queued for dispatch.")
        return

    # Continuous mode (local dev / always-on server).
    log.info(f"Continuous mode. Polling every {POLL_INTERVAL}s.")
    while True:
        try:
            if not edgar_is_open():
                time.sleep(POLL_INTERVAL)
                continue
            seen, queued = poll_once(seen, watchlist)
            if queued:
                log.info(f"{queued} filing(s) queued this cycle.")
        except Exception as e:
            log.exception(f"Unexpected poller error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
