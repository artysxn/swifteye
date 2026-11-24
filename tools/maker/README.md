# The steered resting orders, live on paper (tools/maker)

Built 24 Nov 2026 on the PC. Background and every test: AGENTS.md section 8l. Setting it up on another machine (the Mac):
`MAC_HANDOFF.md` in this folder.

## What it is

A Python program next to the collector. On paper (the default, and what runs now) it places **no orders anywhere**.
About once a second it decides which
orders a real bot would have resting on Polymarket's match-winner markets, then watches Polymarket's public trade
stream and counts a paper fill whenever a real trade reaches the price of an order that was resting at that moment.

The rules are the ones the backtests settled on:

- only games being played, two-way sports, book alive (newest row 15 s old at most), price 10-90c;
- **entry**: spread 5c or more; one cent inside it; only a side the bookmakers' last 30 s are not against; only where
  the fill model expects a gain; 10 shares, 20 where the model's call is at or above its usual level (0.64c);
  at most 2 orders deep a game;
- **exit**: while shares are held, the order that sells them rests one cent inside any spread of 2c+, except while
  the bookmakers moved our way in the last 30 s; what is never sold is paid at the result;
- an order exists only from the moment its decision is ready plus 0.3 s (the trip to the exchange), only while the
  paper wallet can pay for it, and is pulled if no newer decision confirms it within 5 s; a pulled order stays live
  0.3 s more, as a real cancel would; one fill a side a game a minute;
- Polymarket takes no order under 5 shares: a holding smaller than that (left by a part fill) cannot be offered; it
  waits for the result, or is netted when a later entry on the other side fills.

## Files

| File | What |
|---|---|
| `feat.py` | the model's 173 inputs for one game at one moment, by calling the research code itself |
| `parity.py` | checks on recorded games that those inputs equal the backtest's (they do, exactly) |
| `export_model.py` | trains the five-seed model on every recorded fill and writes `model.npz` |
| `model.py` | scores a row with that file in plain numpy |
| `paperbot.py` | the bot (paper; real money only with the collector, see the end) |
| `test_real.py` | checks of the real-money bookkeeping, no collector or exchange needed |

In the data folder (`run/maker`): `config.json` (settings; edit and restart), `model.npz`, `state.json` (wallet and
positions; read again on a restart; delete it to start over at the configured wallet), `journal.jsonl` (every fill,
every closed trade, every result, one line a minute), **`status.txt` (the summary to read)**, `orders.jsonl` (every
order a real bot would have sent or pulled: token id, BUY / SELL, price, size, post-only; selling the home side
without holding it is written as buying the away token at 1 - price), `tokens.json`, `bot.log`, `bot.pid`.

## Run

The collector must be running on the same machine (the bot reads its database read-only and its `/clock`).

    python -I tools/maker/export_model.py --signals run/signals/mac/lab --out run/maker      (once; needs the causal fills)
    python -I tools/maker/paperbot.py --db run/oddsfeed.db --data run/maker

Checks: `python -I tools/maker/parity.py --db <a recording>`.

On the PC it runs hidden in the background (started 24 Nov 2026). Stop and start, PowerShell, from the project folder:

    taskkill /PID (Get-Content run\maker\bot.pid) /T /F
    $p = Start-Process python -ArgumentList '-I','-u','tools/maker/paperbot.py','--db','run/oddsfeed.db','--data','run/maker' -WorkingDirectory (Get-Location) -RedirectStandardOutput run\maker\bot.log -RedirectStandardError run\maker\bot.err -WindowStyle Hidden -PassThru; $p.Id | Set-Content run\maker\bot.pid

A restart keeps the wallet and the shares held (`state.json`). It does not survive a reboot or the PC going to sleep.

After a collector restart the bot waits until 15 minutes of a game are on record before deciding anything in it
(`min_history_s`): the inputs have long memories and are cold until then.

It reads the database with short indexed queries only (checked with EXPLAIN QUERY PLAN), about ten a second. Do not
add long scans to it, and on the PC never open the collector's `/stats` page or run an unindexed query while it
records: the database is on a hard disk, one count over `changes` takes minutes there, and for that long the
collector cannot fold its write-ahead file back. Measured 24 Nov: two such counts and one full scan took the file
past 1 GB, after which it grew 100 MB a minute and did not recover by itself (1.9 GB in 25 minutes; the collector's
writer also dropped 22 batches, "db write failed: database is locked"). Cure: stop the bot and the collector, run
`PRAGMA wal_checkpoint(TRUNCATE)` on the database (39 s), start both. With the bot running the file stays under
20 MB and nothing is dropped.

## What the paper run can and cannot show

It shows, against real trades and with our real reaction time: how many fills, what they keep, whether exits happen,
the wallet. It still assumes a real order of ours at that price would have been first in line, and that our order
would not have changed what others did. The journal stores, for every fill, how far inside our recorded best quote
the real trade printed (`trade_inside_our_recorded_quote_c`: 0 or less = nobody was quoting better than what we had
recorded; around 1 = someone was already at our price).

## Real money: built 24 Nov 2026, never run against Polymarket

Two halves, and BOTH must say "real" before an order can leave:

- the collector's **order keeper** (`src/maker.rs`, pages under `/maker`): it holds the key, sends the orders, reads
  the fills and guards the money. Its Real switch is off after every start of the collector;
- this bot with `"mode": "real"` in its config: it only hands the keeper, every pass, the whole set of orders it
  wants resting (`/maker/sync`), and books the fills the keeper reports. It never talks to Polymarket about orders,
  never switches Real on, and stops at start if Real is off in the collector.

