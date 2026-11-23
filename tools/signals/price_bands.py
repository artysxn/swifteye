"""Wave starts vs a fixed clock, by the price paid (python -I tools/signals/price_bands.py DIR).

Owner, 24 Nov: "holding longer means we are no longer riding a wave, we are gambling ... buying anywhere at any time at
random and holding for 30 minutes might be profitable too"; "randomly trading at < 50 should also be profitable".
The control: waves2.py build --grid 300 --name waves2_grid.npz = one entry every 5 minutes of play, both sides, no
wave at all, valued exactly like the wave starts (ask 1 s later, both fees, depth, sold into the bid 30 min later).
Per sport group and price band, one position a game: the wave starts (waves2.npz) next to the clock (waves2_grid.npz).
If the clock makes the same money, the wave adds nothing and the price band is the whole effect."""
import os
import sys

import numpy as np

BANDS = ((0, .10), (.10, .30), (.30, .50), (.50, .70), (.70, .80), (.80, .90), (.90, 1.01), (0, .50), (.50, 1.01))
GROUPS = (('tennis', 'cs2'), ('tennis',), ('cs2',), ('football',), ('basketball',))


def load(path):
    D = np.load(path)
    Y, TG = D['Y'], D['TG']
    return {'y': Y[:, 8], 'y10': Y[:, 2], 'buy': TG[:, 4], 's30': TG[:, 5], 'sp': D['sport'], 't': D['t'], 'g': D['game'].astype(np.int64), 'typ': D['W'][:, 9],
            'ok': np.isfinite(Y).all(1) & np.isfinite(TG[:, 4])}


def place(S, idx, hold=1_801_000):
    busy, out = {}, []
    for i in idx[np.argsort(S['t'][idx], kind='stable')]:
        g = int(S['g'][i])
        if S['t'][i] < busy.get(g, -1):
            continue
        busy[g] = int(S['t'][i]) + hold
        out.append(i)
    return np.array(out, np.int64)


def cell(S, m, key='y'):
    idx = place(S, np.flatnonzero(m))
    if len(idx) < 15:
        return f'{len(idx):>5} too few'.ljust(46)
    v, g = S[key][idx], S['g'][idx]
    ug, inv = np.unique(g, return_inverse=True)
    gs = np.bincount(inv, v)
    rng = np.random.default_rng(1)
    b = [gs[rng.integers(0, len(ug), len(ug))].sum() / len(v) for _ in range(2000)]
    return f'{len(v):>5} ${v.mean():+6.2f} ({np.quantile(b, .05):+.1f}..{np.quantile(b, .95):+.1f}) win {np.mean(v > 0) * 100:.0f}%'.ljust(46)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    d = sys.argv[1]
    Wv, Ck = load(os.path.join(d, 'waves2.npz')), load(os.path.join(d, 'waves2_grid.npz'))
    print('Hold 30 minutes, one position a game. Each cell: trades, $ per $100 order (90% range of that average), share won.')
    for grp in GROUPS:
        print(f'\n{"+".join(grp)}')
        print(f'   {"price paid":<10} {"WAVE START (any line)":<46} {"SUPERLINE WAVE START":<46} {"FIXED CLOCK, NO WAVE":<46}')
        for lo, hi in BANDS:
            lab = f'{int(lo * 100)}-{min(int(hi * 100), 100)}c'
            f = lambda S: S['ok'] & np.isin(S['sp'], grp) & (S['buy'] >= lo) & (S['buy'] < hi)
            print(f'   {lab:<10} {cell(Wv, f(Wv))} {cell(Wv, f(Wv) & (Wv["typ"] == 0))} {cell(Ck, f(Ck))}')
    # how the under-10c trades win and lose (tennis + cs2, wave starts)
    for name, S in (('wave starts', Wv), ('fixed clock', Ck)):
        idx = place(S, np.flatnonzero(S['ok'] & np.isin(S['sp'], ('tennis', 'cs2')) & (S['buy'] < .10)))
        v, buy, s30 = S['y'][idx], S['buy'][idx], S['s30'][idx]
        mult = s30 / buy
        print(f'\nUnder 10c, tennis + cs2, {name}: {len(v)} trades, average price paid {buy.mean() * 100:.1f}c, average sale price {s30.mean() * 100:.1f}c')
        for lab, m in (('token worthless or nearly (sold under half the price paid)', mult < 0.5), ('sold between half and the price paid', (mult >= 0.5) & (mult < 1.0)),
                       ('sold at 1-2x the price paid', (mult >= 1.0) & (mult < 2.0)), ('sold at 2-5x', (mult >= 2.0) & (mult < 5.0)), ('sold at 5x or more', mult >= 5.0)):
            if m.any():
                print(f'   {lab:<60} {m.mean() * 100:5.1f}% of trades, ${v[m].mean():+8.2f} each, ${v[m].sum():+9,.0f} in all')
        print(f'   10-minute sale of the same trades: ${S["y10"][idx].mean():+.2f} per $100')


if __name__ == '__main__':
    main()
