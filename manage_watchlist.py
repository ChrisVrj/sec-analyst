#!/usr/bin/env python3
"""
manage_watchlist.py — friendly editor for cik_map.json (the sec-analyst watchlist).

You give it ticker symbols; it looks up the CIK for you from SEC's official
ticker->CIK table (https://www.sec.gov/files/company_tickers.json) and keeps
cik_map.json sorted and correctly formatted. No more hand-finding CIK numbers.

USAGE
  # Add one or more tickers (CIKs looked up automatically):
  python manage_watchlist.py add PTMN EARN NHP

  # Remove tickers:
  python manage_watchlist.py remove BCIC BK

  # Reconcile against a plain text file of tickers (one per line, like
  # all-symbols.txt). Shows what's missing/unknown; add --apply to write them:
  python manage_watchlist.py sync all-symbols.txt
  python manage_watchlist.py sync all-symbols.txt --apply

  # Show current watchlist size / look up a single ticker without writing:
  python manage_watchlist.py list
  python manage_watchlist.py lookup TPZ

NOTES
  * Stdlib only — no pip install needed. Runs on your PC and in GitHub Actions.
  * SEC requires a descriptive User-Agent with a contact email; it's set below.
    Keep a real contact in CONTACT_EMAIL.
  * The SEC table is cached next to this script (company_tickers_cache.json) for
    24h so repeated runs don't re-download ~1MB each time. Use --refresh to force.
  * CIKs are written zero-padded to 10 digits to match the existing file format.
"""

import argparse
import json
import os
import sys
import time
import urllib.request

CONTACT_EMAIL = "chrisdoesdocu@gmail.com"
USER_AGENT = f"sec-analyst watchlist tool {CONTACT_EMAIL}"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

HERE = os.path.dirname(os.path.abspath(__file__))

