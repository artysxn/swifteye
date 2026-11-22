"""Looser rules outside the peak hours (12-21 UTC): would they pay?

Every variant is judged only on trades that were REALLY available: Polymarket is re-read by its own clock
(server_ts_ms), so a late feed on our side cannot invent a trade. $12 a trade, give-up 5 minutes, fills from the
replay's saved blocks (order-book depth, 1c off every sale).
"""
import sqlite3, datetime as dt, statistics as st, time, bisect, random
from collections import Counter, defaultdict
import replay_eval as R

C = R.load()
db = sqlite3.connect('file:/Users/danielvinichenko/swifteye/oddsfeed.db?mode=ro', uri=True)
U = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
hour = lambda c: U(c['tf']).hour
PEAK = lambda c: 12 <= hour(c) < 21
ci, gi = R.CATCH.index(1.0), R.GIVE.index(300)
fee = lambda p: 0.05 * p * (1 - p)

BASE = dict(min_move=0.10, max_followed=0.05, min_edge=0.04, max_spread=0.04, fams=2, cons=0.03, lag=True, gap=0.10, cap=0.90, basketball=False, pause=True)


def passes(c, p):
    cx = c['cx']
    if c['bm'][0] < p['min_move']: return 'move too small'
    if cx['fam_moved'] < p['fams'] or cx['cons_move'] < p['cons']: return 'too few bookmaker groups moved'
    if max(c['fol']) > p['max_followed'] or c['fol_start'] > p['max_followed']: return 'Polymarket already followed'
    if max(c['sp']) > p['max_spread'] + 1e-9: return 'spread too wide'
    if c['edge'][0][ci] < p['min_edge'] or c['edge'][1][ci] < p['min_edge']: return 'too little room after costs'
    if c['sport'] == 'basketball' and not p['basketball']: return 'basketball'
    if p['pause'] and c['pause']: return 'right after a pause'
    if (max(c['dec']) if c['dec'][0] > 0 else c['px'] + 0.01) > p['cap']: return 'price above the cap'
    if p['lag'] and (cx['jumps'] == 0 or cx['follow'] < 0.3): return 'Polymarket did not lag earlier in this game'
    g = R.gap_before(c)
    if p['gap'] is not None and g is not None and g >= p['gap']: return 'the two were already apart'
    return None


_q = {}


def truly(c, entry, max_fol):
    """(really available?, true buy price) by Polymarket's own clock."""
    k = (c['match_id'], c['outcome'], c['t0'], c['tf'])
    if k not in _q:
        rows = sorted(db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL",
                                 (c['match_id'], c['outcome'], c['t0'] - 60_000, c['tf'] + 120_000)).fetchall())
        time.sleep(0.008)
        T = [r[0] for r in rows]
        at = lambda x: (lambda i: rows[i] if i >= 0 else None)(bisect.bisect_right(T, x) - 1)
        _q[k] = (at(c['tf']), at(c['t0'] + 1500))
    b, b2 = _q[k]
    if not (b and b2): return False, entry
    d = c['dir']; buy = b[2] if d > 0 else 1 - b[1]
    fol = ((b2[1] + b2[2]) / 2 - c['pre_mid']) * d / c['bm'][1]
    return (fol <= max_fol and buy - entry <= 0.02 + 1e-9), max(buy, entry)


def run(p, rows):
    """The bot's trades under rules `p` on `rows`, kept only where really available."""
    out, recent, busy = [], {}, {}
    for c in rows:
        if passes(c, p): continue
        m = c['match_id']
        if m in recent and c['tf'] - recent[m] < 120_000: continue
        recent[m] = c['tf']
        if c['nofill']: continue
        x = c['exits'][ci][gi]
        if not x: continue
        if busy.get(m, 0) > c['tf']: continue
        busy[m] = x['ts']
        f = R.fill(c, x, 12.0, c['edge'][1][ci] - R.CAP >= p['min_edge'] - 1e-9)
        if not f: continue
        ok, px = truly(c, f['entry'], p['max_followed'])
        if not ok: continue
        n = f['spent'] / f['entry']; ex = f['exit']
        pnl = n * (ex - px) - n * (fee(px) + (fee(ex) if 0 < ex < 1 else 0))
        out.append(dict(c=c, pnl=pnl, spent=n * px))
    return out


def boot(T, n=400):
    by = defaultdict(list)
    for t in T: by[t['c']['match_id']].append(t['pnl'])
    ks = list(by); rnd = random.Random(5); v = []
    for _ in range(n): v.append(sum(sum(by[rnd.choice(ks)]) for _ in ks))
    v.sort()
    return v[int(n * .05)], v[int(n * .95)]


