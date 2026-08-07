## Imported Claude Cowork project instructions

# sec-analyst — Project Context & Handoff

This document is a complete handoff brief for the **sec-analyst** project (GitHub repo: `ChrisVrj/sec-analyst`, public). It is meant to be pasted into a new Cowork project (or given to any new Claude session) so work can continue without re-deriving context. It covers the goal, the full architecture, every file's role, the exact bugs that were hit and fixed, the current open issues, and ideas for what's next.

---

## 1. What this project is and why it exists

Chris is a fixed-income trader specializing in **preferred stocks, baby bonds, exchange-traded debt, closed-end funds (CEFs), and business development companies (BDCs)**. These are niche, illiquid securities where material information (redemptions, new issuances, distribution changes, NAV updates) moves prices but isn't covered by mainstream financial news. SEC EDGAR is the primary source of truth for this information, but filings are dense legal/financial documents that take real time to read.

**The system's job:** continuously watch SEC EDGAR for new filings from a tracked list of companies/funds, pull the actual filing text (not just the cover page), have a free LLM summarize it through a fixed-income-trader lens, and push that summary to a Discord channel (`#sec-filings`) in near real time — so Chris can react to material events within minutes of them being filed, without reading every filing himself.

**Hard constraints that shaped every design decision:**
- **Must be free.** No paid hosting, no paid LLM API. Runs entirely on GitHub Actions (free for public repos) + OpenRouter's free-tier models + a Discord webhook (free).
- **Must not run on Chris's own PC.** It needs to run unattended, on a schedule, in the cloud.
- **Needs near-real-time coverage during specific hours**, not 24/7 (see schedule section) — to conserve OpenRouter free-tier rate limits and keep GitHub Actions usage sane.
- **Signal over noise.** Early versions flooded Discord with irrelevant filings (Form 4 insider trades from nobody, structured notes from banks that aren't even listed). The system actively filters these out before they ever reach the LLM.

---

## 2. High-level architecture / pipeline

```
GitHub Actions (cron, every 5 min, 24/7)
        │
        ▼
[Step: Check trading window in bash] ──(outside window)──► exit, do nothing (cheap)
        │ (inside window)
        ▼
[Loop for ~4 minutes, polling every 30s]
        │
        ├──► edgar_poller.py --once
        │       1. Fetch EDGAR's "current filings" Atom feed (last 100 filings, all companies)
        │       2. Filter to CIKs in cik_map.json (the watchlist)
        │       3. Skip anything already in seen_accessions.json
        │       4. For new matches: fetch the actual filing index page, find the
        │          primary document + EX-99.x exhibits, fetch + strip HTML from each
        │       5. Write a JSON payload per filing to filings-inbox/*.json
        │       6. Record accession in seen_accessions.json (persisted between runs
        │          via GitHub Actions cache)
        │
        └──► openrouter_dispatch.py   (only runs if filings-inbox/ has files)
                1. For each JSON file in filings-inbox/:
                     a. Run prefilter.should_skip() — drop noise without an LLM call
                     b. If not skipped: call OpenRouter (with model fallback chain)
                        using a fixed-income-analyst system prompt
                     c. Post the LLM's summary to the Discord webhook
                     d. Move the JSON file to filings-inbox/processed/
                2. Record dispatched accessions in dispatched_accessions.json
                   (also cached between runs) so restarts never double-post
```

This whole loop (poll → filter → summarize → post) repeats every 30 seconds for as long as the GitHub Actions job is inside a trading window. Each job has a 10-minute timeout and self-limits to ~4 minutes of looping, so a new job effectively picks up every ~5 minutes where the last one left off (state survives via the two cached JSON files).

---

## 3. File-by-file reference

All files live at the repo root unless noted.

### `edgar_poller.py`
The watcher. Two modes:
- `--once` — poll a single time and exit. **This is the only mode GitHub Actions uses.**
- (no flag) — infinite loop, polling every `POLL_INTERVAL` (60s) forever. This was the original local-dev mode before the project moved to GitHub Actions; it's kept for local testing but isn't used in production.

Key internals:
- `USER_AGENT = "OpenClaw SEC Monitor chrisdoesdocu@gmail.com"` — SEC EDGAR requires a descriptive User-Agent with a contact email on all requests, or it will block you. **Do not remove or genericize this.**
- `edgar_is_open()` — returns False on weekends, on a hardcoded list of federal holidays (2026–2027 dates baked in — needs updating for 2028+), and outside 6 AM–10 PM Eastern Time. This is a *safety net*, not the primary scheduling mechanism (see §6).
- `load_watchlist()` — reads `cik_map.json` and returns `{cik_without_leading_zeros: TICKER}`. Supports two JSON shapes: `{"TICKER": "0001234567"}` or `{"TICKER": {"cik": "0001234567"}}`.
- `fetch_recent_filings()` — hits `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&dateb=&owner=include&count=100&output=atom`, which is SEC's global "last 100 filings across all companies" Atom feed. Parses out accession number, CIK, entity name, form type, file date, and the filing index URL from each entry.
- **Document extraction (this was the subject of a major bug fix — see §7):**
  - `fetch_filing_text(accession, cik)` builds the EDGAR filing index URL, fetches it, then calls `extract_document_urls()` to parse the actual filing's document table.
  - `extract_document_urls()` parses the HTML `<table>` of documents in the index page. It identifies the **Seq=1 document as the primary filing document**, and separately collects any **EX-99.x exhibit URLs** (these are where 8-K press-release content actually lives — the primary 8-K document itself is often just a one-paragraph cover page that says almost nothing).
  - It fetches the primary doc + up to 2 EX-99.x exhibits, strips HTML tags from each (`strip_html()`, capped at 300,000 characters per document — raised from an original 80,000 cap that was truncating content), and joins them with `\n\n---\n\n` separators.
  - `_is_valid_doc_href()` filters out SEC navigation links, XBRL/XML/image/CSS/JS files, and anything that isn't a real `.htm`/`.html`/`.txt` filing document. It also strips the `/ix?doc=` prefix that wraps iXBRL-viewer links.
- `write_filing_payload()` — writes the final JSON to `filings-inbox/{accession}.json` with fields: `ticker`, `accession`, `cik`, `entity_name`, `form_type`, `file_date`, `filing_url` (the EDGAR index URL — this becomes the "Link:" in the Discord post), `filing_text` (the concatenated stripped text), `detected_at_utc`.
- `seen_accessions.json` — a flat list of every accession number the poller has ever seen, capped at the most recent 10,000. Prevents re-fetching/re-queuing the same filing across runs. **This file must be cached between GitHub Actions runs (it is — see poll.yml) or the system will re-process every filing on every run.**

### `prefilter.py`
Pure noise-reduction, runs **before** any LLM call (LLM calls are the scarce/rate-limited resource). Added specifically because two categories of filings were drowning out everything that mattered:

1. **Form 3/4/5 (insider ownership filings).** Public companies generate dozens of these a day from routine executive stock grants/sales — almost never trading-relevant. The filter **skips all Form 3/4/5 filings unless the filer name matches a tracked activist** in `ACTIVIST_FILERS` (currently: Saba Capital, Boaz Weinstein, Bulldog Investors, Phillip/Phil Goldstein, Karpus Management/Investment, 1607 Capital, City of London Investment, Sit Investment Associates, Relative Value Partners, Western Investment, RiverNorth Capital/Funds, Almitas Capital, Ancora Holdings/Advisors, Wynnefield Capital, 180 Degree Capital, Source Capital, Bandera Partners, Lazard Asset Management, Matisse Capital). These are funds known for activist campaigns in CEFs/BDCs (tender offer pressure, board fights, etc.) — when *they* file a Form 4, it's often a precursor to a catalyst. Matching is case-insensitive substring match against `entity_name + filing_text`.
2. **424B*/FWP (prospectus supplements / free writing prospectuses) for unlisted retail structured products.** Big banks (Citi, BofA, JPMorgan, RBC, etc.) file enormous numbers of these for products like autocallable notes, buffer notes, market-linked notes — almost always **unlisted** (no exchange ticker, $1,000 denomination, sold retail/private-bank channel). These are not tradeable by Chris and were previously generating generic, useless summaries. The filter drops a 424B/FWP filing if its text contains structured-note language (`autocallable`, `contingent coupon`, `buffer note`, `barrier note`, etc.) or explicit unlisted language (`"will not be listed on any exchange"`, `"we do not intend to list"`, etc.) — **unless** the filing also explicitly states it will list on NYSE or NASDAQ (`LISTED_SIGNALS`), in which case it's always kept regardless of other signals. This listed-override exists because some legitimately tradeable exchange-listed preferred/baby-bond offerings could otherwise share boilerplate language with structured notes.

`should_skip(filing) -> (bool, reason)` is the single entry point `openrouter_dispatch.py` calls. Returns `(True, "<reason>")` to skip, `(False, "")` to proceed to the LLM. Skipped filings are moved straight to `filings-inbox/processed/` with a `skip_` filename prefix and logged — they never count against the OpenRouter rate limit and never hit Discord.

**⚠️ Precedence bug fixed Aug 2026 — do not reinstate the old ordering.** `is_unlisted_offering()` used to check `LISTED_SIGNALS` *first* and return early on a match. An RBC Nasdaq-100 buffer note (accession `0000950103-26-011945`, $1,000 par, explicitly unlisted) reached Discord because the loose signal `"listed on the nasdaq"` matched the **Nasdaq-100 Index's own definition** — *"100 of the largest non-financial companies listed on The Nasdaq Stock Market"* — inside the underlier description. That forced a keep despite the filing stating twice that the notes **will not be listed**. Current precedence:
1. An explicit `UNLISTED_SIGNAL` skips the filing outright and is **not** overridable — "the notes will not be listed on any securities exchange" has no benign reading.
2. Otherwise structured-note vocabulary skips it, *unless* a `LISTED_SIGNAL` says this offering will list (the escape hatch for exchange-traded baby bonds sharing payoff vocabulary).

Every `LISTED_SIGNALS` entry must describe **the security being offered**, not merely mention an exchange — prefer `"apply to list"` / `"approved for listing"` / `"under the symbol"` phrasings. Bare `"listed on the nasdaq"` and `"listed on the new york stock exchange"` were removed for this reason. `STRUCTURED_NOTE_SIGNALS` also gained the payoff vocabulary that filing used (`participation rate`, `upside participation`, `buffer amount`, `buffer level`, `underlier`, `initial/final underlier level`, `structured note`), any one of which would have caught it independently. Covered by `test_pipeline.py`.

**This file is meant to be tuned over time.** If Chris notices a relevant activist fund or a relevant exchange-listed issuer being filtered out, or noise getting through, the lists at the top of `prefilter.py` are the place to edit. All matching is simple lowercase substring matching — no regex needed for the activist list, though the signal lists do use plain substrings too (not regex).

### `openrouter_dispatch.py`
The summarizer + Discord poster. Reads every `*.json` in `filings-inbox/`, in filename-sorted order.

**The filename is historical and misleading — as of Aug 2026 this module is provider-agnostic and calls NVIDIA first.** It was deliberately *not* renamed: `.github/workflows/poll.yml`, `README.md` and this document all reference it, and the live workflow is the one file in the project that must not break. If you rename it, update all four places plus the duplicate copy under `SEC Analyst/`.

- **Provider chain (Aug 2026 — migrated off OpenRouter-primary).** `PROVIDERS` is an ordered list of dicts; `ATTEMPTS` flattens it into `(provider, model)` pairs tried in order:
  1. **NVIDIA NIM** — `https://integrate.api.nvidia.com/v1/chat/completions`, key `NVIDIA_API_KEY` (`nvapi-...`), OpenAI-compatible. Models: `nvidia/nemotron-3-super-120b-a12b` → `nvidia/nemotron-3-ultra-550b-a55b` → `nvidia/nemotron-3-nano-30b-a3b`. Free tier ≈ **40 req/min per model with no published daily cap** — this is the reason for the migration; OpenRouter's free tier caps at ~200 req/day, which was the binding constraint. 2s sleep between calls.
  2. **OpenRouter** — fallback only, key `OPENROUTER_API_KEY`. Chain now leads with `openai/gpt-oss-120b:free` (empirically the most available, per bug #4 below) rather than the Meta model. 4s sleep between calls.
- **A provider with no key is dropped from `ATTEMPTS` entirely**, so deleting a GitHub secret is the supported way to disable a provider — do not add code to "turn off" OpenRouter.
- **Sleep is per-provider, not global.** `dispatch()` returns the number of seconds to sleep (the serving provider's `sleep` value, or `0` when the filing was pre-filtered), and `main()` sleeps that long. The old global `SLEEP_BETWEEN_CALLS` constant is gone; `DEFAULT_SLEEP` is only used on the total-failure path.
- **Error taxonomy drives fallback granularity:**
  - `400`/`404` → `_ModelUnavailableError` → try the **next model**.
  - `401`/`403` → `_ProviderUnavailableError` → skip **every remaining model on that provider**. This exists because an expired NVIDIA key would otherwise waste three round-trips per filing failing identically.
  - `429`/`5xx` → retry once after 3s, then next model.
- **⚠️ NVIDIA API keys expire ~6 months after issue.** When that happens the dispatcher logs the auth failure, drops the NVIDIA chain, and keeps running on OpenRouter — the feed does *not* go dark, so **the failure is silent unless someone reads `dispatch.log`**. Regenerate at `build.nvidia.com/settings/api-keys`. This is a real "it'll break in six months and nobody will notice" trap; see §9.
- **Nemotron 3 is a reasoning model — this is the one genuinely new failure mode the migration introduced.** By default it emits chain-of-thought. The request sends top-level `chat_template_kwargs: {"enable_thinking": false}` to suppress it. Two safety nets behind that:
  - `strip_reasoning()` removes any `<think>...</think>` block that leaks through, and handles the *unterminated* case (thinking that ran past `max_tokens` leaves an opening tag with no closer — truncate there). Without this, a leaked scratchpad would eat the 1900-char Discord budget and dump the model's reasoning into `#sec-filings`.
  - If `content` comes back empty but `reasoning_content` is populated, the answer is salvaged from `reasoning_content` rather than alerting on an empty response.
  - If a model rejects `chat_template_kwargs` with a 400, `call_model()` retries **the same model once without the extra param** before declaring it unavailable — otherwise one unsupported param would burn the entire chain.
- **Empty content now raises instead of alerting.** Previously an empty response posted a ⚠️ alert immediately; now it raises inside `_post_chat`, which falls through to the next model. Only exhausting the whole chain produces the ❌ Discord alert.
- **The system prompt** is the most-iterated part of this whole project (see §7 for the history of why). The current version (as of the latest commit) instructs the LLM to:
  - Identify the single highest-priority event type in the filing (redemption > new listed issuance > M&A/change-of-control > tender/exchange offer > distribution change > CEF/BDC NAV update > other) and lead with it.
  - Always write a line-1 headline with an emoji (mapped per event type), ticker, form, date.
  - Always write a "Company:" line.
  - **Conditionally include a "highlight block"** (Discord markdown `##` header + a `>` blockquote with a **verbatim quote** from the filing) — but only for priority 1–4 events, and only if literally stated in the text. The prompt explicitly forbids fabricating these quotes — "if unsure, omit."
  - Fill in one of several structured body templates depending on filing type (CEF/BDC NAV with full balance-sheet figures and prior-period comparisons, new issuance with coupon/par/maturity/call/listing/use-of-proceeds, redemption with price/date, distribution with current-vs-prior, M&A with treatment of preferreds/baby bonds, or a generic 2–4 sentence "OTHER" fallback).
  - Always end with `Link: <EDGAR URL>` and `Accession: <number>` — **never omit these**, since the Discord message is often the *only* place Chris sees a filing and he needs to click through to verify.
  - Stay under 1800 characters total (Discord's hard limit is 2000; `MAX_DISCORD_CHARS = 1900` is also enforced in code as a hard truncation safety net after the LLM responds).
  - Never fabricate figures/dates/tickers — use "n/d" for undisclosed fields.
- **`build_user_message()`** sends the LLM: ticker, form type, filed date, entity name, accession, CIK, EDGAR URL, then the full filing text (`MAX_TEXT_CHARS = 400_000`, truncated with a `"...(truncated)..."` marker if longer — in practice the poller's 300k-per-document cap means this rarely triggers, but it's a safety net for filings with many exhibits).
- **Discord posting (`post_discord`)** — POSTs `{"content": <text>}` as JSON to the webhook URL. **Critically uses `User-Agent: "DiscordBot (https://github.com/ChrisVrj/sec-analyst, 1.0)"`** — Discord's Cloudflare WAF blocks the default Python `urllib` User-Agent string with HTTP 403 / Cloudflare error code 1010. This single header was the root cause of a multi-day "0 messages delivered after 100+ runs" outage (see §7) and must never be changed back to a generic/default User-Agent.
- **Message assembly — `finalize_message()` (added Aug 2026).** The `Link:` / `Accession:` footer is built from the filing payload, **not** written by the model, and the prompt now explicitly forbids the model from writing one. Two production failures motivated this:
  1. A BGT `N-CSR/A` summary ran long and the old blind `summary[:1900]` cut it off mid-word (`"Does not reflect deri"`), **taking the footer with it** — leaving a post with no way to reach the filing, which is the one thing every post must carry.
  2. A model-written URL can simply be wrong; the payload's URL came from EDGAR and is correct by construction.

  Order of operations: strip any model-written footer → `trim_long_lines()` → fit the body to `MAX_DISCORD_CHARS` minus the footer → append the real footer. `fit_to_budget()` cuts on a line boundary where possible, else a word boundary, never mid-word. The URL is wrapped in `<...>` to suppress Discord's link-preview embed and keep the channel scannable.
- **`trim_long_lines()`** caps `**Label:** value` lines at `MAX_FIELD_LINE` (240 chars) and prose lines at `MAX_PROSE_LINE` (420). The same BGT post contained a single ~1,400-character "field" that was raw prospectus boilerplate copied out of the filing. **Blockquote (`>`) lines are exempt** — those are the verbatim highlight quotes and are supposed to be long.
- **Priority routing (added Aug 2026).** `classify_priority()` maps a summary to tier 1–4 (redemption / public listing / M&A-COC / tender-exchange — exactly the tiers the prompt allows a `##` highlight block for) or 0 for routine. Detection uses **only** the `##` header lines and the line-1 emoji, never a substring search over the body: a NAV report saying "redemption of shares at net asset value" in passing must stay routine, and a whole-summary search would have made it urgent. Tier 1 is checked first so a merger that also redeems preferreds classifies as a redemption. Routing is opt-in via two settings, both no-ops when unset: `DISCORD_WEBHOOK_URGENT` (urgent posts go there *instead of* the main channel, keeping it pure signal for push notifications) and `DISCORD_URGENT_MENTION` (`@here` or `<@&ROLE_ID>`, prepended). **If the urgent webhook fails, `send_discord()` retries on the main webhook** — a dedicated channel must never be the reason an urgent filing is lost.
- **Direct document links (added Aug 2026).** `filing_url` is EDGAR's *index* page — a table of contents, one click away from any actual content. `edgar_poller.fetch_filing_documents()` now returns `(text, doc_urls)` and the payload carries `primary_doc_url` (+ `exhibit_urls`), so the footer leads with `Document: <...>` and keeps `Index: <...>` as the route to exhibits the poller didn't fetch. `build_footer()` falls back to the old single `Link:` line when `primary_doc_url` is absent, so payloads written before this change still work. `fetch_filing_text()` is retained as a text-only wrapper.
- **Error handling philosophy:** if the LLM call fails entirely (all models exhausted) or returns empty content, the dispatcher doesn't just log and drop the filing — it posts a `❌`/`⚠️` **alert to the same Discord channel** with the ticker, form type, EDGAR link, and accession number, so Chris always knows a filing needs manual review even when automation fails. The filing JSON is still moved to `processed/` (prefixed `err_`) so it isn't retried forever.
- **Rate limiting:** `SLEEP_BETWEEN_CALLS = 4` seconds between actual LLM calls (not between filings — pre-filtered/skipped filings don't sleep). This keeps the request rate around ~15/min, safely under OpenRouter's free-tier ~20/min cap. `dispatch()` returns a bool indicating whether an LLM call was actually attempted, and the main loop only sleeps when that's true.
- **Idempotency:** `dispatched_accessions.json` (capped at 10,000, persisted via GitHub Actions cache) ensures a filing already posted to Discord is never posted twice, even across job restarts. If a duplicate is somehow found sitting in the inbox, it's moved out with a `dup_` prefix without any LLM call or Discord post.

### `cik_map.json`

**🔴 There must only ever be ONE of these, at the repo root.** From ~Mar–Aug 2026 a second copy existed at `SEC Analyst/cik_map.json`, and `manage_watchlist.py` (which lived in that folder and resolved its path as `os.path.join(HERE, "cik_map.json")`) wrote every edit into it. The poller resolves `BASE_DIR / "cik_map.json"` where `BASE_DIR = GITHUB_WORKSPACE` = **the repo root**, so it never read that file. Consequence: commit `3d01329` "Add watchlist tickers (EARN, NHP, FBYD, BMNR, LILA, TPZ); relabel BK→BNY, BCIC→PTMN" **silently never took effect** — those six tickers were unmonitored for roughly seven weeks and the two relabels never applied. Fixed Aug 2026 by merging the newer copy into the root file (686 tickers), deleting the duplicate, moving `manage_watchlist.py` to the repo root, and anchoring its path there. If you ever find yourself adding a second copy of a state or config file, don't.

The watchlist. Maps tickers to CIK numbers — this is what determines which companies/funds the poller even looks at out of the hundreds of filings EDGAR processes per polling cycle. **This file is the single most important lever for what the system covers.** Format is `{"TICKER": "0001234567"}` (10-digit CIK, leading zeros optional — the code strips them). If this file is missing, the poller logs a warning and matches **every** filing on EDGAR (almost certainly not desired — effectively disables the watchlist filter). Chris maintains this list himself; it is not currently synced from any external source. *(Not read directly during this session — verify current contents/tickers in the repo before assuming what's covered.)*

### `.github/workflows/poll.yml`
The orchestrator. See §6 for the full scheduling story — this has been rewritten twice in this project already because of a GitHub-specific gotcha.

Current structure:
1. **`Check trading window`** step (id: `window`) — computes UTC time and day-of-week in bash, decides if "now" falls inside one of Chris's two trading windows, and sets a `proceed` output (`true`/`false`).
2. Every subsequent step (`Checkout`, `Set up Python`, both cache-restore steps, the poll/dispatch loop, and the log-upload step) carries `if: steps.window.outputs.proceed == 'true'` (the log upload also ANDs in `always()` so it still runs/skips correctly on failure paths). When outside the window, the job exits almost immediately after step 1 — costing essentially nothing.
3. Inside the window, the job: restores `seen_accessions.json` and `dispatched_accessions.json` from GitHub Actions cache, then runs a bash `while` loop for up to 240 seconds, calling `python edgar_poller.py --once` every iteration, checking if anything landed in `filings-inbox/`, and if so immediately running `python openrouter_dispatch.py`. Sleep between iterations is 30s (or less near the end of the 4-minute window). Logs (`edgar_poller.log`, `dispatch.log`) are uploaded as a workflow artifact (7-day retention) for debugging.
4. **Secrets/vars consumed:** `NVIDIA_API_KEY` (GitHub secret — primary provider), `OPENROUTER_API_KEY` (GitHub secret — fallback provider, optional), `DISCORD_WEBHOOK` (GitHub secret — currently hardcoded as a fallback was discussed but the canonical approach is the GitHub secret; see §7 for the saga around this), plus optional GitHub Actions *variables* `NVIDIA_MODELS` and `OPENROUTER_MODEL` for overriding either model chain. Defaults live in `openrouter_dispatch.py`, not in the workflow.

**The cron trigger itself is intentionally `*/5 * * * *` (every 5 minutes, all day, every day) rather than a set of narrow time-window cron expressions.** This looks wasteful but isn't — see §6, it's the actual fix for a real GitHub Actions reliability bug.

---

## 4. Trading windows / schedule (current target)

Chris is based in **Sofia, Bulgaria**. Windows are defined and evaluated in **Sofia local time** (Aug 2026 rewrite — previously UTC minute constants):

| Window | Sofia local | ET equivalent | Notes |
|---|---|---|---|
| Day | 13:00 – 18:00, Mon–Fri | 06:00 – 11:00 ET | Opens on EDGAR's first filing minute |
| Night | 23:00 – 03:00, Mon–Fri evenings | 16:00 – 20:00 ET | US after-hours 8-K flow |

The night window deliberately spans midnight and is valid **Tue–Sat** for its 00:00–03:00 half, because that half belongs to the *preceding* weekday's evening. Saturday 00:00–03:00 is Friday's US after-hours session (wanted); Monday 00:00–03:00 would be Sunday's (not wanted, and excluded).

**Why the day window starts at 13:00 and not earlier.** EDGAR only accepts filings **06:00–22:00 ET**, and Sofia 13:00 *is* 06:00 ET in both aligned DST regimes — the earliest minute at which a filing can exist. The window was 11:00–16:00 until Aug 2026, which spent its first two hours (Sofia 11:00–13:00 = 04:00–06:00 ET) polling a system that wasn't accepting filings: `edgar_is_open()` short-circuited every cycle and the loop idled. Moving to 13:00–18:00 kept the same 5-hour runtime but made all of it productive — **35 → 45 productive hours/week for identical cost**. Do not move the start earlier; there is nothing there to find.

During the 2–3 weeks each spring/autumn when US and EU clocks are out of step, Sofia 13:00 lands on 07:00 ET rather than 06:00 — still live, just an hour into the day. Not worth compensating for.

**DST is no longer a manual step.** All window arithmetic happens in Sofia local time via the runner's tzdata, so the EEST/EET switch needs no edits. A sanity check asserts the offset is `+0300` or `+0200` and emits a GitHub `::warning::` otherwise — if tzdata ever vanished from the runner image, `TZ=` silently yields UTC and every boundary would shift 2–3 hours, which is exactly the kind of silent breakage this project keeps getting bitten by.

---

## 5. Secrets, config, and external accounts involved

- **GitHub repo:** `ChrisVrj/sec-analyst`, public (this is what makes GitHub Actions free/unlimited-minutes).
- **GitHub Secrets** (Settings → Secrets and variables → Actions → Secrets): `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, `DISCORD_WEBHOOK`. `DISCORD_WEBHOOK` **plus at least one** of the two API keys are required, or `openrouter_dispatch.py` exits with `SystemExit(1)` at startup.
- **GitHub Actions Variables** (same location, "Variables" tab): `NVIDIA_MODELS` and `OPENROUTER_MODEL` — both optional comma-separated / single-model overrides.
- **NVIDIA account** (`build.nvidia.com`, free NVIDIA Developer Program, no credit card): the primary LLM provider since Aug 2026. Free-tier inference on Nemotron 3, ~40 req/min per model, no published daily cap, $0 cost. **Keys expire roughly 6 months after issue** and are regenerated at `build.nvidia.com/settings/api-keys`. Rate limits are explicitly documented as best-effort, not an SLA — throttling below 40 RPM is possible under load, which is exactly what the OpenRouter fallback is there to absorb.
- **OpenRouter account:** now the *fallback* provider. Free-tier models only, $0 cost. Chris raised the account's spend limit to $3/week purely to stop a spurious 402 error — actual spend should always be $0 since only `:free` models are used. "Last Used" showing "Never" on the OpenRouter dashboard was a red herring during debugging — check `dispatch.log` / workflow run logs instead of trusting that dashboard field for activity confirmation. Since the migration this dashboard will legitimately show little to no activity, because NVIDIA serves nearly everything.
- **Discord:** a webhook URL for the `#sec-filings` channel. **This URL is a secret** — anyone with it can post to the channel. It has been rotated at least once already after troubleshooting confusion (see §7). If asked to "hardcode" it anywhere, push back gently — it should live in GitHub Secrets, not in committed code, for the same reason any other credential shouldn't be committed.
- **SEC EDGAR contact email:** `chrisdoesdocu@gmail.com`, baked into `USER_AGENT` in `edgar_poller.py`. SEC's fair-access rules require a working contact in the User-Agent string on all automated requests; don't strip this.

---

## 6. The GitHub Actions scheduling bug (important — don't regress this)

**Symptom:** with a `poll.yml` that used several precise `cron:` entries meant to fire every 5 minutes inside the two trading windows, the actual observed runs (visible in the Actions tab) were **1–2+ hours apart**, not 5 minutes apart — despite the workflow itself succeeding every time it did run.

**Root cause:** GitHub silently throttles/deprioritizes `schedule:`-triggered workflow runs on repositories GitHub's scheduler considers low-activity. This is undocumented behavior (GitHub's docs only officially admit to "up to 15 minutes" of delay under high platform load), but it's a widely-reported community issue, and it was clearly happening here — narrow multi-entry cron schedules were being skipped wholesale for long stretches.

**Continuous-coverage rewrite (Aug 2026).** The job no longer loops for a fixed ~4 minutes. It now loops **until the current window closes** (up to 5h; GitHub's per-job ceiling is 6h, `timeout-minutes: 320`), polling every **15 seconds**. This removes the ~30s of blindness that used to occur at every run boundary, plus GitHub's scheduling jitter on top of it. Three pieces make it work together:

1. `cron: '*/5 * * * *'` stays — its job is keeping GitHub's scheduler warm (see below), not triggering the actual work.
2. `concurrency: {group: edgar-poller, cancel-in-progress: false}` — GitHub permits at most one **running** plus one **pending** run per group, and each new trigger replaces the pending one. So the `*/5` ticks during a live window can't double-post, and a replacement run is always parked ready to take over within seconds if the long run dies.
3. The window step emits `run_seconds` alongside `proceed`; the loop uses it as its deadline.

**Expected and harmless:** while a long run is live, the Actions tab fills with **cancelled runs that never executed a step** (~60 per day window) — that is the pending-slot mechanism working. The run that matters is the single long-lived one.

**Known limitation of long jobs:** `actions/cache` only writes in its post-job step, so `seen_accessions.json` / `dispatched_accessions.json` are persisted once, at window close. If a job is killed mid-window that state is lost — but the blast radius is bounded, because the poller only ever sees EDGAR's last-100-filings feed, so at worst a couple of recent filings get re-posted rather than a whole window's worth.

**⚠️ CORRECTION (Aug 2026) — the `*/5` cron did NOT fix the throttling.** This section previously claimed it did. Measured from the actual run history on 2026-08-06/07, consecutive triggers were **174, 139, 98, 134, 102, 92, 348, 179 and 104 minutes apart** — a cadence of 1.5–3 hours, never 5 minutes. Run *numbers* are consecutive across those gaps, so the runs are not merely delayed, **they are never created**. Corroborating arithmetic: the repo has ~1,218 total workflow runs after ~4 months; a true `*/5` cadence would have produced ~34,000. Assume the cron fires roughly every two hours and design for that. Do not add more cron entries hoping to compensate — narrow crons were what got throttled hardest in the first place, and `*/5` is still the best backstop available.

**Consequence, and the fix: pre-window arming.** With a ~2h trigger cadence, a window is covered only if a trigger happens to land inside it. On 2026-08-06 none landed between 21:39 and 03:27 and **the entire night window was missed**. So a trigger landing shortly *before* a window now holds the runner and opens with the window instead of exiting:
- Look-ahead `MAX_WAIT_SECONDS` = 2h, matched to the observed cadence.
- `MAX_TOTAL_SECONDS` = 5h45m covers wait + polling combined, against the 6h job ceiling (`timeout-minutes: 350`).
- A wait that would leave under 30 min of polling is not worth a runner, so it skips.
- Replayed against the real 2026-08-06 triggers: 12:14 arms and covers the full 13:00–18:00, and 21:39 arms and covers the full 23:00–03:00.

**Cost of arming:** the job holds a hosted runner while idle, up to 2h. Public-repo minutes are unlimited so this is free, but it is genuinely idle capacity — keep the look-ahead proportionate and don't extend arming to cover the whole day.

**What arming cannot fix:** runs #1220 and #1221 both died after exactly 15m02s with `The job was not acquired by Runner of type hosted even after multiple attempts` plus an `Internal server error` correlation ID. That is GitHub failing to allocate a runner, entirely server-side, and no workflow change addresses it. The mitigation is the `*/5` backstop plus the parked pending run — both of which need GitHub's scheduler to be working at all.

**The original (still-valid) reasoning for the single cron:** replace all narrow cron entries with a **single `*/5 * * * *` entry that fires every 5 minutes, all day, every day** (not just during trading windows). A high-frequency, simple, constantly-firing cron is treated by GitHub's scheduler as "active" and gets honored reliably. The actual "should we do real work right now" decision moved **out of the cron expression and into a bash time-window check inside the job** (the `Check trading window` step described in §3). Outside the windows, the job still fires every 5 minutes but does almost nothing and finishes in seconds — which costs nothing on a public repo's unlimited Actions minutes.

**Manual runs bypass the window check (added Aug 2026).** The step's first branch is `if [ "$GITHUB_EVENT_NAME" = "workflow_dispatch" ]` → `PROCEED=true`. Without it, a manual "Run workflow" fired outside a Sofia window skips every step and produces a green, completely inert run — which reads as success but polls nothing and posts nothing, and cost real debugging time at least once. `edgar_poller.py`'s own `edgar_is_open()` (6 AM–10 PM ET, weekdays, non-holiday) remains as a second guard on manual runs, so a manual trigger at 3 AM ET still polls nothing — it just logs "EDGAR is closed right now" instead, which is at least diagnosable.

**A subtlety that was caught and fixed within this same change:** the first draft of the window-check used `exit 0` inside the bash step to "skip the rest of the job." **This does not work** — in GitHub Actions, `exit 0` (or any exit code) only ends that one step; subsequent steps still run regardless. The corrected version sets a step **output** (`echo "proceed=$PROCEED" >> "$GITHUB_OUTPUT"`) and every later step carries an explicit `if: steps.window.outputs.proceed == 'true'` condition. **If anyone "simplifies" this back to an `exit 0` pattern, the window check will silently stop working** (the job will fully execute on every single 5-minute tick, 24/7, burning OpenRouter rate limit and posting Discord noise/alerts outside trading hours).

---

## 7. History of bugs hit and fixed (so they aren't reintroduced)

This project went through a long, painful debugging arc. Recording it so a future session (or a future "let's optimize this" pass) doesn't undo a fix without realizing why it's there.

1. **Discord HTTP 403, zero messages delivered across 100+ workflow runs.**
   Root cause: Discord's API sits behind Cloudflare, and Cloudflare's WAF blocks the default `Python-urllib/3.x` User-Agent header outright (Cloudflare error code 1010 — an IP/client-signature block, not an auth problem). It looked like an auth/webhook problem for a long time because the symptom (403) is the same surface error you'd see from a bad/rotated webhook URL — which led to a lot of wasted effort regenerating Discord webhooks and bots that weren't the actual problem. **Diagnosed by adding a `curl` step to the workflow that POSTed directly to the webhook** — curl got HTTP 204 (success) while the Python script got 403 on the exact same URL in the exact same job, which proved conclusively it was a client-signature issue, not a credentials issue. **Fix:** set `"User-Agent": "DiscordBot (https://github.com/ChrisVrj/sec-analyst, 1.0)"` on the Discord POST request. Do not remove this header or revert it to a default urllib UA.
   *(Side effect of the diagnostic curl step: it was left in the workflow afterward and ended up posting a literal "test from github actions" message to the live Discord channel on every single run, every 5 minutes during active windows — flooding the channel with noise. It has since been removed from `poll.yml` entirely.)*

2. **Filing summaries were generic/identical regardless of filing content.**
   This had two compounding root causes, discovered progressively:
   - **2a.** `extract_document_url`-style logic was sometimes picking up a generic SEC navigation link (e.g. a homepage `/index.htm` link present on every EDGAR page) instead of the actual filing document, before document-table parsing was added. Fixed by requiring hrefs to either be relative or contain `/archives/edgar/data/` to be considered valid filing documents (`_is_valid_doc_href`).
   - **2b. (Bigger issue.)** Even once the correct document was found, the system was only fetching the **primary document** of the filing — which, for an 8-K, is frequently just a one-paragraph cover page ("see attached exhibit") with **none of the actual press-release content**. The real substance lives in **EX-99.x exhibits**, which were never being fetched at all. Compounding this, `strip_html()` capped extracted text at **80,000 characters**, which (combined with only fetching one document) meant the LLM was sometimes working from almost nothing. **Fix:** `extract_document_urls()` was rewritten to parse the EDGAR filing index's document `<table>`, identify the Seq=1 primary document *and* any EX-99.x exhibits, fetch up to 3 documents total (primary + 2 exhibits) and concatenate them, and the per-document character cap was raised from 80,000 to 300,000.
   - Chris's own diagnosis mid-investigation, verbatim: *"I think it doesn't feed the filing itself but the content of this page [the -index.htm page]. Because every summary is the same."* — this turned out to be exactly right and directly motivated fix 2b.

3. **LLM returning dismissive non-answers (e.g. "No fixed-income impact") even when the filing clearly contained relevant content** (a specific case: a TWO/Two Harbors-style merger filing with explicit preferred-stock redemption language got summarized as having no fixed-income impact). Root cause: an earlier system prompt revision had given the LLM an explicit escape hatch to write a one-line dismissal when it judged content irrelevant — the model was over-using that escape hatch. **Fix:** removed the dismissal option entirely; the prompt now mandates a full structured summary for every filing, every time, with the "OTHER" template as the only fallback (still 2–4 sentences of real content, never a dismissal).

4. **Persistent OpenRouter 429s on the top of the fallback chain** (`llama-3.3-70b-instruct:free`, `gemma-4-31b-it:free`). These are **upstream provider-side rate limits** (Venice/Google AI Studio backing those specific free endpoints), not something fixable from this codebase. Handled, not fixed: the model fallback chain absorbs it — `openai/gpt-oss-120b:free` has empirically been the most reliably available model and ends up serving most requests in practice.

5. **A spurious OpenRouter 402 "spend limit" error** despite using exclusively `:free` models. Worked around by Chris raising the OpenRouter account's weekly spend cap to $3 (which should never actually be charged against, since free models cost $0 — this was a defensive/placebo fix more than a root-cause fix; if 402s recur, look at OpenRouter account-level settings, not the code).

6. **Confusion around the Discord webhook secret not "sticking"** in GitHub's secret-edit UI (the value field always appeared empty when reopened — this is actually **normal, expected GitHub behavior**: secret values are never redisplayed once saved, by design, for security. It looked like a bug but wasn't one). This caused multiple unnecessary webhook rotations and bot recreations before the *real* problem (the User-Agent/Cloudflare issue in #1) was found.

7. **The GitHub Actions schedule-throttling issue** — see §6 in full above.

8. **A leftover test fixture** (`filings-inbox/test_999999.json`), originally created via PowerShell `Out-File` to manually exercise the dispatcher without waiting for a real filing. PowerShell's `Out-File` writes a UTF-8 **BOM** (byte-order mark) by default, which broke `json.loads()` when the dispatcher tried to read it. Not worth re-fixing generally (the dispatcher doesn't need BOM-tolerant JSON parsing for real EDGAR-sourced files, which are always clean UTF-8) — just don't create test fixtures this way again; use a heredoc or a Python one-liner instead, or `Out-File -Encoding utf8NoBOM` if PowerShell must be used.

---

## 8. Current system prompt (verbatim, for reference)

The full text is in `openrouter_dispatch.py` as the `SYSTEM_PROMPT` constant. Summary of its structure (don't duplicate-maintain this section vs. the code — treat the code as canonical and this as an index):
- Priority-ordering instructions (redemption > listed new issuance > M&A/COC > tender/exchange > distribution > CEF/BDC NAV > other).
- A strict output template: emoji+ticker+form+date headline → company line → optional verbatim-quote highlight block (only for priority 1–4, only if literally stated, never fabricated) → one of six structured body templates chosen by filing type → mandatory `Link:` and `Accession:` footer.
- An emoji legend tying specific emoji to specific event types (🚨 redemption, 📢 new listed issuance, ⚠️ M&A/COC, 🔁 tender/exchange, 💰 distribution raise, ✂️ distribution cut, 📊 CEF/BDC financials, 🏦 structured product [rare, pre-filter usually drops these], 📄 other prospectus, 📋 other/housekeeping, 👤 activist insider activity).
- Hard constraints: ≤1800 characters, Discord-flavored markdown only (`##`, `**`, `>`), never fabricate figures/dates/tickers (use "n/d" or drop the line), include prior-period comparisons for CEF/BDC NAV and distributions whenever disclosed.

If asked to revise this prompt again, **read the actual current `SYSTEM_PROMPT` text in the file first** rather than assuming this summary is complete — it's a long, detailed prompt and this section is intentionally compressed.

---

## 9. Known open items / good starting points for the next session

- **The NVIDIA key expires ~6 months after issue (so ~Feb 2027 if created Aug 2026), and the failure is silent** — the dispatcher falls back to OpenRouter and keeps posting, so nothing looks broken while the daily-capped fallback quietly becomes the primary. Worth a calendar reminder, or a cheap monitor: alert to Discord if `provider["name"] == "openrouter"` serves N filings in a row while `NVIDIA_API_KEY` is set.
- **The NVIDIA model IDs in `NVIDIA_MODELS` were not verified against a live key** when the migration was written — they came from NVIDIA's public docs. A retired/renamed ID 404s and falls through to the next entry, so a stale ID degrades rather than breaks, but if `dispatch.log` shows the whole NVIDIA chain 404ing, re-check IDs with `curl -H "Authorization: Bearer $NVIDIA_API_KEY" https://integrate.api.nvidia.com/v1/models`.
- **The output-quality delta from switching models is unmeasured.** The `SYSTEM_PROMPT` was tuned over many iterations against OpenRouter's Llama/gpt-oss models; Nemotron 3 may follow the strict Discord template differently (particularly the conditional highlight block and the "never fabricate / use n/d" rules). Worth eyeballing the first ~20 real posts before assuming parity, and re-tuning the prompt against Nemotron specifically if the template slips.
- ~~**DST transition isn't automated.**~~ **Fixed Aug 2026.** The window-check now reads `TZ=Europe/Sofia date +%z` from the runner's tzdata and picks the EEST (`+0300`) or EET (`+0200`) constant set itself, so the last-Sunday-of-October and last-Sunday-of-March edits are no longer needed. Window semantics are unchanged — both regimes still map to Sofia 11:00–16:30 and 23:00–01:30. An unexpected offset (tzdata missing from the runner image) emits a GitHub `::warning::` and falls back to EEST rather than silently shifting both windows by an hour.
- **`FEDERAL_HOLIDAYS` in `edgar_poller.py` only covers 2026–2027.** Needs extending before it runs out, or rewriting to compute holidays programmatically (e.g. via the `holidays` Python package) rather than hardcoding dates.
- **`cik_map.json` watchlist maintenance is manual.** No tooling currently exists to add/remove tickers, validate CIKs, or check for typos. Could be worth a small helper script (e.g. look up a ticker's CIK from SEC's own ticker-to-CIK JSON file at `https://www.sec.gov/files/company_tickers.json`) so Chris can add coverage by ticker symbol instead of hand-finding CIK numbers.
- **`prefilter.py`'s activist list and structured-note/listed signal lists are static and manually maintained.** Worth periodically reviewing against real Discord output — if Chris notices either false negatives (relevant filings dropped) or false positives (junk getting through), the fix is almost always editing these lists, not the matching logic itself.
- ~~**No automated tests.**~~ **Partly addressed Aug 2026:** `test_pipeline.py` is an offline, dependency-free regression suite (`python test_pipeline.py`), run automatically by `.github/workflows/test.yml` on any push touching a `.py` file. It covers the prefilter precedence rule, the `finalize_message()` footer/truncation guarantees, and `strip_reasoning()`. **Every case in it corresponds to a bug that actually reached Discord — add one whenever a bad post gets through.** Still uncovered: `edgar_poller.py`'s document extraction (`extract_document_urls()` returning a non-empty list for a known accession, and the primary-doc + EX-99.x concatenation), which is where bug #2 lived and is the most valuable remaining gap.
- **Logs are only retained 7 days** via the workflow artifact upload. If multi-week debugging is ever needed, that retention may need lengthening, though this trades off against GitHub's storage limits for public repos.
- **GitHub Secrets UI confusion (see bug #6)** will likely resurface if anyone other than Chris tries to update `DISCORD_WEBHOOK` or `OPENROUTER_API_KEY` later — worth just remembering that the empty-looking field after save is expected/normal, not a sign the update failed.

---

## 10. Quick reference

- **Repo:** `https://github.com/ChrisVrj/sec-analyst` (public)
- **Discord channel:** `#sec-filings`
- **SEC contact email (in User-Agent):** `chrisdoesdocu@gmail.com`
- **LLM provider:** NVIDIA NIM / Nemotron 3 (primary, free tier), OpenRouter free models (automatic fallback)
- **Trading windows (Sofia local):** 13:00–18:00 and 23:00–03:00, Mon–Fri (= 06:00–11:00 and 16:00–20:00 ET). Continuous polling every 15s for the whole window, one long job per window.
- **Core files:** `edgar_poller.py` (fetch+extract), `prefilter.py` (noise filter), `openrouter_dispatch.py` (summarize+post), `cik_map.json` (watchlist), `.github/workflows/poll.yml` (orchestration/schedule)
- **State files (must stay cached across Actions runs):** `seen_accessions.json`, `dispatched_accessions.json`
- **Queue directories:** `filings-inbox/` (pending), `filings-inbox/processed/` (done — prefixed `skip_`/`err_`/`dup_` to indicate why/how it left the queue)
