# oddsfeed: handoff guide for AI agents

Read this first, then README.md (user-facing details). This file covers how the
system works, how to change it safely, and what is still open.

## 1. What it is

A single Rust binary that collects **live football odds** from 7 bookmakers and
**Polymarket**. It links the same game across sources, logs every price change
with an NTP-corrected millisecond timestamp to SQLite, and serves a local HTTP API.
**Main goal (from the user): measure the delay between bookmaker odds moves and
Polymarket's crowd price.**

- Project: `D:\Dev\claude\oddsfeed`
- Run dir: `D:\Dev\claude\oddsfeed\run` (holds `oddsfeed.db` + `log.txt`)
- API: `http://127.0.0.1:8787`, live table: `http://127.0.0.1:8787/ui`

## 2. Build / run / restart

```bash
cd /d/Dev/claude/oddsfeed && cargo build --release      # ~15 s incremental
cargo test --release                                      # 3 unit tests (matcher, lag)
cd run && ../target/release/oddsfeed.exe > log.txt 2>&1   # run in background
```

- **Stop it before rebuilding.** Windows locks the running exe ("Access is denied").
  PowerShell: `Get-Process oddsfeed | Stop-Process -Confirm:$false`
- `src/ui.html` is compiled in (`include_str!`). Rebuild and restart after editing it.
- Optional config: copy `oddsfeed.example.toml` to `run/oddsfeed.toml`.
  Per book: `enabled`, `poll_ms`, `param`.
- Health check: `curl localhost:8787/books` (per source: polls, errors, last_error,
  live_events). `log.txt` shows warnings.

## 3. Architecture (data flow)

```
books/*.rs  --Snapshot-->  engine.rs (diff vs last state)  --Write-->  store.rs (SQLite writer thread)
  poll every 1 s              | matcher.rs links games               \--> broadcast -> /stream (SSE)
polymarket.rs (WebSocket) ----/
api.rs reads live state from Engine (memory) and history from SQLite (read-only connections)
lag.rs computes book <-> Polymarket delays from the `changes` table
clock.rs: SNTP offset; model::now_ms() = PC clock + offset (THIS PC RUNS ~2.6 s FAST)
```

| File | Role |
|---|---|
| `src/main.rs` | Config, startup (clock sync -> DB restore -> writer -> books -> API), `start_books()` registers each adapter |
| `src/model.rs` | Shared types: `Market` (1x2/total/handicap/btts), `Outcome`, `QuoteIn`, `EventIn`, `Snapshot`, `Change`, `ChangeKind` |
| `src/books/mod.rs` | `Book` trait (`fetch() -> Snapshot`), `run()` poll loop with backoff, `fetch_json()` (stamps receive time), `is_esoccer()` filter, JSON helpers |
| `src/books/<x>.rs` | One adapter per platform: kambi (Unibet), sportybet, betpawa, betika, linebet, fonbet, altenar (VINN), polymarket |
| `src/engine.rs` | In-memory state per book/event/quote. `apply()` diffs a full snapshot and emits `Write`s. Events missing 3 polls and 15 s are closed. `merge()` / `unlink()` for manual fixes |
| `src/matcher.rs` | Cross-book matching: exact Sportradar id first, else same sport + kick-off within 45 min + fuzzy team names (`Names`: accents, FC/CF stop-words, Utd->United; women/U21/reserves tags must match) |
| `src/store.rs` | Schema, batched writer (one transaction about every 200 ms), `restore()` (match ids survive restarts), `query_json()` (read-only SQL) |
| `src/api.rs` | axum routes, SSE stream, CORS for file:// and localhost only |
| `src/lag.rs` | Delay analysis (see section 6) |
| `src/clock.rs` | SNTP against Cloudflare/Google/pool, resync every 10 min, `GET /clock` |

**Key rule:** an adapter returns the FULL live list each poll. The engine works
out open/move/suspend/resume/close by diffing. `price: None` means suspended.
Prices are decimal odds. Handicap `line` is always the HOME team's handicap.

## 4. Database (`run/oddsfeed.db`, SQLite WAL, safe to read while running)

- `matches(id, sport, home, away, league, start_ms, sr_id, created_ms)`
- `book_events(book, book_event_id, match_id, ..., first_seen_ms, last_seen_ms)`
- `changes(id, ts_ms, book, book_event_id, match_id, market, line, outcome, kind, old_price, new_price, book_ts_ms)`
  - `ts_ms` = our NTP-corrected receive time. `book_ts_ms` = the source's own stamp
    (Kambi to 1 s; SportyBet/Betika/Polymarket in ms; others null).
  - `kind`: open | move | suspend | resume | close. After every restart, all live
    quotes appear once more as `open`.