A bot on paper that reaches an armed keeper is refused; so is a "real" bot at a keeper that is off.

### What was tested, and what was not

- 116 unit tests of the collector pass. The new ones: the signed post-only order body (price, size, amounts, the
  signature recovers to the key), the planning (only the difference is sent; a part-filled order stays), every
  limit, the fill bookkeeping, and **the whole real path against a pretend exchange on the same machine**
  (`maker::venue_tests`): place, part fill, re-price, an order that fills just before its cancel, a post-only
  refusal, the bot going silent, the heartbeat chain, Real off, state on disk.
- `python -I tools/maker/test_real.py`: the bot books a reported fill on the right side and its cash matches the real
  dollars; exits sell the token held; in real mode a public trade never becomes a fill.
- A rehearsal runs on the PC: the paper bot hands its orders to the keeper (`"mirror": true`), which keeps a pretend
  table and sends nothing. A hand-off takes 2 ms.
- **NOT tested: one real call.** The paths, field names and replies come from Polymarket's documentation as read on
  24 Nov 2026 (`POST /order` with `orderType: GTC, postOnly: true`; `DELETE /orders`; `GET /data/orders`;
  `GET /data/order/{id}`; `POST /v1/heartbeats`). The first real call is the owner's test below.

### What guards the money (all in the collector)

| Guard | Default | Where |
|---|---|---|
| Largest order | 20 shares | `max_order_shares` |
| Most shares of one token, held + resting buys | 40 | `max_position_shares` |
| Most dollars at risk at once (cost of shares held + resting buys) | $175 | `max_open_usd` |
| Orders resting at once | 30 | `max_live_orders` |
| A sale never exceeds what the keeper itself bought | always | |
| The bot is silent for 3 s: every order is pulled | 3000 ms | `sync_timeout_ms` |
| The collector dies or loses its line: Polymarket cancels every order of the key after 10 s without our heartbeat | on | `venue_heartbeat` |
| The account (cash + positions) is worth this much less than at arming, twice 30 s apart: Real off, everything pulled | $30 | `max_loss_usd` |
| The bots' kill switch: Real off, everything pulled | | Bots page |

Change them with `POST /maker/settings` (only the fields given); they are kept in `maker.json` next to the database.
Every real placement, cancel and fill is a row in `bot_orders` (bot = "maker").

### Known gaps (read before arming)

- Fills are read once a second. A fill in the last second before a re-price is not known yet, so the new order can
  add to it: the most one token can reach is `max_position_shares`, not the bot's own 20.
- The heartbeat is Polymarket's own switch and it covers EVERY open order of the API key. While Real is on, nothing
  else may rest orders with that key, and switching Real off lets the venue cancel those too. Turn
  `venue_heartbeat` off if that is a problem (then only the collector's own 3 s rule protects you, and nothing does
  if the collector itself dies).
- The loss guard looks at the whole account. Another bot losing money on the same account trips it.
- The bot's cash is its own count. Polymarket pays a won game only when the shares are redeemed, and a small
  holding that was netted by a later entry sits on the account as a home + away pair until the result: until then
  the account has less free cash than the bot thinks, and the venue refuses what it cannot pay (the keeper waits 5 s
  and tries again; `real_status.txt` shows the refusals).
- Right after a buy fills, a sale of those shares can be refused for a few seconds until they have arrived; the
  keeper tries again.
- The keeper sends its cancels and orders one after another: a re-price is two trips to the exchange. The paper bot
  assumes 0.3 s; `GET /maker` shows the real time (`place_latency_ms`).
- There is no page for it yet: the calls below are the interface.

### Steps (the owner, on the machine with the key: the Mac)

1. Get this code there, `cargo build --release`, restart the collector (the model ships as `tools/maker/model.npz`
   and is copied into the data folder at the first start).
2. Paper first, there too: start the bot with `"mirror": true` and let it run. `status.txt` must show the bot
   deciding and the keeper line "rehearsal, nothing is sent".
3. The test order (real, costs nothing unless the price falls 10c in one second; needs about $5 free cash):

       curl -X POST localhost:8787/maker/test -H "Content-Type: application/json" -d "{\"confirm\":\"TEST\"}"

   It places one post-only buy of the smallest size 10c under the best bid of the last token the bot asked for
   (or `"token": "..."`), reads it back, cancels it and reads it again. `"ok": true` = all four steps went as the
   documentation says. Anything else: do not arm; send me the reply.
4. Limits for a first day, for example 5-share orders in at most a few games:

       curl -X POST localhost:8787/maker/settings -H "Content-Type: application/json" -d "{\"max_order_shares\":10,\"max_position_shares\":20,\"max_open_usd\":40,\"max_loss_usd\":10}"

   and in the bot's `config.json`: `"base_shares": 5`, `"wallet": 40`.
5. Real on (this is the arming; nobody else does it):

       curl -X POST localhost:8787/maker/real -H "Content-Type: application/json" -d "{\"real\":true,\"confirm\":\"REAL MONEY\"}"

6. Stop the paper bot, set `"mode": "real"` in `config.json`, start the bot. It writes `real_status.txt`,
   `real_journal.jsonl`, `real_orders.jsonl`, `real_state.json`; the paper files stay as they are.
7. Watch `GET /maker` (orders, fills, holdings, errors, latency) and `real_status.txt` ("this bot and the collector
   hold the same shares").
8. Off: `{"real": false}` to `/maker/real`, or the kill switch, or simply stop the bot (orders are pulled 3 s later).

I would not arm before the paper run has shown, over about a week, fills that mostly print at or through our
recorded quote and a wallet near the conservative backtest ($280-360 after six days from $175).
