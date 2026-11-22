"""The 'wait and confirm' system in detail (found in systems_backtest.py, 21 Nov 2026): buy D seconds after the
bookmaker's move, only if the trigger still holds half of it AND the other bookmaker families have made X% of it,
at the price really there then. Variations, combinations, and the live real trades. Needs the cached signals."""
import sys, os, json, bisect, sqlite3
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import importlib.util
src = open(os.path.join(HERE, 'systems_backtest.py')).read().split("HDR = f\"")[0]
exec(compile(src, 'systems_backtest_head', 'exec'))
HDR = f"  {'':56s} {'n':>4s} {'won':>5s} {'profit':>9s} {'each':>6s} | max drop, worst trade | 90% range | days 17/18/19/20 | halves | against the bot as it is"
def show2(name, T, base):
    s = stats(T, base)
    if not s: print(f"  {name:56s} no trades"); return
    print(f"  {name:56s} {s['n']:4d} {s['won'] * 100:4.0f}% {s['p']:+9.2f} {s['p'] / s['n']:+6.2f} | drop {s['dd']:6.2f} worst {s['worst']:+6.2f} | {s['lo']:+5.0f} to {s['hi']:+5.0f} | {' '.join(format(x, '+6.1f') for x in s['day'])} | {s['h'][0]:+6.1f} {s['h'][1]:+6.1f} | {s['p'] - sum(t['pnl'] for t in base):+7.2f} ({s['dlo']:+.0f} to {s['dhi']:+.0f}), better in {s['better'] * 100:3.0f}%")
conf = lambda D, x: (lambda f: f[f'held{D}'] is True and f[f'oth{D}'] is not None and f[f'oth{D}'] >= x)
B0 = run(BASE)
print("== WAIT AND CONFIRM on the bot's rules: how sensitive is it?"); print(HDR); show2('the bot as it is', B0, B0)
for D in (5, 10):
    for x in (0.10, 0.25, 0.50):
        show2(f"wait {D} s, stands, others made {x * 100:.0f}%+", run(dict(BASE, delay=D * 1000), conf(D, x)), B0)
show2('others made 25%+ at 5 s, but buy at once (not possible live)', run(BASE, conf(5, 0.25)), B0)
print("\n== COMBINED"); print(HDR)
for name, p, ex, hold in (
        ('confirm 5 s / 25%, give-ups held', dict(BASE, delay=5000), conf(5, 0.25), True),
        ('confirm 5 s / 25%, move 8+ points', dict(BASE, delay=5000, min_move=0.08), conf(5, 0.25), False),
        ('confirm 5 s / 25%, move 7+ points', dict(BASE, delay=5000, min_move=0.07), conf(5, 0.25), False),
        ("confirm 5 s / 25%, WITHOUT the bot's delay rule", dict(BASE, delay=5000, lag=None), conf(5, 0.25), False),
        ('confirm 5 s / 25%, no delay rule, move 8+', dict(BASE, delay=5000, lag=None, min_move=0.08), conf(5, 0.25), False),
        ('confirm 5 s / 25%, no delay rule, not AHEAD', dict(BASE, delay=5000, lag=None), AND(conf(5, 0.25), notahead), False),
        ('confirm 5 s / 25% on the looser rules', dict(LOOSE, delay=5000), conf(5, 0.25), False),
        ('confirm 5 s / 50% on the looser rules', dict(LOOSE, delay=5000), conf(5, 0.50), False),
        ('confirm 5 s / 25%, looser rules, follows', dict(LOOSE, delay=5000), AND(conf(5, 0.25), follows), False),
        ('confirm 5 s / 25%, looser rules, not AHEAD', dict(LOOSE, delay=5000), AND(conf(5, 0.25), notahead), False)):
    show2(name, run(p, ex, hold), B0)
T = run(dict(BASE, delay=5000), conf(5, 0.25))
print("\n   the confirm 5 s / 25% trades by sport: " + ', '.join(f"{sp} {sum(1 for t in T if t['c']['sport'] == sp)} trades {sum(t['pnl'] for t in T if t['c']['sport'] == sp):+.2f}" for sp in sorted({t['c']['sport'] for t in T})))
print("   caught up: " + f"{sum(bool(t['caught']) for t in T)} of {len(T)}; the bot as it is: {sum(bool(t['caught']) for t in B0)} of {len(B0)}")
# ---- live
print("\n== THE 14 LIVE REAL TRADES: would the confirmation have let them through? (they were bought 3.6-4.6 s after the move)")
tot = {True: [0, 0, 0.0], False: [0, 0, 0.0]}
for i, data in db.execute("SELECT id, data FROM bot_trades WHERE real = 1 AND ts_ms >= 1795129200000 AND closed_ms IS NOT NULL ORDER BY ts_ms"):
    v = json.loads(data)
    c = dict(match_id=v['match_id'], outcome=v['outcome'], t0=v['move_ms'], trigger=v['trigger'], dir=v['dir'], _mv=v['move'])
    f = feats(c); ok = f['held5'] is True and f['oth5'] is not None and f['oth5'] >= 0.25
    tot[ok][0] += 1; tot[ok][1] += v['pnl'] > 0.005; tot[ok][2] += v['pnl']
    print(f"   #{i} {v['home'][:16]:16s} trigger stands at 5 s: {str(f['held5']):5s}  others made {('  -' if f['oth5'] is None else format(f['oth5'] * 100, '4.0f') + '%')} of it  -> {'BUY ' if ok else 'skip'}  {v['reason'][:9]:9s} {v['pnl']:+.2f}")
print(f"   confirmed: {tot[True][0]} trades, {tot[True][1]} won, {tot[True][2]:+.2f} | skipped: {tot[False][0]} trades, {tot[False][1]} won, {tot[False][2]:+.2f}")