def line(name, T, base=None):
    if not T: return f"  {name:46s} trades   0"
    tot = sum(t['pnl'] for t in T); lo, hi = boot(T) if len({t['c']['match_id'] for t in T}) >= 3 else (float('nan'), float('nan'))
    extra = '' if base is None else f" | the added trades alone: {len(T) - base[0]:+d}, {tot - base[1]:+.2f}"
    return (f"  {name:46s} trades {len(T):3d} in {len({t['c']['match_id'] for t in T}):3d} games | {sum(t['pnl'] > 0 for t in T) / len(T) * 100:3.0f}% won | {tot:+8.2f} "
            f"(90% range {lo:+6.0f} .. {hi:+6.0f}) | {tot / sum(t['spent'] for t in T) * 100:+5.1f}% a $ | worst {min(t['pnl'] for t in T):+6.2f}{extra}")


if __name__ == '__main__':
    OFF = [c for c in C if not PEAK(c)]; ON = [c for c in C if PEAK(c)]
    hrs = (max(c['tf'] for c in C) - min(c['tf'] for c in C)) / 3600e3
    print(f"recording {hrs:.0f} h; off-peak (outside 12-21 UTC) is about {hrs * 15 / 24:.0f} h of it, with {len(OFF)} candidate rows (7+ point moves); peak has {len(ON)}")

    def moves(rows):
        seen = {}; n = 0
        for c in sorted(rows, key=lambda c: c['tf']):
            k = (c['match_id'], c['outcome'], c['dir'])
            if k not in seen or c['tf'] - seen[k] > 90_000: n += 1
            seen[k] = c['tf']
        return n
    print("\nA. WHAT STOPS THE OFF-PEAK MOVES (the first rule each one fails, current rules)")
    why = Counter(passes(c, BASE) or 'PASSES' for c in OFF)
    for k, n in why.most_common(): print(f"   {n:6d} rows  {k}")
    print("   sports of the off-peak rows:", dict(Counter(c['sport'] for c in OFF).most_common(6)))

    print("\nB. OFF-PEAK: each rule loosened on its own, then some together (really-available trades only)")
    b_off = run(BASE, OFF); base = (len(b_off), sum(t['pnl'] for t in b_off))
    print(line('current rules', b_off))
    V = [
        ('move 9 points (now 10)', dict(min_move=0.09)), ('move 8 points', dict(min_move=0.08)), ('move 7 points', dict(min_move=0.07)),
        ('Polymarket followed up to 10% (now 5%)', dict(max_followed=0.10)), ('followed up to 20%', dict(max_followed=0.20)),
        ('room 3c after costs (now 4c)', dict(min_edge=0.03)), ('room 2c', dict(min_edge=0.02)),
        ('spread up to 6c (now 4c)', dict(max_spread=0.06)),
        ('consensus moved 2 pt (now 3)', dict(cons=0.02)), ('no consensus rule', dict(cons=0.0)),
        ('1 bookmaker group is enough (now 2)', dict(fams=1, cons=0.0)),
        ('no "lagged earlier in this game" rule', dict(lag=False)),
        ('no gap rule', dict(gap=None)), ('allow right after a pause', dict(pause=False)), ('price cap 95c (now 90c)', dict(cap=0.95)),
        ('basketball allowed', dict(basketball=True)),
        ('basketball allowed + no "lagged earlier"', dict(basketball=True, lag=False)),
        ('no "lagged earlier" + move 8 pt', dict(lag=False, min_move=0.08)),
        ('no "lagged earlier" + followed 10% + room 3c', dict(lag=False, max_followed=0.10, min_edge=0.03)),
        ('everything loose (8 pt, 10%, 3c, 6c, no lag rule)', dict(min_move=0.08, max_followed=0.10, min_edge=0.03, max_spread=0.06, lag=False)),
    ]
    res = {}
    for name, kw in V:
        T = run(dict(BASE, **kw), OFF); res[name] = T
        print(line(name, T, base))

    print("\nC. THE SAME CHANGES IN THE PEAK HOURS (a loosening that only works off-peak on a handful of trades is probably luck)")
    b_on = run(BASE, ON); base_on = (len(b_on), sum(t['pnl'] for t in b_on))
    print(line('current rules', b_on))
    for name, kw in V:
        if name.startswith(('move 8', 'followed up to 10', 'room 3c', 'no "lagged earlier" rule', 'basketball allowed', 'no consensus', 'everything loose', 'no "lagged earlier" + move')):
            print(line(name, run(dict(BASE, **kw), ON), base_on))
