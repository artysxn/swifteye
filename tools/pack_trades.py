"""Pack every paper and real trade the bots made into ONE file to take to another computer (owner, 23 Nov 2026:
"Pack the data (buy price, sell price, from different bookmakers + graph) from all the paper and real trades ...
into a single file").

Output: one self-contained HTML page. Opened in a browser it lists the trades and draws each one's chart (the same
Chart.js chart the bot sends to Telegram, built by the bot's own test program). The data itself sits in the page as
plain JSON inside <script type="application/json" id="swifteye-trades">, so a script can read it back:

    import json, re; s = open(path, encoding='utf-8').read()
    data = json.loads(re.search(r'id="swifteye-trades">(.*?)</script>', s, re.S).group(1))

Per trade: the bot's own record (buy and sale price, shares, fees, result, reason, signal details), Polymarket's
bid / ask / last for every outcome of the game, and every bookmaker's winner-market odds for every outcome, from
15 minutes before the buy to 10 minutes after the sale (5 minutes for the pre-match favourite bets, held to the
end of the game). Reads are bounded by game id (indexed), never by time alone, so the live feed is not slowed.

usage: python3 tools/pack_trades.py [out.html]
"""
import sys, os, json, glob, sqlite3, subprocess, tempfile, time, datetime as dt

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(f"~/Documents/dev/swifteye_trades_{dt.date.today():%Y-%m-%d}.html")
VIEWER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pack_trades_viewer.html')
BIN = max(glob.glob(os.path.expanduser('~/swifteye-src/target/release/build/oddsfeed/*/out/oddsfeed-*')), key=os.path.getmtime)
NO = dt.timezone(dt.timedelta(hours=1))
BEFORE_MS = 15 * 60_000
AFTER_MS = {'favs': 5 * 60_000}
AFTER_DEFAULT_MS = 10 * 60_000
# Real trades of 19 Nov 2026: old rules and old sale code, three sold by hand at estimated prices. Kept in the file,
# but the owner leaves them out of every real-money figure (bots::REAL_COUNTS_SINCE_MS = 20 Nov 00:00 Norway).
REAL_COUNTS_SINCE_MS = 1795129200000
BOT_NAMES = {
    'snipe': 'Snipe bot (follows a bookmaker move Polymarket has not made yet)',
    'snipe-trial': 'Snipe bot, trial wallet (paper; sports kept off real money)',
    'snipe-1book': 'Snipe bot, one-bookmaker test (paper, 18 Nov)',
    'favs': 'Pre-match favourites (paper; bought before the start, held to the result)',
}


