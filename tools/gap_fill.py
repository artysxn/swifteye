"""Fill the gaps in the Polymarket recording from Polymarket's OWN public trade history, and re-price the replay's
trades at the prices that really traded (owner, 21 Nov 2026: "is there no way to fix the gaps? buy more expensive to
retain the trades?").

For every replay trade under the bot's rules and a little looser (gap trades AND clean trades, the clean ones to check
the method): the game's moneyline market on Polymarket (Gamma API by event id), every trade in it (data API v2), each
trade turned into a price for OUR side. Trade times are whole seconds and run about 3 s behind the order book.
  real entry   the first trade from the buy on (within 90 s); a trade that hit the bid counts 1c dearer for a buy
  real exit    the target when a trade reaches it within 5 minutes, else the last trade by then less 1c
               (no trade at all in those 5 minutes: the next trade within 30 minutes, else the game's result)
Three versions per trade: the replay's own number; "pay whatever it costs" (buy at the real price, however far it
has moved); "the bot's limit" (buy only when the real price is within 2c of the price the replay saw).
Paced (one request per 1.2 s) and it waits while the bot's feed is late.  usage: gap_fill.py [fetch|report]
"""
import sys, os, json, sqlite3, subprocess, time, difflib, random
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
MODE = sys.argv[1] if len(sys.argv) > 1 else 'all'
sys.argv = ['x', 'lib']
import sweep as S
OUT = os.path.expanduser('~/swifteye/research/pm_trades'); os.makedirs(OUT, exist_ok=True)
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
fee = S.fee
random.seed(8)


def get(u):
    for k in range(4):
        try:
            r = subprocess.run(['curl', '-s', '-m', '30', u], capture_output=True, text=True).stdout
            return json.loads(r)
        except Exception:
            time.sleep(2)
    return None


def feed_ok():
    try:
        d = json.loads(subprocess.run(['curl', '-s', '-m', '8', 'http://127.0.0.1:8787/bots'], capture_output=True, text=True).stdout)
        return (d['snipe']['status'].get('pm_lag_ms') or 0) < 400
    except Exception:
        return True


def fetch_trades(cond, need_from):
    """Every trade of a market, newest first, by cursor, until everything from need_from - 15 min on is in."""
    rows = []; cur = None; complete = False
    for page in range(60):
        while not feed_ok(): print('   bot feed late: waiting 20 s', flush=True); time.sleep(20)
        r = get(f"https://data-api.polymarket.com/v2/trades?conditionId={cond}&limit=500" + (f"&cursor={cur}" if cur else '')); time.sleep(1.2)
        d = (r or {}).get('data') or []
        rows += [(int(x['timestamp']), int(x['outcome_index']), x['side'], float(x['price']), float(x['size'])) for x in d]
        pg = (r or {}).get('pagination') or {}
        if not d or not pg.get('has_more') or not pg.get('next_cursor'): complete = bool(r) and 'error' not in (r or {}); break
        if min(x[0] for x in rows) < need_from - 900: complete = True; break
        cur = pg['next_cursor']
    return rows, complete


def trades_of(m, need_from, need_to):
    """[(ts, outcome index, side, price, size)] of the game's moneyline market, and the outcome names + result."""
    f = os.path.join(OUT, f'{m}.json')
    if os.path.exists(f): return json.load(open(f))
    ev = db.execute("SELECT event_id FROM pm_quotes WHERE match_id=? LIMIT 1", (m,)).fetchone()
    if not ev: return None
    e = get('https://gamma-api.polymarket.com/events/' + str(ev[0])); time.sleep(1.2)
    ms = (e or {}).get('markets') or []
    ml = [x for x in ms if x.get('sportsMarketType') == 'moneyline'] or [x for x in ms if len(json.loads(x.get('outcomes') or '[]')) == 2]
    if not ml: return None
    mk = max(ml, key=lambda x: x.get('volumeNum') or 0)
    rows, complete = fetch_trades(mk['conditionId'], need_from)
    try: res = [float(x) for x in json.loads(mk.get('outcomePrices') or '[]')]
    except Exception: res = []
    out = dict(cond=mk['conditionId'], outcomes=json.loads(mk.get('outcomes') or '[]'), result=res, vol=mk.get('volumeNum'), rows=sorted(rows), complete=complete)
    json.dump(out, open(f, 'w'))
    return out


