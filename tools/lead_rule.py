"""Did the bookmaker that triggered a buy LEAD the other bookmakers, or was it the last to move?

Found 21 Nov 2026 in the per-trade audit (tools/trade_audit.py): in 10 of 14 real buys of 20 Nov the trigger
bookmaker jumped TOWARD the level where the other bookmaker families already stood (a line that reopened late),
and only 1 of those 10 caught up; the 4 where the trigger moved first all caught up.

For every backtest trade (tools/sweep.py, 17-19 Nov) this reads that game's bookmaker rows around the move
(changes by match_id + time: indexed, light) and measures, on the outcome's own price scale:
  toward = how much of the trigger's move the OTHER families had already made before it
           (their middle just before the move, less the trigger's old price, as a share of the move)
           1 = the others were already where the trigger jumped to; 0 = the trigger moved first
  follow = how much of the move the others made in the 60 s after it
usage: lead_rule.py
"""
import sys, os, sqlite3, bisect, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.argv = sys.argv[:1]
import sweep as S

db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)


def feats(c):
    m, o, trig, t0, d, mv = c['match_id'], c['outcome'], c['trigger'], c['t0'], c['dir'], c['_mv']
    books = {}
    for t, b, p in db.execute("SELECT ts_ms, book, new_price FROM changes WHERE match_id = ? AND market = '1x2' AND outcome = ? AND ts_ms >= ? AND ts_ms <= ? AND new_price IS NOT NULL AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms",
                              (m, o, t0 - 1_200_000, t0 + 61_000)):
        books.setdefault(b, ([], []))
        books[b][0].append(t); books[b][1].append(1 / max(p, 1.0001))

    def at(b, t):
        if b not in books: return None
        i = bisect.bisect_right(books[b][0], t)
        return books[b][1][i - 1] if i else None

    def others(t):
        fam = {}
        for b in books:
            f = S.platform(b)
            if f == S.platform(trig): continue
            p = at(b, t)
            if p is not None: fam.setdefault(f, []).append(p)
        means = sorted(sum(x) / len(x) for x in fam.values())
        return (means[len(means) // 2] if means else None), len(means)

    new = at(trig, t0 + 1500)
    ob, n = others(t0 - 1000)
    o60, _ = others(t0 + 60_000)
    if new is None or ob is None or mv <= 0: return dict(toward=None, follow=None, n=n)
    old = new - d * mv
    return dict(toward=d * (ob - old) / mv, follow=None if o60 is None else d * (o60 - ob) / mv, n=n, level=d * (ob - (c['_px'] if d > 0 else 1 - c['_px'])))


def line(name, X, base=None):
    n = len(X)
    if not n: return f"  {name:52s} {0:4d}"
    w = sum(t['pnl'] > 0 for t in X); p = sum(t['pnl'] for t in X); caught = sum(bool(t['caught']) for t in X)
    return f"  {name:52s} {n:4d} {w * 100 // n:4d}% {p:+9.2f} {p / n:+6.2f} {caught * 100 // n:5d}%" + (f"   ({p - sum(t['pnl'] for t in base):+8.2f} vs no rule)" if base else '')


out = {}
for title, p in (("THE BOT'S RULES, as it saw the feed (17-19 Nov)", dict(S.BASE, legacy=True)), ("THE BOT'S RULES, feed on time", dict(S.BASE)),
                 ("LOOSER RULES (bigger sample)", dict(S.BASE, legacy=True, min_move=0.07, cons=0.0, fams=1, lag=None, gap=None, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False))):
    T = S.sim(p)
    for t in T: t['f'] = feats(t['c'])
    K = [t for t in T if t['f']['toward'] is not None]
    print(f"\n== {title}")
    print(f"  {'':52s} {'n':>4s} {'won':>5s} {'profit':>9s} {'each':>6s} {'caught':>6s}")
    print(line('all trades', T)); print(line('  no other bookmaker family to compare with', [t for t in T if t['f']['toward'] is None]))
    print('  -- how much of the move the other bookmakers had ALREADY made before the trigger moved')
    for lo, hi, nm in ((-9, 0.0, 'none: the trigger moved first, others behind it'), (0.0, 0.5, 'under half'), (0.5, 0.9, 'half to 90%'), (0.9, 9, '90% or more: trigger was the last to move')):
        print(line(f"   {nm}", [t for t in K if lo <= t['f']['toward'] < hi]))
    print('  -- as a rule: buy only when the trigger LED (others had made less than X of the move)')
    for k in (0.3, 0.5, 0.7, 0.9):
        print(line(f"   keep toward < {k:.1f}", [t for t in T if t['f']['toward'] is None or t['f']['toward'] < k], T))
    print('  -- what the others did in the 60 s AFTER the move (not known at the buy; for understanding only)')
    for lo, hi, nm in ((-9, 0.2, 'did not follow'), (0.2, 0.5, 'followed partly'), (0.5, 9, 'followed at least half')):
        print(line(f"   {nm}", [t for t in K if t['f']['follow'] is not None and lo <= t['f']['follow'] < hi]))
    print('  -- led or lagged, by whether the feed was late at the buy')
    for late in (False, True):
        for led in (True, False):
            print(line(f"   feed {'late' if late else 'on time'}, trigger {'led' if led else 'lagged'}", [t for t in K if (t['c']['late_ms'] > 1000) == late and (t['f']['toward'] < 0.5) == led]))
    out[title] = [[t['c']['tf'], t['c']['sport'], t['c']['match_id'], round(t['pnl'], 3), bool(t['caught']), t['c']['late_ms'], t['f']['toward'], t['f']['follow']] for t in T]
json.dump(out, open(os.path.expanduser('~/swifteye/research/lead_rule.json'), 'w'))
