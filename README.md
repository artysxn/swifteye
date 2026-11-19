# Swifteye

Swifteye records live odds from about 28 bookmaker feeds plus Polymarket and Kalshi, measures
which source moves first, and runs a bot that buys on Polymarket when it lags a bookmaker move.

It is one Rust program (`oddsfeed`) with a web app, a local API and a Telegram bot built in.

- **Sports:** football, tennis, basketball, hockey, volleyball, baseball, American football, MMA
  and e-sports (CS2, Dota 2, LoL, Valorant and others).
- **Markets:** match winner everywhere; totals, handicaps and both-teams-to-score where a feed has them.
- **Storage:** every price change, with a millisecond timestamp, in one SQLite file.

> The snipe bot can trade real money. It is off by default and only trades with your own
> Polymarket keys. Nothing here is financial advice, and backtest results are not a promise.

## Quick start

```bash
cargo build --release
./target/release/oddsfeed            # optional argument: path/to/oddsfeed.toml
```

Then open <http://127.0.0.1:8787>. The app and API listen on localhost only. There is no login.

Copy `oddsfeed.example.toml` to `oddsfeed.toml` to choose sports and feeds. Every key is optional.

## Run 24/7 on a Mac

```bash
git clone https://github.com/artysxn/swifteye.git ~/swifteye-src
cd ~/swifteye-src && ./deploy/mac/install.sh
```

- The script builds the program and installs a launchd agent (`com.swifteye.oddsfeed`). It
  starts at login, restarts after a crash and keeps the Mac awake on power.
- First time only, it tells you to install Rust and Xcode's command line tools if they are missing.
- Data, config and logs go in `~/swifteye/`: `oddsfeed.db`, `oddsfeed.toml`, `bots.json`, `logs/oddsfeed.log`.
- Update: `cd ~/swifteye-src && git pull && ./deploy/mac/install.sh`
- Stop: `./deploy/mac/uninstall.sh` (keeps the data).
- From another computer, use an SSH tunnel: `ssh -L 8787:127.0.0.1:8787 you@your-mac`.
- The database grows by roughly 2 GB a day with every feed on.

## The web app

| Group | Pages |
|---|---|
| Delay | Overview, Rankings (who moves first), Snipe window, Latency |
| Markets | Matches, Sheet, Charts, Movers, Edges, Arbitrage Scout, Bots, Suspicious |
| Tools | Data, Query (read-only SQL), API, Control panel, Polymarket, Kalshi, Telegram |
| Other | Statistics, Changelog, Settings |

The **Changelog** page lists what changed and when. Add an entry at the top of
`src/web/changelog.js` when something a user would notice ships.

## Sources

| Platform | Feeds |
|---|---|
| Polymarket | CLOB market WebSocket (pushed), order book depth |
| Kalshi | WebSocket (needs an API key) |
| 1xBet platform | Linebet, Melbet, BetWinner, Megapari, PariPesa, 22Bet, 22Bit, 1xBit |
| Kambi | Unibet, LeoVegas, Paf |
| Fonbet platform | Fonbet, PARI, Mostbet |
| Altenar | VINN, BetKing |
| Betby | Duel |
| Own feeds | Pinnacle, Marathonbet, Leon, Olimpbet, Betcity, M88, Thunderpick, SportyBet, betPawa, Betika |

Every adapter reads the same public JSON the bookmaker's own live page loads. Nothing logs in,
solves challenges or works around blocks. `docs/BOOKMAKERS.md` has the details per feed,
including the ones that are blocked.

Feeds that share one trading desk count as one group when the bot asks "how many bookmakers moved".

## Timestamps

`ts_ms` is the moment a change arrived, in milliseconds, corrected against public NTP servers
(`GET /clock`). Bookmakers are polled about once a second; Polymarket and Kalshi are pushed.
Bookmaker feeds carry their own delay of 1 to 3.5 seconds, and polling faster does not remove it.

## The snipe bot

When several bookmakers move and Polymarket has not followed yet, the bot buys the stale
Polymarket price and sells when Polymarket catches up.

**A buy needs all of these:**

- A bookmaker move of at least 10 points, still there 1 and 1.5 seconds later.
- At least 2 independent bookmaker groups moved, and their middle price moved 3 points or more.
- Polymarket followed at most 5% of the move, counted from the first bookmaker that moved.
- Polymarket and the bookmakers agreed before the move (under 10 points apart).
- Earlier in this game, Polymarket did lag the bookmakers.
- Spread at most 4¢, expected gain at least 4¢, price at most 90¢, no basketball.
- The decision was reached on time (not more than 1 second late).

**It sells** when Polymarket has made the full move, at 99¢ or more, or after 5 minutes.
There is no stop-loss price: in testing, stops lost more than they saved.

**Paper and real money**

- Paper and real have separate wallets and stakes. Paper is on by default; real money is off.
- With real money on, each paper trade copies the real order's fill and sale.
- One open position a game, per wallet.

**Safety for real money**

- A daily loss limit that counts open positions.
- A run stops itself after 3 losing trades in a row, when under half the trades win after 10,
  or when orders keep failing.
