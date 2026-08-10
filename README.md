# sec-poller

GitHub Actions–based SEC filing monitor. Polls EDGAR continuously during your
trading windows, matches filings against your watchlist, calls an LLM for a
fixed-income-analyst summary, and posts to Discord.

**Coverage:** one long-running job per window polls every 15 seconds with no
gaps. Windows are evaluated in Sofia local time, so the EEST/EET switch needs
no maintenance.

| Window | Sofia | ET | Why |
|---|---|---|---|
| Day | 13:00 – 18:00 Mon–Fri | 06:00 – 11:00 | Opens on EDGAR's first filing minute |
| Night | 23:00 – 03:00 Mon–Fri eve | 16:00 – 20:00 | US after-hours 8-K flow |

Both sit entirely inside EDGAR's filing hours (06:00–22:00 ET), so no cycle is
wasted. The day window was 11:00–16:00 until Aug 2026, which spent its first
two hours polling a system that wasn't accepting filings yet.

**GitHub throttles the `*/5` cron to roughly one trigger every 2 hours** — this
is measured, not theoretical (see AGENTS.md §6). A window would therefore be
covered only when a trigger happened to land inside it. To compensate, a
trigger landing within 2 hours *before* a window holds the runner and opens
with the window rather than exiting. Look for `(armed early)` in the run log.

The LLM is **NVIDIA Nemotron 3** via NVIDIA NIM (free, ~40 req/min, no daily
cap), with **OpenRouter free models as an automatic fallback**.

## Repository structure

```
sec-poller/
├── edgar_poller.py          # polls EDGAR, writes matched filings to filings-inbox/
├── openrouter_dispatch.py   # reads inbox, calls NVIDIA NIM (OpenRouter fallback), posts to Discord
│                           #   (filename is historical — it is provider-agnostic now)
├── prefilter.py             # drops noise filings before any LLM call
├── manage_watchlist.py      # add/remove tickers by symbol; edits cik_map.json
├── test_pipeline.py         # offline regression suite
├── cik_map.json             # your watchlist: {"TICKER": "0001234567", ...}
│                           #   SINGLE SOURCE OF TRUTH — never create a second copy
├── seen_accessions.json     # auto-managed, cached between runs
├── dispatched_accessions.json  # auto-managed, cached between runs
└── .github/
    └── workflows/
        └── poll.yml         # cron schedule + job steps
```

## One-time setup (15 minutes)

### 1. Create the GitHub repo

Create a **public** repo (unlimited Actions minutes) or private repo (2,000
free minutes/month — probably enough, but public is safer for this use case).

Push all files from this directory to the root of the repo.

### 2. Add GitHub Secrets

Go to: **Settings → Secrets and variables → Actions → Secrets**

