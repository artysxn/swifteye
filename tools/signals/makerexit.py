"""Rule + model, second round of improvements: when to get out, and how big (24 Nov, PC).

  python tools/signals/makerexit.py --signals DIR [--wallet 175]

Needs makerbetter.py (it saves the five-seed model's calls and their usual size per day in makerbetter_calls.npz).
The base here = makerbetter's result: spreads of 5c+, bookmaker rule, five-seed model expects a gain, 10 shares, doubled
where the call is above its usual level ("two steps"), the exit order always resting.
 1 SMARTER EXITS  the exit order rests only in spreads of 3 / 4 / 5c+; only while the bookmakers have not moved OUR
                  way in the last 30 s (if they have, keep the position); only while the model does not expect the
                  sale to lose (its call for that fill, as if it were an entry on that side, above a line).
 2 SIZE           the same thing at 15 and 20 shares, with the lowest point of the wallet and the stress table.
Each line: wallet after the 6 days | both halves from a fresh wallet | what it adds to the base [90% by game]."""
import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import makerlib as ML  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--wallet', type=float, default=175.0)
    ap.add_argument('--name', default='makersteer_flow.npz')
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    S = ML.Sim(a.signals, a.name)
    stem = '' if a.name == 'makersteer_flow.npz' else a.name.replace('.npz', '') + '_'
    C = np.load(os.path.join(a.signals, stem + 'makerbetter_calls.npz'))
    P, c0 = C['P'], C['c0']
    sc_days = sorted({d_ for d_ in S.days_all if np.isfinite(P[S.rowday == d_]).any()})
    S.set_days(sc_days)
    d1, d2 = [x for x in sc_days if x <= '2026-11-20'], [x for x in sc_days if x >= '2026-11-21']
    rng = np.random.default_rng(0)
    W0 = a.wallet
    fam30 = S.col('fam_up30', 'fam_dn30')
    rel = np.nan_to_num(P) / c0
    two = lambda base: np.maximum(5, np.round(base * np.where(rel >= 1, 2.0, 1.0)))
    kw0 = dict(entry_mask=P > 0, size_arr=two(10))
    base = S.run(w0=W0, **kw0)

    def show(name, ref=None, **kw):
        r = S.run(w0=W0, **kw)
        h1, h2 = S.run(w0=W0, days=d1, **kw)['final'], S.run(w0=W0, days=d2, **kw)['final']
        cl = r['closed']
        dsum, lo, hi = ML.paired(ref or base, r, rng)
        res_ = cl[:, 3] == 1
        say(f'   {name:<66} ${W0:.0f} -> ${r["final"]:>7.2f} | halves ${h1:>6.2f} / ${h2:>6.2f} | {len(cl):>5,} trades, won {np.mean(cl[:, 0] > 1e-9) * 100:2.0f}%, {cl[:, 0].sum() / max(cl[:, 1].sum(), 1) * 100:+.2f}c a share, '
            f'left to the result {res_.mean() * 100:2.0f}% (${cl[res_, 0].sum():+.0f}) | vs the base ${dsum:>+7.2f} [{lo:+.0f}..{hi:+.0f}] | lowest ${r["low"]:.0f}')
        return r, h1, h2
    say(f'RULE + MODEL, ROUND 2. {S.NG} games, in play, {", ".join(x[8:] for x in sc_days)} Nov, ${W0:.0f}. THE BASE = spreads of 5c+, bookmaker rule, five-seed model expects a gain, 10 shares doubled where the call is above its usual level, the exit order always resting.')
    b_, h1b, h2b = show('the base', **kw0)
    say('')
    say('1. SMARTER EXITS')
    R = {}
    for ms_ in (3, 4, 5):
        R[f'sp{ms_}'] = (show(f'the exit order only in spreads of {ms_}c+', exit_minsp=ms_, **kw0), dict(exit_minsp=ms_))
    R['bk'] = (show('no exit while the bookmakers moved OUR way in the last 30 s', exit_mask=fam30 >= 0, **kw0), dict(exit_mask=fam30 >= 0))
    for t in (-2.0, -1.0, -0.5, 0.0):
        R[f'm{t}'] = (show(f'no exit where the model expects that sale to lose more than {abs(t):g}c', exit_mask=~(np.nan_to_num(P, nan=0.0) < t), **kw0), dict(exit_mask=~(np.nan_to_num(P, nan=0.0) < t)))
    R['bk+m'] = (show('both: bookmakers not our way AND the model above -1c', exit_mask=(fam30 >= 0) & ~(np.nan_to_num(P, nan=0.0) < -1.0), **kw0), dict(exit_mask=(fam30 >= 0) & ~(np.nan_to_num(P, nan=0.0) < -1.0)))
    show('NO exit order at all (everything to the result)', exit_any=False, **kw0)
    good = [k for k, ((r, h1, h2), _) in R.items() if h1 > h1b and h2 > h2b]
    kw1 = dict(kw0)
    if good:
        best = max(good, key=lambda k: min(R[k][0][1] - h1b, R[k][0][2] - h2b))
        kw1.update(R[best][1])
        say(f'   -> better than the base in both halves: {", ".join(good)}; taken: {best}')
    else:
        say('   -> nothing beats the base in both halves; the exit stays as it is')
    say('')
    say('2. SIZE (with the exit chosen above)')
    runs = {}
    for b in (10, 15, 20, 30):
        k_ = dict(kw1)
        k_['size_arr'] = two(b)
        k_['size'] = float(b)
        runs[b] = (show(f'{b} shares, doubled where the call is high', **k_), k_)
    say(f'   stress table, wallet after the 6 days from ${W0:.0f}: ' + ' / '.join(f'{b} shares' for b in runs))

    def stress(name, extra_mask=None, **more):
        vals = []
        for b, (_, k_) in runs.items():
            kk = {**k_, **more}
            if extra_mask is not None:
                kk['entry_mask'] = kk['entry_mask'] & extra_mask
            vals.append(S.run(w0=W0, **kk))
        say(f'      {name:<64} ' + ' / '.join(f'${v["final"]:>7.2f}' for v in vals) + '   lowest ' + ' / '.join(f'${v["low"]:.0f}' for v in vals))
    stress('as tested')
    stress('every fill half a cent worse', haircut=0.005)
    stress('every fill 1c worse', haircut=0.01)
    stress('2 s reaction', L=2)
    stress('2 s reaction and half a cent worse', L=2, haircut=0.005)
    stress('no part fills', whole_only=True)
    b2m = S.cfg(0, 2, 1) & S.live & np.isin(S.rowday, sc_days) & S.has & (S.spr >= 4.5) & (fam30 >= 0) & (P > 0)
    for frac in (0.1, 0.2):
        cut = np.quantile(S.k300[b2m & np.isfinite(S.k300)], 1 - frac)
        stress(f'faster bots take our best {int(frac * 100)}% of entry fills (hindsight)', extra_mask=~(b2m & (np.nan_to_num(S.k300, nan=-99) >= cut)))
    with open(os.path.join(a.signals, stem + 'makerexit.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