- A kill switch stops all new trades.
- Every real buy is recorded as it goes out; a buy left without a result is reported at the next start.

The settings are on the **Bots** page and in `bots.json`.

## The replay

The replay runs the bot's rules over the recorded prices. It finds moves the same way the live
bot does and measures them with the same steps, so it describes what the bot would have done.
It fills orders from the recorded order book and takes 1¢ off every sale.

`tools/` holds the checks that keep the two honest:

| Script | What it does |
|---|---|
| `live_emu.py` | An offline copy of the live decision path, run over the database |
| `check_live.py` | Compares the running bot's logged decisions with that copy |
| `live_cands.py`, `live_eval.py` | Save every candidate move with its price path, then compare rules in seconds |
| `replay_eval.py` | Backtests rule variants on the replay's saved blocks |
| `compare_replay.py` | Compares the replay with the offline copy, trade by trade |

A replay result is a backtest on the days it was built from. Treat it as an estimate.

## Telegram

Link a bot on the **Telegram** page. It sends buys and sales with charts, and answers commands.

| Command | What it does |
|---|---|
| `/status`, `/today`, `/open` | Where both wallets stand |
| `/safety` | Where a real run stands against each stop |
| `/rules`, `/settings`, `/signals` | What the bot trades and why it skipped moves |
| `/realon` | Starts a real run. It shows what would start and waits for `/realon yes` |
| `/realoff`, `/stoprun` | Real money off |
| `/kill`, `/unkill` | Stop or resume all new trades |
| `/help` | Every command |

Only the linked chat can use the commands.

## Data model

Tables in `oddsfeed.db` (SQLite, WAL; any SQLite tool can read it while the program runs):

- `matches(id, sport, home, away, league, start_ms, sr_id, created_ms)`: one row per real game.
- `book_events(book, book_event_id, match_id, …)`: each source's event, linked to a match.
- `changes(id, ts_ms, book, match_id, market, line, outcome, kind, old_price, new_price, book_ts_ms)`:
  the append-only price log. Prices are decimal odds. Polymarket and Kalshi are stored as `1 / mid`.
  - `market`: `1x2` | `total` | `handicap` | `btts`. Two-way sports store their winner under `1x2`.
  - `kind`: `open`, `move`, `suspend`, `resume`, `close`.
- `pm_quotes`, `kalshi_quotes`: best bid and ask with size, per outcome.
- `bot_trades`, `bot_orders`, `bot_signals`, `bot_runs`: what the bots did and why.

Games are linked across sources by Sportradar id where a feed has one, otherwise by sport,
start time and team names. Fix a wrong link with `POST /matches/{into}/merge/{from}` or
`POST /events/{book}/{event_id}/unlink`.

## API

The **API** page in the app lists every endpoint. The most used:

| | |
|---|---|
| `GET /books` | Per source: polls, errors, live events |
| `GET /matches`, `GET /matches/{id}` | Live games and their current prices |
| `GET /matches/{id}/changes` | One game's full price history |
| `GET /changes` | The change log, filtered |
| `GET /stream` | Server-Sent Events, one per price change |
| `GET /query?sql=SELECT…` | Any read-only SQL |
| `GET /lag`, `GET /lag/events` | Bookmaker and Polymarket delay, and the jumps behind it |
| `GET /bots/panel?real=0` | A wallet's trades, signals and state |
| `GET /bots/sim?kind=…` | A replay result |
| `GET /adaptive` | The replay's status and the rule picker's history |

```bash
curl "localhost:8787/matches?min_books=3"
curl -N "localhost:8787/stream?market=1x2"
curl -G localhost:8787/query --data-urlencode "sql=SELECT book, COUNT(*) FROM changes WHERE kind='move' GROUP BY book"
```

## Layout

```
src/main.rs           config, startup, one task per source
src/books/*.rs        one adapter per platform
src/engine.rs         diffs each snapshot against the last one into changes
src/matcher.rs        links the same game across sources
src/store.rs          SQLite schema, batched writer, read-only queries
src/clock.rs          NTP clock correction
src/lag.rs, rank.rs   who moved first and by how much
src/snipe.rs          snipe window analysis and shared price helpers
src/consensus.rs      the bookmakers' middle price and how Polymarket follows it
src/snipebot.rs       the live snipe bot: trigger, measure, rules, buy, sell
src/adaptive.rs       the replay and the rule picker
src/bots.rs           wallets, settings, real runs and their safety stops
src/exec.rs           Polymarket and Kalshi orders
src/arb*.rs           arbitrage scout, paper arbitrage and its backtest
src/suspect.rs        suspicious games
src/telegram.rs       the Telegram bot and its charts
src/api.rs            HTTP API and the web app's files
src/web/              the web app (compiled into the program)
tools/                research and checking scripts
deploy/mac/           install and uninstall scripts
```

Web files are compiled in. Set `ODDSFEED_WEB_DIR=<repo>/src/web` to serve them from disk while editing.

Adding a source: implement `Book::fetch`, returning a `Snapshot` of live events with normalised
quotes, and register it in `start_books` in `main.rs`. Another brand on a platform that is
already wired is usually one line.
