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
- **HKJC** (`books/hkjc.rs`, record-only): GraphQL GET `info.cld.hkjc.com/graphql/base-get/` with the
  page's exact `matchList` query text. Needs `Content-Type: application/json` on the GET (Apollo CSRF guard;
  without it the origin answers 400 "Internal server error - BAD_REQUEST", which edge-cache hits hide).
  One market family per request (HAD+EHA / HDC+EDC / HIL+EHL). HDC/HIL `condition` "a/b" = quarter line
  (mean). Pool `updateAt` does not move with prices: no source stamp. Guests' page polls every 12 s;
  logged-in users get Solace MQTT push (`wss://ueb.hkjc.com`, token needed, not used).
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

## 8d. Browser books (2026-11-23)

BC.Game, Rollbit, Rainbet, CSGO500, CSGOEmpire, Cloudbet and Shuffle come from `browser_books.py`
(repo root, written by the local Qwen agent; use it as written). oddsfeed starts it with the repo's
`.venv` Python (`requests` + `camoufox`), restarts it when it exits, and points it at its own
`bind` address; it POSTs `{book, recv_ms, events}` to `/ingest` (`books/browser.rs`).

- The five sptpub books: `GET {host}/api/v4/live/brand/{brand}/en/0` → `version` → `.../en/{version}`,
  football only, 1X2 (market 1) + totals (market 18), every 5 s, plain HTTP. The script reads the
  `version` slice only (= `top_events_versions`), about half the live football board; the Rust
  Betby adapter (Duel) reads `top` + `rest`.
- Cloudbet: `/sports-api/c/v6/sports/events?live=true&sports=soccer`, then one
  `/sports-api/c/v6/sports/events/{id}` per game, fetched inside a headless Camoufox page.
- Shuffle: `shuffle.com/sports?section=live` rendered and read as text every 60 s; its page mixes
  sports and the script tags every game football. `/ingest` drops "football" games priced
  home/away with no draw and no other market (NCAA basketball, etc.).
- `/ingest` fixes on the way in: raw PC-clock `recv_ms` → NTP-corrected; no `live` flag → live;
  1X2/BTTS `line 0.0` → no line; BTTS over/under → yes/no; `start_ms 0` → unknown; then the usual
  e-soccer and followed-sport filters.
- The script's lines "<book> poll fail: ..." become that book's poll errors on the Feeds page.
  It posts nothing when a list is empty, so a book with no live games shows its last poll time.
- All seven are RECORD_ONLY. Not used: Gamdom and CSGORoll (no sports section).
- Config: `[browser_books] enabled / dir / python`; per book `[books.bcgame] enabled / paused`.
  The Mac has no `.venv` with camoufox yet, so these books are off there (one warning at start).

## 8e. Trade review (2026-11-23)

Page `#/review` (`src/web/pages7.js`), server `src/review.rs`, table `trade_labels` (id, created_ms,
updated_ms, match_id, kind, outcome, data JSON). Every change also rewrites `<db dir>/trade_labels.json`.
The owner marks what the bots should have seen, to tune the move detection against.

- Game picker: `GET /review/games?kind=bought` (games in `bot_trades`, plus the replay paper bot's
  Polymarket trades: `adaptive::closed_since(0)`, the snipe rules over every recorded game, cached
  60 s in `review::replay_trades`, shown as bot "replay" with ids "r<n>") or `kind=other` (started
  games with a Polymarket `book_events` row and a bookmaker, in neither), `q`, `book`, `random=1`.
  `bot_trades` lives where the bots run (the Mac); this PC's db has none, so here Bought = replay
  (27 trades in 21 games on 23 Nov). The replay is empty for a minute or two after a start.
- `GET /review/game/{id}`: Polymarket bid/ask per outcome (`pm_quotes`, size-only rows dropped), every
  bookmaker's 1X2 from `changes` + `changes_research`, `bot_trades`, `bot_signals`, marks.
- A mark: `kind` move | buy | adjust | no_buy | ok; `outcome`; `side` yes|no (No = the complement
  token, priced 1 - Yes bid, as snipebot buys it); `buy_ms`, `sell_ms`, `cap` (price of the token
  bought); `trade_id` (a `bot_trades` id) or `replay_trade` (the replay trade's JSON, stored whole
  as `trade` with bot "replay"), one of them required for adjust / no_buy / ok; `pm_seg` and `book_seg` {t0, y0, t1, y1
  (+ book)}: the two painted lines, y = the outcome's probability.
- On save (`review::save`): `snapshot.buy` / `.sell` = Polymarket bid/ask and the token price there,
  `snapshot.buy.books(_30s_before)` = every bookmaker's raw 1/odds and margin-free share;
  `analysis` (`review::analyse`) = each line timed where its recorded series (Polymarket mid,
  bookmaker 1/odds) crossed halfway between its values at the line's ends: `delay_measured_ms`
  (PM - book), `delay_painted_ms` (middle to middle), `ratio` (PM move / book move), Pearson
  `corr_lag0` and the best lag in ±60 s (`best_lag_ms`, `corr_best`) over the lines ± 60 s,
  `warnings` (a line on a price that did not move or moved the other way), and `series` (the raw
  rows of that window, so marks survive the prune job).
- A bought game opens on its trade, drawn like the Telegram trade chart (`telegram.rs::trade_chart_at`):
  the token bought in cents, buy - 10 min to past the sale; Polymarket sell (bid side) and buy price
  and ONLY the bookmaker the bot acted on (owner, 23 Nov: more lines were too hard to tell apart).
  Other bookmakers and "All bookmakers" (median of feed-family means, `families` from
  `snipe::platform`) are added per trade from the legend ("Add a line", `st.tsel`, not remembered); the bot's plan from its data (`pays_at`: pre_mid + catch x move - spread/2,
  give-up = ts + give_up_s, break-even after fees) drawn faint; "your version" on top, dragged in
  place (buy, sale, cash-out line, cap line, give-up line; hit-tested in `pointerdown` capture so
  uPlot does not zoom). Replay trades get `t0`/`spread` (trade_json), `catch`/`give_up_s`
  (closed_since) and `pre_mid` (review::game: PM mid at t0 - PRE_MS). "Whole game" = the full game in
  the outcome's Yes cents; B + click places a missed buy (cash-out +5c, cap = price, give-up +15 min).
  Marks store `target` (cash-out price) and `give_up_ms` too.
- Everything autosaves (owner, 23 Nov): every drag, painted line, verdict and note saves 500 ms after
  the last change (`changed` -> `saveNow`, one chain so a new mark is never created twice). A trade
  keeps ONE mark (`markFor`: bot trade id, or replay ts + outcome) that loads back into the editor on
  open; kind = the verdict (ok / no_buy / adjust), else adjust if your version differs from the bot's,
  else move (a painted line) or note; a mark with nothing left is deleted. One painted line alone is
  kept (the delay needs both). K / P / B switch a tool on (never off; Esc stops). The whole game keeps
  several marks ("New mark") and draws the others faint.
- The picker shows Status per game (Not marked / Marked / "1 of 2 marked" + the verdicts; filter
  All / Not marked / Marked); a bought game is marked when every trade has a mark. Trade chips tick.
