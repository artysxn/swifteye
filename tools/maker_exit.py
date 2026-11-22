"""Cost cut: sell with a RESTING order at the target instead of hitting the bid when Polymarket has caught up.
Polymarket's sports fee is paid by the taker only, so a resting sale pays no fee and gives up no spread.
Clean replay trades only. For each trade, Polymarket's own-clock quotes after the buy (bid, ask, last trade):
  today        the replay's result: sell at the bid once the BID reaches the target (fee + 1c), else give up at 5 min
  resting      a sell order rests at the target from 2 s after the buy; it is filled when the bid reaches the target
               (certain) or when a trade prints ABOVE the target (certain: it had to take our price first);
               'incl. at' also counts a trade AT the target (not certain: others may be ahead in the queue)
  lower        the same, resting 1c or 2c under the target
Not filled by 5 minutes: the order is cancelled and the position is sold as today.
usage: maker_exit.py"""
import sys, os, json, sqlite3, random, time
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.argv = ['x', 'lib']
import sweep as S
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
fee = S.fee
orig = S.gate
random.seed(12)
_path = {}


def path(c):
    k = (c['match_id'], c['outcome'], c['tf'])
    if k not in _path:
        q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask, last FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? AND outcome=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY 1",
                       (c['match_id'], c['tf'] - 5_000, c['tf'] + 330_000, c['outcome'])).fetchall()
        d = c['dir']
        _path[k] = [(t, b if d > 0 else 1 - a, a if d > 0 else 1 - b, None if l is None else (l if d > 0 else 1 - l)) for t, b, a, l in q]
        time.sleep(0.005)
    return _path[k]


def resting(t, off=0.0, incl_at=False):
    """(pnl, filled as maker?) with a resting sale at target - off."""
    c = t['c']; n = t['spent'] / t['entry']
    our_pre = c['pre_mid'] if c['dir'] > 0 else 1 - c['pre_mid']
    px = round(min(our_pre + c['bm'][1] - off, 0.99), 3)
    t_on = c['tf'] + 2_000; t_off = c['tf'] + 300_000
    P = path(c); last0 = None
    for ts, b, a, l in P:
        if ts <= t_on: last0 = l; continue
        if ts > t_off: break
        new_trade = l is not None and l != last0
        if b >= px - 1e-9 or (new_trade and (l > px + 1e-9 or (incl_at and l >= px - 1e-9))):
            return n * (px - t['entry']) - n * fee(t['entry']), True
        last0 = l
    return t['pnl'], False


def run(p, extra=None):
    S.gate = (lambda c, pp, cc: orig(c, pp, cc) and extra(c)) if extra else orig
    try: return S.sim(p)
    finally: S.gate = orig


def line(name, T, vals, base):
    by = defaultdict(float)
    for t, (v, f) in zip(T, vals): by[t['c']['match_id']] += v - t['pnl']
    ks = list(by); bs = sorted(sum(by[random.choice(ks)] for _ in ks) for _ in range(2000))
    tot = sum(v for v, f in vals); sp = sum(t['spent'] for t in T)
    days = ' '.join(f"{sum(v - t['pnl'] for t, (v, f) in zip(T, vals) if t['c']['_day'] == d):+5.1f}" for d in ('17', '18', '19', '20'))
    print(f"     {name:44s} sold at the target {sum(f for v, f in vals):3d} of {len(T)} | {100 * sum(v > 0 for v, f in vals) / len(T):3.0f}% won | ${tot:+7.2f} ({100 * tot / sp:+5.1f}% a $) | v today {tot - base:+6.2f} (90% range {bs[100]:+.1f} to {bs[1900]:+.1f}) | gain by day {days}")


B = dict(S.BASE, legacy=False, skip=('dota2',))
sets = (("the bot now (no Dota 2)", B, None), ("moves from 8 pt + lagged 50%", dict(B, min_move=0.08, lag=0.5), None),
        ("a wider set for a bigger sample (move 7+, followed 10%, room 3c, any number a game)", dict(B, min_move=0.07, max_followed=0.10, min_edge=0.03, one_per_game=False), None),
        ("loose rules + lagged earlier (the losing set)", dict(B, min_move=0.07, cons=0.0, fams=1, gap=None, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False), None))
for nm, p, ex in sets:
    T = run(p, ex)
    base = sum(t['pnl'] for t in T)
    print(f"\n  {nm}: {len(T)} clean trades, today ${base:+.2f} ({100 * base / sum(t['spent'] for t in T):+.1f}% a $), {sum(t['caught'] for t in T)} sold at catch-up, {100 * sum(t['pnl'] > 0 for t in T) / len(T):.0f}% won")
    line("resting sale at the target (certain fills)", T, [resting(t) for t in T], base)
    line("  ... incl. trades printed AT the target", T, [resting(t, 0, True) for t in T], base)
    line("resting 1c under the target", T, [resting(t, 0.01) for t in T], base)
    line("resting 2c under the target", T, [resting(t, 0.02) for t in T], base)
    line("resting 3c under the target", T, [resting(t, 0.03) for t in T], base)