def our_index(c, outs):
    sim = lambda a, b: difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
    if len(outs) != 2: return None
    a = sim(c['home'], outs[0]) + sim(c['away'], outs[1]); b = sim(c['home'], outs[1]) + sim(c['away'], outs[0])
    if abs(a - b) < 0.25: return None
    home = 0 if a > b else 1
    side = c['outcome'] if c['dir'] > 0 else ('away' if c['outcome'] == 'home' else 'home')     # the side we buy
    return home if side == 'home' else 1 - home


def real(c, t, tr):
    """The trade again at the prices that really traded. None = no usable evidence."""
    i = our_index(c, tr['outcomes'])
    if i is None: return None
    # each trade as (time, price for our side, +1 = it lifted OUR ask / -1 = it hit OUR bid)
    P = [(ts, p if oi == i else 1 - p, (1 if sd == 'BUY' else -1) * (1 if oi == i else -1)) for ts, oi, sd, p, sz in tr['rows'] if 0.001 <= p <= 0.999]
    tf = c['tf'] / 1000
    after = [x for x in P if x[0] >= tf]
    first = [x for x in after if x[0] <= tf + 90]
    before = [x for x in P if tf - 600 <= x[0] < tf]
    if not first: return dict(why='no trade in the 90 s after the buy', n90=0)
    buy = first[0][1] + (0.01 if first[0][2] < 0 else 0.0)
    our_pre = c['pre_mid'] if c['dir'] > 0 else 1 - c['pre_mid']
    target = min(our_pre + c['bm'][1], 0.99)
    ex = None; caught = False
    for ts, p, sd in after[1:] if len(after) > 1 else []:
        if ts > tf + 306: break
        if p - (0.01 if sd > 0 else 0.0) >= target - 1e-9: ex = target; caught = True; break
    if ex is None:
        w = [x for x in after if x[0] <= tf + 306]
        if len(w) >= 2: ex = max(w[-1][1] - (0.01 if w[-1][2] > 0 else 0.0) - 0.01, 0.0)
        else:
            nxt = [x for x in after if tf + 306 < x[0] <= tf + 1800]
            if nxt: ex = max(nxt[0][1] - 0.02, 0.0)
            elif tr['result'] and len(tr['result']) == 2: ex = tr['result'][i]
            else: return dict(why='no later trade and no result', n90=len(first))
    buy = min(max(buy, 0.01), 0.99)
    n = 12.0 / buy
    pnl = n * (ex - buy) - n * (fee(buy) + (fee(ex) if 0 < ex < 1 else 0))
    return dict(buy=buy, ex=ex, caught=caught, pnl=pnl, moved=buy - t['entry'], n90=len(first), before=before[-1][1] if before else None, target=target)


def collect():
    P = dict(S.BASE, legacy=False, clean=False)
    sets = (("bot's rules", P), ("a little looser (move 7+, followed 10%, room 3c)", dict(P, min_move=0.07, max_followed=0.10, min_edge=0.03, one_per_game=False)))
    return [(nm, S.sim(p)) for nm, p in sets]