| Secret name          | Required | Value                                              |
|----------------------|----------|----------------------------------------------------|
| `DISCORD_WEBHOOK`    | yes      | Your full Discord webhook URL                      |
| `NVIDIA_API_KEY`     | primary  | NVIDIA NIM key (`nvapi-...`) from [build.nvidia.com/settings/api-keys](https://build.nvidia.com/settings/api-keys) |
| `OPENROUTER_API_KEY` | fallback | OpenRouter key (`sk-or-v1-...`)                    |
| `DISCORD_WEBHOOK_URGENT` | optional | Second webhook for priority events — see below |

At least one of `NVIDIA_API_KEY` / `OPENROUTER_API_KEY` must be set or the
dispatcher exits. Providers with no key are silently dropped from the chain,
so **deleting a secret is how you disable a provider** — no code change needed.

> ⚠️ **NVIDIA keys expire ~6 months after issue.** When that happens the
> dispatcher logs an auth failure, skips the whole NVIDIA chain, and quietly
> keeps running on OpenRouter — so the feed won't go dark, but you also won't
> notice unless you check `dispatch.log`. Set a calendar reminder to
> regenerate the key.

### 3. (Optional) Override the model chains via variables

Go to: **Settings → Secrets and variables → Actions → Variables**

| Variable name      | Effect                                                        |
|--------------------|---------------------------------------------------------------|
| `NVIDIA_MODELS`    | Comma-separated NVIDIA chain, replaces the built-in default   |
| `OPENROUTER_MODEL` | Model to try first on the OpenRouter fallback chain           |

Default NVIDIA chain (tried in order — a retired ID 404s and falls through):

1. `nvidia/nemotron-3-super-120b-a12b` — 120B MoE, 1M context, the workhorse
2. `nvidia/nemotron-3-ultra-550b-a55b` — 550B MoE, slower
3. `nvidia/nemotron-3-nano-30b-a3b` — fast last resort

Confirm what your key can actually reach:

```bash
curl -s -H "Authorization: Bearer $NVIDIA_API_KEY" https://integrate.api.nvidia.com/v1/models | grep -o '"id":"[^"]*"'
```

### 4. Upload your watchlist

Your `cik_map.json` is already in the correct format. Just commit it:
```
{"ABR": "0001253986", "AGNC": "0001423689", ...}
```

### 5. Initialize the seen/dispatched files

Commit empty arrays so the cache has something to restore:
```bash
echo '[]' > seen_accessions.json
echo '[]' > dispatched_accessions.json
git add seen_accessions.json dispatched_accessions.json
git commit -m "init: empty seen/dispatched accession files"
git push
```

### 6. Enable Actions and test

1. Go to the **Actions** tab in your repo
2. Click **EDGAR SEC Poller** → **Run workflow** (manual trigger)
3. Watch the run — it will say "EDGAR is closed" if run outside hours,
   or process any live filings if run during market hours
4. Check your Discord channel for the first post

## Rate limits

Sleep between calls is chosen per provider, based on which one actually
served the filing.

| Limit                    | Value        | Impact                                     |
|--------------------------|--------------|--------------------------------------------|
| EDGAR fair-access        | 10 req/s     | 15s poll = 0.07 req/s ✅                    |
| NVIDIA NIM free req/min  | ~40/model    | 2s sleep keeps you at ~30 — best effort, not a guarantee |
| NVIDIA NIM free req/day  | none published | No daily ceiling to plan around ✅       |
| OpenRouter free req/min  | 20           | 4s sleep keeps you at ~15                  |
| OpenRouter free req/day  | 200          | Fallback only, so rarely approached        |
| GitHub Actions (public)  | Unlimited    | No concern                                 |
| GitHub Actions (private) | 2,000 min/mo | 100 filings × 7 min/run = ~700 min/mo ✅   |

## Priority routing

Redemptions, new listings, M&A and tender offers (priority 1–4) can be split
off from routine NAV/distribution traffic.

| Setting | Type | Effect |
|---|---|---|
| `DISCORD_WEBHOOK_URGENT` | secret | Priority events post **here instead of** the main channel |
| `DISCORD_URGENT_MENTION` | variable | Prepended to priority posts, e.g. `@here` or `<@&ROLE_ID>` |

Both are optional. With neither set, behaviour is unchanged — everything lands
in one channel with no pings. Create a `#sec-urgent` channel, add its webhook,
and turn on push notifications for that channel only.

Classification reads the `##` highlight header and the line-1 emoji, not the
body prose — a NAV report that mentions "redemption of shares at net asset
value" in passing stays routine. If the urgent webhook fails, the post is
retried on the main webhook rather than lost.

**On calendar-driven forms the emoji alone doesn't ping.** Proxies, annual
reports, 10-K/10-Q and fund periodics (`ARS`, `DEF 14A`, `N-CSR`, …) need an
actual `##` highlight block to route urgent, because the model has been seen
putting 🚨 on an annual report and ⚠ on a routine annual-meeting proxy. Every
issuer files those every year, so a stray emoji there becomes a recurring ping.
Merger-vote proxies (`DEFM14A`) are not on the list, and a `DEF 14A` that
genuinely quotes change-of-control terms still pings on its header.

**New issuances (tier 2) additionally have to be tradeable**, or they demote to
the main channel. The target is exchange-listed income securities at retail
denomination — $25-par preferreds, depositary shares, baby bonds. A filing is
demoted when its body shows `Listing: UNLISTED`, `Par: $1,000` (institutional
paper, even if NYSE-listed), `Product: common stock`, or no preferred /
depositary / baby-bond / $25-par signal at all. Redemptions, M&A and tender
offers are **not** gated — a missed call notice costs more than a spare ping.

## Tests

```bash
python test_pipeline.py
```

Offline, no secrets, no network. Runs automatically via `.github/workflows/test.yml`
on any push touching a `.py` file. Every case corresponds to a bug that actually
reached Discord — **add one whenever a bad post gets through.**

## Monitoring

- **Logs**: Each run uploads `edgar_poller.log` and `dispatch.log` as
  artifacts (retained 7 days). View them in the Actions tab.
- **Discord alerts**: The dispatcher posts a ❌ or ⚠️ message to Discord
  whenever a filing fails to process, with a direct EDGAR link for manual review.

## Running locally (optional)

```bash
# Continuous mode (original behavior, no --once flag):
python edgar_poller.py

# Dispatcher (reads whatever is in filings-inbox/):
NVIDIA_API_KEY=nvapi-... DISCORD_WEBHOOK=https://... python openrouter_dispatch.py
```
