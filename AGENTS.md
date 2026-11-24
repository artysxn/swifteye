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
- **Linebet** (1xBet platform): `/service-api/LiveFeed/Get1x2_VZip?sports=1&count=500&mode=4...`. T 1/2/3 = 1X2, 7/8 = handicap, 9/10 = total, 180/181 = BTTS, `B` = blocked. Lists simulated leagues ("Short Football 3x3", "MLS+", "4x4"), which `is_esoccer` filters out. LiveFeed returns at most 200 games per request whatever `count` says, so live is asked in groups (`live_groups`); `champs=<league id>[,..]` filters by league (1-3 ids worked, 5+ answered 406).
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
4. LONG-TERM (owner, 23 Nov 2026): DIRECTION FROM WAVE TIMING. "Direction is the gap: nothing we have says which way
   well enough. If we can predict how long a wave lasts, say 30 s, then we can tell which direction a wave will go:
   at 30 s into an up wave, the next one goes down more often than up." Pilot: `tools/signals/pilot_wavedir.py`
   (do waves alternate; fading exactly at the peak as the ceiling; light models "has it peaked" / "fading beats
   following"). Results in section 8h.
5. LONG-TERM (owner, 23 Nov 2026): A WINNER MODEL with its own view of where the match is headed (live score, the
   teams' record, the suspicious-match signals), bets held to the result. Pilot: `tools/signals/pilot_winner.py`,
   `value.py`. Results in section 8h.

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
- Since 23 Nov the five Betby brands are read by the Rust Betby collector on every machine (main.rs
  Betby list, live every 3 s, pre-match every 5 min, every sport, the whole board) and `/ingest`
  ignores what the script posts for them (`browser::READ_BY_RUST`). The script stays as written; on
  the Mac only Cloudbet and Shuffle need it. Same games and event ids as Duel; on 23 Nov 09:40 a
  third to two thirds of their winner prices equalled Duel's, the rest differed (margins, timing).
  `snipe::platform` groups them with Duel.
- Config: `[browser_books] enabled / dir / python`; per book `[books.bcgame] enabled / paused`.
  The Mac has no `.venv` with camoufox, so Cloudbet and Shuffle are off there (one warning at start).

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
- REAL ODDS: POOLING POLYMARKET, KALSHI, THE BOOKMAKERS AND THE SUPERLINE AGAINST OFFICIAL RESULTS (`fair.py results /
  build / test`, 24 Nov; results = Gamma outcomePrices of the resolved moneyline markets, cached in fair_results.json:
  4,043 of 4,133 games; 54,535 snapshots of 3,225 games at 120 / 60 / 30 / 10 / 3 min before kick-off and 10..90 min
  in play). Bets held to the result pay the cost once. (1) WHO IS CLOSEST: before kick-off log loss Polymarket 0.5736,
  bookmakers' consensus 0.5747, superline 0.5744, mean of PM + books 0.5737; in play 0.5241 / 0.5267 / 0.5259; where
  Kalshi is quoted too PM 0.5864, Kalshi 0.5875, books 0.5890. Polymarket's price is the best single forecast and
  averaging does not improve it. (2) PM 10 min before kick-off is calibrated in every band (priced 6 / 15 / 25 / 38 /
  50 / 62 / 75 / 85 / 94% -> won 8 / 17 / 27 / 35 / 50 / 63 / 74 / 85 / 90%); buying at the ask loses -$1.4..-11 per
  $100 in every band; favourites at 55-90c: 1,913 bets -$2.30 (90% -4.9..+0.2), tennis +$1.02, football -$9.82;
  21-23 Nov -$4.27. (3) When the bookmakers are 4+ pt away from PM the truth is nearer the bookmakers (220 sides: PM
  74%, books 68%, won 67%; 205 sides: PM 27%, books 33%, won 31%); at 2-4 pt PM is right; superline and Kalshi
  disagreements: PM right. (4) POOL (logistic in log-odds with interactions) and boosted trees, trained on games 6 h+
  before each day: both WORSE than PM's own price out of sample (log loss +0.006..+0.008, 90% range above 0); the
  pool's weights: PM x0.97, bookmakers' gap -0.19, superline's +0.32. (5) BETS: the model's bets lose (-$14..-23 per
  $100 before kick-off); plain rules: bookmakers 2+ / 4+ pt above the cost -$6.49 (355 bets) / -$5.21 (95; 90%
  -44..+42); superline 4+ pt above the cost +$20.34 (146; 90% -16.7..+61.4); favourite 55-90c with bookmakers 1+ pt
  above the cost (the favs rule at T-10) 32 bets +$10.97 (90% -8.4..+29.0). Nothing is distinguishable from zero:
  a 27c bet has a standard deviation of ~160% of the stake, so a 7% edge needs ~2,000 bets (months at ~14 a day).
  In-play "bet on Kalshi where PM is above its cost" +$10 is the stale-Kalshi-recording artifact.
- "SUPERLINE 4+ PT ABOVE THE COST" TAKEN APART (`fair_sl.py`, 24 Nov, owner: "investigate"). The +$20.34 over 146 bets
  is not robust: counting only the 120 / 60 / 30 / 10-min snapshots (so a later closing price exists) the same rule
  is 137 bets at -$4.82 per $100 (90% -35..+30); the median bet is -$100 and without the 5 best it is -$41.79 (27c
  tokens: a lottery). The low-noise check, Polymarket's own move by kick-off (mid at the last snapshot minus the
  price paid; no signal = -1.17c, the half spread): superline 1+ pt above the cost -0.60c, 2+ -0.43, 3+ -0.30, 4+
  +0.02, 5+ +0.65, 6+ +1.35 (90% -1.28..+4.28), 10+ +1.76; mirror (superline 4+ / 6+ pt BELOW the bid) -1.70c /
  -2.69c. So Polymarket does drift about 1-2.5c toward the superline before kick-off when the gap is 4-6+ pt (a real,
  monotone effect), but that is a quarter of the gap: selling before kick-off loses -$13 (4+), -$10 (5+), -$5 (6+)
  per $100. How much of a gap is TRUE (logistic on results, log-odds = a + b PM + c (line - PM), resampled by game):
  superline c = +0.36 (90% -0.11..+0.89) 10 min before, +0.40 at 60 min, +0.36 in play; bookmakers' middle +0.33 /
  +0.45 / +0.13; highest family +0.19. Best estimate: the truth is about a third of the way from Polymarket to the
  superline, not proven different from zero. At a third, a 6-pt gap is worth 2 pt = the cost of the bet (fee +
  half spread at ~27c); only gaps of 8-10+ pt would clear it, and those are ~20 bets a week. By slice (all noise at
  these sizes): cs2 +$37 (32 bets), price 30-55c +$31 (32), 55c+ -$28 (21), first seen 2 h before -$55 (54).
- SUSPICIOUS-MATCH BETTING PATTERNS (`suspect_model.py fetch / test`, 24 Nov; cs2_activity has no direction, so every
  trade of 812 lower-tier CS2 games was fetched from the Data API and the patterns rebuilt from trades up to 10 min
  before the listed start; 373 games had 15+ such trades; median $1,875 traded by 39 wallets). The price is nearly
  right with a mild favourite-longshot tilt (priced 23 / 38 / 50 / 62 / 79% -> won 19 / 38 / 52 / 66 / 82%): always
  the favourite -$1.08 per $100, always the underdog -$14.43 (90% -25.6..-2.9). Patterns as bets: big money ($500+
  wallets, 20%+ of volume) on the FAVOURITE +$10.26 (103 bets, won 77% vs 70% needed; 90% -0.6..+20.4), on the
  UNDERDOG -$22.44 (31); one wallet with 40%+ of the money: its side +$6.18, against it -$34.47 (90% -51.0..-16.8);
  following a 10+ pt drift -$16.04 (90% -28.5..-2.9), fading it -$11.01; late money +$2.87; suspect.rs score 80+:
  favourite +$2.74, underdog -$18.12 (78 games: the favourite won 68% at 65c, so the 8-of-17 seen on 17-21 Nov was
  small-sample); games with 3+ big underdog bets: underdog -$17.64. A logistic model on all patterns is worse than
  the price alone (log loss +0.030, 90% +0.006..+0.056). Reading: heavy underdog money and drift do NOT win; the
  big wallets are mostly on the favourite and mostly right; nothing here is a proven edge on 373 games.
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

## 8h. Night of 23-24 Nov 2026 (Mac): every taker idea fails; the venue pays the resting side; liquidity rewards

Full log with every number: `~/Documents/dev/SWIFTEYE-NIGHT-LOG.md`. Data: `~/swifteye/research/lab` (lab files, paths),
`~/swifteye/research/rewards`. Python: `~/swifteye/venv-lab/bin/python` (torch on the CPU: macOS 13 has no MPS).

TWO TRAPS found on the way (apply to every earlier result in 8g that reads `pm_quotes` through Python):
- HOLES. A game's recorded Polymarket book can freeze for minutes or hours while it trades on (game 42521: bid 29 /
  ask 35 from minute 17 to the end, real trades 37c -> 100c). `tools/signals/paths.py` writes per-second files with
  `age` (seconds since the game's newest Polymarket row) and `late`; `paths.book()` gives fresh-only marks and
  `paths.row_quality()` flags lab rows. Rule: decide, count and value only where age <= 15 s (feed <= 1 s late).
  Half of the "CS2 bookmakers lead" result was holes. The lab's money labels were NOT re-run with this rule.
- PRINTS. A trade seen as "pm_quotes.last changed" is a biased sample (repeats at one price = the sweeps are
  missing): it showed the resting side keeping +0.51c a share; Polymarket's full public trade record with sizes
  (`prints_fetch.py`, Data API /v2/trades) shows 0.00c a trade, -0.35c a share.

RESULTS (3,493 games, 17-23 Nov; ranges by game are in the log):
- Value (`value.py`): who was right about the WINNER when Polymarket and the bookmakers' level disagree. beta (share of
  a gap that was real) -0.13; calm gaps of 3+ pt -0.05. Polymarket's price is the better forecast in every sport
  (CS2 +0.26 at 10 min on live rows only, 99 games). Bet toward the bookmakers, held to the result: -$2.70 per $100.
