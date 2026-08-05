# sec-poller

GitHub Actions–based SEC filing monitor. Polls EDGAR during your trading
windows, matches filings against your watchlist, calls an LLM for a
fixed-income-analyst summary, and posts to Discord.

The LLM is **NVIDIA Nemotron 3** via NVIDIA NIM (free, ~40 req/min, no daily
cap), with **OpenRouter free models as an automatic fallback**.

## Repository structure

```
sec-poller/
├── edgar_poller.py          # polls EDGAR, writes matched filings to filings-inbox/
├── openrouter_dispatch.py   # reads inbox, calls NVIDIA NIM (OpenRouter fallback), posts to Discord
│                           #   (filename is historical — it is provider-agnostic now)
├── cik_map.json             # your watchlist: {"TICKER": "0001234567", ...}
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
| NVIDIA NIM free req/min  | ~40/model    | 2s sleep keeps you at ~30 — best effort, not a guarantee |
| NVIDIA NIM free req/day  | none published | No daily ceiling to plan around ✅       |
| OpenRouter free req/min  | 20           | 4s sleep keeps you at ~15                  |
| OpenRouter free req/day  | 200          | Fallback only, so rarely approached        |
| GitHub Actions (public)  | Unlimited    | No concern                                 |
| GitHub Actions (private) | 2,000 min/mo | 100 filings × 7 min/run = ~700 min/mo ✅   |

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