# The poller resolves the watchlist as BASE_DIR/cik_map.json where BASE_DIR is
# GITHUB_WORKSPACE (the repo root). This tool MUST edit that same file.
#
# It previously lived in SEC Analyst/ and wrote to its own directory, so every
# edit landed in a second copy the poller never read — EARN, NHP, FBYD, BMNR,
# LILA and TPZ were "added" in Jun 2026 and silently never monitored, and the
# BK->BNY / BCIC->PTMN relabels never took effect. Keep this path anchored to
# the repo root; do not reintroduce a second cik_map.json anywhere.
CIK_MAP_PATH = os.environ.get("CIK_MAP_PATH") or os.path.join(HERE, "cik_map.json")
CACHE_PATH = os.path.join(HERE, "company_tickers_cache.json")
CACHE_TTL_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# SEC ticker -> CIK table
# ---------------------------------------------------------------------------
def _download_sec_table():
    req = urllib.request.Request(SEC_TICKERS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


def load_sec_table(refresh=False):
    """Return {TICKER_UPPER: '##########'} from SEC's official table (cached)."""
    raw = None
    if not refresh and os.path.exists(CACHE_PATH):
        age = time.time() - os.path.getmtime(CACHE_PATH)
        if age < CACHE_TTL_SECONDS:
            with open(CACHE_PATH, "r", encoding="utf-8") as fh:
                raw = fh.read()
    if raw is None:
        print("Downloading SEC ticker table ...", file=sys.stderr)
        raw = _download_sec_table()
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            fh.write(raw)

    data = json.loads(raw)
    table = {}
    # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    for row in data.values():
        ticker = str(row.get("ticker", "")).upper().strip()
        cik = row.get("cik_str")
        if ticker and cik is not None:
            # First occurrence wins; SEC lists the primary ticker first.
            table.setdefault(ticker, f"{int(cik):010d}")
    return table


def normalize_ticker(t):
    return t.upper().strip().lstrip("$")


# ---------------------------------------------------------------------------
# cik_map.json I/O
# ---------------------------------------------------------------------------
def load_map():
    if not os.path.exists(CIK_MAP_PATH):
        return {}
    with open(CIK_MAP_PATH, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_map(m):
    ordered = {k: m[k] for k in sorted(m)}
    with open(CIK_MAP_PATH, "w", encoding="utf-8") as fh:
        json.dump(ordered, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def resolve(tickers, table):
    """Return (found dict, not_found list)."""
    found, missing = {}, []
    for raw in tickers:
        t = normalize_ticker(raw)
        if not t:
            continue
        # SEC uses dashes for class shares (e.g. BRK-B); try a few variants.
        cik = table.get(t) or table.get(t.replace(".", "-")) or table.get(t.replace("-", "."))
        if cik:
            found[t] = cik
        else:
            missing.append(t)
    return found, missing


def cmd_add(args):
    table = load_sec_table(refresh=args.refresh)
    m = load_map()
    found, missing = resolve(args.tickers, table)

    added, already = [], []
    for t, cik in found.items():
        if t in m and m[t] == cik:
            already.append(t)
        else:
            verb = "updated" if t in m else "added"
            m[t] = cik
            added.append((t, cik, verb))

    if added:
        save_map(m)
    for t, cik, verb in added:
        print(f"  {verb:8} {t:8} {cik}")
    if already:
        print(f"  already present (unchanged): {', '.join(sorted(already))}")
    if missing:
        print(f"  NOT FOUND on SEC (check the symbol): {', '.join(missing)}")
    print(f"\nWatchlist now has {len(m)} tickers.")


def cmd_remove(args):
    m = load_map()
    removed, absent = [], []
    for raw in args.tickers:
        t = normalize_ticker(raw)
        if t in m:
            del m[t]
            removed.append(t)
        else:
            absent.append(t)
    if removed:
        save_map(m)
        print(f"  removed: {', '.join(removed)}")
    if absent:
        print(f"  not in watchlist: {', '.join(absent)}")
    print(f"\nWatchlist now has {len(m)} tickers.")


def _read_ticker_file(path):
    tickers = []
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # tolerate "123  TICKER", "TICKER,...", or bare "TICKER"
            parts = line.replace(",", " ").split()
            tickers.append(parts[-1] if parts else line)
    return tickers


def cmd_sync(args):
    table = load_sec_table(refresh=args.refresh)
    m = load_map()
    wanted = [normalize_ticker(t) for t in _read_ticker_file(args.file)]
    wanted = list(dict.fromkeys(t for t in wanted if t))  # dedupe, keep order

    missing_from_map = [t for t in wanted if t not in m]
    found, not_on_sec = resolve(missing_from_map, table)
    extra_in_map = [t for t in sorted(m) if t not in wanted]

    print(f"File lists {len(wanted)} unique tickers; watchlist has {len(m)}.\n")

    print(f"To ADD ({len(found)} resolved):")
    for t in sorted(found):
        print(f"  {t:8} {found[t]}")
    if not_on_sec:
        print(f"\nIn file but NOT FOUND on SEC ({len(not_on_sec)}): {', '.join(sorted(not_on_sec))}")
    if extra_in_map:
        print(f"\nIn watchlist but NOT in file ({len(extra_in_map)}) — left untouched:")
        print(f"  {', '.join(extra_in_map)}")

    if args.apply and found:
        m.update(found)
        save_map(m)
        print(f"\nApplied. Watchlist now has {len(m)} tickers.")
    elif found:
        print(f"\n(dry run — re-run with --apply to write these {len(found)} additions)")


def cmd_list(args):
    m = load_map()
    print(f"{len(m)} tickers in {CIK_MAP_PATH}")
    if args.show:
        for t in sorted(m):
            print(f"  {t:8} {m[t]}")


def cmd_lookup(args):
    table = load_sec_table(refresh=args.refresh)
    found, missing = resolve(args.tickers, table)
    for t in sorted(found):
        print(f"  {t:8} {found[t]}")
    if missing:
        print(f"  NOT FOUND: {', '.join(missing)}")


def main():
    p = argparse.ArgumentParser(description="Manage cik_map.json by ticker symbol.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add", help="add/lookup tickers and write them")
    pa.add_argument("tickers", nargs="+")
    pa.add_argument("--refresh", action="store_true", help="force re-download of SEC table")
    pa.set_defaults(func=cmd_add)

    pr = sub.add_parser("remove", help="remove tickers")
    pr.add_argument("tickers", nargs="+")
    pr.set_defaults(func=cmd_remove)

    ps = sub.add_parser("sync", help="reconcile against a text file of tickers")
    ps.add_argument("file")
    ps.add_argument("--apply", action="store_true", help="write the additions (default is dry run)")
    ps.add_argument("--refresh", action="store_true", help="force re-download of SEC table")
    ps.set_defaults(func=cmd_sync)

    pl = sub.add_parser("list", help="show watchlist size")
    pl.add_argument("--show", action="store_true", help="print every ticker")
    pl.set_defaults(func=cmd_list)

    pk = sub.add_parser("lookup", help="look up CIKs without writing")
    pk.add_argument("tickers", nargs="+")
    pk.add_argument("--refresh", action="store_true")
    pk.set_defaults(func=cmd_lookup)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