- `pm_quotes(ts_ms, server_ts_ms, match_id, event_id, outcome, bid, ask, last)`:
  Polymarket top of book, as probabilities 0..1.
- Polymarket rows in `changes` use `book='polymarket'`, with price = 1/mid.
  A spread over 0.25 is stored as null.
- `kalshi_quotes(event, outcome, bid, ask, est)` keeps **Kalshi's own home/away** (event title
  order), unlike every other table. Readers flip it with `matcher::names_swapped` (book_events
  names vs matches names), as `snipe::KalshiQuotes` does. Reading it by the match's outcome
  without that flip prices the other team (this cost the paper sim ~$330 before 2026-11-17).
- An `open` row after earlier rows for the same (book, outcome) = a gap in recording (restart,
  or the event dropped out of the feed). `lag::load_series` returns these as breaks; `jumps()`
  ignores moves across them and the sim skips such triggers (`data_gap`).

## 5. API reference

| Endpoint | Purpose |
|---|---|
| `GET /` | endpoint list |
| `GET /books` | per-source status |
| `GET /matches?q=&book=&min_books=&all=1` | live matches, linked books, best 1X2 |
| `GET /matches/{id}` | all current quotes per book |
| `GET /matches/{id}/changes?market=&book=&outcome=&line=&since=` | history, oldest first |
| `GET /board?market=1x2\|total\|handicap\|btts&min_books=` | live table JSON |
| `GET /changes?since=&until=&book=&match=&market=&outcome=&kind=&line=&limit=&order=` | change log |
| `GET /stream?match=&book=&market=` | SSE, one `change` event per price change |
| `GET /query?sql=SELECT...&limit=` | any read-only SQL |
| `GET /stats` | row counts, changes per book in the last 5 min |
| `GET /lag?since=&until=&min_jump=0.04&window_s=90&settle_s=10&clock=receive\|source&match=&book=` | delay summary |
| `GET /lag/events?...&trigger=book\|polymarket&limit=` | each jump behind the summary |
| `GET /clock` | clock offset in use |
| `POST /matches/{into}/merge/{from}` | fix a missed link |
| `POST /events/{book}/{event_id}/unlink` | fix a wrong link |

Times are ms since epoch (UTC). Example:
`curl -G localhost:8787/query --data-urlencode "sql=SELECT book,COUNT(*) FROM changes GROUP BY book"`

## 6. Delay method (lag.rs)

1. 1X2 prices become implied probability `1/odds`, one step series per (match, source, outcome).
2. A jump = a move of at least `min_jump` within `settle_ms` (a spike that reverts is ignored).
3. The other side "follows" if, over ±`window_ms`, it moves the same way by at least half as much.
4. Both sides are timed at the halfway crossing of their own move.
   `delay_ms = polymarket_time - book_time` (positive = Polymarket later).
5. `trigger=book` (book jumped, did PM follow) and `trigger=polymarket` (PM jumped,
   each book plus "first book").
6. Jumps are skipped if the other side wasn't being recorded yet.

**Status:** pipeline verified only on one 4 a.m. USL game, with no goal while
Polymarket was recorded. Two small draw moves showed PM about 22 s behind.
**Not a result.** Collect goals and red cards in busy hours before drawing
conclusions. Bookmaker times include their feed delay (Unibet ~1.7 s,
SportyBet ~3.0 s, Betika ~3.5 s, Polymarket ~19 ms); `clock=source` removes it
where the source has a stamp.

## 7. Source notes (what took time to find)

