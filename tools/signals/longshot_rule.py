"""The plain longshot-wave rule on a waves2 dataset (python -I tools/signals/longshot_rule.py FILE.npz).

No model: at a wave start in play, buy the wave's side if its price is in a band, sell into the bid 30 min later; one
position a game. Dollars per $100 order as waves2.py values them (fees, depth both sides, collapsed tokens at 0).
Found 24 Nov on the Mac recording of 17-23 Nov by looking at all its days (tennis and CS2 under 10c), so it needs data
it has never seen: run this on a later recording (waves2.py build --since ... --name ...)."""
import datetime as dt
import sys

import numpy as np

D = np.load(sys.argv[1])
Y, TG, SP, W, T, G = D['Y'], D['TG'], D['sport'], D['W'], D['t'], D['game'].astype(np.int64)
ok = np.isfinite(Y).all(1) & np.isfinite(TG[:, 4])
buy, s30 = TG[:, 4], TG[:, 5]
f = lambda x: dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).strftime('%d %b %H:%M')
print(f'{len(T)} wave starts, {len(np.unique(G))} games, {f(T.min())} -> {f(T.max())} UTC; by sport: ' + ', '.join(f'{s} {len(np.unique(G[SP == s]))} games' for s in sorted(set(SP.tolist()))))


def place(idx, hold=1_801_000):
    busy, out = {}, []
    for i in idx[np.argsort(T[idx], kind='stable')]:
        g = int(G[i])
        if T[i] < busy.get(g, -1):
            continue
        busy[g] = int(T[i]) + hold
        out.append(i)
    return np.array(out, np.int64)


def line(name, idx, j=8):
    if len(idx) < 8:
        print(f'  {name:<50} {len(idx)} trades: too few')
        return
    v, g = Y[idx, j], G[idx]
    ug, inv = np.unique(g, return_inverse=True)
    gs = np.bincount(inv, v)
    rng = np.random.default_rng(1)
    b = [gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(4000)]
    srt = np.sort(v)[::-1]
    days = np.array([dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).strftime('%d') for x in T[idx]])
    dec = (s30[idx] >= .97) | (s30[idx] <= .03)
    print(f'  {name:<50} {len(v):>5} trades {len(ug):>4} games ${v.sum():+8,.0f} (90% {np.quantile(b, .05):+,.0f}..{np.quantile(b, .95):+,.0f}) ${v.mean():+6.2f}/$100 win {np.mean(v > 0) * 100:.0f}% | '
          f'w/o best 5: ${srt[5:].sum():+,.0f} | by day ' + ' '.join(f'{d}:{v[days == d].sum():+.0f}' for d in sorted(set(days))) + f' | decided at sale {dec.mean() * 100:.0f}%')


for sp in (('tennis', 'cs2'), ('tennis',), ('cs2',)):
    for lo, hi in ((0, .10), (.10, .20), (.20, .30)):
        line(f'{"+".join(sp)} {int(lo * 100)}-{int(hi * 100)}c', place(np.flatnonzero(ok & np.isin(SP, sp) & (buy >= lo) & (buy < hi))))
tc = ok & np.isin(SP, ('tennis', 'cs2')) & (buy < .10)
line('tennis+cs2 0-10c, superline waves only', place(np.flatnonzero(tc & (W[:, 9] == 0))))
line('tennis+cs2 0-10c, Polymarket waves only', place(np.flatnonzero(tc & (W[:, 9] == 1))))
line('tennis+cs2 0-10c, sold after 20 min', place(np.flatnonzero(tc)), 7)