- Winner model (`pilot_winner.py`: trees starting from the market price; bookmakers, live score, record, suspicious
  flags): log-loss worse than the bare price (-3.5 x1000); bets on its disagreements -$8.70 per $100.
- Wave direction (`pilot_wavedir.py`): waves alternate (65%; small 84%, big 33%); fading at the TRUE peak is right 56%
  and makes -$7.70 per $100; "fading beats following" model right 88% on its top picks and loses even when right.
- Live score as a head start (`scorelead.py`): our score feed is behind Polymarket everywhere; a CS2 round = 1.3c.
- Model lab (`lab.py test`, 85 min on the Mac's CPU): wave start 0.801 / 0.779, next wave 0.774, wave length 34 s v
  51 s, superline right 0.714, high wave 0.780. Picks: wave start top 1% -$15.69 per $100 (loses even when right),
  high wave top 3% needs 79% right, has 62%. COMBINED model -$3.25 per $100 (2,082 trades), the same without the
  other models' calls (-$3.24); no model -$11.86. Nothing is above zero as a buy-and-sell trade.
- Same bet, two prices (`identity.py`): esports side markets that are the same event (after map 1 of a best-of-3,
  "over 2.5 maps" = "the map-1 loser wins map 2"; "leader -1.5" = "leader wins map 2"): 21 episodes of 1c+ locked
  after fees in 2.5 days with strict freshness, $16-32 on a $100 cap. Real, tiny.
- Resting orders (`rspread.py`, `makersim.py`, `makersim2.py` on real trades): all the resting side's profit is queue
  position (a level that survives the print keeps +0.67c, one that is taken out loses -0.85c). True realized spread:
  <=1c spreads -0.20c a trade, 3-4c +0.55, 5c+ +1.28. Our order one cent inside a 5c+ spread: +0.5c a share at 2 s
  reaction, negative at 3 s. Orders resting away from the price: negative.

POLYMARKET FACTS (docs.polymarket.com, read 24 Nov): makers never pay a fee; sports taker fee 0.05 x p(1-p); marketable
orders on sports markets wait in a delay window and cannot be cancelled meanwhile (Gamma `secondsDelay` = 1 on our
games), resting orders can always be pulled; post-only orders; smallest order 5 shares. So the venue charges and
delays exactly what the snipe bot does.

LIQUIDITY REWARDS (the one positive lead; NOT verified by our own orders):
- Public list `clob.polymarket.com/rewards/markets/multi`: 8,359 markets with a daily pot, $144.7k a day in all;
  minimum size 20 shares in most; each minute an order scores ((v - s) / v)^2 x shares (v = the market's max
  distance from the midpoint, s = its own), a maker's score is its smaller side (one side alone / 3), the day's pot is
  shared by score over the minutes in which anyone qualified. Public per-wallet record: Data API `/v2/user-pnl`
  (trading result, reward income, maker rebates by day), `/v2/activity`.
- Real wallets (`tools/rewards/farmers.py`): 536 wallets that rested orders in small reward markets in late October,
  followed 1-23 Nov: 52% in profit, median +$16; rewards $1.32M, trading -$5.73M. For reward farmers trading losses
  run at 92-99% of rewards. Volume $20k-200k: 56% in profit, median +$229. Small quiet farmers (62): rewards $17.0k,
  trading -$9.9k, half in profit. A few bots keep 20-40% (0x9efda3d6: rewards $123k, trading -$76k, +$52k in 23 days,
  up 22 of 24): the minimum size in thousands of tiny pots, merging at once (`bots.py`).
- Cost of a resting fill, from those bots' own fills (`markouts.py`, 2,460): +0.8c after 5 min, -1.4c after 1 h,
  -2.4c at the end = about -$0.50 per 20-share fill (-$1.27 in low-competition markets with a bigger pot).
- Night rule (02:30): pot $5+ a day, min size <= 50, 24-hour volume under $300, 3+ days to the end, a real two-sided
  book (< 20c wide, 5-95c), others' qualifying score <= 20; the minimum size on both sides. **Corrected in the
  morning (24 Nov 06:00), three things:**
  1. Others' score is NOT min(Q bids, Q asks). With the price in 10-90c a one-sided maker still scores a third of
     his side: others hold between max/3 and (2 min + max)/3; outside 10-90c between 0 and min. Use the high bound.
  2. Our order's score must use its real cent price (rounding can cut the score 4x at the edge of the limit).
  3. The "+$6.81 a market-day" week-of-history number is VOID as a reward estimate: it assumed last night's empty
     books for the whole week. 99 of the 102 picks had a pot dated 23 Nov and 72 were markets created that day.
     Only its fill rate / fill cost stand (0.5-0.8 fills a day, a fill -$0.2..-$0.5, worst -$8; `hist_cost.py`).
- What the rule really finds: BRAND-NEW markets the bots have not reached. They arrive within hours: the minimum
  order's share of the pot goes 26% (first hour) -> 15% (hours 2-5); others' median score 7 -> 36. For ALL quiet
  long-dated reward markets by age the minimum order earns $0.1-2.3 a market-day, against 0.5-2 fills a day at
  $0.5-1.3 each: break-even, as the real wallets showed. So: placing by hand once a day is NOT a profit.
- What is left (unproven): a scanner bot that rests the minimum pair where nobody qualifies yet and leaves when
  others arrive. Forward check with exact prices, 5.1 h, rule at the first pass (`forward3.py`): pot share
  $4.56-14.16 a market-day by distance (edge ... 1c), 2.1-4.0 fills a day, net +$1.87..+$9.07 at $1.27 a fill.
  Paper bot on the same hours (`paperbot.py --prefix rec1`): profit only if filled shares are HELD (+$5..+$12 a
  slot-day); selling them at the bid of an empty book loses everything (one fill: bought at 77c, bid 6c, -$14).
  Fills cluster right after a market starts to look empty (waiting 50 minutes before entering: almost no fills).
- Checks that passed: the Yes book is the exact mirror of the No book (so /book shows both sides); Polymarket's own
  `market_competitiveness` agrees with the book score (rank correlation +0.92; scale ~8% of ours); re-set interval
  hardly matters; `POST clob.polymarket.com/books` returns 200 books in 0.2 s.
- Running since 24 Nov 05:51 (`tools/rewards/rec2.py`, 48 h): ~1,540 quiet reward markets every 2 minutes, the
  reward list re-read every 30 minutes (new markets enter). `paperbot.py` trades on it on paper; `pick.py` lists
  the best markets of the moment from it (stale within hours).
- Weak points: fill cost has a fat tail and too few fills seen (16-28); filled shares in an empty book cannot be
  sold at a fair price; the mid of an empty book is not a price; the payout itself (a tiny order alone taking most
  of a pot) is only the published formula, never seen paid to us; capacity is ~100 markets and a few hundred $ a
  day in all, shared with every other scanner; pots and rules change daily.

## 8i. Order desk (2026-11-24): bets at any book through one call

Owner: "Set up an integration to be able to place orders on the different websites through an api / bot. I will then
later wire it myself to be able to work on the website (you dont need to bypass cloudflare)." Full description:
`docs/DESK.md`. Read it before touching `src/desk.rs`.

- `src/desk.rs`: tickets of legs ("back this outcome at this book for at least these odds with this stake"), placed
  one at a time in the order given. Polymarket legs: Swifteye itself (`exec::pm_order`, FAK at the price that keeps
  `min_odds` after the fee). Every other book: a DRIVER, the owner's own program where he is signed in; it pulls work
  (`GET /desk/next`, long poll) and answers (`POST /desk/result`). Swifteye never signs in to a bookmaker here and
  does nothing about a site's bot checks.
- Modes: `paper` (default; fills at the recorded price / the Polymarket book), `dry` (drivers read the bet slip and
  report its odds and limit, nothing confirmed), `real`.
- Real needs ALL of: desk `real`, the book's `real`, stake <= the book's `max_stake`, the book's 24-hour `daily_cap`,
  the bots' kill switch off. Switching real on or raising a limit needs `"confirm": "REAL MONEY"`. Agents never send
  that to the owner's desk: arming is the owner's (same rule as the Bots page).
- Every changing call and `/desk/next` need `X-Desk-Key` (made on first start, in `<db dir>/desk.json`, private like
  telegram.json; never shown by the API). Without it a web page in the same browser could queue a bet via localhost.
- Invariants (tested): an order is handed out ONCE, ever; a driver that goes silent leaves the leg `unknown` and the
  ticket `check`, never a re-send; a failed later leg after a placed one = `exposed` (+ Telegram for real); later legs
  are re-sized to what leg 1 really got; a restart cancels untaken legs and marks held ones `unknown`.
- Files next to the database: `desk.json` (settings + key), `desk_tickets.jsonl` (every state of every ticket; last
  line of an id wins). No SQLite table on purpose (the writer's lock, 21 Nov). Real Polymarket orders also go to
  `bot_orders` as bot `desk`.
- `oddsfeed desk-sandbox [port] [folder]`: the desk alone with two pretend bookmakers (`alpha`, `beta` with swapped
  teams) and a pretend Polymarket book; no feed, no database, no account; a real Polymarket leg is refused there.
  `tools/desk/driver.py` (`--mock` = a pretend bookmaker; else three methods to write per site).
- Tested 24 Nov on the Mac: 12 tests (`cargo test desk::`, one runs the live desk in-process against a real engine),
  and the sandbox over HTTP with the Python mock driver (paper, dry, real, expiry, limits, key, a refused browser
  preflight). NOT tested: a real Polymarket order through the desk, any real bookmaker site (no site driver exists).
- BOOKMAKER LOGINS (same day; owner: "Set up a sign-in system using email and password that i input for them but make
  sure the bots can use my account"): the owner types them on the Order desk page (`web/desk.js`, `#/desk`), they live
  in `<db dir>/desk_accounts.json` (mode 600; never committed, never shown again, the login shown cut short). Saving is
  allowed from the app's own page on this machine (Origin = the local host it was sent to) or with the desk key. The
  ONLY reader of a password is a driver: `GET /desk/account?book=` + key; it reports `POST /desk/account/state`
  (`signed_in`, `signed_out`, `refused`, `needs_owner`). After `refused` / `needs_owner`, or 3 hand-outs with no
  `signed_in`, the login is held back until the owner saves again (or signs in by hand and the driver says so); dry
  and real tickets for that book are refused; one Telegram message. Swifteye never signs in to a bookmaker itself, and
  nothing here answers a captcha or a code. Agents never type the owner's passwords (tests use made-up values in the
  sandbox). `tools/desk/driver.py`: `Site.signed_in()` / `Site.sign_in()` / `NeedsOwner`.
- Not built: handing in tickets or changing limits from the page, Kalshi legs, Polymarket markets other than the
  winner, maker orders, anything that decides what to bet. `POST /desk/from_arb` turns a tracker row into a ticket, but tracker rows are unchecked against bet
  slips (8h, "arbitrage"): run them `dry` first.

## 8j. Kalshi feed rebuilt (2026-11-24): why it went silent, what changed, how to check it

Owner: "And fix the kalshi feed". Code: the socket part of `src/books/kalshi.rs`; `GET /kalshi/feed`;
`oddsfeed kalshi-probe <minutes> <out folder> <folder holding kalshi.json>` (the Kalshi feed alone: no other feed, no
database, no bot, no order); `tools/kalshi_feed_check.py <out folder>` (holds a probe's recording against Kalshi's
public trades).

- ROOT CAUSES, from the Mac's own log (`~/swifteye/logs/oddsfeed.log`, 18-22 Nov) and Kalshi's WebSocket documentation:
  1. A FALSE "sequence gap". Kalshi numbers every message of a connection's order-book stream: snapshots, changes AND
     its "ok" reply to a command on that stream (a repeated `subscribe` that adds contracts). The feed counted only
     snapshots and changes, so an "ok" followed by a change looked like a lost message, and the connection was dropped.
     Log: 1,545 such drops in 4.9 days; exactly one number skipped in 1,446 of them (two in the other 99); 63% within
     15 s of connecting (median 1 s: the second subscribe command's "ok"). A busy market only raises the odds that a
     change, not a snapshot, follows the "ok": it was never about price jumps.
  2. The wait before reconnecting doubled on every drop and NEVER came back down (a session only ever ended with an
     error): 60 s in 1,318 of the 1,545 drops = 22.6 hours spent waiting to reconnect. This is "the whole Kalshi feed
     silent 21.2 h of 59.3 h" (8g).
  3. During a drop the old order books stayed in memory, on record and in the arbitrage's ladders as if current.
  4. The socket was read on the shared runtime, behind every other feed and the bots, and rows were stamped when
     processed, not when received.
- WHAT CHANGED: its own thread and runtime; everything waiting is read before anything is published (one publish per
  contract per batch); every numbered message of the book stream is counted; a real lost message empties EVERY book
  and asks for fresh snapshots on the same connection (`update_subscription` / `get_snapshot`), reconnecting only if
  under 80% are back in 20 s; a book is read (best prices, depth, ladder) only while IN STEP, and a dropped connection
  empties all of them (rows are written with `book_bid` / `book_ask` NULL at that moment); a book that names other
  best prices than Kalshi's ticker for 10 s gets a fresh snapshot (at most every 5 min per contract); the reconnect
  wait is 1 s after a connection that lived a minute, doubling to 30 s only for failures in a row; finished games
  are forgotten from the subscription list.
- DATA: `kalshi_quotes.ts_ms` is now the time the message was taken off the socket. New column `book_ts_ms` =
  Kalshi's own time of the order-book change behind `book_bid` / `book_ask` (their delay = `ts_ms - book_ts_ms` on
  rows written by a book change). `bid` / `ask` are still Kalshi's `ticker` channel, which Kalshi sends once a second
  by design (so up to ~1 s behind the book): NOT changed, the engine's Kalshi price and the bots' inputs are the same
  source as before. `GET /kalshi/feed`: books in step, lost messages, reconnects, delay behind Kalshi's clock.
- DISCOVERY (found by the live check, same day): a series that could not be read in a round (Kalshi answered "too many
  requests": 8 requests at a time was over its limit) was skipped, and its games fell out of the window until a later
  round got through. The game list swung 86 -> 121 -> 88 -> 240 -> 200 -> 76 in ten minutes, and a contract whose game
  had fallen off was not recorded although the socket still carried it (2-minute holes in single contracts). Now: such
  a series keeps its games, 4 requests at a time with a pause after a refusal, and the contract -> game table is only
  ever added to. After: 231 -> 276 -> 278 -> 277.
- NOT changed: which price feeds the engine and the bots; the REST re-seed.
- VERIFIED 24 Nov on the Mac with the feed ALONE (`kalshi-probe`, debug build; 8.7 min, 566 contracts, 750,349
  messages = 1,400 a second): 0 lost messages, 0 reconnects; messages arrive a median 41 ms after Kalshi sent them
  (99 in 100 under 54 ms); longest silence 1.4 s; contracts were added mid-run (the case that used to drop the
  connection) with nothing lost. Against Kalshi's public trades in the 80 busiest contracts (1,809 trades, sweeps
  counted once): our book's best price just before the trade WAS the traded price in 99.4% (Kalshi's clock), and it
  was what we were showing at the trade's own moment in 93.0% (our clock; the rest moved in the last tens of ms).
  The first run, before the discovery fix, scored 84.9% / 73.8%: every miss was a contract whose game had fallen off
  the list. An earlier run also showed the in-place re-sync working (238 books back in step within seconds) and one
  "connection reset by peer" after 10 min, back in 1 s. On 23 Nov the old feed scored 1% at busy moments.
- NOT verified: inside the full collector (Kalshi is paused on the PC; nothing here ran with the other feeds and the
  bots beside it), a release build, a busy US evening, a truly lost message (only the false one, which exercised the
  same recovery). The trade check must use the one-contract floor (crumbs of 0.01-0.5 contracts sit in front of real
  prices) and remember that Kalshi stamps a trade's book change a few ms BEFORE the trade.
- Every conclusion drawn from our Kalshi RECORDING before this (8g: "Polymarket leads Kalshi", feed holes, the paper
  arbitrages of 18 Nov, the Kalshi-lag locks) was measured on the broken feed and must be measured again before it is
  used. What Kalshi's own public trades showed (real gaps are swept in milliseconds) does not depend on our feed.

## 8k. Which way will the next wave go: the full direction test (24 Nov 2026, PC, on the Mac recording)

Owner, 24 Nov: "There may be a positive gain to be made if we are able to predict which way a wave will go with a
model, because we have a lot of signals and edges available otherwise that are beating the general public."
`tools/signals/wavedir.py build / test` (build 5 min with 22 workers, test 25 min on the 4090; data
run/signals/mac/wavedir.npz, report wavedir.txt). It follows the Mac's two rules of 8h: rows are decided only where
the recording is alive (the game's newest Polymarket row <= 15 s old, feed <= 1 s late: 18.9% of rows dropped) and a
sale is valued at the first live bid (a dead recording = unknown, not 0, not the frozen price). 1,351,278 in-play
decision seconds, 2,882 games; scored on 19-23 Nov, every model trained on the days before the day it is scored on.

A wave's FIRST MOVE = the second after its CUSUM onset; a call made before it is made before the price has moved.
- IS A WAVE COMING within 30 s: AUC 0.741 (up 0.799, down 0.777; a BIG wave of 0.45+ log-odds: 0.808 / 0.794). A wave
  follows 23.4% of decision seconds, 76% of the model's top 1%.
- WHICH WAY, given one comes within 30 s. (a) Every such second: 85.4% right (AUC 0.942), but that is one mechanical
  fact: in 95% of them Polymarket is already moving, and a wave the same way cannot "start" while one runs, so the
  next start is the pull-back ("against the side Polymarket is moving" alone: 86.5%; waves alternate 64.5%, as the
  Mac's pilot found). (b) POLYMARKET AT REST (no move for 10 s, both CUSUMs at 0; 9,052 seconds, 1,245 games): a
  model trained only there is right 58.3% (AUC 0.618); its surer half 63.4%, its surest 10% 71.7% (644 seconds, 279
  games). So the inputs DO carry direction before the move. Simple rules at rest: superline "ahead" 63.2% (has a view
  13% of the time), bookmaker families that moved in 5 s 59.2% (38%), order book best level 58.2%, order-flow
  imbalance 60 s 57.3%, superline gap 54.9%, trade prints 50%, Polymarket's last wave 50.6%. By the size of the wave
  that came: under 0.25 log-odds 58.7%, big (0.45+) 55.5%, the biggest (1.0+, goal-size) 45.6%: the bigger the wave,
  the less anything says which way. Within 2 min, at rest: 56.5%.
- THE CEILING ($ per $100, fees + depth; no model). Bought the side the wave then took: -$5.15 sold after 1 min,
  -$4.63 after 2 min, -$4.18 after 5 min, -$4.32 when the wave ends, +$1.18 at the very top of the wave (not
  knowable). Wrong way -$17. No wave -$11. A right call LOSES on the average wave: the median wave is 0.28 log-odds
  (about 7 pt at 50c), the spread 2c and the two fees 2.5c, and a fixed exit does not sell at the top. By price paid:
  20-80c -$2.07 (2 min), outside -$8.11. From rest: -$1.01 (2 min), -$0.24 (5 min), +$0.17 (wave ends). Only BIG
  waves pay when right: biggest 20% +$10.91 (2 min) / +$13.86 (5 min); big waves from rest +$20.65 / +$25.10.
  Big waves are 17% of the waves that start from rest = 1.9% of resting seconds.
- THE NUMBERS A CALL NEEDS (from rest, all 7 days; `wavedir_rest.py`, wavedir_rest.txt): big wave right +$19.96 / wrong
  -$37.72 (2 min), small wave right -$5.54 / wrong -$15.44, no wave -$10.33. If a big wave were CERTAIN the
  direction must be right 65% (2 min) / 62% (5 min). With a PERFECT direction a big wave must follow 34% / 30% of
  the calls; with 75% direction 65% / 55%. The model has 55.5% direction on big waves and a big wave after 8-12% of
  its at-rest calls. While Polymarket is moving it is worse (big right +$8.04, needs 78% direction).
- THE MODEL'S CALLS AS TRADES (thresholds from held-back earlier data, one position a game), every rule loses and
  every 90% range is below zero: up-minus-down chance top 0.1% 1,328 trades -$9.59 (2 min); chance x direction top
  0.1% 567 trades -$11.74; big-up minus big-down top 0.1% 569 trades -$4.97 (5 min); at rest only: 539 trades
  -$5.86, big waves at rest 627 trades -$6.04. The calls ARE right about the wave (their way after 57-84%, the other
  way 0-1%) and still lose: the surest calls sit seconds before the move at the widest spreads.
- ONE MODEL FOR DOLLARS that reads the inputs and all eight calls: -$2.28..-$4.03 per $100 (498-2,189 trades); the same
  model without the calls -$1.63..-$2.84. The direction calls add nothing to it.
- WAVE AFTER WAVE (237,783 waves): same way as the one before 35.5%; within 30 s of the last one ending 34.0%, later
  than that 50%. At a wave's onset the gap to the superline pointed its way 76.4% (Polymarket coming back to the
  level), the superline being in a wave pointed its way only 40.0% (Polymarket moved first and is pulling back),
  superline "ahead" 58.4%.
- LIVE SCORES (book_scores, 22 Nov 05:40 - 23 Nov 12:52; 21,065 changes two books agreed on, 537 games): Polymarket
  had moved before we saw the score everywhere. Football goals: +11.5c in the 60 s before, +9.8c in the last 10 s,
  0.0c in the 10 s after; buying the scorer then -$8..-$12 per $100. CS2 rounds: +2.0c before, 0 after, -$6..-$8.
  Tennis -$8..-$11, basketball -$13. Where Polymarket had not moved yet: -$7..-$15. Same answer as the Mac's
  scorelead.py with a stricter event rule (a second book must show the new score).
- New inputs tried: trade prints from pm_quotes.last (a biased sample, see 8h PRINTS; as a direction input ~50%)
  and the last waves' directions. Correction to 8g: map-winner markets ARE recorded since 21 Nov
  (pm_sister_quotes, kind child_moneyline; the Mac's identity.py uses them).
- READING. Direction is not the missing piece. Before the move the inputs call it 58% (72% for the surest tenth),
  which is real, but a right call is worth less than the round trip on all but the biggest fifth of waves, and for
  those the direction is the event itself (55%, 46% for goal-size moves) and our event feeds trail Polymarket.
  Untested use of the same calls: steering RESTING orders (which side to pull), on the Mac's real trade prints
  (makersim.py); a taker cannot use them.

RIDING A WAVE THAT HAS ALREADY STARTED (`waveride.py build / test`, waveride.npz / waveride.txt; owner, 24 Nov: "if a
wave lasts for 15 seconds, and we detect it 1 second in, there's still 14 seconds"). Direction is then known, nothing
is predicted. 179,212 waves in play with a live recording, 2,827 games, 17-23 Nov.
- TIME LEFT IS NOT MOVE LEFT. First move -> top takes 3 / 25 / 94 s (quartiles), so there is time; but the biggest
  single second carries 57% of the whole move (half or more in one second in 60% of waves) and that second IS the
  first move in 41% of waves (median 2 s after it). The whole move is 3.5c at the median (price 46c). A taker's buy
  lands 1 s later at the earliest (Polymarket's delay window): by then the mid has made 33% of the move (mean 44%)
  and the ask is 2.5c above the mid before the wave.
- THE WAVE'S SIDE, bought at each moment ($ per $100; 2 min / 5 min; hindsight that it IS a wave): 1 s before the
  first move -1.79 / +0.09; in the second of the first move -5.43 / -3.14; 1 s after -6.67 / -4.25; 3 s after
  -7.35 / -5.11; 10 s after -8.89 / -6.55; 1 s after DETECTION (CUSUM alarm, 82% of the move made) -13.77 / -13.32.
  Sold at the exact top (not knowable) +3.4..+4.4 before detection, -4.83 after it.
- BIG WAVES ARE SLOWER, and there the owner is right: big waves from rest (3,185; whole move 12.5c, to the top
  58 / 155 / 393 s; 14% of the move made 1 s in). Before the first move +28.68 / +33.08; 1 s after +10.57 / +15.48;
  3 s after +8.36 / +14.92; 5 s after +3.89 / +9.18; 10 s after +0.58 / +4.76; at detection -5.82 / -4.42. About
  half the profit survives getting in 1-3 s late, IF one knows at that second that it will be a big wave.
- WHAT CAN BE DONE LIVE. 61,123 first ticks from rest (2,694 games): 15% become a wave, 2.7% a big wave. Buy every
  first tick -$10.50 (one position a game: 21,691 trades -$10.78, 90% far below 0). By tick size: under 0.03
  log-odds -10.57 (4% waves), 0.06-0.12 -10.69 (25%), 0.12-0.30 -11.84 (98% waves, 11% big), 0.30+ -13.56 (100% /
  65% big: the jump was the move). The dial between first tick and detection (CUSUM at 25 / 50 / 75 / 100% of the
  alarm level): -10.54 / -10.76 / -10.73 / -10.72: flat. Hindsight split: ticks that became a big wave +4.12 /
  +6.33, a small wave about -11, no wave -10.9.
- A MODEL AT THE FIRST TICK ("will this tick become a big wave", all inputs seen from the tick's side; trained on
  earlier days, 30-min gap; 50,070 ticks scored): AUC 0.826; its top 1% are waves 97% and BIG 40% (all ticks 2.3%),
  but break-even needs a big wave after ~59% of picks and its surest picks are ticks that already jumped: -$9.35 per
  $100 (481 trades). A model trained on the dollars instead: top 1% -$2.64 (460 trades, 90% -1,440..-1,004), top 10%
  -$3.91 (3,559). Nothing above zero.
- READING. The money in "right call on a big wave from rest" is real and half of it is still there 1-3 s in; what is
  missing is knowing AT THE FIRST TICK that this tick is one of the 2.7%. Waiting until it is clear costs 82% of
  the move. A round trip costs ~9-10% of the stake (2c spread + 2.5c fees on a 50c token); the median wave's whole
  move is 3.5c.

## 8l. Resting orders steered by the bookmakers, and the rewards recorder on the PC (24 Nov 2026, PC)

Owner, 24 Nov: "experiment with resting orders then, and liquidity rewards." Data: run/signals/mac/lab (paths.py and
prints_fetch.py run here on the Mac recording: 1,006 two-way games, 309,086 public trades; no football, it is
three-way) and run/rewards_pc.

RESTING ORDERS. The Mac's `makersim2.py` gives the same baseline here (968 games, 237,923 real trades): what the
resting side of ALL real trades kept after 5 min, cents a share: spread 1c or less -0.47, 2c +0.29, 3-4c +1.16
[+0.33..+1.92], 5c+ +1.92 [+0.48..+3.67]; our own order one cent inside a 5c+ spread in a calm market: reaction 0 s
+0.65, 1 s +0.11, 2 s -0.07, 3 s -0.53 (hockey is an outlier at -5c: not understood, check its markets).
`makersteer.py build / test` asks the new question: a resting order only has to decide WHICH SIDE TO LEAVE IN THE
BOOK, and the direction study (8k) says the bookmakers know a little. Same fill rule as makersim2 (a real taker trade
reaching our price while we are first in line, or trading through it; live recording only; 20 shares; one fill a side
a minute); every fill is stored with all inputs known at the second the quote was decided, seen from our side of the
fill. 805 games with fills, 75,449 simulated fills over 12 settings (inside 2 / 3 / 5c+, 1 or 2 s late, calm or any
market), in play. Cents a share kept after 5 min, share-weighted, [90% by game]:
- PLAIN, any market: inside 2c+ -0.42 (1 s) / -1.17 (2 s); 3c+ -0.17 / -1.03; 5c+ +0.32 [-0.03..+0.69] / -0.77.
- WHICH SIGNALS SPLIT GOOD FILLS FROM BAD (inside 2c+, 2 s; 12,915 fills, all -0.98): the superline "ahead" on our
  side +1.13 [+0.03..+2.23] (676 fills), against us -3.22 [-3.96..-2.39] (1,060); bookmaker families that moved in the
  last 30 s: with us -0.18, against -1.77; superline above / below Polymarket: -0.23 / -1.92. Inside 5c+: families in
  5 s +1.50 [+0.07..+2.96] / -2.01 [-3.40..-0.74]; superline ahead +1.52 / -4.96 [-6.62..-3.48]. Polymarket's OWN
  move works the other way (a fill after it moved our way keeps -1.4, after it moved against us +0.7: the pull-back
  of 8k). Order-book size, order flow and trade prints split nothing.
- RULES, inside 5c+, 1 s late (4,517 fills): plain +0.61 [+0.26..+0.95], 17-20 Nov +0.51, 21-23 Nov +0.72, held to
  the result +1.48 [+0.15..+2.67]. "Never leave a side against the bookmakers' last 30 s": 72% of the fills, +1.21
  [+0.75..+1.65], halves +1.03 / +1.40, at the result +2.56 [+0.84..+4.19]. "Only with them": 22%, +2.17
  [+1.15..+3.18]. "Never against the superline's 60 s move" +1.28 [+0.80..+1.80]. At 2 s the plain order is -0.31 and
  the same rules bring it to +0.10 / +0.87 [-0.31..+1.95]: reaction time decides.
- A MODEL ON FILLS (trees on the fills of earlier days, all settings together, 10-min gap; 62,029 fills scored on
  18-23 Nov; rank correlation with what a fill kept +0.13). Leaving a side only where it expects a gain: inside 5c+,
  1 s: 1,920 of 4,152 fills, +2.33 [+1.75..+2.95], at the result +3.24 [+1.32..+5.05] (the pulled ones -0.57);
  5c+, 2 s: +1.43 [+0.64..+2.23]; 3c+, 1 s: +1.26 [+0.77..+1.78]; 2c+, 1 s: 4,439 of 12,755, +1.04 [+0.58..+1.52]
  (pulled -1.05); 2c+, 2 s: +0.21 [-0.22..+0.68]. By day (2c+, 1 s) -0.14, +0.53, +1.30, +1.11, +1.42, +2.06.
- LEAK CHECK (`build --lag 6 --name makersteer_lag6.npz`): a public trade's stamp is a few seconds late and is placed
  within 5 s, so an input read "at the decision" could hold a move made after the real trade. With every input read
  6 s EARLIER: superline ahead still +2.90 [+0.78..+4.93] / -2.96 (5c+, 2 s); the 5 s family signal is gone (it is
  6 s stale by construction); 5c+, 1 s: never against the bookmakers' 30 s +0.98 [+0.47..+1.42] (halves +0.86 /
  +1.11, result +2.14 [+0.57..+3.74]), never against the superline's 60 s move +1.32 [+0.79..+1.81], model +2.14
  [+1.37..+2.91] (result +2.98 [+0.68..+5.23]); 2c+, 1 s model +0.50 [+0.04..+0.93]. The truth is between the two.
- DOLLARS A DAY on the scored days, these 805 games only (5-min mark / held to the result; an order of S shares takes
  min(S, the real trade)): 5c+, 1 s, plain S 20 +75 / +159, S 100 +110 / +368; never against the bookmakers S 20
  +94 / +194, S 100 +218 / +554, S 500 +289 / +434 (leak check +76 / +161, +161 / +477, +194 / +225); model S 100
  +221 / +332, S 500 +355 / +274. 2c+, 1 s, model: S 100 +263 / +445 (leak check +49 / +139). The trades that
  filled us: median 23 shares, 90% under 223. So: a few hundred dollars a day at most at these sizes.
- NOT in the test: other makers stepping in front of us, the queue at a shared price, our own order changing who
  trades, getting out of a position other than waiting for the result, football. Nothing here has met a real
  order. FIRST POSITIVE RESULT of the project that holds in both halves of the week, out of sample by day and under
  the leak check; next = a live paper version in the collector (needs Polymarket's trade events with sizes recorded).

WHAT IMPROVES IT (`makersteer_tune.py`, on the same fills; 100-share orders, 6 scored days, these 805 games):
- ONLY WIDE SPREADS. By the spread when the quote was decided (1 s late; plain -> model keeps): 2c -1.35 -> -0.07;
  3c -0.63 -> +0.44; 4c -0.31 -> +0.01; 5-6c +0.24 -> +1.80 [+0.64..+2.94]; 7-9c +1.18 -> +2.49 [+1.15..+3.80];
  10c+ +0.73 -> +2.28 [+0.50..+4.22]. Under 5c nothing pays even with the model.
- SPEED. One more second costs the plain order 0.6-0.8c a share (5c+: +0.41 at 1 s, -0.39 at 2 s; makersim2 in a calm
  market: +0.65 at 0 s, +0.11 at 1 s); the model's picks lose less (5c+: +1.77 -> +1.30).
- NOT TOO PICKY. Model line at 0 to +0.5c is best (2c+: 4,439 fills +0.85c, $263 a day; +0.5c: 2,050 fills +1.55c,
  $224); above +1c it falls apart (830 fills, +0.38 [-0.95..+1.83]). Rule AND model lifts the cents (5c+: +1.77 ->
  +2.29 [+1.49..+3.09]) but not the dollars ($221 -> $195 a day). Staking by the call adds little.
- MORE DAYS. The model's kept fills by the day scored (fills seen before): 18 Nov (4k) -0.17, 19 Nov (12k) +0.43,
  20 Nov (19k) +0.59, then +1.1 / +1.4 / +2.1 on 21-23 Nov: it is still improving with data.
- A REAL BOOK OF POSITIONS (fills in time order, a cap on shares held per game, buys and sells netted, the rest held to
  the result, so nothing is left unpaid): 5c+, 1 s, never against the bookmakers: cap 300 shares $+325 a day
  [+41..+616], 83% of days up, worst day -$508, about $5k tied up on the busiest day; cap 1,000 $+554 [+150..+959],
  worst day -$153. 5c+, 2 s, model: cap 1,000 $+354 [+91..+635]. Plain 5c+: $+337 [-120..+769]. 2c+ with the model:
  $+386 [-184..+973] (not clear of zero). A cap of 100 shares throws most of it away.
- Not testable on recorded data: other makers stepping in front, pulling an order inside the taker's 1 s delay
  window, joining the best price instead of improving it, football (three-way), reward pots on the same orders.
  Paper-bot settings these tests point to: spreads of 5c+ only, one cent inside, 100 shares, react under 1 s, pull the
  side the bookmakers' last 30 s (or a superline lead) is against, cap 300-1,000 shares a game, hold to the result.

A $175 WALLET, MODEL VARIANTS, FINE TUNING (`makerwallet.py`; owner, 24 Nov: "is there any code, model improvement,
or settings fine tuning that will make it work better? Assume a 175 dollar wallet"). Needs makersteer_states.npz (what
a bot saw every in-play second of every game; written by makersteer.py build). The wallet test replays every fill in
time order across all 805 games: an order only exists if the wallet can pay for it at that second (a buy of S shares
at q locks S x q, an offer S x (1 - q) unless we hold the shares; widest spreads get the money first; at a fill a
median 6 orders qualify at once, $46 to rest them all at 20 shares), positions net out in a game, the rest is paid
at the result. 6 days (18-23 Nov), spreads of 5c+, never against the bookmakers' last 30 s, cap 2 orders a game.
- HELD TO THE RESULT, $175 becomes: 5 shares an order $247 (lowest $156), 10 shares $324, 20 shares $401, 40 shares
  $0.07 (ruin: 80 shares x ~50c = a quarter of the wallet on one game's result; the edge is ~2.5c on ~50c, so the
  Kelly stake is about 5% of the wallet a game), 10% of the wallet an order $44. Ranges by game are wide (5 shares
  $137..$347). And tuning is worthless here: over 576 settings the result of one half of the week says nothing
  about the other (rank correlation +0.09; the best of 18-20 Nov, $1,218, made $25 on 21-23 Nov).
- GETTING OUT (a code change, the one that matters): while we hold shares in a game the order that reduces the
  position always rests, one cent inside whatever the spread is (2c+), whatever the rule says; it needs no money.
  $175 becomes: 5 shares $269 ($188..$354 by game, lowest $146), 10 shares $305 ($160..$448), 20 shares $299, 40
  shares $183; about two thirds of entries are sold again before the result. Now settings carry over: rank
  correlation between the halves +0.66, 85% of the 576 settings grow the wallet in both halves.
- WHAT THE SETTINGS ARE WORTH (getting out, 10 shares): no rule $217; bookmaker rule $305; + never against a
  superline lead $299; only with the bookmakers $298 ($232..$367); bookmaker rule + the fill model $403
  ($307..$499, lowest $169, 60% of games up); model alone $408. Spreads 3c+ $118 (loses), 4c+ $203, 5c+ $305, 6c+
  $337 ($213..$462), 7c+ $325, 8c+ $318. The cap per game (1 / 2 / 4 / 8 orders) changes nothing ($301-$309).
  Medians over the grid, first / second half: with the model $284 / $320, without $215 / $255.
- A SHARE OF THE WALLET AN ORDER (the order grows with the wallet): with exits and the model, 10% an order, the most
  robust setting of the grid (best worse-half): $575 and $721 on the two halves, $1,406-$1,572 over the six days
  straight, lowest $167. Do not read the level as a forecast: it compounds a simulated edge on one week and assumes
  we are always first in line; fills are capped by the real trades (median 23 shares) so it cannot scale far.
- MODEL VARIANTS (12, each trained on earlier days, compared on the 4,152 fills inside 5c+ at 1 s, each keeping the
  same ~46% of fills; first model +1.66c [+1.00..+2.36], $216 a day at 100 shares): none is clearly better. Linear
  model on 49 inputs +2.12c, +$62 a day vs the first [-6..+130]; five seeds averaged +1.91c, +$42 [-8..+89]; bigger
  trees +$32 [-18..+84]; 1-min target +$11; training only on wide spreads is WORSE (-$85 [-159..-18]): the narrow-
  spread fills teach it what a bad fill looks like. Rank correlation with what a fill keeps 0.12-0.14 for all: the
  inputs carry about this much, a cleverer model does not add to it. More days of fills and fresher bookmaker
  feeds would.
- For a $175 wallet these point to: spreads of 5c+ (6c+ if the wallet should work less), bookmaker rule + the fill
  model, 10 shares an order (or 5% of the wallet), the exit order always resting, react under 1 s.

THE AUDIT (`makeraudit.py --signals DIR --results fair_results.json`, makeraudit.txt; owner: "how many trades? how
often does it win? is the test correct?" and "WHY does it make money, on what, which games, every game?"). Setting A
= 5c+ spreads, bookmaker rule, 10 shares, exit order, $175; B = A + the fill model. 18-23 Nov, 805 games.
- WHAT: the match-winner market of a game, in play, only in seconds with a 5c+ spread: 31% of the in-play time with
  a live book (424 of 1,354 h); 70% of games have a minute of it, the median game 13 min. A makes 2,242 entry fills
  (374 a day) in 59% of the games, B 1,165 (194 a day) in 39%; a traded game has 2 trades at the median, a tenth
  have 12+. By sport (A): cs2 958 trades +$48, valorant 748 +$50 (its spread is 5c+ 76% of the time), american
  football +$34, dota2 +$17, basketball +$4, tennis -$3, hockey -$21. It lives on thin esports markets.
- WHY, cents a share (5c+, 1 s): at the fill the spread pays +3.14 (we buy that far under the middle of the book);
  over the next 5 min the price moves -2.27 against us (the other side is right on average); kept +0.87. Fills the
  rule allows: +3.26 - 1.85 = +1.41; fills it forbids: +2.83 - 3.36 = -0.53; allowed and the model agrees:
  +3.58 - 0.96 = +2.63; allowed but the model says no: -0.02. The income is the spread paid by people who cross a
  wide gap; the filters only remove the ones who know something.
- TRADES (first in, first out): A 2,815 trades (469 a day), won 58%, average win +$0.67, average loss -$0.81,
  +4.6c a trade; 88% closed by an exit fill (won 62%, +$388, held 3 min), 12% left to the result (won 24%, -$257);
  days -12 / +25 / +27 / +68 / +1 / +20; the 10 best of 473 games make MORE than the whole profit (without them
  -$19). B 1,474 trades (246 a day), won 66%, win +$0.63, loss -$0.75, +15.5c a trade; 91% exits (won 68%, +$282),
  9% to the result (won 40%, -$53); all six days up; 60% of games up; without the 10 best games still +$110.
- CORRECT? The mechanics check out: the wallet replay equals a sum by hand ($540.00 both ways), trades add up to the
  wallet change, the results equal Polymarket's official ones in 805 of 805 games, bookmaker signals 6 s stale
  change little ($293 / $396), the rule upside down loses ($125), a random rule allowing the same share of fills
  does no better than no rule ($230 median vs $217; the real rule $305), both halves are up.
- BUT IT IS THIN. Per share traded A makes +0.69c, B +2.35c. Wallet after 6 days from $175 (A / B): every fill half a
  cent worse $119 / $311; 1c worse $15 / $218; 2c worse $0 / $48; 2 s reaction $0 / $220; 2 s and half a cent worse
  $0 / $137; 2 s, half a cent, stale signals and no part fills $0 / $91; a random half of the entry fills $226 /
  $290; faster bots take our best 10% of fills $25 / $264, best 20% $0 / $166. So the rule alone (A) is NOT a
  result: any small worsening erases it. With the model (B) it survives one moderate worsening at a time but not
  two together. Unprovable offline: that we are first in line at our price and that we react inside a second.
  Earlier lines in this section that quote the rule-only wallet ($305) must be read with this.

IMPROVING RULE + MODEL (`makerlib.py` = the wallet replay as a class; `makerbetter.py`, `makerexit.py`; needs
`makersteer.py build --name makersteer_flow.npz`, which also adds the REAL ORDER FLOW inputs `fl_*` (signed shares
the takers bought minus sold over 30 / 120 / 600 s from Polymarket's public trades, read 6 s before the decision).
Owner: "can we improve it further? Maybe something to do with confidence scaling". $175, 10 shares, 18-23 Nov;
each idea changes one thing, is run on both halves from a fresh wallet, and is compared with the base by game.
About 40 variants were tried on six days, so single gains are suggestive, not proven.
- HELPED (kept only if better in BOTH halves): FIVE SEEDS AVERAGED $384 -> $406 (+$22 [-7..+53]); CONFIDENCE
  SCALING, two steps (20 shares where the call is above its usual level of earlier days, else 10) $406 -> $479
  (+$72 [+26..+118]; per share +2.33c -> +2.58c; the same total size flat makes $437; scaling it UPSIDE DOWN loses
  $44: the call's order carries information); A SMARTER EXIT (the exit order does not rest while the bookmakers
  moved OUR way in the last 30 s) $479 -> $502 (+$23 [-40..+85]); the model version of it (no exit where the model
  expects that sale to lose more than 1c) $548 (+$69 [+2..+133]); all five such variants beat the base in both
  halves. An exit fill is as likely to be a bad fill as an entry.
- DID NOT HELP: the real order flow as model inputs ($359, -$25 [-70..+21]); a higher line (call above +0.5c
  $383, above +1c $282) or a lower one (-0.5c $348); 3-4c spreads on the surest calls (-$15); switching off sports
  that lost on earlier days (-$29; with the model every sport is up: cs2 +$103, valorant +$80, hockey +$19,
  dota2 +$11, tennis +$9, basketball +$8); the model without the rule is the same (+$12 [-17..+42]).
- HURT: a TIME STOP (selling a stuck position into the bid as a taker): after 2 min $102, 5 min $233, 10 min $286,
  20 min $365, 40 min $371, 90 min $390 against $406. No exit order at all $418 against $479.
- SIZE is leverage, not edge: 10 / 15 / 20 / 30 shares (with the scaling and the smarter exit) $502 / $605 / $705 /
  $830, per share the same +3.1c. If faster bots take our best 20% of fills: $162 / $158 / $80 / $110.
- STRESS, wallet after 6 days from $175 (five seeds | + scaling | + smarter exit): as tested 406 | 479 | 502; every
  fill half a cent worse 312 | 364 | 406; 1c worse 217 | 260 | 323; 2 s reaction 213 | 250 | 329; 2 s AND half a
  cent worse 125 | 162 | 249; the best 10% of fills lost 266 | 309 | 310; the best 20% lost 172 | 187 | 162. The
  combined bad case moved from a loss to a gain; losing the best fifth of the fills is still about break-even.
- Settings these point to for $175: spreads of 5c+, bookmaker rule, five-seed model call above 0, 10 shares (20 on a
  call above its usual level), the exit order resting except while the bookmakers moved our way (or the model
  expects the sale to lose over 1c), no stops.

HOW ACCURATE IS IT (`makeralign.py`, makeralign.txt; owner: "is this backtest 100% accurate?"). No. Measured:
- WHEN A TRADE HAPPENED. The public trade stamp is a whole second and runs late; each trade is placed within
  [stamp - 5, stamp] where the recorded best quote is nearest its price. Of the entry fills: 19% CLEAN (price matched
  and the book did not move for 6 s: placement cannot matter), 41% PLACED (matched, but the book moved in the
  window), 41% NO MATCH (the trade's price is not at our recorded best quote, median 0.5c off: our book is one
  snapshot a second). The edge per share is in all three (rule + model, 5c+: CLEAN +2.51 [+1.24..+3.71], PLACED
  +3.00, NO MATCH +3.29), so placement does not create it; the dollars do depend on counting the less certain fills.
  TRAP hit on the way: prices in the paths files are 32-bit floats; comparing them to a trade price needs a
  tolerance (0.05c), not 1e-6 (the first run called 76% of fills "no match").
- WERE WE FIRST IN LINE (the assumption that cannot be simulated; the trade's own price is evidence). 84% of entry
  fills: the real trade printed AT or THROUGH the best quote we had recorded, so nobody was quoting better and our
  order one cent inside would have been taken first. 14%: a sub-cent print inside it. 2%: at our price (someone was
  already there; these 21-61 fills show +6..+9c a share, i.e. they are the too-good ones). Counting ONLY the 84%
  (entries and exits), the final setting makes $175 -> $377 (halves $265 / $286, +2.39c a share, lowest $166)
  instead of $502; with every fill half a cent worse on top $293; at 2 s reaction $276.
- So: headline $502, conservative fill counting $377, conservative and half a cent worse $293. Exact: the money
  accounting, the official results, no fee for resting orders (Polymarket's documentation), no future data in the
  model. Not in any number: other bots reacting to our order, our order changing who trades, sub-second reaction,
  order rejections and downtime, anything beyond these six days and 805 games, football; and the extras were
  picked among ~40 variants on the same days.

LIQUIDITY REWARDS ON THE PC. `run/rewards_pc` = a copy of tools/rewards (the scripts write next to themselves) +
`launch.py` (runs one under `python -I`). Recorder 2 runs there since 24 Nov 13:01 Norway time for 72 h
(`python -I launch.py rec2.py 72`, PID in rec2.pid; it stops when the PC is switched off). Read it with
`python -I launch.py paperbot.py --quick` once it holds a day. New tool `tools/rewards/week_cost.py` (copy it there to
run): the markets the rule picks at the newest pass, and what a week of real trades would have cost the minimum pair.
At 13:18: 41 markets pass (pots $792 a day, the minimum pair's expected share $526 a day, nobody else qualifying in
61%). Cost over the last week there (38 markets, 212 market-days, pair 3c from the price, re-set every 2 min): 0.80
fills a market-day, a fill worth -$0.46 after 1 h / -$0.50 after 24 h / -$0.48 at the end (20 shares), worst -$8.61:
about -$0.40 a market-day, the same as the Mac's figures. The reward side is NOT a weekly number: 26 of the 35 picks
have a pot dated TODAY (47% of all watched markets do), i.e. "empty" means "new pot", as the Mac found. Break-even is
a pot share of ~$0.40 a market-day, 4% of what the formula gives while nobody else is there (net +$9.47 a market-day
at 100%, +$4.54 at half, +$0.60 at a tenth). Open: how many hours a new pot stays empty (the recorder), and whether
the formula pays a lone minimum order as published (only a real order can show that; the owner's call).

## 8m. The steered resting orders live ON PAPER (24 Nov 2026, PC): tools/maker

Read `tools/maker/README.md` first. On paper nothing here sends an order. The real-money half was built later the same day: 8n.

WHAT RUNS ON THE PC since 24 Nov 17:35 Norway time: the collector (`run/collector.pid`, started from
`target\release\oddsfeed.exe` in `run`, log `run/log.txt`) and `tools/maker/paperbot.py` (`run/maker/bot.pid`,
`bot.log`), paper wallet $175. Read `run/maker/status.txt`; every fill / closed trade / result is in
`run/maker/journal.jsonl`, every order a real bot would have sent or pulled in `run/maker/orders.jsonl`. Both stop
when the PC sleeps or reboots; `state.json` keeps the wallet and the shares across a bot restart.

HINDSIGHT FOUND IN THE RESEARCH INPUTS, fixed, results held. Writing the live inputs (`tools/maker/feat.py`, which
calls the research code on a game's rows up to one moment) and comparing them with the full-recording inputs
(`parity.py`) showed three inputs that knew the future:
- `waves.book_lines` used a bookmaker from its FIRST price if that price changed at any later time, and took the set
  of outcomes from the whole recording. Now `book_lines(rows, outcome, causal=True)`: outcomes seen so far, and a
  book's line starts at its second real value. `features.build(..., t_end=, causal=)` passes it on.
- `since_pm_end`: a wave still running when the data ends was closed by force and read as ended. `lead.trends` now
  marks it `open`, `labfeat.fin` skips those.
- `wavedir.flow` returned zeros before a game's first trade print instead of computing the same as later.
Defaults keep the old behaviour; the causal set is `makersteer.py build --causal --name makersteer_causal.npz`. With
it parity is exact: 82 moments in 14 games, 164 rows x 167 inputs, every value identical; one live decision takes
0.14 s (median). Re-run of section 8l on the causal fills: five seeds $399; + two-step sizing $464 (+$65,
[+17..+112]); + smart exit $490; counting only fills at or through our recorded quote $357; + half a cent worse
$283; at 2 s reaction $270. Use THESE numbers, not 8l's $502 / $377 / $293. `makerbetter.py --model "five seeds
averaged"` evaluates the deployed model (its automatic pick chose "+ real order flow" on the causal data).

THE MODEL FILE. `export_model.py` trains the five seeds on every in-play fill of every setting (53,745 fills 17-22
Nov, stopped on the last fifth) and writes `run/maker/model.npz`; `model.py` scores it in plain numpy (gap to the
trained models 0.000001c). Usual call c0 = 0.642c (walk-forward median of positive calls on entry fills) = where the
bot doubles its order.

THE BOT (paperbot.py, rules = the backtest's final setting): two-way sports in play, book at most 15 s old, price
10-90c; entry one cent inside a 5c+ spread on a side the bookmakers' last 30 s are not against and where the call is
positive, 10 shares or 20 at c0, 2 orders deep; exit order one cent inside any 2c+ spread unless the bookmakers moved
our way; one worker process per group of games keeps each game's rows and reads only new ones. Made stricter than the
backtest on purpose: an order counts only from decision-ready + 0.3 s and a pulled order stays live 0.3 s
(`order_delay_ms`); no order under 5 shares (`min_order_shares`: a smaller holding waits for the result or is netted
by a later entry); no decision in a game until 15 min of it is on record (`min_history_s`). A fill = a real trade
from Polymarket's public stream (`last_trade_price`: price, size, side, ms) reaching the price of an order resting at
the trade's own time. Checks in status.txt: "taker buys printed at or above the middle" (96% on the first evening:
buy / sell is read the right way round), data-to-order delay (0.5 s median), loop time.
First evening: fills arrive (a 20-share entry after 34 s of resting, a part exit at the same price); the bot changes
about 20 orders a minute, because the order follows the best price cent by cent.

TRAPS HIT ON THE PC (hard disk):
- NEVER call the collector's `/stats` page or run an unindexed query against the live database here. `/stats` counts
  the whole `changes` table: minutes on this disk, and the query keeps running after the client gives up. For that
  long the write-ahead file cannot be folded back. Two such calls plus one full scan of mine took the file past 1 GB;
  from there it grew 100 MB a minute and did not recover (1.9 GB in 25 min; readers get slow, overlap, and the
  writer's 60-250 ms tries never find a gap), and the writer dropped 22 batches ("db write failed: database is
  locked", 16:01-16:07 UTC, so the PC's recording of those minutes has holes). Cure: stop the bot and the collector,
  `PRAGMA wal_checkpoint(TRUNCATE)` (39 s), start both. Healthy = the file stays under 20 MB with the bot running.
- To watch it without touching the database: read the first 120 bytes of `oddsfeed.db-shm` UNBUFFERED (frames at
  offset 16, frames copied at 96, reader marks at 100..119; a buffered read crosses the lock bytes at 120 and fails).
- Take `now` AFTER the database reads of a pass: a slow pass (token lookups) otherwise asks for decisions as of
  seconds ago. Token lookups are capped at 3 a pass.
- Patch scripts through a shell here-document lose `\n` inside strings: write them with the editor.

REAL MONEY: see 8n (built the same evening; nothing below has met the real venue).

## 8n. The order keeper for resting orders: src/maker.rs (24 Nov 2026, PC). Built, tested on a pretend exchange, NEVER run against Polymarket

Owner said "build it". Read `tools/maker/README.md` ("Real money") for the owner's steps and the known gaps.
AGENT ON THE MAC: start with `tools/maker/MAC_HANDOFF.md` (what to pull, build and start on paper, and what only
the owner may do).

SHAPE. Two halves that must both say "real":
- `src/maker.rs` in the collector (where the key is): `/maker` (view), `/maker/sync` (the bot's call),
  `/maker/settings` (limits, `maker.json`), `/maker/real` (`{"real": true, "confirm": "REAL MONEY"}`; off after every
  start), `/maker/test` (`{"confirm": "TEST"}`: one real post-only buy 10c under the best bid, read back, cancelled).
  Spawned in main.rs; one tick a second.
- `tools/maker/paperbot.py` with `"mode": "real"`: hands over its whole wanted set every pass (one order per key
  "<game>:<side>", token, BUY / SELL, whole cents, size) and books the fills the keeper returns
  (`on_real_fill`); real ledger in `real_*` files; refuses to start while Real is off; on paper `"mirror": true`
  rehearses the hand-off (the keeper keeps "dry-N" orders and sends nothing). A sync whose `mode` does not match the
  keeper's switch is refused and does not count as a sign of life.

RULES IN THE KEEPER (unit-tested, `maker::tests`): only the difference is sent (`plan`); an order whose cancel the
venue did not confirm blocks its key until its last state is read; a part-filled order stays while the bot still
asks for it; `admit` = the limits (20 shares an order, 40 of a token held + resting, $175 at risk, 30 orders, a sale
only of what the keeper bought); fills = `size_matched` of the key's open orders read each second, booked at the
order's own price; cancelled orders are read once more by id (`verify`) for a fill that came just before. Money
guards: bot silent 3 s -> pull; Polymarket's heartbeat every 5 s (`POST /v1/heartbeats`, id chained; the venue
cancels EVERY order of the API key 10 s after the last one: also other programs' orders, also after Real is switched
off); account worth (cash + Data API value) $30 under the arming level on two readings -> Real off; kill switch ->
Real off. Every real placement / cancel / fill -> `bot_orders` (bot "maker"); holdings, fill log and the resting
order ids -> `maker_state.json` (a restart cancels what was left resting). A refused placement is tried again after
1 s ("crosses": the price moved through ours, post-only refused it; expected) or 5 s (anything else).

exec.rs additions (the taker path `pm_order_once` is untouched): `pm_rest_place` (GTC + `postOnly: true`, price must
be whole cents and on the market's tick, size in hundredths), `pm_cancel` (`DELETE /orders`), `pm_open_orders`,
`pm_order_state`, `pm_heartbeat`, `pm_equity`; `clob()` is Polymarket's address except in this crate's tests
(`TEST_CLOB`). API facts were read from docs.polymarket.com on 24 Nov 2026 (place-orders, manage-orders,
order-lifecycle, error-codes, the OpenAPI file); the docs disagree with themselves on a few points (trade status
names, minimum size in shares or dollars, batch-cancel limit): none of those is relied on.

TESTS. `cargo test` = 116 pass. `maker::venue_tests::the_real_path_against_a_pretend_exchange` runs an axum server
that behaves as the documentation says and drives: refused while off, rehearsal, arming words, place (signed
headers, post-only GTC, amounts), no-op on the same set, part fill, re-price, an order filled just before its cancel
(nothing new placed until the fill is read), limits, a sale beyond holdings, a "crosses" refusal and its pause, the
bot going silent, the heartbeat chain, Real off, state on disk. `python -I tools/maker/test_real.py` = the bot's
real bookkeeping. On the PC the rehearsal runs live (`mirror` on; a hand-off takes 2 ms).
NOT DONE: one real call (no key on the PC, and agents place no orders). First real call = the owner's `/maker/test`.

NOT BUILT / WEAK: no web page for it; cancels and placements go one after another (a re-price = two round trips);
fills are a second late (bounded by `max_position_shares`); the bot's cash runs ahead of the account until won
shares are redeemed and for netted dust pairs; the loss guard is account-wide.

STATE ON THE PC, end of 24 Nov: collector rebuilt 18:12 Norway time with the keeper (the running binary lacks two
later source changes that only matter with Real on: the retry pause and the default of 30 orders, set there through
`/maker/settings` instead); paper bot restarted with `mirror` on, paper wallet $182 after an hour (15 closed trades,
far too few to mean anything). Committed and merged to main the same evening (owner asked); the model
file ships as `tools/maker/model.npz`.

TRAP (PC): do not compile into `target/` on D: while the collector records. A 60 s `cargo check --release` made the
writer drop 8 batches ("db write failed: database is locked", 17:14-17:16 UTC): the disk is shared. Build with
`CARGO_TARGET_DIR` on C: (no drops), or with the collector stopped. This, not the bot's reads, is the likely cause
of most of the 22 drops at 16:01-16:07 UTC as well.

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
