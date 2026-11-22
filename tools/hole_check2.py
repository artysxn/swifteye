"""Holes in the Polymarket recording at the moment of a replayed buy.
For each replayed trade: pool the quotes of up to 10 OTHER games that were live around it (by Polymarket's own clock)
and measure the silent gap in that pooled stream that contains the buy. With 10 live games the pooled stream normally
has several quotes a second, so a silent gap of seconds means the recorder received nothing: the replay then 'buys'
a price it could not have seen moving. usage: hole_check2.py"""
import sys, os, json, sqlite3, bisect, statistics as st, time, random
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.argv = ['x', 'lib']
import tells as T
S = T.S
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
L = json.load(open(os.path.expanduser('~/swifteye/research/feed_lag.json')))
CACHE = os.path.expanduser('~/swifteye/research/hole_gap.json')
H = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
allrows = sorted(S.ROWS, key=lambda c: c['t0']); T0 = [c['t0'] for c in allrows]


def gap(c):
    k = T.key(c)
    if k in H: return H[k]
    lo, hi = bisect.bisect_left(T0, c['t0'] - 900_000), bisect.bisect_right(T0, c['t0'] + 900_000)
    cnt = defaultdict(int)
    for x in allrows[lo:hi]:
        if x['match_id'] != c['match_id']: cnt[x['match_id']] += 1
    ts = []; used = 0
    for m, _ in sorted(cnt.items(), key=lambda x: -x[1])[:14]:
        o = [r[0] for r in db.execute("SELECT COALESCE(server_ts_ms, ts_ms) FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=?", (m, c['t0'] - 60_000, c['tf'] + 150_000))]
        o = [t for t in o if c['t0'] - 60_000 <= t <= c['tf'] + 60_000]
        if len(o) >= 30: ts += o; used += 1
        if used >= 10: break
    time.sleep(0.01)
    if used < 3: H[k] = None; return None
    ts.sort()
    i = bisect.bisect_right(ts, c['tf'])
    a = ts[i - 1] if i else c['t0'] - 60_000; b = ts[i] if i < len(ts) else c['tf'] + 60_000
    j = bisect.bisect_right(ts, c['t0'])
    # quotes in the pooled stream between the move and the buy, and the usual number in a stretch that long
    n_in = i - j; usual = len(ts) * (c['tf'] - c['t0']) / (c['tf'] - c['t0'] + 120_000)
    H[k] = dict(gap=(b - a) / 1000, before=(c['tf'] - a) / 1000, games=used, n_in=n_in, usual=usual)
    return H[k]


def table(U, name):
    for r in U: r['h'] = gap(r['c']); r['lag'] = L.get(T.key(r['c']))
    json.dump(H, open(CACHE, 'w'))
    U = [r for r in U if r['h'] and r['lag'] is not None]
    print(f"\n==== {name}: {len(U)} trades with 3+ other live games to compare")
    print("  the bot's feed then      | trades | silent gap in the pooled stream of other games at the buy: median s / 90% s | share over 2 s | quotes of other games between the move and the buy vs usual")
    for nm, lo, hi in (('on time (under 0.3 s)', -1e12, 300), ('0.3-3 s late', 300, 3000), ('3-15 s late', 3000, 15000), ('15 s+ late', 15000, 1e12)):
        g = [r for r in U if lo <= r['lag'] < hi]
        if len(g) < 4: continue
        gp = sorted(r['h']['gap'] for r in g)
        print(f"  {nm:24s} | {len(g):4d} | {st.median(gp):6.2f} / {gp[int(len(gp) * .9)]:6.2f} | {100 * sum(x > 2 for x in gp) / len(gp):3.0f}% | {st.median(r['h']['n_in'] for r in g):5.0f} vs {st.median(r['h']['usual'] for r in g):5.0f}")
    hole = [r for r in U if r['h']['gap'] > 2]; ok = [r for r in U if r['h']['gap'] <= 2]
    for nm, g in (('buy made inside a silent gap of 2 s+', hole), ('buy made while quotes were flowing', ok)):
        if g: print(f"  {nm:38s} {T.line(g)} | ${sum(r['pnl'] for r in g):+8.2f}")


if __name__ == '__main__':
    table(T.universe(dict(T.P, one_per_game=False)), "BOT's rules")
    table(T.universe(T.MID), "bot's rules a little looser")
