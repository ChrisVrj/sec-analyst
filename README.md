# sec-poller

GitHub Actions–based SEC filing monitor. Polls EDGAR continuously through the
filing day, matches filings against your watchlist, calls an LLM for a
fixed-income-analyst summary, and posts to Discord.

**Coverage:** one long-running job polls every 15 seconds through EDGAR's
entire filing day. Times are evaluated in Sofia local time, so the EEST/EET
switch needs no maintenance.

| Session | Sofia | ET |
|---|---|---|
| Mon–Fri | 13:00 – 05:00 next day | 06:00 – 22:00 |

That is EDGAR's filing day end to end. Until Sep 2026 this was two narrow
windows — 06:00–11:00 and 16:00–20:00 ET — with **five uncovered hours across
the middle of the US session**. Nuveen's NMCO rights offering was filed at
13:51 ET on 2026-08-27, into that hole; a cron trigger landed at 13:51 ET the
same day, found itself between windows, and exited after six seconds.

**GitHub throttles the `*/5` cron to roughly one trigger every 2 hours, and up
to 11 hours in a bad stretch** — measured, not theoretical (see AGENTS.md §6).
Three things compensate:

- a trigger landing within 2 hours *before* the session holds the runner and
  opens with it (`(armed early)` in the run log)
- a job runs to the session end or its 5h45m ceiling, so a 16-hour session is
  two or three chained jobs rather than hundreds of short ones
- **every run starts with a catch-up sweep**, in or out of session — see below

**Catch-up sweep.** EDGAR's current-filings feed pages backwards with `start=`,
so a run can read its way back through a blackout instead of seeing only the
newest 100 entries. That matters more than it sounds: at the 17:20–17:30 ET
deadline rush one page spans **five and a half minutes**, and 100 entries is
only ~50 filings because ownership forms emit one entry per role. The feed on
its own remembers almost nothing.

    python edgar_poller.py --catchup              # default: back 8h, ≤40 pages
    python edgar_poller.py --catchup --catchup-hours 2 --max-pages 12

A gap in GitHub's cron now costs latency, not the filing. `--max-queue`
(default 40) bounds a cold start after a lost cache; anything past the cap is
named in `edgar_poller.log` rather than silently dropped.

**One filing, one decision.** The feed gives a filing one entry per *role*,
each carrying its own CIK — a Form 4 lists the insider first and the issuer
second; a bank shelf lists the funding subsidiary first and the guarantor
second. Deciding on the first entry and marking the whole accession seen threw
away the role that names your company. In one 600-entry sample, **114 of the
119 multi-role filings touching the watchlist had the watchlist CIK in the
second entry** — including a Saba Form 4 on ECF. Roles are now kept through the
feed walk and the watchlist role is the one that decides.

**A failed fetch is retried, not written off.** EDGAR returns 503s and read
timeouts under normal load, and an index page is not always complete in the
seconds after a filing appears — which is exactly when a 15-second poll first
asks for it. Such a filing used to be marked seen and never looked at again.
It is now retried for `MAX_FETCH_ATTEMPTS` cycles, and giving up posts a ❌ to
Discord with the EDGAR link instead of a line in a log nobody opens.

The LLM is **NVIDIA Nemotron 3** via NVIDIA NIM (free, ~40 req/min, no daily
cap), with **OpenRouter free models as an automatic fallback**.

## Repository structure

```
sec-poller/
├── edgar_poller.py          # polls EDGAR, writes matched filings to filings-inbox/
├── openrouter_dispatch.py   # reads inbox, calls NVIDIA NIM (OpenRouter fallback), posts to Discord
│                           #   (filename is historical — it is provider-agnostic now)
├── prefilter.py             # drops noise filings before any LLM call
├── triage.py                # urgency from form_type + filing_text, independent
│                           #   of how the model words its summary; can only
│                           #   promote a filing, never demote one
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
| EDGAR fair-access        | 10 req/s     | 3 feed pages per 15s poll = 0.2 req/s ✅    |
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

Classification reads the `##` highlight header and the line-1 emoji of the
**posted body**, not the body prose — a NAV report that mentions "redemption of
shares at net asset value" in passing stays routine. If the urgent webhook fails, the post is
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

