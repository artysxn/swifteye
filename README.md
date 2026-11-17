# oddsfeed

One small Rust binary that polls live football odds from several bookmakers,
matches the same game across books, logs **every price change** with a
millisecond timestamp to SQLite, and serves it all as a local HTTP API.

```
cargo build --release
cd run && ../target/release/oddsfeed.exe        # optional: oddsfeed.exe path/to/oddsfeed.toml
```

API on `http://127.0.0.1:8787`. Live table in a browser: `http://127.0.0.1:8787/ui`.

## Run 24/7 on a Mac

```bash
git clone https://github.com/artysxn/swifteye.git ~/swifteye-src
cd ~/swifteye-src && ./deploy/mac/install.sh
```

- First time only: the script tells you to install Rust (`rustup`) and Xcode's command line tools if they're missing.
- It builds the collector and installs a launchd agent (`com.swifteye.oddsfeed`). The agent
  starts at login, restarts within 10 s if the collector crashes, and keeps the Mac awake on
  power (`caffeinate`). For a closed MacBook, use an external display or keep the lid open,
  and turn on "Start up automatically after a power failure" in System Settings.
- Data, config and logs go in `~/swifteye/`: `oddsfeed.db`, `oddsfeed.toml`, `logs/oddsfeed.log`.
- Update: `cd ~/swifteye-src && git pull && ./deploy/mac/install.sh`
- Stop: `./deploy/mac/uninstall.sh` (keeps the data).
- Status: `curl localhost:8787/books`. Live table: <http://127.0.0.1:8787/ui>.
- From another computer: the API only listens on localhost (there is no login). Use an SSH
  tunnel, `ssh -L 8787:127.0.0.1:8787 you@your-mac`, then open `http://127.0.0.1:8787/ui` locally.
