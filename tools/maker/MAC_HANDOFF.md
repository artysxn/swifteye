# For the agent on the Mac: what arrived on 24 Nov 2026 and what to do with it

Written on the PC by the agent that built it. The owner will ask you to pull this and restart the bot on the Mac.
Read this page, then `tools/maker/README.md`. Background: AGENTS.md 8l (the research), 8m (the paper bot), 8n (the
order keeper).

## What this is, in five lines

1. **The strategy**: resting orders on Polymarket's in-play match-winner markets, one cent inside spreads of 5c or
   more, only on a side the bookmakers' last 30 s are not against, only where a small model expects a fill to keep
   money; an always-resting exit order. Backtest on your recording of 17-23 Nov: $175 -> about $280-360 in six days
   on the conservative reading (AGENTS.md 8l, 8m). Thin, unproven live.
2. **The paper bot** (`tools/maker/paperbot.py`): decides once a second per game, and counts a paper fill when a real
   public trade reaches a price where its order would have been resting. It sends nothing anywhere.
3. **The order keeper** (`src/maker.rs`, pages under `/maker` in the collector): the real-money half. It keeps the
   bot's wanted orders on Polymarket as post-only orders, reads the fills, enforces the limits. **Real is off after
   every start of the collector.**
4. Both halves must say "real" before an order can leave: the keeper's switch AND `"mode": "real"` in the bot's
   config. Today only the paper half runs.
5. Tested on a pretend exchange only (116 unit tests). **No real order has ever been sent by this code.**

## What you do now (paper only)

    git pull                                   # main and signals-lab point at the same commit
    cargo test                                 # 116 pass; maker::venue_tests opens a port on 127.0.0.1
    cargo build --release                      # then restart the collector the way AGENTS.md section 2 says
    curl -s localhost:8787/maker               # must show "real": false

Python needs numpy, scipy and websockets (`python3 -c "import numpy, scipy, websockets"`; use the project's venv
interpreter if they live there). Then, from the project folder:

    mkdir -p run/maker
    [ -f run/maker/config.json ] || printf '{"mode": "paper", "mirror": true}' > run/maker/config.json
    nohup python3 -I -u tools/maker/paperbot.py --db run/oddsfeed.db --data run/maker > run/maker/bot.log 2> run/maker/bot.err &
    echo $! > run/maker/bot.pid

- `--db` is the collector's database (give the real path if the run folder is elsewhere); `--api http://host:port`
  if the collector is not on 127.0.0.1:8787.
- The model ships with the code (`tools/maker/model.npz`) and is copied into the data folder at the first start.
- `"mirror": true` makes the bot rehearse the hand-off to the keeper: the keeper keeps pretend orders ("dry-N") and
  sends nothing. It must stay a rehearsal: check the status line below.
- Stop it: `kill $(cat run/maker/bot.pid)`; its six worker processes end with it (check with `pgrep -f paperbot`).
  A restart keeps the paper wallet and the shares (`run/maker/state.json`). One bot per data folder.

After a minute read `run/maker/status.txt`. Healthy looks like:

- "N games being played and watched, M qualify for a decision";
- "trade stream: connected", and after some trades "taker buys printed at or above the middle of the book" well
  above 50% (it was 92-97% on the PC: buy / sell is read the right way round);
- "an order is in place 0.5 s after the data it was decided on" or better;
- "order keeper (collector): rehearsal, nothing is sent".

No decisions in a game until 15 minutes of it are on record ("under 15 min of this game on record"): after a long
collector outage that is expected. `run/maker/bot.err` should stay empty.

Optional checks: `python3 -I tools/maker/test_real.py` (the bot's real-money bookkeeping, no network) and
`python3 -I tools/maker/parity.py --db <a recording>` (the live inputs equal the backtest's; it reads whole games,
so run it on a copy or at a quiet time).

## What you must NOT do

The Mac holds the trading key, so here the real half can actually be switched on. That is the owner's act alone
(AGENTS.md 8i, 8n):

- do not POST to `/maker/real` with `"real": true`;
- do not POST to `/maker/test` with `"confirm": "TEST"` (it places a real order);
- do not put `"mode": "real"` in the bot's config;
- do not raise the limits in `maker.json` / `/maker/settings` on your own.

If the owner arms it himself and asks you to watch: `curl -s localhost:8787/maker` shows the orders, fills, holdings,
errors and the placement latency; the bot's real ledger is in `run/maker/real_status.txt` and `real_journal.jsonl`.
While Real is on, Polymarket's heartbeat is being sent: when it stops (Real off, or the collector down for 10 s)
the venue cancels EVERY open order of the API key, also ones other programs placed (for example the liquidity-
rewards idea of 8h). Tell the owner before running anything else that rests orders with the same key.

## What to report back

After a day: the wallet line and "closed trades" from `status.txt`; the number of fills; and from
`journal.jsonl` how the fills printed against our recorded quote (`trade_inside_our_recorded_quote_c`: 0 or less =
nobody was quoting better, around 1 = someone was already at our price). The PC runs the same bot on paper on its own
recording; the two wallets are separate and need not agree fill for fill (different feeds, different timing).

## Things that bit on the PC (may or may not apply on the Mac)

- Heavy reads on the live database stall the collector there (hard disk). The bot's own queries are short and
  indexed; do not add scans to it, and do not call the collector's `/stats` while it records on a slow disk.
- Take `now` after the database reads of a pass (done in the code): a slow pass otherwise decides on old data.
- The research inputs had three bits of hindsight (AGENTS.md 8m). They are fixed behind `causal=True`; the live bot
  uses the causal path. If you rebuild fills or retrain (`makersteer.py build --causal`, then
  `tools/maker/export_model.py`), keep it causal or the live model will not match its training.