- **Unibet**: `eu-offering-api.kambicdn.com/offering/v2018/ubse/listView/football/all/all/all/in-play.json`. Odds and lines are ×1000.
- **SportyBet**: `/api/ng/factsCenter/liveOrPrematchEvents?sportId=sr:sport:1&marketId=1,18,16,29`. Sportradar market/outcome ids. `lastOddsChangeTime` in ms.
- **betPawa**: `/api/sportsbook/v4/events/lists/by-queries?q=<json>`. Needs header `x-pawa-brand: betpawa-nigeria`. Market 3743 = 1X2. Sportradar id in `widgets`.
- **Betika**: `live.betika.com/v1/uo/matches?sport=14`. Times are UTC+3. `parent_match_id` = Sportradar id.
- **Linebet** (1xBet platform): `/service-api/LiveFeed/Get1x2_VZip?sports=1&count=500&mode=4...`. T 1/2/3 = 1X2, 7/8 = handicap, 9/10 = total, 180/181 = BTTS, `B` = blocked. Lists simulated leagues ("Short Football 3x3", "MLS+", "4x4"), which `is_esoccer` filters out.
- **Fonbet**: website is Cloudflare-challenged, but the line CDN `line52w.bk6bba-resources.com/events/list?version=N` is open. Deltas by `packetVersion`, full refresh every 30 s. Factors: 921/922/923 = 1X2, 930/931 = total, 927/928 = handicap.
- **VINN**: Altenar `sb2frontend-altenar2.biahosted.com/api/widget/GetLiveEvents?integration=vinn&sportId=66`.
- **Polymarket**:
  - Gamma `/events?tag_id=100350` (soccer). Limit above 50 needs gzip.
  - Title "Home vs. Away". Three moneyline markets; Yes token from `clobTokenIds`.
  - `gameId` is used as a "weak" Sportradar id (`sr_weak`).
  - WS `wss://ws-subscriptions-clob.polymarket.com/ws/market`: subscribe `{"assets_ids":[...],"type":"market","custom_feature_enabled":true}`, send `PING` every 10 s.

## 8. Open work (ranked)

1. Let it run through busy hours and report `/lag` on real goal moves. Tune `min_jump` / `window_s` against `/lag/events`.
2. More books:
   - Winline: binary WebSocket `wss://wss.winline.ru/data_ng?client=newsite&nb=true`, needs protocol decoding.
   - Not traced yet: Favbet, STS, Norsk Tipping, 1Win, 20Bet, GGBET, Betway, thunderpick.
   - Other Kambi/Altenar brands are one-line additions in `start_books`.
3. Kambi push feed instead of 1 s polling. Polymarket totals/handicap markets.

To add a book: create `src/books/x.rs` implementing `Book` (return the full live
list as `EventIn`s with normalized `QuoteIn`s), add `pub mod x;` to
`books/mod.rs`, and add a `start!(...)` line in `main.rs`.

## 8b. Multi-sport + more books (2026-11-17, Mac session)

- Sports: `cfg.sports` (keys in `books::SPORTS`) + `cfg.esports`. `books::run` drops events of
  sports not followed (`Config::follows`). `books::three_way(sport)`: football/handball store
  home/draw/away; every other sport stores a TWO-WAY WINNER (overtime/series included) under
  `Market::X12` with only home/away. `books::winner_quotes` drops a draw-bearing X12 for a two-way
  sport (that is regular-time 1X2, a different market). `sport_key()` maps source sport names.
- Pinnacle (`books/pinnacle.rs`): guest Arcadia API (`x-api-key` from pinnacle.com), per sport
  `matchups[/live]` + `markets[/live]/straight`; American odds; only `type=matchup, units=Regular`;
  event id = `parentId` when live. Idle sports re-checked every 30 s.
- 1xBet platform (`books/linebet.rs`) is now multi-brand: linebet, melbet, betwinner, megapari
  (same feed, near-identical prices). Sport ids in `linebet::SPORT_IDS`; 401/402 = winner incl. OT.
  1xbet.com, paripesa, 22bet are Cloudflare-blocked from here.
- Kambi brands: unibet (ubse), leovegas (leose), paf (paf). Live list is `listView/all/...`;
  pre-match per sport slug. Kambi's list view has only 3-way regular-time hockey, so Kambi hockey
  links but has no winner price.
- Polymarket discovery adds Gamma tags basketball 28, tennis 864, hockey 100088, baseball 678,
  volleyball 102883 (`parse_two_way`). Kalshi is still football + e-sports only.
- M88 (`books/m88.rs`, Asian book): guest JSON at msports.m88.com (`Bearer GUEST`), odds_type 3 =
  decimal, sports 10 football / 11 tennis / 12 basketball / 52 e-sports, game_type 1 = 1X2, 12 = winner,
  `match_date` UTC+8.