- The database grows with every price change (mostly Linebet's many lines), roughly hundreds of MB a week.
  Check it with `du -h ~/swifteye/oddsfeed.db`.
Measured on this PC: ~60 MB RAM, ~3% of one core with 7 books, 9.6 MB exe.

## Books

| Book | Status | Source | Markets | Book's own timestamp |
|---|---|---|---|---|
| Unibet | ✅ | Kambi offering API (`ubse`) | 1X2, total, Asian handicap, BTTS* | per outcome, 1 s precision |
| SportyBet | ✅ | factsCenter live list (NG) | 1X2, totals (all lines), handicap, BTTS | per market, ms |
| betPawa | ✅ | sportsbook v4 by-queries (NG) | 1X2 | – |
| Betika | ✅ | live.betika.com (KE) | 1X2 | per outcome, ms |
| Linebet | ✅ | 1xBet-platform LiveFeed (football filter) | 1X2, totals, handicaps (all lines), BTTS | – |
| Fonbet | ✅ | public line CDN, incremental by version | 1X2, main total, main handicap | – |
| VINN | ✅ | Altenar widget API (`vinn`) | 1X2, totals, BTTS | – |
| **Polymarket** | ✅ | CLOB market **WebSocket** (push) + Gamma for the game list | 1X2 (home/draw/away Yes tokens) | server stamp per message, ms |
| Winline | ⏳ | binary WebSocket (`wss.winline.ru/data_ng`) | needs protocol decoding | |
| Favbet, STS, Norsk Tipping, 1Win, 20Bet, GGBET, Betway, thunderpick | ⏳ | push sockets / not yet traced | possible, not done | |
| Pinnacle | ❌ | needs a logged-in account | not automated | |
| bet365 | ❌ | the given link is Sportradar stats, not odds; bet365 itself is bot-protected | | |
| Merkur Bets | ❌ | geo-blocked outside Germany | | |
| BetBoom | ❌ | 401 + bot challenge | | |
| Novibet, M88 | ❌ | Cloudflare 403 to plain clients (not tried further) | | |

\*whatever the in-play list carries for that match.

Every adapter reads the same public JSON the bookmaker's own live page loads.
Nothing logs in, solves challenges, or works around blocks. Fonbet's website
shows a Cloudflare check, but its odds CDN is open, so we read the CDN.
E-sports and simulated football (FIFA 2x4 min, "Short Football 3x3", "MLS+",
"LFL 5x5") are dropped unless `include_esoccer = true`.

## What "down to the millisecond" means here

`ts_ms` = the moment the response (or WebSocket message) carrying the change
arrived, in ms since epoch, **NTP-corrected**. At startup and every 10 min the
service asks public NTP servers how wrong the PC clock is (`GET /clock`) and adds
that offset to every timestamp. This PC runs **2.6 s fast**. Without the
correction every bookmaker would look 2.6 s slower against Polymarket's server
stamps.

Bookmakers are polled every 1 s (`poll_ms`), so a change is seen within one poll
plus that feed's own delay. Polymarket is pushed, so it arrives within ms.

Measured (corrected clock), from each source's own change stamp to our receipt:

| source | avg | range |
|---|---|---|
| Polymarket (WebSocket) | 19 ms | 16–47 ms |
| Unibet (Kambi, stamp has 1 s precision) | 1.7 s | 0.35–3.3 s |
| SportyBet | 3.0 s | 0.3–32 s |
| Betika | 3.5 s | 0.5–6.2 s |

The bookmaker delay is in the public feeds themselves (CDN caching, the "live
delay" books apply), and polling faster does not remove it.

## Bookmakers vs Polymarket: the delay measurement

Polymarket lists a football game as three Yes/No markets (home, draw, away).
Each Yes token's mid (best bid + best ask) / 2 is the crowd's probability.
It's stored in the change log as decimal odds `1 / mid` (book `polymarket`),
so it sits next to the bookmakers on `/board`. The raw bid/ask/last per token,
with Polymarket's own server time, go to `pm_quotes`. A spread over 25 cents is
treated as "no price".

Polymarket's `gameId` is the Sportradar match id (checked on USL New Mexico v
Sacramento = SportyBet `sr:match:76582718`), so games link exactly to
SportyBet/betPawa/Betika, and by name to the others.

`GET /lag` (and the panel under the table on `/ui`) measures, on the 1X2 market:

1. Every price becomes implied probability `1/odds`.
2. A **jump** = a move of ≥ `min_jump` (default 0.04 = 4 points) within `settle_s` (10 s).
3. For each jump, look ±`window_s` (90 s) at the other side. If it moved the same
   way by at least half as much, it **followed**.
4. Each side is timed when it crosses halfway through its own move.
   **`delay_ms = Polymarket time − book time`** (positive = Polymarket later).
5. Two directions:
   - `trigger: book`: a bookmaker jumped; how long until Polymarket followed.
   - `trigger: polymarket`: Polymarket jumped; how long until each book followed.
     `first book` is the earliest of them.
6. Jumps from before both sides were being recorded are ignored.

Parameters: `since, until, min_jump, window_s, settle_s, match, book`, and
`clock=source` to use each feed's own stamps instead of our receive time.
`GET /lag/events` lists every jump with both sides' before/after levels, to audit
a number.

Reading it: bookmaker numbers include their 1–3.5 s feed delay above. That is
the delay you would actually have watching these feeds. `clock=source` removes it
for SportyBet/Betika/Unibet, the books whose feeds carry a stamp.

**Not yet measured for real.** So far it has seen one Polymarket game (a 4 a.m.
USL match) and no goal while Polymarket was recorded. The two small draw moves
it caught had Polymarket ~22 s behind the books. That shows the pipeline works.
It is not a result. Real numbers need goals and red cards in busy hours.

## Data model

Tables in `oddsfeed.db` (SQLite, WAL; open it with any SQLite tool while running):

- `matches(id, sport, home, away, league, start_ms, sr_id, created_ms)`: one row per real-world game.
- `book_events(book, book_event_id, match_id, …, first_seen_ms, last_seen_ms)`: each book's event, linked to a match.
- `changes(id, ts_ms, book, book_event_id, match_id, market, line, outcome, kind, old_price, new_price, book_ts_ms)`:
  the append-only log.
  - `market`: `1x2` | `total` | `handicap` | `btts` (all full-time)
  - `line`: total = goal line; handicap = **home** team's handicap
  - `outcome`: `home draw away over under yes no`
  - `kind`: `open` (first seen), `move`, `suspend` (→ null), `resume`, `close` (line/event gone)

### Cross-book matching

1. Exact: SportyBet, betPawa and Betika expose Sportradar match ids, so those link exactly.
2. Fuzzy: same sport, kick-off within 45 min (4 h if names are near-identical),
   both team names similar after normalising (accents, FC/CF/…, Utd→United).
   Women / U21 / reserves tags must agree, so youth sides never merge with first teams.
   One event per book per match.

Fix mistakes by hand: `POST /matches/{into}/merge/{from}` or `POST /events/{book}/{event_id}/unlink`.

## API

| | |
|---|---|
| `GET /` | endpoint list |
| `GET /ui` | live table (1X2 / total / handicap / BTTS), best price highlighted, moves flash |
| `GET /books` | per book: polls, errors, last error, request time, live events/quotes, changes |
| `GET /matches?q=&book=&min_books=&all=1` | live matches, which books have them, best 1X2 across books |
| `GET /matches/{id}` | every current price for one match, per book, with `updated_ms` / `book_ts_ms` |
| `GET /matches/{id}/changes?since=&market=&book=&outcome=&line=` | that match's full history, oldest first |
| `GET /board?market=1x2&min_books=2` | the live table as JSON |
| `GET /changes?since=&until=&book=&match=&market=&outcome=&kind=&line=&limit=&order=` | the change log |
| `GET /stream?match=&book=&market=` | Server-Sent Events: one `change` event per price change as it lands |
| `GET /query?sql=SELECT…&limit=` | any read-only SQL |
| `GET /stats` | totals + changes per book in the last 5 min |
| `GET /lag?since=&min_jump=&window_s=&clock=` | bookmaker ↔ Polymarket delay summary per book |
| `GET /lag/events?match=&book=&trigger=` | every jump behind those numbers |
| `GET /clock` | measured PC clock offset applied to all timestamps |

Examples:

```bash
curl "localhost:8787/board?market=1x2&min_books=3"
curl "localhost:8787/matches/1/changes?market=1x2&outcome=home"
curl -N "localhost:8787/stream?market=1x2"
curl -G localhost:8787/query --data-urlencode "sql=SELECT book, COUNT(*) FROM changes WHERE kind='move' GROUP BY book"
```

Which book moved first on each 1X2 change of a match (lead/lag):

```sql
SELECT book, outcome, ts_ms, book_ts_ms, old_price, new_price
FROM changes WHERE match_id = 1 AND market = '1x2' AND kind = 'move'
ORDER BY ts_ms;
```

## Layout

```
src/main.rs        config, startup, one task per book
src/books/*.rs     one adapter per platform (kambi, sportybet, betpawa, betika, linebet, fonbet, altenar, polymarket)
src/clock.rs       SNTP clock-offset correction
src/lag.rs         bookmaker <-> Polymarket jump matching and delay stats (+ unit tests)
src/engine.rs      diffs each snapshot against the last one -> changes
src/matcher.rs     cross-book event matching (+ unit tests)
src/store.rs       SQLite schema, batched writer thread, read-only queries
src/api.rs         axum HTTP API + SSE
src/ui.html        the /ui page (compiled into the exe)
```

Adding a book: implement `Book::fetch` returning a `Snapshot` of live events
with normalised quotes, and register it in `start_books` in `main.rs`. Other
Kambi or Altenar brands are one line each (different offering/integration name).
