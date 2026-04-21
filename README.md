# sec-poller

GitHub Actions–based SEC filing monitor. Polls EDGAR every 30 minutes during
market hours, matches filings against your watchlist, calls OpenRouter for an
AI summary, and posts to Discord.

## Repository structure

```
sec-poller/
├── edgar_poller.py          # polls EDGAR, writes matched filings to filings-inbox/
├── openrouter_dispatch.py   # reads inbox, calls OpenRouter, posts to Discord
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

| Secret name          | Value                                      |
|----------------------|--------------------------------------------|
| `OPENROUTER_API_KEY` | Your OpenRouter API key (`sk-or-v1-...`)   |
| `DISCORD_WEBHOOK`    | Your full Discord webhook URL              |

### 3. (Optional) Set the model via a variable

Go to: **Settings → Secrets and variables → Actions → Variables**

| Variable name      | Value (example)                                   |
|--------------------|---------------------------------------------------|
| `OPENROUTER_MODEL` | `meta-llama/llama-3.1-8b-instruct:free` (default) |

Good free alternatives:
- `mistralai/mistral-7b-instruct:free`
- `google/gemma-3-12b-it:free`

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

| Limit                  | Value           | Impact                                    |
|------------------------|-----------------|-------------------------------------------|
| OpenRouter free req/min| 20              | 4s sleep between calls keeps you at ~15   |
| OpenRouter free req/day| 200             | 3,000 filings/month = ~100/day ✅         |
| GitHub Actions (public)| Unlimited       | No concern                                |
| GitHub Actions (private)| 2,000 min/mo  | 100 filings × 7 min/run = ~700 min/mo ✅  |

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
OPENROUTER_API_KEY=sk-or-v1-... DISCORD_WEBHOOK=https://... python openrouter_dispatch.py
```
