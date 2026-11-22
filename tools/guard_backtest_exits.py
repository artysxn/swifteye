"""Robustness check for tools/guard_backtest.py: the backtest checks every BUY against Polymarket's own clock, but its
SALES come from the replay, which reads the feed as it reached us. During a late feed that sale price is old.
Here every trade is sold at the same moment, at the price Polymarket really showed then (own clock).
Light on the database: one indexed read per trade. Needs ~/swifteye/research/guard_backtest.json (the feed delays).
"""
import sys, os, json, time, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.argv = sys.argv[:1]
import sweep as S

db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
LAG = {}
for name, rows in json.load(open(os.path.expanduser('~/swifteye/research/guard_backtest.json'))).items():
    for tf, sp, lag, pnl, caught in rows: LAG[tf] = lag


def true_bid(m, o, d, t):
    """What a sale of the bought side got at t, by Polymarket's own clock."""
    q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms BETWEEN ? AND ?", (m, o, t - 180_000, t + 180_000)).fetchall()
    q = [x for x in q if x[0] <= t]
    if not q: return None
    _, b, a = max(q, key=lambda x: x[0])
    if d > 0: return b
    return None if a is None else 1 - a


T = S.sim(dict(S.BASE, legacy=True))
groups = {'feed over 1 s late at the buy': [], 'feed on time': []}
for i, t in enumerate(T):
    c = t['c']; lag = LAG.get(c['tf'])
    if lag is None: continue
    tb = true_bid(c['match_id'], c['outcome'], c['dir'], t['xts'])
    n = t['spent'] / t['entry']
    t['true_exit'] = tb
    t['true_pnl'] = t['pnl'] if tb is None or not (0 < t['exit'] < 1) else n * (tb - t['entry']) - n * (S.fee(t['entry']) + (S.fee(tb) if 0 < tb < 1 else 0))
    groups['feed over 1 s late at the buy' if lag > 1000 else 'feed on time'].append(t)
    if i % 20 == 19: time.sleep(0.1)
print(f"{'':32s} {'trades':>6s} | {'as backtested':>24s} | {'sold at the real price':>26s} | sale price: backtest - real, average")
for name, X in groups.items():
    if not X: continue
    k = [t for t in X if t['true_exit'] is not None and 0 < t['exit'] < 1]
    gap = sum(t['exit'] - t['true_exit'] for t in k) / len(k) if k else 0.0
    print(f"{name:32s} {len(X):6d} | {sum(t['pnl'] > 0 for t in X):3d} won, {sum(t['pnl'] for t in X):+8.2f}        | {sum(t['true_pnl'] > 0 for t in X):3d} won, {sum(t['true_pnl'] for t in X):+8.2f}          | {gap * 100:+.1f}c over {len(k)} sales")
late = groups['feed over 1 s late at the buy']
print('\nworst five differences among the late-feed trades (backtest sale -> real sale):')
for t in sorted(late, key=lambda t: t['true_pnl'] - t['pnl'])[:5]:
    c = t['c']; print(f"  {c['sport']:7s} {c['home'][:16]} v {c['away'][:16]} | bought {t['entry']:.2f} | sale {t['exit']:.2f} -> {t['true_exit'] if t['true_exit'] is None else round(t['true_exit'], 2)} | {t['pnl']:+.2f} -> {t['true_pnl']:+.2f}")