The one common-share exception is a **CEF or BDC rights offering**, which has
its own tier-2 rule (🧨) and is deliberately **not** gated: shares outstanding
rise on a fixed date at a price struck off NAV, so a fund trading at a premium
gives that premium up. It would fail the retail-income test by construction,
which is exactly why the test does not apply to it.

Tender offers (`SC TO-I`, `SC TO-T`, `SC 14D9`, `SC 13E3`) page on the form
type alone, amendments included — a Schedule TO is a standing bid at a stated
price with an expiry, and an amended one is usually a price bump.

**Two independent guards, pulling in opposite directions.** `triage.py` reads
`form_type` and `filing_text` — the EDGAR payload, which does not vary with
phrasing — and can only ever *promote*. `classify_priority()` reads the summary
and can demote. One stops a miss the reader never learns about; the other stops
a ping the reader cannot see the reason for.

**Nothing without a headline is ever posted.** A completion has to open with
`[emoji] **TICKER | FORM | YYYY-MM-DD**` on line 1, or it is the model
narrating the task instead of doing it. A failing completion goes back through
the fallback chain first; if every model fails, the channel gets a ⚠️ alert
with the EDGAR link rather than the model's notes to itself. The check is
anchored to the start of a line on purpose — the leaked post on 2026-09-01
contained a perfectly well-formed headline quoted mid-sentence.

**Routing reads the message that gets posted, not the model's raw output.**
Those are not the same string: the dispatcher drops a second summary copy,
strips the model's deliberation, and truncates to Discord's cap. Three
Goldman/Prudential note supplements pinged `#sec-urgent` on 2026-08-31 while
the visible body said `Listing: UNLISTED` / `Par: $1,000` and carried no
highlight block — whatever justified the ping lived in the part that never got
posted. The invariant now: **whatever routes a message is visible in it.**

## What never reaches the LLM

`prefilter.py` drops noise before a single token is spent. Two rules matter
most, and both are tuned against real filings:

| Dropped | Signal |
|---|---|
| Form 3/4/5 from anyone but a tracked activist | filer name |
| Structured notes (autocallable, buffer, participation rate, underlier) | payoff vocabulary |
| Explicitly unlisted offerings | "will not be listed…", and the term-sheet field `Listing: None` |
| $1,000-and-up paper | a stated `Denominations: $1,000` / `Minimum Denomination: $1,000`, with no $25-par or depositary-share signal anywhere |

**Rights offerings are kept unconditionally**, checked before everything else.
A CEF rights prospectus runs 250–300k characters of fund boilerplate, and
somewhere in it sits a risk factor about structured notes, a line about
preferred shares that "may not be listed" for their first 30 days, and an
expense table denominated in $1,000. Any of those can look like a bank note to
a substring matcher — and one of them did: RiverNorth's RIV rights offering was
dropped in Aug 2026 by the sentence *"Structured Notes Risks. The Underlying
Funds may invest in structured notes."*

Because that keep overrides everything, it demands real evidence — the topic
on the **cover page** *and* a mechanic of a live offer (over-subscription
privilege, primary subscription, record-date stockholders, transferable
rights). Measured first mention of the topic: RIV 350 chars in, NMCO 158; a
BDC common-stock ATM 9,632; a 7.00% notes offering 166,347.

**Shelf boilerplate doesn't count as a listing statement.** Every shelf's base
prospectus says *"Unless we inform you otherwise in the applicable prospectus
supplement, the debt securities will not be listed on any securities
exchange"* — including the shelf a $25-par preferred is taken down under. A
hedged occurrence is ignored; an unhedged one anywhere in the document still
drops the filing.

⚠ **A false skip is silent.** No post, no Discord error, no way to notice —
it looks exactly like a quiet day, and RIV went unnoticed for four days. A
false *keep* costs one LLM call and one skippable post. Tune towards keeping,
and make every signal describe *the security being offered* — never a risk
factor, an index definition, a portfolio holding, or another security class on
the same shelf.

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

# Recover anything filed while nothing was listening:
python edgar_poller.py --catchup --catchup-hours 8

# Dispatcher (reads whatever is in filings-inbox/):
NVIDIA_API_KEY=nvapi-... DISCORD_WEBHOOK=https://... python openrouter_dispatch.py
```