- Kalshi: key in `<db dir>/kalshi.json` (Settings or `POST /kalshi/settings`); RSA **or Ed25519**
  (Kalshi accepts Ed25519 signatures). With a key: authenticated WS `ticker` channel push, poll 250 ms
  (memory read) + REST re-seed every 30 s. Key edits apply on restart. Discovery now covers basketball,
  hockey, baseball, NFL/NCAAF, tennis, UFC, volleyball series; idle series re-checked every 30 min.
  Kick-off = `close_time - 48 h` (occurrence_datetime is the expected END), else end - typical length.
- Cloudflare challenge / blocked (not bypassed, by design): Favbet, GG.BET, 20Bet, Betfair,
  sportsbet.io, bet365, Altenar widget from curl (VINN itself works from the service). Rivalry's
  `/api/v1/matches` returns an empty list. Betway's captured API needs event ids from a session.
- Probed and NOT usable from here: bwin, Betano, Betsson, Stake, bet9ja, vbet (403/bot walls),
  Cloudbet (needs API key), Superbet (endpoint guess 404). Rivalry `/api/v1/matches` answers (e-sports book, not wired).

## 8c. More bookmakers (2026-11-17, round 2)

See `docs/BOOKMAKERS.md` for every site probed (status, protection, platform, how its odds feed
works or would work). Added: PariPesa, 22Bet, 22Bit, 1xBit (1xBet classic feed + the brand's
`partner` id, which these newer brands require), BetKing UA (Altenar `betking`), Olimpbet
(`books/olimp.rs`), Duel (`books/betby.rs`, reusable for any Betby brand), Mostbet, Leon,
Marathonbet, Betcity, PARI (Fonbet platform; Fonbet adapter now covers every sport). Tools: `tools/probe_books.mjs`, `tools/find_apis.mjs`.