def chart(tid, open_, now):
    """The Telegram chart config, from the bot's own test program (as sent with the SALE, or the BUY if open)."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, 'c.json')
        env = dict(os.environ, SWIFTEYE_DB=DB, TRADE_ID=str(tid), OUT=out, NOW=str(now))
        if open_:
            env['OPEN'] = '1'
        r = subprocess.run([BIN, '--ignored', '--exact', 'telegram::bot_text_tests::dump_trade_chart'], env=env, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            return None
        return json.load(open(out))


def r4(x):
    return None if x is None else round(x, 4)


def pack(c, row, now):
    tid, bot, real, ts, mid, closed, data = row
    v = json.loads(data)
    sell_ms = closed or v.get('exit_ts')
    t0 = ts - BEFORE_MS
    t1 = min(now, (sell_ms or now) + AFTER_MS.get(bot, AFTER_DEFAULT_MS))
    m = c.execute("SELECT sport, home, away, league, start_ms FROM matches WHERE id = ?", (mid,)).fetchone() or (None,) * 5

    # Polymarket: every outcome, changes of (bid, ask, last) only, plus where each stood when the window opens.
    pm, last = {}, {}
    outs = [o for (o,) in c.execute("SELECT DISTINCT outcome FROM pm_quotes WHERE match_id = ? AND ts_ms BETWEEN ? AND ?", (mid, t0, t1))]
    for o in outs:
        s = c.execute("SELECT ts_ms, bid, ask, last FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms < ? ORDER BY ts_ms DESC LIMIT 1", (mid, o, t0)).fetchone()
        pm[o] = [[0, r4(s[1]), r4(s[2]), r4(s[3])]] if s else []
        last[o] = tuple(pm[o][0][1:]) if s else None
    for t, o, b, a, l in c.execute("SELECT ts_ms, outcome, bid, ask, last FROM pm_quotes WHERE match_id = ? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", (mid, t0, t1)):
        k = (r4(b), r4(a), r4(l))
        if k != last.get(o):
            pm.setdefault(o, []).append([t - t0, *k])
            last[o] = k

    # dir -1 = the bot bought the No token of `outcome`: its bid is 1 - ask, its ask 1 - bid (as telegram.rs draws it).
    yes = (v.get('dir') or 1.0) > 0

    def pm_at(o, t):
        p = None
        for x in pm.get(o, []):
            if x[0] + t0 > t:
                break
            p = x
        if not p:
            return None
        flip = lambda x: None if x is None else r4(1 - x)
        b, a, l = p[1], p[2], p[3]
        return {'bid': b, 'ask': a, 'last': l} if yes else {'bid': flip(a), 'ask': flip(b), 'last': flip(l)}

    # Bookmakers: the winner market ('1x2', also two-way winners), every book and outcome (Kalshi included;
    # Polymarket's own rows in `changes` are left out, its bid / ask above is the full record). The whole game's history
    # up to the window end is read (by game id) so each line starts where the book stood and the at-buy / at-sale
    # snapshots see books that had not moved for a while.
    books, state, at = {}, {}, {'buy': {}, 'sell': {}}
    marks = [('buy', ts)] + ([('sell', sell_ms)] if sell_ms else [])
    started = False
    for t, b, o, p in c.execute("SELECT ts_ms, book, outcome, new_price FROM changes WHERE match_id = ? AND market = '1x2' AND ts_ms <= ? AND book != 'polymarket' ORDER BY ts_ms", (mid, t1)):
        while marks and t > marks[0][1]:
            name, _ = marks.pop(0)
            for (bb, oo), pp in state.items():
                if pp is not None:
                    at[name].setdefault(bb, {})[oo] = pp
        if t >= t0 and not started:
            started = True
            for (bb, oo), pp in state.items():
                books.setdefault(bb, {}).setdefault(oo, []).append([0, pp])
        if t >= t0:
            books.setdefault(b, {}).setdefault(o, []).append([t - t0, p])
        if p is not None:
            state[(b, o)] = p
        elif (b, o) not in state:
            state[(b, o)] = None
    for name, _ in marks:
        for (bb, oo), pp in state.items():
            if pp is not None:
                at[name].setdefault(bb, {})[oo] = pp
    if not started:
        for (bb, oo), pp in state.items():
            books.setdefault(bb, {}).setdefault(oo, []).append([0, pp])

    o = v.get('outcome')
    def implied(odds):
        q = 1 / max(odds, 1.0001)
        return r4(q if yes else 1 - q)
    side = {k: {bb: {'odds': ps.get(o), 'implied': implied(ps[o])} for bb, ps in sorted(at[k].items()) if ps.get(o)} for k in at}
    ch = chart(tid, closed is None, (closed + 3000) if closed else now)
    return {
        'id': tid, 'bot': bot, 'real': bool(real),
        'counts_in_real_results': bool(real) and ts >= REAL_COUNTS_SINCE_MS,
        'open': closed is None,
        'sport': v.get('sport') or m[0], 'league': m[3], 'home': v.get('home') or m[1], 'away': v.get('away') or m[2],
        'start_ms': v.get('start_ms') or m[4], 'match_id': mid,
        'outcome': o, 'token': 'yes' if yes else 'no', 'side_name': {'home': v.get('home') or m[1], 'away': v.get('away') or m[2]}.get(o, o),
        'buy_ms': ts, 'buy_price': r4(v.get('entry')), 'sell_ms': sell_ms, 'sell_price': r4(v.get('exit')),
        'shares': v.get('contracts'), 'cost': r4(v.get('cost')), 'pnl': r4(v.get('pnl')), 'reason': v.get('reason'),
        'trigger': v.get('trigger'), 'move_ms': v.get('move_ms'),
        'polymarket_at': {'buy': pm_at(o, ts), 'sell': pm_at(o, sell_ms) if sell_ms else None},
        'bookmakers_at': side,
        'window': {'from_ms': t0, 'to_ms': t1},
        'polymarket': pm, 'bookmakers': books,
        'record': v,
        'chart': ch,
    }


def main():
    t_start = time.time()
    c = sqlite3.connect('file:' + DB + '?mode=ro', uri=True, timeout=10)
    now = int(time.time() * 1000)
    rows = c.execute("SELECT id, bot, real, ts_ms, match_id, closed_ms, data FROM bot_trades ORDER BY id").fetchall()
    trades = []
    for i, row in enumerate(rows):
        trades.append(pack(c, row, now))
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(rows)} trades ({time.time() - t_start:.0f} s)", flush=True)
    doc = {
        'what': 'Swifteye: every paper and real trade the bots made, with Polymarket and bookmaker prices around each one.',
        'generated': dt.datetime.now(NO).strftime('%Y-%m-%d %H:%M Norway time'),
        'generated_ms': now,
        'bots': BOT_NAMES,
        'readme': {
            'times': 'All *_ms are Unix milliseconds (UTC). The page shows Norway time (UTC+1).',
            'prices': 'buy_price / sell_price and Polymarket bid / ask / last are probabilities 0-1 (0.48 = 48 cents a share).',
            'token': "'yes' = the bot bought `outcome` (record dir 1); 'no' = it bought the No token of `outcome`, i.e. bet against it (record dir -1). buy_price / sell_price are the token bought.",
            'polymarket': "polymarket[outcome] = [[ms after window.from_ms, bid, ask, last], ...]: a row each time one of the three changed; the first row at 0 is where it stood when the window opens.",
            'bookmakers': "bookmakers[book][outcome] = [[ms after window.from_ms, decimal odds or null = suspended], ...] for the winner market ('1x2', two-way winners too). The row at 0 is where the book stood when the window opens.",
            'bookmakers_at': "Each bookmaker's decimal odds for `outcome` at the buy and at the sale, and implied = the token bought: 1 / odds for 'yes', 1 - 1 / odds for 'no' (the bot's own measure, margin not removed).",
            'polymarket_at': "Polymarket's bid / ask / last for the token bought, at the buy and at the sale (for 'no': bid = 1 - ask of `outcome`, ask = 1 - bid). The raw series in `polymarket` are always for `outcome` itself.",
            'chart': 'The Chart.js 2 config of the chart the bot sends to Telegram (as sent with the sale; with the buy if still open). x = minutes, y = cents.',
            'record': "The bot's own stored record of the trade (bot_trades.data), unchanged.",
            'counts_in_real_results': 'False for the real trades of 19 Nov 2026 (old rules, three sold by hand at estimated prices), which the owner leaves out of real-money results.',
            'window': 'Prices are kept from 15 minutes before the buy to 10 minutes after the sale (5 for favourites), or to the moment the file was made.',
        },
        'trades': trades,
    }
    blob = json.dumps(doc, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/')
    page = open(VIEWER, encoding='utf-8').read().replace('/*__DATA__*/', blob)
    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(page)
    charts = sum(1 for t in trades if t['chart'])
    print(f"{len(trades)} trades, {charts} charts, {os.path.getsize(OUT) / 1e6:.1f} MB -> {OUT} ({time.time() - t_start:.0f} s)")


if __name__ == '__main__':
    main()
