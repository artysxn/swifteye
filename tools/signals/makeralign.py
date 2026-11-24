"""How well is each simulated fill's moment pinned down? (24 Nov, PC; owner: "is this backtest 100% accurate?")

  python tools/signals/makeralign.py --signals DIR [--wallet 175]

The one hindsight step inside the resting-order test: Polymarket's public trades carry a 1-second stamp that runs a
few seconds late, and makersim2.align() places each trade on the second within [stamp - 5, stamp] whose best price
(the bid for a taker's sale, the ask for a buy) is nearest the trade's price. Every fill of makersteer_flow.npz is
looked up again and put in one of three classes:
  CLEAN      the best price equals the trade's price and did not change anywhere in those six seconds: the quote we
             would have had is the same whichever second the trade really happened in;
  PLACED     the best price changed inside the window; the trade sits on a second where it matches exactly (the likely
             one, not a certain one);
  NO MATCH   no second in the window has the trade's price at the best quote (the trade printed inside the spread or
             our recording missed a book change): the least certain.
Then: what each class keeps per share, and the final setting's wallet replay when only CLEAN fills count as entries,
and when only CLEAN fills count at all (entries and exits). Needs makerbetter.py's calls.
WAS SOMEONE ALREADY INSIDE THE SPREAD? The test assumes our order, one cent better than the best quote we recorded, is
first in line. The real trade's own price says something about that: a taker's sale that printed AT the recorded bid
(or below it) met nobody better than that bid, so our better bid would have been taken first; a sale that printed
ABOVE the recorded bid was taken by someone already quoting inside the spread (our one-second snapshot did not show
them), and at our price or better we would at most have shared the queue behind them. Fills are split that way."""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import makerlib as ML  # noqa: E402
import paths as PT  # noqa: E402
from makersim2 import align  # noqa: E402


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
    M, A = PT.load(a.signals)
    idx = {int(M['match'][i]): i for i in range(len(M['key'])) if M['k'][i] == 0}
    cat = np.full(len(S.Fm), -1, np.int8)
    dlt = np.full(len(S.Fm), np.nan)
    ins = np.full(len(S.Fm), np.nan)                  # cents the real trade printed INSIDE the best quote we had recorded (<= 0: at it or through it)
    order = np.argsort(S.gm, kind='stable')
    gms = S.gm[order]
    cuts = np.r_[0, np.flatnonzero(gms[1:] != gms[:-1]) + 1, len(gms)]
    for a_, b_ in zip(cuts[:-1], cuts[1:]):
        rows = order[a_:b_]
        m = int(gms[a_])
        i = idx[m]
        o, n, t0s = int(M['off'][i]), int(M['n'][i]), int(M['t0'][i]) // 1000
        bid, ask, mid, fresh, mark = PT.book(A, o, n, float(M['won'][i]), maxage=15.0)
        g = json.load(open(os.path.join(a.signals, 'pm_trades', f'{m}.json')))
        tr = np.array([(t[0], t[1], t[2], t[3], 1 if (t[4] or '').upper() == 'BUY' else -1) for t in g['trades']], float)
        ts = tr[:, 0].astype(np.int64)
        keep = (ts >= t0s + 40) & (ts < t0s + n - 2)
        tr, ts = tr[keep], ts[keep]
        home = tr[:, 1] > 0.5
        side = np.where(home, tr[:, 4], -tr[:, 4]).astype(np.int64)
        p = np.where(home, tr[:, 2], 1 - tr[:, 2])
        od = np.argsort(ts, kind='stable')
        ts, side, p = ts[od], side[od], p[od]
        u = align(ts, side, p, bid, ask, t0s, n)
        okb = fresh[u] & fresh[u - 1]
        ts, side, p, u = ts[okb], side[okb], p[okb], u[okb]
        by = {}
        for j in range(len(u)):
            by.setdefault((int(u[j]), int(side[j])), []).append(j)
        for r in rows:
            k, sgn, q = int(S.tms[r] // 1000) - t0s, int(S.sg[r]), S.q[r]
            js = [j for j in by.get((k, -sgn), []) if sgn * (q - p[j]) >= -1e-9]
            if not js:
                continue
            j = js[0]
            touch = []
            for c in range(6):
                kk = ts[j] - c - t0s
                if 1 <= kk < n:
                    touch.append(bid[kk - 1] if side[j] < 0 else ask[kk - 1])
            touch = np.array(touch, float)
            d_ = abs((bid[k - 1] if side[j] < 0 else ask[k - 1]) - p[j]) * 100
            dlt[r] = d_
            ins[r] = ((p[j] - bid[k - 1]) if side[j] < 0 else (ask[k - 1] - p[j])) * 100
            const = np.isfinite(touch).all() and (touch.max() - touch.min() < 5e-4)        # prices are stored as 32-bit floats: compare within a twentieth of a cent
            cat[r] = 0 if (const and d_ < 0.05) else (1 if d_ < 0.05 else 2)
    np.savez(os.path.join(a.signals, stem + 'makeralign.npz'), cat=cat, dlt=dlt, ins=ins)
    C = np.load(os.path.join(a.signals, stem + 'makerbetter_calls.npz'))
    P, c0 = C['P'], C['c0']
    sc_days = sorted({d_ for d_ in S.days_all if np.isfinite(P[S.rowday == d_]).any()})
    S.set_days(sc_days)
    W0 = a.wallet
    fam30 = S.col('fam_up30', 'fam_dn30')
    rel = np.nan_to_num(P) / c0
    kw = dict(entry_mask=P > 0, size_arr=np.maximum(5, np.round(10 * np.where(rel >= 1, 2.0, 1.0))), exit_mask=fam30 >= 0)
    b2 = S.cfg(0, 2, 1) & S.live & np.isin(S.rowday, sc_days) & S.has
    ent = b2 & (S.spr >= 4.5)
    names = ('CLEAN (price matched, book still for 6 s)', 'PLACED (price matched, book moved in the window)', 'NO MATCH (price not at the best quote)')
    w10 = np.minimum(10, S.raw)
    rng = np.random.default_rng(0)

    def stat(mk):
        ok = mk & np.isfinite(S.k300)
        if ok.sum() < 20:
            return f'{"-":>24}'
        v, ww, g_ = S.k300[ok], w10[ok], S.gm[ok]
        ug, inv = np.unique(g_, return_inverse=True)
        sums, cnts = np.bincount(inv, v * ww, len(ug)), np.bincount(inv, ww, len(ug))
        bs = [(lambda z: sums[z].sum() / max(cnts[z].sum(), 1e-9))(rng.integers(0, len(ug), len(ug))) for _ in range(400)]
        return f'{np.average(v, weights=ww):+5.2f} [{np.percentile(bs, 5):+.2f}..{np.percentile(bs, 95):+.2f}]'
    say(f'HOW WELL IS EACH FILL\'S MOMENT PINNED DOWN? {len(S.Fm):,} simulated fills; a class was found for {np.mean(cat >= 0) * 100:.1f}% of them.')
    say('   all fills of the setting the wallet replays (one cent inside, 1 s late, in play): ' + '; '.join(f'{names[c].split(" (")[0]} {np.mean(cat[b2] == c) * 100:.0f}%' for c in range(3)))
    say('   entry fills (spreads of 5c+): ' + '; '.join(f'{names[c].split(" (")[0]} {np.mean(cat[ent] == c) * 100:.0f}%' for c in range(3)) + f'; where there is no match the best quote is a median {np.nanmedian(dlt[ent & (cat == 2)]):.1f}c from the trade price (a quarter of them {np.nanquantile(dlt[ent & (cat == 2)], 0.75):.1f}c+)')
    say('')
    say('   cents a share kept after 5 min [90% by game], 10-share orders, spreads of 5c+, 1 s:')
    say(f'   {"":<52}{"every fill":>32}{"rule allows":>34}{"rule + model":>34}')
    for c in range(3):
        mk = ent & (cat == c)
        say(f'   {names[c]:<52}{mk.sum():>6,} {stat(mk)}   {(mk & (fam30 >= 0)).sum():>6,} {stat(mk & (fam30 >= 0))}   {(mk & (fam30 >= 0) & (P > 0)).sum():>6,} {stat(mk & (fam30 >= 0) & (P > 0))}')
    say('')
    say(f'   the final setting (five-seed model, 10 / 20 shares, the smarter exit), ${W0:.0f} wallet, {len(sc_days)} days:')
    for nm, kk in (('every fill counts (as reported)', kw),
                   ('only CLEAN fills can be entries (exits as before)', {**kw, 'entry_mask': kw['entry_mask'] & (cat == 0)}),
                   ('only CLEAN fills count at all (entries and exits)', {**kw, 'entry_mask': kw['entry_mask'] & (cat == 0), 'exit_mask': kw['exit_mask'] & (cat == 0)}),
                   ('CLEAN or PLACED (no "no match" fills anywhere)', {**kw, 'entry_mask': kw['entry_mask'] & (cat <= 1) & (cat >= 0), 'exit_mask': kw['exit_mask'] & (cat <= 1) & (cat >= 0)}),
                   ('only PLACED and NO MATCH fills as entries (the uncertain ones alone)', {**kw, 'entry_mask': kw['entry_mask'] & (cat >= 1)})):
        r = S.run(w0=W0, **kk)
        cl = r['closed']
        say(f'      {nm:<70} ${W0:.0f} -> ${r["final"]:>7.2f} | {len(cl):>5,} trades, won {np.mean(cl[:, 0] > 1e-9) * 100 if len(cl) else 0:2.0f}%, {cl[:, 0].sum() / max(cl[:, 1].sum(), 1) * 100:+.2f}c a share | lowest ${r["low"]:.0f}')
    say('')
    at, part, full = ins <= 0.05, (ins > 0.05) & (ins < 0.95), ins >= 0.95
    say('WAS SOMEONE ALREADY INSIDE THE SPREAD when the real trade happened? (entry fills, spreads of 5c+, 1 s)')
    say(f'   the trade printed AT or THROUGH the best quote we had recorded: {np.mean(at[ent]) * 100:.0f}% of entry fills (our better order would have been first); '
        f'between it and our price (a sub-cent print): {np.mean(part[ent]) * 100:.0f}%; AT OUR PRICE, one cent inside: {np.mean(full[ent]) * 100:.0f}% (someone was already quoting there: we would have been behind them)')
    say(f'   {"":<52}{"every fill":>32}{"rule allows":>34}{"rule + model":>34}')
    for nm, g_ in (('at or through the recorded quote', at), ('a sub-cent print inside it', part), ('at our price: someone already there', full)):
        mk = ent & g_
        say(f'   {nm:<52}{mk.sum():>6,} {stat(mk)}   {(mk & (fam30 >= 0)).sum():>6,} {stat(mk & (fam30 >= 0))}   {(mk & (fam30 >= 0) & (P > 0)).sum():>6,} {stat(mk & (fam30 >= 0) & (P > 0))}')
    say(f'   the final setting, ${W0:.0f} wallet, {len(sc_days)} days:')
    for nm, kk in (('every fill counts (as reported)', kw),
                   ('fills where someone was already at our price do NOT count (entries and exits)', {**kw, 'entry_mask': kw['entry_mask'] & ~full, 'exit_mask': kw['exit_mask'] & ~full}),
                   ('only trades AT or THROUGH the recorded quote count (entries and exits)', {**kw, 'entry_mask': kw['entry_mask'] & at, 'exit_mask': kw['exit_mask'] & at}),
                   ('the same, entries only (exits as reported)', {**kw, 'entry_mask': kw['entry_mask'] & at}),
                   ('the same, and every fill half a cent worse', {**kw, 'entry_mask': kw['entry_mask'] & at, 'exit_mask': kw['exit_mask'] & at, 'haircut': 0.005}),
                   ('the same, at 2 s reaction', {**kw, 'entry_mask': kw['entry_mask'] & at, 'exit_mask': kw['exit_mask'] & at, 'L': 2})):
        r = S.run(w0=W0, **kk)
        cl = r['closed']
        h1 = S.run(w0=W0, days=[x for x in sc_days if x <= '2026-11-20'], **kk)['final']
        h2 = S.run(w0=W0, days=[x for x in sc_days if x >= '2026-11-21'], **kk)['final']
        say(f'      {nm:<80} ${W0:.0f} -> ${r["final"]:>7.2f} | halves ${h1:.2f} / ${h2:.2f} | {len(cl):>5,} trades, won {np.mean(cl[:, 0] > 1e-9) * 100 if len(cl) else 0:2.0f}%, {cl[:, 0].sum() / max(cl[:, 1].sum(), 1) * 100:+.2f}c a share | lowest ${r["low"]:.0f}')
    with open(os.path.join(a.signals, stem + 'makeralign.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