- Marks are now several per view (23 Nov, second pass; `st.items` in pages7.js): the trade's own mark (verdict +
  your version, linked by trade id / replay ts), buys of your own (kind `buy`, no trade link, B on any view, default
  give-up 2 min), line pairs (kind `move`, no trade link; K/P paint into the picked pair while it lacks that line,
  else a new pair) and a game note. Unlinked buys and pairs of the outcome show on the trade AND the whole game.
  Own buys are Up (side yes of the outcome) or Down (side no): a switch per buy, and dragging a buy's cash-out
  below what it paid flips it (owner, 23 Nov: "some are up, some are predicting down"; the trade view used to
  save every buy in the trade's token, so mark #17 was saved Up and was re-saved as Down). A plan's sale follows
  its cash-out / give-up (planSale) until the sale is dragged by hand (`sell_set` in the mark).
  Each item autosaves on its own 500 ms timer through one save chain. A pair saved inside an older mark (before
  pairs were marks) stays there (`host`): editing it re-saves the host mark.
- Imports: `POST /review/import` (raw body: the "Swifteye trades" HTML page, its JSON; the page unzips a .zip in the
  browser) -> `review_imports` (one row per game of the other machine, id = -game id, data = every trade's price
  window merged, trades with the export's `record`). Re-import replaces in place by `mac_match_id`. Imported books
  windows start 15 min before each buy and end 10 min after the sale, so "Whole game" there is those windows only.
  First import 23 Nov: 188 trades (favs 85, snipe 70, snipe-1book 18, snipe-trial 15; 26 real) in 138 games.
  The prices live in `review_import_data(id, data)`, NOT in `review_imports` (summary columns only): with them
  inline (39 MB over 138 rows) the Bought list took 6 s on a cold disk cache. init() moves old rows once.
- SPEED TRAP (23 Nov): never build the replay trades on a request. `adaptive::closed_since(0)` backtests every
  recorded game (~5 s); it used to run inside `/review/games` and `/review/game` once a minute (the owner saw
  "Loading the game" and an empty list). `review::refresh_replay` (thread started by init) rebuilds it every
  minute while the page was used in the last 10 min; requests take the latest copy at once. Measured after the
  fix: list ~10 ms, game page 5-45 ms (first ~12 s after a restart slower while the adaptive history rebuilds).
- The chart carries each line flat to the end of the view (trade: window end, capped at now and at an export's
  `until_ms`; whole game: newest row), so a quiet price does not end the chart before the buy. An OPEN trade
  (favs holds to the result; no sale, no give-up) runs to the end of what is known (now / export time): its
  window used to stop 5 min after a pre-match buy, where Polymarket is flat, and hid the game itself.
  Flat pre-match lines are real: checked 23 Nov, trade #186's market sat at 78/79c for the whole hour on
  both machines (this PC's rows there are size-only updates).
- Analysis (toggle, pref `reviewAna`): `src/web/waves.js` (browser port of tools/waves, kept in step by
  `node tools/waves/test_parity.mjs`) on the game's own series, cached per outcome; ~40 ms on a 145k-row game.
  Chart: superline series, wave shading (orange from first onset to Polymarket's when books led, blue mark when PM
  led), onset ticks per line, the trade's wave outlined and labelled (`tradeWave`: same direction, <= 3 min before
  the buy, the one with the trigger near the bot's move first). Measured panel: the trade's wave (onset order with
  the buy, PM completion at the buy), and per line on the game: waves, first, before-PM, median lead, HY lead-lag
  and ρ (only with >= 8 moves, capped at ±1: the HY ratio is not bounded on few moves), plus all games from
  `GET /review/wave_scores` (`<db dir>/waves/scores.json` written by run.py).
- TRAP (23 Nov): adaptive.rs keeps its replay filters (`Flt`) in a THREAD-LOCAL. `bot_sim` and the
  research replays set it and leave it on tokio's blocking threads, so anything reading it later on
  such a thread inherits a random filter (the Bought list went 30 trades -> 0). `view` and
  `closed_since` now run inside `with_paper_flt` (cap only, the initial value they were written for)
  and restore the thread's value; with `Flt::live()` this PC's data gives 0 trades. Any new caller of
  the replay must set the filter it means.
- Chart: `SE.chart` takes `opts.draw(u)` for an extra canvas layer; set `c.textAlign = 'left'` in it
  (uPlot leaves it right-aligned). Painting uses an overlay inside `u.over` that takes pointer
  events only while a tool is on (K bookmaker line, P Polymarket line, B buy, S sell, Esc). A painted
  bookmaker line is given to the bookmaker whose line follows it best (13 points along it), the
  selected ones first. Polymarket is always drawn full; bookmakers at 10% until clicked in the legend
  (selection kept in pref `reviewSel`, saved with a mark as `books_shown`; colours swapped in place
  by setting `u.series[i].stroke` and `u.redraw(true)`, so the zoom stays).

## 8f. Waves, delay and correlation: the new detection system (2026-11-23, step 1 of 4)

Owner's diagnosis: the bot cannot tell which line is which or which WAVE has already happened, so it
often buys a move Polymarket made earlier (in all 7 painted pairs Polymarket moved 6-139 s BEFORE the
bookmaker; 18 of 25 replay trades fired on a bookmaker closing a gap Polymarket had opened). A wave
lasts seconds, so a 15-minute give-up is gambling. Plan (owner approved, build in this order):
1. waves + register, 2. per-source delay and correlation, 3. the "superline" (fastest credible
reaction of every bookmaker, filtered), 4. only then the bot (baseline before the wave's EARLIEST
onset in any source, sequential test instead of N bookmakers, wave-based exit).

Theory (all in log-odds x = logit p; each source = late noisy view of one latent price X):
- delay: Hayashi-Yoshida lead-lag on increments (no common grid, no Epps bias); a two-way model
  t_s(w) = T_w + δ_s + ε over all sources (median polish; δ_PM = 0); race shares (Cox) later.
- waves: per-source segments of same-direction moves (dead band for PM flicker), >= 3 pt and 0.12
  log-odds, <= 20 s long, holding half 10 s later; a reopen after a suspension counts from the price
  before it. Register: same direction, onset within 180 s of the first, each source once.
  Completion c_s(w,t) is measured from just before the EARLIEST onset in any source.
- correlation: separate bias (level offset), tracking error, increment correlation at the lag,
  pass-through (PM move per unit of the source's), precision (PM followed) and recall.
- superline (step 3): jump-aware filter (IMM / Gaussian sum) over families weighted by freshness and
  reliability, Wald SPRT for "enough confirmation"; scored like any source.

Tool: `tools/waves/` (`waves.py` library, `run.py`, `test_waves.py` synthetic cases with known answers).
`python tools/waves/run.py [--days N] [--sport S] [--clock receive|source]` writes
`<db dir>/waves/{report.txt, scores.json, waves.jsonl}` and checks the painted marks (Trade review).
"ANY BOOK" = per wave, whichever bookmaker moved first (the superline's ceiling).

## 8g. Lead strip, trend waves, signal model (2026-11-23, owner's goals 1-3)

- LEAD STATE (`tools/waves/lead.py`, browser `waves.js lead()`; parity in `test_parity.mjs`): on a 1 s grid,
  e = logit(superline) - logit(PM mid) - b, b a robust slow offset (half-life 600 s, residual clipped at 1.5 s;
  a gap open > 180 s is STALE and absorbed fast), z = e / s (s = calm-time EWMA of |e| as sigma, floor 0.03),
  open at |z| >= 2.5, close < 1.0. Who opened it: moves since the last calm second; superline -> AHEAD_UP/DOWN,
  PM -> PM_AHEAD. Expected catch-up = pass (1 - e^-kappa h) e. `lead_fit.py` (28 s, all games): pass 0.17,
  kappa 0.0305/s; the superline itself snaps back ~30% of an "ahead" gap; ahead gaps right (PM moves half the
  gap their way within 60 s) 50%, wrong first 24%. Time shares: ahead 3%, PM ahead 7%, level 89%.
- TREND WAVES: two-sided CUSUM on 1 s log-odds steps, k 0.002/s, h 0.12; onset = last second U was 0,
  detected at U >= h, peak, end when U is back at 0. PM follows 74% of superline waves, but PM's onset is
  first by ~1 s at the median (superline first in 46%).
- Viewer (Analysis on): a canvas strip under the chart (`drawStrip`, redrawn with every uPlot draw, same x):
  Lead row (green/red superline ahead, blue PM ahead, grey stale), Superline and Polymarket wave rows; hover
  read-out (`leadText`). Measured panel: lead at the bot's buy and per-game lead/wave stats.
- SIGNAL MODEL (`tools/signals/`): `features.py` (94 causal features on the 1 s grid: PM and superline returns,
  gap/state, CUSUMs, each of 12 bookmaker families' gap and 10 s move, breadth counts, PM order-book imbalance
  and depth (pm_quotes sizes, since 18 Nov), sport, in-play), labels on executable prices (enter 1 s later at
  the ask / 1 - bid, fees 5% p(1-p) both fills, take-profit / stop / horizon); `build.py` (decision seconds =
  any line changed; ~960k rows, 100 s), `train.py` (walk-forward by kick-off 60/20/20; logistic, MLP up/down
  heads, a directional regression net; thresholds chosen on validation; backtest one position at a time;
  rules for comparison; permutation importance), `marks_eval.py` (the network on the owner's marks).
  RESULT 23 Nov: NOTHING is profitable on the test games. Round-trip cost median 3.3c (4.5c at 40-60c);
  even perfect direction held 3 min nets +0.6c on average. Up/down heads correlate +0.8 (they detect
  volatility, not direction; top inputs: price level, time in game, spread). Directional net: correlation
  with the move +0.117 with the order book (+0.089 without), 57% right on moves over 3c; trades still
  -2.3 to -4c. Rules (superline ahead, 3 families moved, superline wave) -3.3 to -4.1c; every second -4.7c.
  The network disagrees with the owner's 6 marks on game -89. Outputs in <db dir>/signals/ (report.txt,
  model.pt/json; report_tp1_stop4.txt = the first label).
- WAVE MODELS (owner's factors, 23 Nov): `wave_targets.py`, `build_waves.py` (3 min, waves_A/B.npz),
  `train_waves.py` (20 s, waves_report.txt, wave_model.pt). A: one net, heads = hazard of a PM CUSUM wave
  starting within 30 s (up/down), 10/50/90% quantiles of the capturable move in 180 s (best sell price minus
  price paid), time to it, and inside a wave the time / points left to the peak. B: continuation value
  (Longstaff-Schwartz style) of an open position checked every 10 s: delta = (plain policy's end) - (sell now);
  sell when predicted delta < -m (one step of policy improvement). Test results: wave start AUC 0.81 up /
  0.78 down, and directional (heads correlate -0.06, the earlier barrier heads +0.8); time left in a wave
  43 s median error vs 46 s guessing; move quantiles calibrated (10% / 88% below the 10 / 90% lines) but
  direction on big moves only 53%. Entries: down on the predicted move break-even (91 trades, 68% win,
  -0.02c), up -3.3c, wave-start score -3.9 to -4.7c (waves of 3 pt are smaller than the 3.3c cost).
  Cash-out model: all positions -3.82c -> -3.42c; on the entry model's positions -5.95c -> -3.90c; a fixed
  4c stop is worse (-4.02c). Next: hazard of BIG waves only (>= cost + margin), football-only, maker exits.
- MARKETS DIFFER (owner, 23 Nov): features.py has 5 regime inputs over the last 10 min (ctx_pm_rate,
  ctx_book_rate, ctx_spread, ctx_vol, ctx_gap_scale; 99 inputs in all), and train_waves.py reports by sport
  and by Polymarket activity tercile. Wave start AUC: football 0.85/0.85, tennis 0.82/0.79, basketball
  0.78/0.81, esports 0.75/0.71. Other databases: `build_waves.py --db X --out DIR` (older schemas: no
  order-book sizes / no changes_research are handled), then `train_waves.py --data run/signals DIR [--sport S]`.
  Sources are kept apart by game id, so the same game recorded on two machines counts twice (both land in
  the same walk-forward split, since it is by kick-off); merge with the Data page first when that matters.
- MODEL vs OLD BOTS (`bot_vs_model.py`, 23 Nov): on its own the model still loses (-1.3 / -3.0c per share on
  test games); per share the replay bot did -12.7c, Mac snipe +1.4c (real money -1.3c, 26 trades), favs
  +4.5c. As a VETO on the old snipe bots it works: of 150 snipe trades bought after the model's training data,
  the 113 where model A's predicted move beat the fees made +1.78c a share ($571), the 37 it would skip
  -8.48c ($-435); difference +10.3c (90% +5.0..+15.9), permutation p < 0.0001. Real-money subset (26) too
  small to tell. Next: rerun on the owner's bigger dataset, then a paper veto in the snipe bot.
- NO HINDSIGHT (owner, 23 Nov): features are causal (value at t from data <= t; waves count from their
  detection, not their onset). Splits are by kick-off with PURGE + EMBARGO in train_waves.py: training rows
  (label horizon 181 s + margin = 200 s included) end before the first validation kick-off, validation rows
  before the test period. Head-to-heads must only score trades of games the model never trained on: use
  `walkforward_bots.py` (train on games before day D, score day D, roll), not the default 60/20/20 split
  (most Mac bot trades fall inside it). Known small leak: lead.py's kappa / pass were fitted on all PC games;
  they only rescale the gap feature (lead_exp), which the network also sees raw.
- MAC DATA (owner's upload 23 Nov: swifteye-data-2026-11-23.zip -> run/import/swifteye-data-2026-11-23/, 17.3 GB,
  17-23 Nov, 3,409 games with PM + a bookmaker; read it from an SSD copy: on D: (HDD) the random reads crawl).
  Datasets in run/signals/mac (waves_A/B 5.3M / 5.7M rows, exit_hold.npz). CLEAN VETO RESULT (walkforward_bots.py,
  purged, train on games before day D, score day D, 18-23 Nov): of 103 snipe trades, the 71 the model keeps
  (predicted move > fees) made +2.30c a share ($562), the 32 it skips -4.61c ($-377), as traded +0.15c ($185);
  kept - skipped +6.91c (90% +1.74..+12.71), p = 0.021. Real-money subset (26) no effect. favs: the edge rule
  never fires pre-match, but its top half by score made +5.66c vs -0.99c (35 each).
- NO SETTLEMENT LUCK (owner, 23 Nov: "we can't tell who wins"): never judge a signal by results that came from
  holding to settlement. 9 snipe trades overstayed their give-up (snipe 5: 3 sold by hand hours later at estimated
  prices, 1 settled, 1 late; snipe-1book 4 settled), and they swing averages by 40-70c a trade; favs holds to the
  result by design and is out of scope for wave signals. `no_settlement.py` values each snipe trade as planned (own
  exit if within its give-up, else the token's sell price at the give-up moment, fees paid). On that basis (99
  trades): as traded +0.74c a share, model-kept 69 +1.81c, vetoed 30 -1.72c; kept - vetoed +3.53c (90% -0.80..+8.00),
  p = 0.11: right direction, NOT significant (the p = 0.02 / 0.0001 results above included settlement outcomes).
  Instant exit still loses (-0.23c vs the bot's +0.74c).
- OWNER'S DRAWN PATTERN (`pattern_catchup.py`, 23 Nov): PM rising (>= 0.08 log-odds in 30 s), a family then jumps
  PAST it (10 s move >= 0.12, moved in the last 5 s), bookmakers >= 0.12 above PM; buy while PM climbs, sell at the
  bookmakers' level. 50,050 instances in 6 days (Mac data). PM reached the bookmakers' level within 3 min only 33%
  of the time (12% when the room is 8+ pt); median room 4.1 pt, best sell within 3 min +1.0c before fees; plain exit
  -3.05c a share (35% win). The model takes 10% of them (39% reach, -2.71c); the old bot traded 6 (all won, +4.25c)
  and skipped 6,607 (book_reverted, move_too_small, already_moved, moved_with_first_book: -3.23c, right to skip).
  The bot's top skip reason overall is already_moved (63,110): PM moved before the bookmaker. Best sport CS2.
- OWNER'S SECOND DRAWING, BOOKMAKER LEADS (`pattern_catchup.py --pattern booklead`): a family jumps >= 0.12 in 10 s,
  PM has not followed (< 0.02 that way in 10 s), the bookmakers (mean) >= 0.12 beyond PM. 42,962 instances: PM reached
  the bookmakers' level within 3 min 33%, plain exit -2.53c. Model-taken 4,870: reached 52%, -1.61c (best of any rule
  so far); old bot traded 21 (-0.44c). Only the big-gap slice is >= 0: model takes AND room >= 8 pt: +0.22c over 321
  (60% win), and it held in both halves of the period (+0.14c / 252, +0.53c / 69). Chosen by looking at all the data:
  needs a clean walk-forward before use. CS2 looked good in-sample (-0.67c) but failed in the second half (-3.43c).
- CORRECTION, COLLAPSED TOKENS (23 Nov, evening): every exit valued from pm_quotes DROPPED a trade whose sale price was
  missing (NaN), and a missing price is almost always a token nobody bids for because it collapsed to ~0 (280 of 287
  sampled: no bids, offered <= 5c). That hid losers: 10% of gap >= 8 trades at a 10-min exit, 22% at 20 min.
  `booklead.sales()` now sells at the first real bid within 10 min of the planned sale, else at 0; hits on a target
  need a real bid. Used by booklead.py, more_bets.py, improve.py; no_settlement.py does the same for the old bots
  (103 trades now, was 99: as planned +0.25c, kept +1.49c, vetoed -2.37c, p = 0.089). Outputs before the fix are in
  run/signals/mac/before_fill/. Corrected (19-23 Nov, clean): rule (now gap >= 10/8 + model OK, sell at the
  bookmakers' level within 5 min) 68 trades +1.77c (90% -1.34..+4.76) total +120c; RULE + VETO 62 trades +2.11c
  (90% -1.05..+5.37) total +131c (was 195 / +1.70c / +331c); relaxed gaps 5-8 +0.34c over 343; dedicated model
  -0.85c over 480. Every number in the bullets below that came from these scripts before this fix is too high,
  most of all for long holds; booklead_exit.py, exit_vs_hold.py, exit_v2.py and wave_targets.py labels were not
  rechecked. pattern_catchup.py still drops instances whose 180 s plain exit is NaN (rare at 180 s).
- BOOKMAKER-LEADS RULE + TRAINED VETO (`booklead_veto.py`, 23 Nov): per day, the rule chosen on earlier days; a veto
  (ridge or small net, chosen per day) trained on every gap >= 4 pt instance before the day predicts the net of the
  rule's exit from ~29 readable inputs; veto threshold chosen on the last 20% before the day (off if it does not beat
  the rule there). Pooled 19-23 Nov: rule alone 273 trades +0.62c (total +168c); rule + veto 195 trades +1.70c
  (90% -0.19..+3.55), total +331c; the 89 it removed -3.02c (90% -5.28..-0.74: reliably bad); relaxing to gaps 5-8
  when the model says yes adds 807 break-even trades (-0.04c); staking 0.5x/1x/2x by prediction tercile +549c on
  the same 195 trades. (Veto and rule each pick one position at a time, so kept + removed != the rule's count.)
- EXIT HELPER FOR RULE + VETO (`booklead_exit.py`, 23 Nov, owner: "train a model itself as a helper"): continuation
  model on the same kind of trade (bookmaker-leads positions with gap >= 4, 14,188 positions, 564,939 checks every
  10 s) under the owner's exit; patience rule chosen per day on validation positions with gap >= 8. On the 195 rule +
  veto trades: your exit +1.70c a share (total +331c), with the helper +1.10c (+215c): -0.59c a trade (90%
  -1.33..+0.15). It sold 47 early (median 80 s): your exit made -1.72c there, the helper -4.17c. Third exit model to
  lose to holding: on these trades, losers still recover more than the model expects (the veto already picked
  trades that tend to come back). Keep rule + veto + your exit.
- MORE-BETS HELPER ON TOP OF RULE + VETO (`more_bets.py build/test`, 23 Nov, owner: "a helper for taking more
  bets"): meta-labelling. Candidates: bookmaker leads with gap 3-8 pt (22,714, -2.27c unfiltered), the owner's 1st
  drawing (47,974, -2.54c), superline taking the lead (lead_up/lead_down onsets, room >= 1 pt; 67,095, -3.55c); all
  valued with the owner's exit (target, else 10 min). Per day: ridge or small net predicts the net from the general
  model's edge + ~30 readable inputs + type + sport; threshold on the last 20% before the day, extras must make money
  there (>= 20 trades) or none are taken; on the day rule + veto first, then extras while NO position is open in that
  game (either outcome). 19-23 Nov: extras 992 trades +0.16c (90% by game -0.56..+0.85), total +158c; by type
  booklead_small -0.08c, drawn +0.14c, superline_lead +0.31c; rule + veto + helper 1,187 trades +0.41c total +489c vs
  rule + veto alone 195 trades +1.70c total +331c. The selection lifts superline_lead from -3.55c to about 0, not
  above it: extras are break-even, same as every volume attempt so far. TRAP: the first run allowed positions on both
  outcomes of one game at once (194 of 1,397 trades): it showed extras +0.42c / +499c, partly from hedged pairs.
  Ranges now also resample whole games (trades in one game move together). 22 Nov extras -1.39c, 20 Nov the
  threshold fell to 0 and took 586 trades at -0.15c. Verdict: do not add to the live version; more days needed.
  After the collapsed-token fix: extras 319 trades -0.15c; only superline_lead is above 0 (+1.47c over 135, by game
  -1.66..+4.41); booklead_small -3.93c, drawn -0.94c.
- WHAT TRAINING CAN STILL IMPROVE (`improve.py build/test`, `improve_check.py`, 23 Nov, owner: "more trades, fewer bad
  trades, more good trades or better trades for the ones we take"). Clean day by day, collapsed tokens valued,
  ranges by game, paired differences of totals at the same money staked, vs rule + veto (62 trades +2.11c, +131c):
  SIZING (stake = veto's predicted net / its validation mean, 0.25x..3x, used on a day only if it beat equal stakes
  on validation): +4.91c per $ staked, +173c at the same money (90% -2..+357, better in 95% of resamples): the one
  lever that helps. EXIT GRID (rule may pick sell at 50/75/100/150% of the way to the bookmakers, H 300/600/1200 s):
  -58c (41%); it picks 20-min holds, which only looked good while collapsed tokens were dropped (first run: +2.79c
  over 899 trades, all from the bias). VETO v2 (+ the general model's 16 outputs as inputs, 5-net ensemble): picked
  the old veto every day, +0c. HELPER v2 (threshold per type, validation mean >= +1c): extras -0.01c over 390.
  improve_check.py on rule + veto: top 10 trades carry +251c of the +131c; football +8.24c (22), cs2 +4.02c (12),
  tennis -1.22c, basketball -5.35c. Verdict: keep rule + veto + owner's exit, add sizing; 62 trades in 4.5 days is
  the limit on what can be learned.
- MONEY BACKTEST OF RULE + VETO + SIZING (`backtest_sized.py`, 23 Nov; .txt/.csv/.png in run/signals/mac): $10,000,
  stake 1% of the balance x the veto's weight, free-cash cap, the 62 trades in time order. Best-price fills: +$1,290
  (+12.9%) vs equal stakes +$979, but ONE trade (Garland v Broadus, No bought at 0.9c, 10,674 shares) is +$1,062 of it.
  BOOK fills (shares capped by depth within 2c at the buy AND at the sale, 1c worse beyond the best level; that trade
  can only exit ~2,500 shares): confidence stakes +$10 (+0.10%), equal stakes -$96, max drawdown -$515 (-5%); without
  the best trade -$198. Sizing adds ~$106 but there is no profit to scale. TRAP: per-share means (cents a share, one
  share a trade) hide that a $ stake buys 100x more shares of a 1c token than of a 50c one, and the book cannot take
  them; judge money results with two-sided depth.
- MONEY-TRAINED MODELS (`money.py build/test`, 23 Nov, owner: "train further, better vetos, detect more signals"):
  161,281 candidates (all bookmaker-leads gaps, 1st drawing, superline lead) labelled with the $ a $100 order really
  makes (owner's exit, two-sided depth within 2c, 1c worse beyond the best level, collapsed tokens at 0, fees). Every
  kind loses $10-17 per $100 on average; by token price 0-5c -$34, 30-50c -$9.5, 85-100c -$3; even 30-70c tokens with a
  1c spread lose $4-7 in every gap band. Clean day by day, flat $100 orders: A rule + veto -$93 (62 trades); B money
  rule + veto -$25 (53); C money signal finder (one model over all kinds, threshold by total minus one SE on
  validation) found nothing worth taking on 4 of 5 days, -$33 on the fifth (48); D = B + C -$57.
- WHY: COST, NOT DELAY (`latency_edge.py`): entering 0 / 0.25 / 0.5 / 1 / 2 / 5 s after OUR receipt of the bookmaker
  jump, Polymarket has moved only +0.06c by 1 s. Mid move our way over the next 5 min vs round-trip cost (half spreads
  + 5% p(1-p) TAKER fee on both fills, venue.rs Fee::PM_SPORTS, makers pay none): gap >= 4 +1.68c vs 3.90c; gap >= 8
  +2.49c vs 4.33c; gap >= 15 +3.97c vs 4.44c. The signal is real and grows with the gap, but taking liquidity both ways
  costs more than the move. More training cannot fix that. Next lever: a MAKER exit (rest the sale as a limit order at
  the bookmakers' level: no fee, no exit half-spread, ~2c saved), simulated with a conservative fill rule on the
  recorded book (filled only when the price trades through it), taker sale at the give-up otherwise.
- DOES POLYMARKET FOLLOW THE SUPERLINE? (`superline_follow.py`, 23 Nov, owner: "superline is ahead a lot"): 68,269
  "superline ahead" onsets in 6 days (~11k a day; median gap 2.5 pt). PM's median move at +10 s .. +5 min is 0; it
  closes half the gap within 1 min 49% of the time and moves 1 pt the other way first 45%. The BIGGER the lead, the
  LESS PM follows: gap 10-20 pt half-closed within 1 min 29%, other way first 63%; gap 20+ 13% / 76%. The 5-min move
  minus the round-trip cost is -4.3c on average. Reading: big persistent leads are mostly the superline being wrong
  (stale / suspended bookmakers, a different market); on real events PM moves within ~1 s, usually before our
  bookmaker feeds (1.7-3.5 s feed delay) show the jump. Levers left: faster bookmaker feeds, maker exits.
- LIMIT ORDERS (`maker.py build/test`, 23 Nov, owner: "test everything"): four ways per candidate, $ per $100 with
  depth: TT taker both ways; TM rest the sale at the target (filled when the bid goes 1c PAST it, no fee); MT rest a
  buy at the bid for 30 s (filled when the ask drops 1c below it, no fee); MM both. Over all 161k candidates every
  cell loses: TM is $1-2 per $100 WORSE than TT (a resting sale gets exactly the target and misses fills, the taker
  sale gets the overshoot; most losers never reach the target either way); limit buys fill 10-17% and lose as much
  (they fill when the price comes against us). Clean rule + veto: TT -$25 (53 trades), TM +$7 (51; 90% -622..+745);
  with limit buys no rule made money on earlier days (no trades); the finder with all 12 exits -$169 (41).
- FASTER FEEDS (`faster.py`, 23 Nov): books that stamp their publish time: Kambi (unibet; 1 s stamps, +0.5 s used),
  SportyBet, Betcity (stamps are UTC+3: -3 h). Our feed is 1.7 s (Kambi), 1.9 s (SportyBet), 3.2 s (Betcity) behind
  (median). 179,070 jumps >= 0.12 log-odds on 2,709 PM games. Polymarket had ALREADY moved the jump's way before an
  ideal feed (publish + 0.25 s) in ~67-72% of jumps. Ideal feed vs today (receive + 1 s), $ per $100, taker:
  small jumps (median 3 pt) -$9.52 vs -$10.28; medium (5 pt) -$9.76 vs -$11.45; big (median 17 pt, goals) -$6.43 vs
  -$7.28: the move left is +5c but the cost is 10-11c (spreads blow out at events). Faster + limit sale is worse
  (-$7.00 on big jumps). Neither lever, nor both, turns a profit; a faster feed is worth ~$0.7-1.7 per $100.
  Reading: Polymarket usually moves first; when it has not moved, the bookmaker is usually not worth following.
- THE 30% PM HAS NOT FOLLOWED (`faster.py` now also dumps faster_events.npz with confirmations from other families,
  spread, price, sport; `faster_slices.py`, 23 Nov, owner: "that's still 30% of opportunities"): eligible jumps 57,504
  (ideal feed) / 46,795 (today). PM then makes >= half the bookmaker's jump within 1 min in 43% / 39%; even those
  lose -$2.85 / -$3.05 per $100 (most jumps are ~3 pt, a round trip costs 5-6c), so a perfect "PM will follow"
  predictor would still lose with this trade. Big jumps (0.7+ log-odds, ~17 pt): PM follows 22%. More confirmations,
  tighter spreads and 20-80c tokens lose less, nothing positive alone. Best slice looking at all days: jump 0.7+,
  2+ other families already moved, spread <= 1c: +$2.78 per $100 today (253) / +$8.46 ideal (192), but the top 5
  trades carry more than all of it, median trade -$0.96, positive 17/18/20 Nov only, football carries it. CLEAN
  (filter chosen on earlier days by total minus one SE): -$642 over 120 trades (90% -1,056..-253); ridge -$258 (88).
- MINIMUM WIN 5-6c (`min_win.py`, 23 Nov, owner: "requiring at least 5-6c for wins"): the bots' min_edge (expected
  profit per share IF PM reaches the target, after half spreads, 1c slippage and both taker fees; the pinned live plan,
  adaptive.rs STRICT Policy, uses 4c; no UI field, a code constant). W = 0/4/5/6/8/10c, $ per $100 with depth:
  every candidate: bookmaker leads -$14.66 (W 4) / -$13.98 (5) / -$13.69 (6); 1st drawing and superline lead no better;
  the win rate FALLS (40% -> 33-35%) because a farther target is reached less often. Clean rule + veto: W 4 -$75 (56),
  W 5 -$100 (48), W 6 +$160 (44; 90% -355..+716), W 8 +$63 (35), W 10 -$308: noise, not a trend. Jumps PM had not
  followed (today's timing): W 5 -$2.39 per $100 (1,333), W 6 -$2.19 (1,113); ideal feed W 8-10 about break-even.
  min_edge sizes the win if it happens, not its chance; it does not make the signals profitable. Not changed in code.
- THREE MORE ANGLES (`angles.py`, 23 Nov, owner: "research harder, figure out a solution"), all LOSE: (A) pre-match,
  PM drifts toward the bookmaker consensus only on big gaps (gap >= 4 pt: +1.5 pt in 60 min) and signal trades lose
  ~$11 per $100 (win 6%: pre-match PM barely moves within the hour); (B) fading PM jumps no bookmaker confirms: tiny
  reversal (+0.2 pt), -$16.64 per $100; following confirmed jumps -$10.53; (C) betting stale BOOKMAKERS after a PM
  jump: their odds are still old 1 s later 35-79% of the time but the value is negative everywhere (margins);
  only Kalshi showed +$3.84 at its MID.
- POLYMARKET LEADS KALSHI (DISPROVED the same night: see "KALSHI PRINTS" below; kept for the record) (`kalshi_lag.py`, `kalshi_check.py`, 23 Nov). PM jump in-play (mid >=
  0.12 log-odds in 10 s, PM spread <= 4c), decide 1 s (or 5 s, if still there) later; buy the Kalshi contract the
  jump's way when Kalshi's ORDER-BOOK ask is >= 5-10c below PM's mid and Kalshi's spread <= 2c; sell into Kalshi's
  bid when it reaches PM's level, else at 5 min; Kalshi fee 7% p(1-p) both fills; outcome flip per names_swapped;
  games dropped when the two venues' 1-min mids correlate < 0.8 (mapping check). TRAP: kalshi_quotes `bid`/`ask`
  are Kalshi's TICKER channel, which lags the order book (after a 2c book move the ticker catches up median 1.5 s,
  p75 22 s, p90 255 s); use `book_bid`/`book_ask` (rebuilt from orderbook_delta). Ticker prices showed +$8.11 per
  $100 clean; order-book prices (recorded only 20 Nov 03:12 -> 22 Nov 14:29 on the Mac) still: room >= 10c, decide
  1 s: 476 trades +$17.07 per $100, $+8,107 (90% +5,660..+10,724) buying 1 s after deciding (0 / 1 / 2 s delay:
  +17.37 / +17.07 / +16.96: the cheap asks persist); room >= 5c: 1,018 trades +$7.92. Where the ticker equals the book
  (sizes known, so not a phantom level): room >= 10c 151 trades +$17.37, every day positive (20: +$12.25, 21: +$14.88,
  22: +$57.46 per $100), median trade +$12.78, $ at the best ask median $1,460 (a $100 order fills). By sport
  football +$26.27, cs2 +$17.03, basketball +$9.89, hockey -$2.87 (2 games). Clean day by day (only 21-22 Nov have
  earlier order-book days): +$1,449 over 102 trades (90% +965..+1,957). Caveats: 2.5 days of order-book data; the
  Mac stopped recording Kalshi's book on 22 Nov 14:29 (check its Kalshi WS); no Kalshi trade prints, so queue / fill
  against faster bots is not modelled beyond the 0-2 s delay test. Next: a paper bot on the Mac.
  CORRECTION (same evening): kalshi_check.py has NO feed-hole check. The earlier Mac test of the same idea
  (`tools/kalshi_snipe.py`, 21 Nov, 1.2 days of book data; report re-printed from run/import/.../research/kalshi_snipe.json)
  skips signals where the WHOLE Kalshi feed (all rows pooled) had a silent stretch > 2 s from 5 s before the signal to
  the buy: then the "old price" is one we stopped seeing. Under its full rules (venues within 5c before, PM 4+ pt jump,
  Kalshi followed <= 20%, 3c+ room after fees, $20 at the ask) it had only 6-11 trades, +17..+24% a $ (90% -14..+79%);
  without the hole check +69% on 30 trades. The whole Kalshi feed was silent > 2 s for 21.2 h of 59.3 h (2,946
  stretches). With the hole check on kalshi_check.py's trades (decide 1 s, spread <= 2c, buy 1 s later): room >= 10c
  292 trades +$3,489 (+$11.95 per $100; 90% by game +2,194..+4,945) but MEDIAN trade -$3.60 (big wins when Kalshi
  catches up), 20 Nov +$2,482, 21 Nov +$1,021, 22 Nov -$15; football +$23.35, basketball +$3.30, cs2 +$23.17, hockey
  -$2.81; room >= 5c 563 trades +$3,054 (22 Nov -$257). The trades inside holes made +$25 per $100: half the earlier
  profit was feed holes. Real but weaker than first reported, not proven, fading on the last day. Any bot must skip
  signals while the Kalshi socket is quiet.
- KALSHI, TESTED HARDER + OUT OF SAMPLE (`kalshi2.py`, 23 Nov night; kalshi2_mac.txt / kalshi2_pc.txt): hole check
  built in (`flow`), Polymarket feed lateness, bookmaker confirmation, 5 exits, and rules FROZEN on the Mac recording
  (kalshi_rule.json) then applied unchanged to THIS PC's recording of 23 Nov 02:49 -> 20:03 UTC (the PC records
  Kalshi's order book since 23 Nov; feed silent > 2 s for 8.3 of 17.2 h: the collector was stopped for hours).
  Mac, one position a game: "plain" (decide 1 s after the jump, order-book ask >= 10c below PM, spread <= 2c, sell at
  PM's level else 5 min) 119 trades / 70 games +$1,836, +$15.43 per $100 (90% +1,149..+2,552), win 50%, median
  -$2.19; "picked" (ask >= 8c below, sell after 5 min) 154 trades +$12.53. PC, never seen: plain 11 trades / 6 games
  +$248, +$22.53 per $100, win 73%; picked 13 trades +$13.87, win 77%. Small but the same sign on another machine
  and day. What the split shows: bookmaker confirmation does not matter (conf >= 1 +$10.32, conf = 0 +$13.67);
  selling after 60 s LOSES (-$2.44, win 10%) while 5 min makes +$11.64: Kalshi takes 1-5 minutes to catch up; the
  profit sits where THIS contract had no quote change for > 10 s (235 trades +$14.12; quoted in the last 10 s: 56
  trades +$2.51) = a book nobody is updating; football +$23.28, cs2 +$23.17, basketball +$3.30, hockey -$2.81;
  22 Nov alone -$0.17 (9 games, mostly basketball / hockey). OPEN RISK: a 10c gap resting for minutes should be
  taken by arbitrage bots; either those asks cannot really be hit (queue, our book rebuild) or nobody is there.
  Only Kalshi's trade prints or real small orders can tell: the collector does not record Kalshi trades yet.
- KALSHI PRINTS: THE KALSHI "EDGE" IS OUR OWN DELAYED RECORDING (`kalshi_prints.py`, 23 Nov night). Kalshi's public trade
  history (GET https://api.elections.kalshi.com/trade-api/v2/markets/trades?ticker=&min_ts=&max_ts=, no key; fields
  created_time, yes_price_dollars / no_price_dollars, taker_side, count_fp; event markets via /events/{ev}?
  with_nested_markets=true) around every test buy of kalshi2.py (ask >= 10c below PM, feed "flowing"): the contracts
  traded actively (median 104 prints in 5 min, 95% had prints in the minute before), yet a real taker bought our side
  at our price or cheaper within 60 s in 1% of 291 Mac buys and 0% of 24 PC buys. Real prints ran a median +13c
  (Mac) / +15.5c (PC) ABOVE the price the test "paid" already in the 30 s BEFORE our buy and +15c / +18c after, the
  same as the gap to Polymarket (median 15c / 17c): Kalshi had already moved with Polymarket; OUR Kalshi order book
  (book_bid / book_ask, and the ticker) was behind by tens of seconds to minutes at exactly the busy moments (selling
  after 60 s lost, after 5 min won = our recording catching up). Example: test buys Yes at 32c while takers were paying
  54-60c for 15 s before and after. The trades that looked best were the ones where nobody really traded there
  (+$13.31 per $100) and the ones with real prints at our price lost (-$11.03). CONSEQUENCES: (1) no Kalshi edge;
  (2) every Kalshi result built on kalshi_quotes or the in-memory Kalshi book is suspect: snipe on Kalshi ("one Kalshi
  market froze 2 min after a goal"), predarb.rs "Kalshi-lag locks", arb.rs / arbsim.rs; a real order there meets the
  real (moved) book; (3) find the delay in books/kalshi.rs: rows are stamped now_ms() when PROCESSED, book deltas
  carry no venue time, ticker stamps already run 1.4 s (median) / 3.5 s (p75) / 13.5 s (p95) behind: suspect a
  backlog in the socket reader at bursts (one lock for every message, try_send to the writer); record the venue's
  own time per delta and compare with /markets/trades. The "whole feed flowing" check does not catch this.
  LESSON: validate any cross-venue lag against the lagging venue's own trade prints before believing it.
- THE MACHINE LOOKS FOR THE SIGNAL (`mine.py build/test`, 23 Nov night; owner: per-game lead record, no bets in the
  first 15 min live, "if x and y and z ... 80% safe in a sample of 50"). 1,618,129 in-play decision seconds x 2
  directions from 2,900 games; inputs = the 99 features (mirrored) + 18 causal per-game lead-record features (rA/rB
  cross-covariance sums at 5/20/60 s, lead20, lead15, wave-first share, lead-state shares, follow share of
  superline-ahead spells, the same frozen at live minute 15); labels = $ per $100 with depth (h60, h300, tp5); every
  sample averages -$11.4. (A) A game's lead is barely persistent: rank correlation of the first 15 min with the last
  15 min +0.14; the superline led in the warm-up in 25% of games, 39% of those still at the end (29% of the others).
  "Superline ahead our way": first 15 min -$10.80, after -$10.61; by any lead-record quintile -$9..-12, best 5%
  -$6.56; "bookmaker jumped, PM not followed": first 15 min -$16.84, after -$14.00 (the warm-up helps a little),
  best 5% of lead -$7.3; no lead-record rule made money on earlier days (no trades). (B) GPU histogram boosted trees
  (depth 6, early stopping, thresholds by total minus one SE on the last 20% before each day): the ranking is real
  (top 0.05% of predictions +$0.76 per $100 over 1,228 samples vs -$11.71 for the rest) but its very top is only
  break-even; trades: all inputs 29 trades +$5.75 (90% -46..+394; passed validation on 1 of 5 days), tp5 105 trades
  -$5.01 with 63% wins (a higher win rate, less money), without the lead record 291 trades +$1.38 (90% -448..+1,304).
  (C) rules spelled out: best leaf found +$17.65 (2,737 samples) -> unseen -$2.06; the 34 boxes with 80%+ wins in
  50+ samples (7,124 samples, 87% wins, +$12.56) -> unseen 52% wins, -$8.93; one box kept its win rate (87% -> 83%)
  at +$0.44 per $100: many small wins, few big losses. Verdict: more model capacity and per-game records do not find
  an edge in these inputs; win rate can be raised (take-profit exits) but not profit.
- THREE FOLLOW-UPS (`followup.py`, mine.npz now has 8 exits per sample: h60, h300, tp5, tp5 + stops s3 / s5 / a2,
  tp5 + time checks t30 / t60; 24 Nov, owner's questions). (1) LEAD INSIDE A GAME, 3,344 games: superline NOT leading
  in the first 15 min (74% of games) -> leading at the end 16%, at some point later 71%, but only 7% of the later
  minutes (median); leading early -> 23% / 86% / 19% (the same for 5, 10, 30 min). Share of a game the superline
  leads: median 11%, over half the game in 4% of games; Polymarket: median 42%, over half in 41%. First half vs second
  half rank correlation +0.41: the fifth of games where it led most early (45% of minutes) led 23% of the second half
  vs 6% elsewhere. NO link to profit: rank correlation of the share with $ about 0; even the top 5% of games lose
  -$8..-10 per $100 (hindsight or first-half -> second-half). (2) RULES OF THE DAY: a model from the day's first 35%
  of games never passed validation; earlier days 559 trades -$2.95; both 188 trades -$0.97 (90% -804..+486). "80%+
  wins in 50+ samples" boxes: from earlier days 89% -> 31% wins on the later games; from the same day's early games
  91% -> 41%, -$12.26. By sport (later games, model from both): football 115 trades -$2.88 (clearly negative), cs2
  -$3.11 (19), basketball -$2.55 (27), tennis +$16.80 (17 trades; 90% -105..+783). MAP 1 WINNER: not recorded
  (pm_quotes has match winner only; changes has 1x2 / total / handicap / btts), so it cannot be tested yet.
  (3) CASHING OUT LOSERS EARLY: every stop or early-out is worse on every set. Model picks (tp5-trained, top 0.2%,
  threshold from earlier days, 1,359 trades): tp5 -$1.62 (54% wins: +$7.88 each, losers -$12.68 each); 3c stop
  -$4.82 (cuts 56% of the eventual winners), 5c stop -$4.47, out at 60 s if not better -$3.35 (cuts 23%); a trade
  starts ~4c down (spread + fees) and wiggles, so winners look like losers early. What did help is the opposite:
  no take-profit (plain 5-minute sale) +$1.55 per $100, +$2,109 (90% -364..+4,872), 41% wins, median trade -$0.26,
  the 20 best trades make more than the total; NOT robust: a model trained on that exit itself gives -$1.28 (1,369
  trades). Tennis is positive in both (+$5.95 over 363, +$2.84 over 223): the one hint, unproven.
- WAVE TO WAVE + HIGH / LOW WAVES (every 20- and 30-minute result and the LONGSHOT-WAVE RULE in this bullet are SURVIVORSHIP BIAS: see the next bullet) (`waves2.py build/test`, `pm_prints.py`, `longshot_rule.py`,
  24 Nov; features.build(full=True) now also returns 'sl' and 'sl_eps'). Owner: "leading" must be measured wave to
  wave. One row per wave START (alarm second) of the superline and of Polymarket, in play: 353k rows, 2,845 games.
  (1) Matched pairs (same direction, onsets within 60 s): Polymarket followed 39% of superline waves, the superline
  36% of Polymarket's; in 89,228 pairs the superline started first 47%, Polymarket 51% (median delay -1 s); when the
  superline is first it is 25 s ahead (median), Polymarket 10 s. Per game: superline first in a median 46% of pairs,
  in more than half the pairs in 40% of games; first half vs second half of a game's pairs +0.36; 0-1 of the first 3
  pairs -> 42% later, 2-3 -> 53%. (So wave to wave the two lines are about even; the earlier "11% vs 42% of the
  game" was a continuous measure.) (2) The record does NOT predict dollars: "superline wave starts, Polymarket has
  not moved" -$10.97 per $100 (5-min sale), the same in every bucket of the game's record; first 15 min the same.
  (3) High wave (best sale within 5 min 6c+ above the buy) = 22% of wave starts; trees tell them at the start with
  AUC 0.77 (top 3% of calls: 56-61% high) but those picks lose with short exits (they are the volatile, expensive
  moments). HOLD LENGTH is what matters: the same top-3% picks make -$11.6 sold after 1 min, -$7.7 after 5, -$3.9
  after 10, +$4.3 after 20 and +$7.18 per $100 after 30 min (978 trades, 653 games, 90% +2,957..+11,327, 45-min
  purge; trails and wave-end exits are the worst). Checks: decided games at the sale (15%) LOSE -$33 and the rest
  make +$14.07; the profit sits where Polymarket's feed was on time (+$8.69 over 819 vs -$0.59 over 159 late);
  Polymarket's own trade history (Gamma /events/{id} -> conditionId, Data API /v2/trades?condition=) around the 25
  best and 60 random picks: real prints a median 1.0c BELOW the price paid and +0.2..+0.6c above the price sold,
  within 3c in 94% / 74-89% (the recording was the real market, unlike Kalshi). BUT the 20 best trades make the whole
  profit, and it is cheap tokens: by price paid 0-10c +$38, 10-30c +$8, 30-70c +$1.85, 70c+ -$2.23; tennis +$20,
  cs2 +$10, football -$6, basketball -$8.
  THE PLAIN RULE under it (no model; exploratory: found looking at all 7 days): tennis or CS2, in play, a wave starts
  for a token priced under 10c -> buy $100, sell into the bid 30 min later, one position a game: 832 trades / 551
  games, +$12,336 (90% +6,046..+18,879), +$14.83 per $100, win 31%, +$5,475 without the 10 best, positive on 6 of 7
  days (17: +2,071, 18: +3,520, 19: -74, 20: +393, 21: +2,177, 22: +2,403, 23: +1,846); superline waves only: 606
  trades +$26.89 (90% +10,090..+22,691); 10-20c about 0, 20-30c -$4.39, football 0-10c +$2.37 (carried by 10 trades),
  basketball -$10.98; sold after 20 min +$10.90, after 10 min -$14.54 (a cheap token's spread is 15-20% of its
  price; the edge is rare comebacks 5c -> 50c+). 38% of positions are decided at the sale. Caveats: thin markets
  (some winners in matches with ~50 prints in all; a $100 order is 1,000-2,000 shares), 7 days, rule found in
  sample. OUT-OF-SAMPLE: `waves2.py build --db run/oddsfeed.db --since <23 Nov 12:50 UTC ms> --sports tennis,cs2
  --name waves2_pc.npz` then `longshot_rule.py` (this PC's recording after the Mac export). FIRST RUN (23 Nov
  13:00 -> 20:53 UTC only: 53 tennis + 6 CS2 games): under 10c 52 trades / 30 games +$56, +$1.07 per $100 (90%
  -1,305..+1,650), win 23%; superline waves only 35 trades +$13.83 (90% -802..+2,082); 10-20c -$9.43, 20-30c -$8.86;
  sold after 20 min -$18.32. Same signs as in sample but far too few trades to confirm or refute a rule that lives on
  about one big comeback in 40 trades. Re-run on several more days of recording before trusting it.
- CORRECTION, SURVIVORSHIP (24 Nov, owner: "holding longer means we are no longer riding a wave, we are gambling, and
  somehow it's working ... buying at random and holding 30 minutes might be profitable too"). He was right. The
  feature grid ends when the bookmakers stop quoting (the game is over); every label builder dropped an entry whose
  sale fell after that (`r + horizon < n`). With a 30-min hold that keeps only games that went on for 30 more minutes,
  which for a cheap underdog means it had not lost yet: the longer the hold, the bigger the flattery (that is why the
  same picks "improved" from 1 min to 30 min). `waves2.py` now runs the price path for the labels on from Polymarket's
  own rows: past the end of the game when its last quote is decided (bid 97c+, or no bid / ask 3c-: the token's final
  value), else only as far as it was recorded. CONTROL: `waves2.py build --grid 300 --name waves2_grid.npz` = an entry
  every 5 min of play, both sides, no wave; `price_bands.py` puts wave starts next to it by price paid. Corrected,
  tennis + CS2, 30-min hold, one position a game, $ per $100 (wave start / superline wave start / fixed clock):
  0-10c -32.47 / -31.22 / -31.68; 10-30c -17.89 / -17.64 / -16.09; 30-50c -11.87 / -12.01 / -11.06; 50-70c -6.01 /
  -5.88 / -6.57; 70-80c -5.18 / -3.95 / -4.75; 80-90c -3.01 / -2.08 / -3.36; 90c+ -3.18 / -2.57 / -3.07. Football
  and basketball the same shape. Every band loses, the wave adds nothing a clock does not, and the loss is the cost:
  under 10c the average price paid is 6.8c and the average sale 5.5c (a 1c spread is 15-20% of a 5c token); 68% of
  those tokens end near zero. The model's top-3% picks after 30 min: -$0.61 per $100 (1,124 trades, 90%
  -2,944..+1,631); sport-only models tennis -$2.49, cs2 -$6.83, football -$6.87. Uncorrected files are in
  run/signals/mac/survivor_bias/. The same drop exists in mine.py / followup.py / features.py labels (5-min horizon:
  small, and those results were negative anyway). LESSONS: (1) never drop an entry because its exit is not in the
  recording: value it, or the test conditions on the future; (2) when a longer hold alone turns a loser into a
  winner, suspect the test; (3) always run the dumb control (a clock) next to a signal. What stands from the bullet
  above: the wave-to-wave facts (1) and (2), and that the trees can tell a high wave (AUC 0.78).
- TUNING THE LONG-HOLD PICKS + A $40 WALLET (`tune30.py`, 24 Nov, on the corrected waves2.npz; owner asked for price
  filters, a veto, fine-tuning and stakes of $10 / $5 / $2 or a share of a $40 wallet). 30-min model's top 3% by
  price paid: 50-70c -$0.67, 70-80c -$1.90, 80-90c -$0.79 (74% wins), 90c+ -$0.97 (81% wins); filters: 70c+ -$1.62
  (949 trades), 80c+ -$1.85 (90% -2,781..-37), 90c+ -$3.20 (90% -2,020..-331), under 50c +$7.12 on 74 trades (90%
  -636..+1,721; the plain rule under 50c is -$17 on thousands). Settings (hold 10/20/30 min, top 1-10%, price range)
  chosen per day on the earlier days looked like +$2..+6.50 per $100 there and lost on every day applied (-$1.07,
  -$1.77, -$1.29, -$3.03): pooled 892 trades -$1.73 per $100 (90% -3,362..+293). $40 wallet on those trades: $10 a
  bet ends at $2.44, $5 at $3.28, 12.5% of the wallet at $23.65, 25% at $9.33; $2 or 5% bets cannot be placed
  (Polymarket's 5-share minimum is $3.50-$4.85 at these prices); above $40 in 0-1% of 1,000 random orders. High win
  rates here are favourites: a win pays 10-25%, a loss takes the stake.
- THE MODEL LAB (`lab.py build/test`, `labfeat.py`; 24 Nov; owner: "train everything ... advanced math and research
  ... higher scores", "a random benchmark for all", "the minimum number required to turn a profit", "one strategy
  that combines all the advantages"). NOT YET RUN IN FULL: the PC was switched off; a 500-game trial got through the
  scoreboard, the combined-strategy step at the end is untested. Run it where the database is:
      python3 -m pip install numpy scipy torch          (once)
      mkdir -p ~/swifteye/research/lab
      python3 tools/signals/lab.py build --db ~/swifteye/oddsfeed.db --signals ~/swifteye/research/lab --workers 8
      python3 tools/signals/lab.py test  --signals ~/swifteye/research/lab | tee ~/swifteye/research/lab/lab_run.log
  build ~10-20 min (two files: lab_sec.npz = every 3rd in-play decision second, lab_evt.npz = every wave start);
  test ~30 min on an NVIDIA card; it picks cuda, else Apple's GPU (mps), else the CPU (slow: hours). If mps throws an
  "operation not supported" error: `LAB_DEVICE=cpu python3 ...`. The scoreboard is written to lab.txt before the
  last step. What it trains (each on the days before the day it is scored on): wave start up / down within 30 s (old
  network on the 99 inputs vs Newton-boosted trees with the logistic loss on the same inputs, on all inputs, and a
  blend with a network); next wave within 2 min; wave length (seconds left to the peak, vs always guessing the
  median); superline right (a superline wave started, Polymarket not in it: does Polymarket start one within 60 s);
  high wave. New inputs (labfeat.py): Hawkes-style intensities of Polymarket / superline / bookmaker-family moves at
  half-lives 5-600 s, burst ratios, order-flow imbalance of Polymarket's best bid / ask, the game's own wave history.
  Each line prints the no-model number (AUC 0.50, the base rate, the median guess) and, where the model drives a
  trade, the hit rate its picks need to break even (L / (W + L)) next to the hit rate they have. COMBINED: a last
  model on the wave starts reads the inputs plus every model's out-of-sample call and predicts dollars; top 3%, one
  position a game, next to the same model without the other models' calls. Labels use the survivorship-safe price
  path (waves2.one with lab=True).
- WHY NOT ON POLYMARKET: Polymarket is the fastest venue we record (it moved before the bookmaker even published in
  ~70% of 179k bookmaker jumps), so the superline, built from bookmakers, mostly trails it; when the superline is
  ahead and PM has not moved, the bookmakers are usually wrong (gap 20+ pt: PM follows 13% within a minute); and on
  the real leads the move left (+1.7..+4c) is smaller than the taker round trip (~4-6c). A lead only pays against a
  venue it leads (Kalshi looked like one, but that was our delayed Kalshi recording: see KALSHI PRINTS).
- ALL VERSIONS SIDE BY SIDE (`compare_versions.py`, 23 Nov; trades bought 19 Nov -> end of the Mac recording, 4.5
  days, cents a share, fees paid, no settlement luck; all "clean" rows chose everything on earlier days only):
  old snipe bots as planned 76 trades (17/day) -0.21c (90% -2.69..+2.16) total -16c; + model veto 59 (13/day)
  +0.42c total +25c (the 17 removed: -2.41c); + exit v1 -0.81c; + exit v2 -0.70c; veto + exit v2 -0.42c;
  bookmaker-leads rule 273 (60/day) +0.62c (-0.86..+2.15) total +168c; dedicated model 739 (163/day) +0.04c total +33c;
  1st drawing, model-taken 404 (89/day) -2.42c. Every range includes 0 except the losers: nothing is proven yet.
- BOOKMAKER-LEADS TRADE, CLEAN TEST (`booklead.py build/test`, 23 Nov, owner: "test it, train it"): the owner's exit
  (sell when the token's sell price reaches the bookmakers' level at entry, else at H) over 42,962 instances: PM
  caught up within 10 min in 50%; H 180 / 300 / 600 s: -2.53 / -2.44 / -2.15c a share. Day by day 19-23 Nov, every
  choice made on earlier days only, one position per game/outcome: all instances -2.73c (18,944); the RULE (room R,
  model OK, exit chosen before each day; from 21 Nov it settles on room >= 8 pt + model OK + sell at the bookmakers
  within 10 min) +0.62c a share over 273 trades (90% -0.86..+2.15), 49% win, +168c; the DEDICATED model (MLP on
  these instances only, 4 exit heads, tau on the last 20% before the day) +0.04c over 739 (90% -0.73..+0.80), 60% win.
  First non-negative clean result, not significant (5 days). Next: a paper trial of the rule live, to collect days.
- EXIT MODEL v2 (`exit_v2.py`, 23 Nov): bot-like entries (>= 2 families moved >= 1 pt in 5 s, spread <= 2c, gap
  >= 0.12 log-odds, PM not following; 23,820 positions, held to the plan -2.74c), entry quality as input, and a
  patience rule (sell when predicted delta < -m for P s, after M s; m/M/P chosen on validation per day). Against
  the bot's planned exits (no settlement, 99 snipe trades): bot +0.74c a share, v2 +0.41c (-0.33c, 90% -1.15..+0.52);
  sold early in 24 trades after a median 97 s (v1: 58 trades, 16 s); there the bot -5.79c, v2 -7.16c. On 20 Nov
  validation chose never to sell early. Validation (simulated plan: +3c / 15 min) says early sales help, the bot's
  real trades say no: the training plan is not the bot's (its target is pre_mid + catch x move, give-up 5/15/30).
  Next: train on the bot's own exit plan. The validation search was O(n^2) (re-indexed per row): fixed with prep().
- INSTANT EXIT vs THE OLD BOT'S HOLDING (`exit_vs_hold.py`, owner: the old bot holds 5/15/30 min because quick
  selling did worse; the model should sell at once when the plan fails). Built 29,960 old-bot-style positions
  (entry at a bookmaker jump PM had not followed, +3c take-profit else 15 min give-up; held they lose -5.45c),
  continuation model, day-by-day walk-forward, judged on 106 Mac snipe trades every 2 s: bot's real result
  -0.23c a share, model -0.88c (-0.66c, 90% -2.60..+1.41). It sold 63 trades after a median 16 s (bot held ~302 s);
  on those the bot made -4.67c, the model -5.78c: a fast sale locks in spread + fees. (Fixed 23 Nov: the path now
  runs on Polymarket's own prices to the bot's real exit; the feature grid ends where the bookmakers stop quoting.) Cause: trained on generic jump entries
  (bad, -5.45c), while the bot's own entries are filtered and come back more. Next: train on bot-like entries
  (the adaptive replay's candidates), give the exit model the entry's quality, require more evidence (min hold
  / margin chosen on bot-like positions).
- MORE TRADES (owner's goal, 23 Nov): model A has an expected-net head (outputs 14-15: net of buying up / down
  now under the plain exit, fees paid) and train_waves.py prints a volume curve (trades a day vs mean / total).
  First fit: too optimistic (trades it rates >= 0c realize about -2c); down at ev >= +1.5c made +1.1c over 58
  test trades (12 a day); up loses at every threshold. Old bots in the export: ~41 trades a day (favs 18.5,
  snipe 15, snipe-1book 4, snipe-trial 3).

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
- v5 (2026-11-18, research via `GET /adaptive/dump?reaction=&confirm=`, every move re-measured with other
  delays): faster reaction (0.25-0.5 s, no confirm) gives FEWER trades at similar return (a 2nd book needs
  time to move); 2.5 s gives more, 4 s hurts Polymarket. Winners: 2+ books, spread <= 2c, >= 4c expected gain,
  >= 7 pt move (PM), sell fully caught up, long give-up; after-pause moves lose on Kalshi. Grid gained give-up
  1800 s (EXIT_HORIZON 35 min); defaults changed to the strict rules (chosen on ALL data then: records that
  replay the past with them are in-sample); plateau now averages eligible neighbours only (ineligible ones
  used to pull every plateau below 0, so Kalshi could never switch).
- Cleanup (`src/prune.rs`, 2026-11-18): every 4 h at 00/04/08/12/16/20 UTC +10 min (and 2 min after start)
  deletes `changes` rows of games never linked to Polymarket or Kalshi and unlisted everywhere for 3 h;
  keeps Kalshi-only games (the bot trades Kalshi) and games in `line_moves`; keeps `matches`/`book_events`.
  Batches of 20k rows with pauses (never blocks the writer). No VACUUM: freed pages get reused. Status on the
  Data page (`/data` -> `cleanup`). Measured when built: off-mission games were 80% of GAMES but only ~2.6% of
  `changes` rows (~35 MB); the bulk is Polymarket 1x2 rows in `changes` (4.2M, duplicating pm_quotes as
  1/mid), bookmaker 1x2 (2.8M) and bookmaker totals/handicaps/BTTS (2.1M).
- Hold-to-result checked against OFFICIAL results (Gamma resolved prices, Kalshi `result`), 2026-11-18:
  no edge (208 one-per-game Polymarket bets won 111 vs 111.2 priced in). An earlier +$19.5k "hold" figure
  came from guessing winners from the last quote and was wrong. The edge is the short lag only.