def fetch(sets):
    games = defaultdict(lambda: [10 ** 12, 0])
    for nm, T in sets:
        for t in T:
            g = games[t['c']['match_id']]; g[0] = min(g[0], t['c']['tf'] // 1000); g[1] = max(g[1], t['c']['tf'] // 1000)
    todo = [m for m in games if not os.path.exists(os.path.join(OUT, f'{m}.json')) or not json.load(open(os.path.join(OUT, f'{m}.json'))).get('rows')]
    print(f"{len(games)} games, {len(todo)} to download", flush=True)
    t0 = time.time()
    for k, m in enumerate(todo):
        f = os.path.join(OUT, f'{m}.json')
        if os.path.exists(f):                      # the market is known already: only its trades are missing
            d = json.load(open(f)); d['rows'], d['complete'] = fetch_trades(d['cond'], games[m][0]); d['rows'].sort(); json.dump(d, open(f, 'w'))
        else:
            tr = trades_of(m, games[m][0], games[m][1])
        if k % 10 == 9: print(f"  {k + 1}/{len(todo)} games, {time.time() - t0:.0f} s", flush=True)
    print(f"downloaded in {time.time() - t0:.0f} s", flush=True)


def report(sets):
    for nm, T in sets:
        print(f"\n==== {nm}: {len(T)} replay trades, ${sum(t['pnl'] for t in T):+.2f} as replayed")
        for lab, want in (('CLEAN trades (quotes were flowing): the check of the method', False), ('GAP trades (inside a hole in the recording)', True)):
            G = [t for t in T if bool(t['c']['blind']) == want]
            R = []
            for t in G:
                f = os.path.join(OUT, f"{t['c']['match_id']}.json")
                tr = json.load(open(f)) if os.path.exists(f) else None
                r = real(t['c'], t, tr) if tr and tr.get('rows') else None
                R.append((t, r))
            ok = [(t, r) for t, r in R if r and 'pnl' in r]
            none = [(t, r) for t, r in R if not (r and 'pnl' in r)]
            print(f"  -- {lab}: {len(G)} trades, replay ${sum(t['pnl'] for t in G):+.2f}")
            print(f"     {len(none)} with no real trade within 90 s of the buy (nobody traded: the price shown could not be tested), replay had them at ${sum(t['pnl'] for t, r in none):+.2f}")
            if not ok: continue
            lim = [(t, r) for t, r in ok if r['moved'] <= 0.02 + 1e-9]
            by = defaultdict(float)
            for t, r in ok: by[t['c']['match_id']] += r['pnl']
            ks = list(by); v = sorted(sum(by[random.choice(ks)] for _ in ks) for _ in range(1500))
            mv = sorted(r['moved'] for t, r in ok)
            print(f"     {len(ok)} with real trades: replay ${sum(t['pnl'] for t, r in ok):+.2f} ({sum(t['pnl'] > 0 for t, r in ok)} won)")
            print(f"        the real price at the buy against the replay's: middle {100 * mv[len(mv) // 2]:+.1f}c, a quarter worse than {100 * mv[3 * len(mv) // 4]:+.1f}c, worst {100 * mv[-1]:+.1f}c; within the bot's 2c limit: {len(lim)} of {len(ok)}")
            print(f"        PAY WHATEVER IT COSTS: ${sum(r['pnl'] for t, r in ok):+.2f} ({sum(r['pnl'] > 0 for t, r in ok)} won of {len(ok)}; 90% range {v[75]:+.0f} to {v[1425]:+.0f}); reached the target {sum(r['caught'] for t, r in ok)}")
            print(f"        WITH THE BOT'S 2c LIMIT: {len(lim)} fill, ${sum(r['pnl'] for t, r in lim):+.2f} ({sum(r['pnl'] > 0 for t, r in lim)} won); the other {len(ok) - len(lim)} do not fill")
            if want:
                print("        the biggest replay winners among them, replayed v real:")
                for t, r in sorted(ok, key=lambda x: -x[0]['pnl'])[:8]:
                    c = t['c']
                    print(f"          {c['_day']} Nov {c['sport']:7s} {(c['home'] + ' v ' + c['away'])[:34]:34s} replay paid {100 * t['entry']:4.1f}c {t['pnl']:+6.2f} | really traded at {100 * r['buy']:4.1f}c ({100 * r['moved']:+5.1f}c), {'reached target' if r['caught'] else 'no target'}, out at {100 * r['ex']:4.1f}c: {r['pnl']:+6.2f}")


if __name__ == '__main__':
    sets = collect()
    if MODE in ('fetch', 'all'): fetch(sets)
    if MODE in ('report', 'all'): report(sets)
