"""How late was the bot's own Polymarket feed at each replayed trade, and does the replay's profit depend on it?
feed lag = receive time minus Polymarket's own clock, median over that game's quotes from 20 s before to 60 s after the move.
usage: feed_lag.py"""
import sys, os, json, sqlite3, statistics as st, time, random
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.argv = ['x', 'lib']
import tells as T
S = T.S
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
CACHE = os.path.expanduser('~/swifteye/research/feed_lag.json')
L = json.load(open(CACHE)) if os.path.exists(CACHE) else {}


def lag(c):
    k = T.key(c)
    if k not in L:
        q = db.execute("SELECT ts_ms - server_ts_ms FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? AND server_ts_ms IS NOT NULL", (c['match_id'], c['t0'] - 20_000, c['t0'] + 60_000)).fetchall()
        L[k] = st.median(r[0] for r in q) if len(q) >= 3 else None
        time.sleep(0.01)
    return L[k]


def table(U, name):
    for r in U: r['lag'] = lag(r['c'])
    json.dump(L, open(CACHE, 'w'))
    U = [r for r in U if r['lag'] is not None]
    print(f"\n==== {name}: {T.line(U)} | ${sum(r['pnl'] for r in U):+.2f}")
    B = (('on time (under 0.3 s)', -1e12, 300), ('0.3-3 s late', 300, 3000), ('3 s+ late', 3000, 1e12))
    for nm, lo, hi in B:
        g = [r for r in U if lo <= r['lag'] < hi]
        if not g: continue
        by = defaultdict(float)
        for r in g: by[r['m']] += r['pnl']
        ks = list(by); v = sorted(sum(by[random.choice(ks)] for _ in ks) for _ in range(800))
        days = ' '.join(f"{dy}:{sum(r['pnl'] for r in g if r['day'] == dy):+6.1f}({sum(1 for r in g if r['day'] == dy)})" for dy in ('17', '18', '19', '20'))
        print(f"  feed {nm:22s} {T.line(g)} | ${sum(r['pnl'] for r in g):+7.2f} (90% range {v[40]:+.0f} to {v[760]:+.0f}) | by day {days}")
        for sp in ('cs2', 'tennis', 'dota2', 'football'):
            x = [r for r in g if r['sport'] == sp]
            if len(x) >= 4: print(f"       {sp:9s} {T.line(x)} | ${sum(r['pnl'] for r in x):+7.2f}")


random.seed(2)
if __name__ == '__main__':
    table(T.universe(dict(T.P, one_per_game=False)), "BOT's rules")
    table(T.universe(T.MID), "bot's rules a little looser (move 7+, followed 10%, room 3c)")
    table(T.universe(dict(T.LOOSE, lag=0.3, bb='none')), "LOOSE rules + lagged earlier")
    table(T.universe(dict(T.LOOSE, bb='none')), "LOOSE rules")