Snipe window research: `tools/snipe_research.mjs` (gates in `src/snipe.rs`, chosen on the first
60% of the data, checked on the last 40%). Kalshi real quotes in `kalshi_quotes` (est=1 rows =
backfill from Kalshi's 1-minute candles, run at startup).

## 9. Hard rules

- Never change the system clock; correct timestamps in software (clock.rs).
- Keep the API bound to 127.0.0.1 (no auth).
- Don't commit or push without the user asking (this folder is not a git repo).
- Report results honestly: say what was and wasn't verified.

## 10. Web app (added 2026-11-17)

- Served at `/` by the same binary. Files: `src/web/index.html`, `app.css`, `app.js` (core: API, live stream,
  tooltips, styled selects, charts, tables), `pages.js` (Overview, Rankings, Latency), `pages2.js`
  (Live, Match, Sheet, Charts), `pages3.js` (Movers, Edges, Query, API docs, Control panel, Polymarket,
  Statistics, Settings), `img/` (logo + source icons), `vendor/` (uPlot).
- They are compiled in. While editing, run with `ODDSFEED_WEB_DIR=<repo>/src/web` to serve them from disk,
  then do a full page reload (hash navigation does not reload CSS/JS).
- Tooltips: any element with `data-tip` (text or HTML), `data-tipsrc="<source>"` (source speed card) or
  `data-tipq` (price card, use `SE.qattr({...})`). Dropdowns: `SE.select(el, {options, value, onChange})`.
- `GET /rankings` (src/rank.rs): per-move ordering of every source, leaderboard, and the Polymarket
  opportunity numbers (how often Polymarket was still 1/2/5/10/30 s behind the first bookmaker, and how
  many points it still had to move). Same parameters as `/lag`.
- Other new endpoints: `/quotes` (every current price), `/info`, `/control/books/{name}`,
  `/pm/settings`, `/pm/account` (Polymarket Data API v2 + signed CLOB reads; never trades).

## 11. Telegram, Arbitrage Scout, market size (2026-11-17)

- `src/telegram.rs`: bot inside the collector. Settings + **token** in `<db dir>/telegram.json`
  (gitignored, never put the token in source: the repo is public). Linking: page link
  `t.me/<bot>?start=<link_code>`, or /start from the handle saved on the page. Only the linked
  chat gets alerts/answers. Alerts: odds moves (from the `live` broadcast, watched matches = bell,
  batched every 4 s, per-line cooldown), arbitrage (scan every 30 s, re-sent after 1 h or +1 pt),
  closed paper-bot trades (default Snipe sim, every 60 s). API: `GET/POST /telegram`,
  `POST /telegram/test`, `GET /telegram/watch`, `POST /telegram/watch/{id} {on}`.
- `src/arb.rs` + `GET /arbs?min_profit=&stake=`: pre-match only; bookmaker prices from memory
  (event seen < 5 min), Polymarket/Kalshi legs at the recorded best ask (No = 1 - Yes bid),
  Kalshi fee included. 1X2/winner (incl. two-leg Yes + prediction-market No), totals/handicaps/BTTS
  at equal lines. No depth, limits or rules checks; warnings say so.
- `src/predmeta.rs` + `GET /pred/meta` (20 s cache): per match, Polymarket/Kalshi liquidity,
  volume, 24 h volume (from discovery; Kalshi liquidity = open interest x last price when its
  liquidity field is 0), price moves in the last hour, cheapest outcome. Matches page tier bar.
- Web: `pages4.js` (Telegram + Arbitrage pages, `SE.bell`, `SE.tier`).
- `src/arbtrack.rs`: live arbitrage tracker, scan every 5 s (near misses to -3% kept for the page).
  Opportunity = match + market + line; rows in `arb_history` (first/last/end ms, end_reason
  gone|kickoff|restart, censored = already open at startup). Lifetime stats use only `gone` and
  uncensored rows (14 days). "Est. left" = median lifetime of past ones that outlived the current
  age, minus the age (needs 5+). `/arbs` serves the tracker's latest scan; `/arbs/history`.
- Order-book depth (2026-11-18): `pm_quotes` and `kalshi_quotes` have bid_size, ask_size,
  bid_depth, ask_depth (contracts; depth = within 2c of the best price). Polymarket keeps the
  full book per token from `book` + `price_change` levels; Kalshi from the keyed WS
  `orderbook_delta` channel (Yes ask = 1 - No bid). Size-only changes are written at most every
  2 s per contract. Rows before this have NULLs = depth unknown.
- Snipe sim: one position per move (same match within 2 min, best edge kept: `same_move`);
  with known depth the stake fills only what rested within 2c (`fill_buy`), exits walk the book
  (`fill_sell`, 5c penalty beyond 2c), under $10 fillable = `book_thin`. Trades carry `filled`
  (null = depth not recorded); summaries have `by_depth`.
- Snipe exit (2026-11-18): "caught up" is judged on the price we would sell at (bid for Yes,
  ask for No) with half the entry spread allowed (`caught_up`), not on the mid: a blown-out
  spread used to trigger a sale far below target.

## 12. Suspicious matches + site search (2026-11-18)

- `src/suspect.rs`, page `#/suspicious` (pages5.js), Telegram alert type `suspicious`
  (`activity` / `lines` switches), thresholds in `<db dir>/suspicious.json`
  (`GET /suspicious?days=`, `POST /suspicious/settings`).
- A. Heavily bet lower-tier CS2 games: Polymarket Gamma tag 100780 (counter-strike-2); trades from
  Data API **v2** `/v2/trades?condition=<conditionId>&limit=1000` (+`cursor`; v1 is retired).
  Fills of one wallet+market+outcome+side within 60 s = one bet. Measures: money traded, big
  underdog bets (>= $200 below 40%), big favourite bets (>= $200 at 50%+), big-bet share, price
  drift first trade -> start. Score = mean percentile vs finished lower-tier games of the last
  14 days (drift half weight). Table `cs2_activity`; finished games stored final once.
  Tier 1 = VRS top 20 or both top 30: Valve's `standings_global_*.md` from GitHub, synced every
  ~12 h; team names matched by key + `ALIASES` + unique prefix; `retier()` re-ranks stored games.
- B. Pre-match line moves (all sports, bookmakers only): per independent platform, first price vs
  now (margin removed); flag when the median platform moved a side >= 8 pts and 2+ platforms
  agree; table `line_moves`. Runs every 60 s on its own loop (the CS2 backfill is slow).
- Search: `GET /search?q=` (matches + leagues from the DB) and the sidebar box (`/` focuses).

## 13. Data page + merging machines (2026-11-18)

- `src/data.rs`, page `#/data` (pages5.js). `GET /data` (tables: rows, bytes from `dbstat` incl.
  indexes, time span; cached 10 min, `?fresh=true`), `GET /data/scan` (.db files in the db folder,
  `import/` and folders saved in `<db dir>/data.json`), `POST /data/export` (VACUUM INTO
  `export/swifteye-<host>-<stamp>.db`), `POST /data/merge {path}`, `GET /data/job`.
- Merge: foreign match ids mapped by shared book event -> Sportradar id -> sport + kick-off ±45 min
  + names (Names sim >= 0.75); otherwise a new id from the live matcher (`alloc_id`, so no clash).
  Price tables (`changes`, `pm_quotes`, `kalshi_quotes` est=0): a foreign row is skipped when this
  db has a row for the same book event (PM event / Kalshi contract) within ±1 minute; batches of
  20k rows with short pauses (the live writer drops a batch after a 5 s lock wait). Idempotent.
  Recorded in table `merges`. Tested on two real-data slices (overlap skipped, new half-hour added,
  second merge added 0).

## 14. Page header + design notes (2026-11-18)

- `P.head(title, tools, back)` renders a title row (`.page-head`: title + `.ph-actions`) and, when there
  are tools, a `.toolbar` row. `SE.tidyHead(el)` (run by the router after every render; call it
  yourself after an async re-render) moves buttons, `a.btn`, chips and plain status spans up into
  `.ph-actions`; filters (search, selects, segs, labelled inputs) stay in the quiet toolbar.
- Snipe window: Setup lives in a `.panel` behind the Settings button; explanations in `<details class="fold">`.
- Suspicious game page (`#/susgame/<event>`): one uPlot chart of home-win probability from every
  source (linked match's 1X2 changes + Polymarket trades, flipped when `pm_swapped`), big bets as
  point series; opens zoomed to start-6h..last trade.
- Game page extras: Kalshi history for any CS2 game Kalshi listed (KXCS2GAME, matched by names + nearest
  date; list cached 30 min, built at start; `kget` retries 429s), and map markers: each Polymarket
  `Map N Winner` market's `closedTime` = map N over (map N+1 starts after a short break); map 1 = Kalshi's
  ticker start time when known (Polymarket's `startTime` is often the scheduled slot, ~1 h early), else
  Polymarket's. `SE.chart` takes `opts.marks = [{ x, label, color }]`.
- Snipe sim (2026-11-18 review): `book_prob` skips suspension rows (a goal = suspend then reopen; the move
  is measured from the price before the suspension, as rank.rs does). This took no_book_price from ~900 to
  ~20 skips and added the goal-type trades (they lost so far). `no_data` split into `no_book_price` /
  `no_market_price`. ROI and per-trade return divide by dollars actually spent (`staked`; partial fills).

## 15. Snipe paper bot: adaptive rules (2026-11-18)

- `src/adaptive.rs`, `GET /adaptive`, Snipe window tab 'Paper bot' (`SE.snipeBot` in pages5.js); Telegram paper
  alerts now come from it (`adaptive::closed_since`). Candidate table of every move (both tests, all
  exits) built in 4-hour chunks from the raw data; re-chooses rules at 00/04/08/12/16/20 UTC per market
  over a 10,368-rule grid incl. `skip_after_pause`. Facts (reaction, confirm, fill, slippage, fees, depth)
  are fixed. Method v3: 30+ trades, mean - 2 SE, plateau, validation on the last 30% before the pick,
  never adopt a negative conservative score, 0.3-pt switch margin, 24-h safety fallback to defaults.
  Runs in table `adaptive_runs` carry `version`; a new `VERSION` deletes them and recomputes the whole
  walk-forward history. v1/v2 lessons: first pick on 22 trades lost $1,000; v2 switched to a negative
  candidate when the current rules had too few trades. Simulator tab has an 'All' range (since=0).
- v4 (2026-11-18): paper balance per market (`<db dir>/paperbot.json` {start, pct}, default $10,000 / 1%;
  `POST /adaptive/settings`; a change deletes the runs and recomputes, `KICK` wakes the loop). Each trade
  stakes pct x (start + closed P&L), capped by free cash (`simulate`). Candidates store price + book depth
  (`buy`, `Exit.sell`) and are priced at any stake by `fill()`. New grid dimension `walk_book` (best price
  only vs up to 2c worse), 20,736 rules; scoring is return on the INTENDED stake (partial fills count for
  less), judged at the stake the balance gives at pick time. `book_test` on each run compares the two order
  sizes on moves with recorded depth.
- Hold-to-result checked against OFFICIAL results (Gamma resolved prices, Kalshi `result`), 2026-11-18:
  no edge (208 one-per-game Polymarket bets won 111 vs 111.2 priced in). An earlier +$19.5k "hold" figure
  came from guessing winners from the last quote and was wrong. The edge is the short lag only.
