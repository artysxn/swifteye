"""What $40 makes with BOTH bots as they run now, over the days of the backtest (owner, 21 Nov 2026).
  snipes       today's rules (moves 8+ pt, lagged earlier 50%+, no Dota 2), clean replay trades only (tools/sweep.py),
               25% of the wallet a trade, half in weak games, never over $12, nothing under $5
  favourites   the pre-match favourite bet without high-suspicion CS2 games (tools/value_test.py), 4% of the wallet
               a bet, held to the result (money back 3 h after the listed start), at most 80% of the wallet tied up
One wallet: a bet or trade needs free cash. Both were CHOSEN on these same days, so this is the flattering case.
usage: combo40.py [favourite share, default 0.04]"""
import sys, os, random, datetime as dt
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
SHARE = float(sys.argv[1]) if len(sys.argv) > 1 else 0.04
sys.argv = ['x', 'lib']
import sweep as S
import value_test as V
NO = dt.timezone(dt.timedelta(hours=1))
day = lambda ms: dt.datetime.fromtimestamp(ms / 1000, NO).strftime('%d')
fl = lambda c: (c['cx']['follow'] - c['cx']['lead']) if c['cx']['jumps'] else None
T = S.sim(dict(S.BASE, legacy=False, skip=('dota2',), min_move=0.08, lag=0.5))
sn = [dict(kind='snipe', t=t['c']['tf'], end=t['xts'], ret=t['pnl'] / t['spent'], weak=(fl(t['c']) is None or fl(t['c']) < 0.2)) for t in T]
t_lo, t_hi = min(c['tf'] for c in S.ROWS), max(c['tf'] for c in S.ROWS)
fb = []
for r in V.bets():
    sc = V.score(r['m'])[0] if r['sport'] == 'cs2' else None
    if sc is not None and sc >= 80: continue
    fb.append(dict(kind='fav', t=r['t'], end=r['start'] + 3 * 3_600_000, won=r['won'], cost=V.cost(r)))
print(f"window {dt.datetime.fromtimestamp(t_lo / 1000, NO):%d %b %H:%M} to {dt.datetime.fromtimestamp(t_hi / 1000, NO):%d %b %H:%M} Norway ({(t_hi - t_lo) / 86_400_000:.1f} days)")
print(f"snipe trades in it: {len(sn)} ({100 * sum(x['ret'] > 0 for x in sn) / len(sn):.0f}% won); favourite bets in it: {sum(t_lo <= x['t'] <= t_hi for x in fb)} of {len(fb)} ({100 * sum(x['won'] for x in fb) / len(fb):.0f}% won)")


def run(events, share=SHARE, start=40.0):
    w = start; openp = []; low = w; taken = defaultdict(int); skipped = defaultdict(int); made = defaultdict(float); ends = {}
    def settle(now):
        nonlocal w, low
        for o in sorted([o for o in openp if o[0] <= now]):
            w += o[2]; made[o[3]] += o[2]; low = min(low, w); ends[day(o[0])] = w
        openp[:] = [o for o in openp if o[0] > now]
    for e in sorted(events, key=lambda e: e['t']):
        settle(e['t'])
        tied = sum(o[1] for o in openp); free = w - tied
        if e['kind'] == 'fav':
            stake = share * w
            if stake < 1.0 or stake > free or sum(o[1] for o in openp if o[3] == 'fav') + stake > 0.8 * w: skipped['fav'] += 1; continue
            pnl = stake / e['cost'] * e['won'] - stake
        else:
            stake = min(0.25 * w, 12.0) * (0.5 if e['weak'] else 1.0)
            if stake < 5.0 or stake > free: skipped['snipe'] += 1; continue
            pnl = stake * e['ret']
        openp.append((e['end'], stake, pnl, e['kind'])); taken[e['kind']] += 1
    settle(10 ** 15)
    return w, low, dict(taken), dict(skipped), dict(made), ends


inw = [x for x in fb if t_lo <= x['t'] <= t_hi]
for name, ev in (("snipes alone", sn), (f"favourites alone at {100 * SHARE:.0f}%", inw), ("BOTH in one wallet", sn + inw)):
    w, low, tk, sk, made, ends = run(ev)
    print(f"  {name:28s} $40 -> ${w:6.2f} | lowest ${low:.2f} | taken {tk}, skipped {sk or 'none'} | made {({k: round(v, 2) for k, v in made.items()})} | end of each day " + ' '.join(f"{d}:{v:.1f}" for d, v in sorted(ends.items())))
print("  other favourite stakes, both bots in one wallet:")
for sh in (0.02, 0.04, 0.06, 0.10):
    w, low, tk, sk, made, ends = run(sn + inw, share=sh)
    print(f"     {100 * sh:3.0f}% a favourite bet: $40 -> ${w:6.2f} (favourites {made.get('fav', 0):+.2f}, snipes {made.get('snipe', 0):+.2f}); lowest ${low:.2f}; skipped {sk or 'none'}")
# how sure: redraw the same number of trades and bets from the ones seen, many times
random.seed(3); res = []
for _ in range(3000):
    ev = [dict(random.choice(sn), t=x['t'], end=x['end']) for x in sn] + [dict(random.choice(inw), t=x['t'], end=x['end']) for x in inw]
    res.append(run(ev)[0])
res.sort()
print(f"  if the same number of trades and bets were drawn again from the ones seen: 1 in 10 below ${res[300]:.0f}, middle ${res[1500]:.0f}, 1 in 10 above ${res[2700]:.0f}; under $40 in {100 * sum(r < 40 for r in res) / 3000:.0f}% of draws")
