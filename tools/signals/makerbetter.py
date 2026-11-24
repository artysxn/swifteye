"""Can rule + model be made better? (24 Nov, PC; owner: "Rule + Model is better, nice. Ok, can we improve it further?
Maybe something to do with confidence scaling")

  python tools/signals/makerbetter.py --signals DIR [--wallet 175]

Needs `makersteer.py build --name makersteer_flow.npz` (the fills with the real order-flow inputs). Everything below is
the $175 wallet replay of makerlib.py from the same start: spreads of 5c+, never against the bookmakers' last 30 s, the
fill model must expect a gain, 10 shares an order, the exit order always resting ("the base"). Each idea changes ONE
thing; next to the wallet it prints both halves of the week (each from a fresh wallet) and what it adds to the base in
dollars with a 90% range resampled by game. Anything that needs a number (a line, a scale) takes it from EARLIER days.
 1 THE MODEL   the same trees with the real order flow as extra inputs; five seeds averaged; both.
 2 HOW SURE    confidence scaling: more shares where the model's call is higher (several shapes, and upside down as a
               check), next to flat orders of the same total size.
 3 THE LINE    how high the call must be; a lower line in wide spreads / a higher one in narrow ones.
 4 STUCK       a time stop: a position still open after T minutes is sold into the bid (taker, with the fee).
 5 WHERE       sports that lost on earlier days are switched off (walk-forward).
 6 TOGETHER    the ones that helped in both halves, combined, and the stress table of makeraudit.py on that."""
import argparse
import datetime as dt
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import makerlib as ML  # noqa: E402
import makersteer as MS  # noqa: E402
import mine as MN  # noqa: E402

DAY = 86_400_000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--wallet', type=float, default=175.0)
    ap.add_argument('--name', default='makersteer_flow.npz')
    ap.add_argument('--model', default='', help='use this model variant for sections 2-6 instead of the one picked by the halves rule, e.g. "five seeds averaged"')
    a = ap.parse_args()
    import torch
    import lab as LB
    sys.stdout.reconfigure(encoding='utf-8')
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    S = ML.Sim(a.signals, a.name)
    stem = '' if a.name == 'makersteer_flow.npz' else a.name.replace('.npz', '') + '_'
    FX, tms, rowday, k300, raw, spr = S.FX, S.tms, S.rowday, S.k300, S.raw, S.spr
    fc = S.fc
    rng = np.random.default_rng(0)
    W0 = a.wallet
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    dev = MN.pick_device()
    tg = lambda v: torch.from_numpy(np.asarray(v)).to(dev)
    # ---------------- models, day by day
    use = S.has & np.isfinite(k300) & S.live
    y = np.clip(np.nan_to_num(k300), -15, 15).astype(np.float32)
    Xb = tg(LB.binned(np.nan_to_num(FX), np.flatnonzero(use & (tms < d_ms(S.days_all[min(2, len(S.days_all) - 1)])))))
    old_cols = torch.tensor([i for i, k in enumerate(MS.FN) if k not in MS.FLOW], device=dev)
    gb = dict(loss='l2', depth=4, min_leaf=100, lam=50.0, row_frac=0.7, n_trees=200, lr=0.05)
    variants = {'the model as it was': (old_cols, 1), 'five seeds averaged': (old_cols, 5), '+ real order flow': (None, 1), '+ real order flow, five seeds': (None, 5)}
    PV = {k: np.full(len(S.Fm), np.nan) for k in variants}
    t0 = time.time()
    for dday in S.days_all[1:]:
        d0 = d_ms(dday)
        tr_all = np.flatnonzero(use & (tms + 600_000 < d0))
        today = np.flatnonzero(use & (tms >= d0) & (tms < d0 + DAY))
        if len(tr_all) < 3000 or not len(today):
            continue
        tr_all = tr_all[np.argsort(tms[tr_all], kind='stable')]
        cut = int(0.8 * len(tr_all))
        va, tr = tr_all[cut:], tr_all[:cut]
        tr = tr[tms[tr] + 600_000 < tms[va].min()]
        for name, (cols, ns) in variants.items():
            pt = np.zeros(len(today))
            for sd_ in range(ns):
                m = LB.GB(**{**gb, 'seed': sd_}).fit(Xb[tg(tr)], tg(y)[tg(tr)], Xb[tg(va)], tg(y)[tg(va)], cols=cols)
                pt += m.predict(Xb[tg(today)]) / ns
            PV[name][today] = pt
    sc_days = sorted({d_ for d_ in S.days_all if np.isfinite(PV['the model as it was'][rowday == d_]).any()})
    S.set_days(sc_days)
    nd = len(sc_days)
    d1, d2 = [x for x in sc_days if x <= '2026-11-20'], [x for x in sc_days if x >= '2026-11-21']
    print(f'models trained ({time.time() - t0:.0f} s)', flush=True)
    fam30 = S.col('fam_up30', 'fam_dn30')
    P = PV['the model as it was']
    base = S.run(entry_mask=P > 0, w0=W0)

    def show(name, ref=None, **kw):
        r = S.run(w0=W0, **kw)
        h1, h2 = S.run(w0=W0, days=d1, **kw)['final'], S.run(w0=W0, days=d2, **kw)['final']
        cl = r['closed']
        dsum, lo, hi = ML.paired(ref or base, r, rng)
        say(f'   {name:<62} ${W0:.0f} -> ${r["final"]:>7.2f} | halves ${h1:>6.2f} / ${h2:>6.2f} | {len(cl):>5,} trades, won {np.mean(cl[:, 0] > 1e-9) * 100:2.0f}%, {cl[:, 1].sum():>6,.0f} shares, {cl[:, 0].sum() / max(cl[:, 1].sum(), 1) * 100:+.2f}c a share | '
            f'vs the base ${dsum:>+7.2f} [{lo:+.0f}..{hi:+.0f}] | lowest ${r["low"]:.0f}')
        return r, h1, h2
    say(f'IMPROVING RULE + MODEL. {S.NG} games, in play, {", ".join(x[8:] for x in sc_days)} Nov. THE BASE: spreads of 5c+, never against the bookmakers\' last 30 s, the model must expect a gain, 10 shares an order, the exit order always resting, ${W0:.0f}.')
    say('Each line: the wallet after the 6 days | each half from a fresh wallet | trades | what it adds to the base, resampled by game.')
    say('')
    say('1. THE MODEL (same trees, other inputs / more seeds)')
    e5 = S.cfg(0, 5, 1) & S.live & np.isin(rowday, sc_days) & S.has
    w10 = np.minimum(10, raw)
    res = {}
    for name in variants:
        keep = e5 & (PV[name] > 0)
        rc = np.corrcoef(np.argsort(np.argsort(PV[name][e5])), np.argsort(np.argsort(k300[e5])))[0, 1]
        say(f'   {name}: of the {e5.sum():,} fills inside 5c+ it keeps {keep.sum():,}, {np.average(k300[keep], weights=w10[keep]):+.2f}c a share after 5 min (rank correlation with what a fill kept {rc:+.3f})')
        res[name] = show(f'   wallet with {name}', entry_mask=PV[name] > 0)
    best_m = max(variants, key=lambda k: min(res[k][1], res[k][2]))
    both_better = [k for k in variants if res[k][1] >= res['the model as it was'][1] and res[k][2] >= res['the model as it was'][2] and k != 'the model as it was']
    use_m = max(both_better, key=lambda k: min(res[k][1], res[k][2])) if both_better else 'the model as it was'
    if a.model:
        use_m = a.model          # the model the live bot runs (tools/maker/export_model.py): evaluate exactly that one
    P = PV[use_m]
    say(f'   -> better than the model as it was in BOTH halves: {", ".join(both_better) if both_better else "none"}. The rest of this report uses "{use_m}".')
    base = S.run(entry_mask=P > 0, w0=W0)
    # the scale of a call, from earlier days
    c0 = np.ones(len(S.Fm))
    elig = S.cfg(0, 2, 1) & S.live & (spr >= 4.5) & (fam30 >= 0) & (P > 0)
    for d_ in sc_days:
        prev = elig & (rowday < d_)
        if prev.sum() >= 100:
            c0[rowday == d_] = np.median(P[prev])
    say('')
    say(f'2. HOW SURE: confidence scaling. Shares = 10 x (the call / the usual call of earlier days), within limits, never under 5. The usual call is {np.median(c0[np.isin(rowday, sc_days)]):.2f}c.')
    rel = np.nan_to_num(P) / c0
    shapes = (('0.5x to 2x', np.clip(rel, 0.5, 2.0)), ('0.5x to 3x', np.clip(rel, 0.5, 3.0)), ('1x to 3x (never smaller)', np.clip(rel, 1.0, 3.0)), ('two steps: 2x above the usual call, else 1x', np.where(rel >= 1, 2.0, 1.0)),
              ('square root, 0.5x to 2x', np.clip(np.sqrt(np.maximum(rel, 0)), 0.5, 2.0)), ('UPSIDE DOWN (more where it is less sure): a check', np.clip(1 / np.maximum(rel, 1e-6), 0.5, 2.0)))
    sized = {}
    for nm, mult in shapes:
        sized[nm] = show(f'by the call, {nm}', entry_mask=P > 0, size_arr=np.maximum(5, np.round(10 * mult)))
    for fl in (12, 15, 20):
        show(f'flat {fl} shares (for the same total size)', entry_mask=P > 0, size=float(fl))
    say('')
    say('3. THE LINE the call must clear')
    for t in (-0.5, -0.25, 0.25, 0.5, 1.0):
        show(f'call above {t:+.2f}c', entry_mask=P > t)
    q75 = np.zeros(len(S.Fm))
    for d_ in sc_days:
        prev = elig & (rowday < d_)
        q75[rowday == d_] = np.quantile(P[prev], 0.75) if prev.sum() >= 100 else 1.5
    show('5c+ as the base, plus 3-4c spreads when the call is in its top quarter', minsp=3, entry_mask=((spr >= 4.5) & (P > 0)) | ((spr < 4.5) & (P > q75)))
    show('line 0 in spreads of 7c+, top-half calls in 5-6c', entry_mask=((spr >= 6.5) & (P > 0)) | ((spr < 6.5) & (P > c0)))
    show('no rule, the model alone', rule='none', entry_mask=P > 0)
    say('')
    say('4. STUCK POSITIONS: a time stop (sold into the bid as a taker T minutes after the fill, fee paid)')
    stops = {}
    for T in (2, 5, 10, 20, 40, 90):
        stops[T] = show(f'time stop after {T} min', entry_mask=P > 0, stop_min=T)
    say('')
    say('5. WHERE: sports that lost on the earlier days are switched off (decided each day from the base\'s own closed trades before it; a sport needs 30 trades to be judged)')
    cl = base['closed']
    cday = np.array([dt.datetime.fromtimestamp(int(t), dt.timezone.utc).strftime('%Y-%m-%d') for t in cl[:, 5]])
    csp = S.ssport[cl[:, 4].astype(int)]
    ok_sport = np.ones(len(S.Fm), bool)
    notes = []
    for d_ in sc_days:
        off = [j for j in range(len(S.sports)) if ((cday < d_) & (csp == j)).sum() >= 30 and cl[(cday < d_) & (csp == j), 0].sum() < 0]
        if off:
            ok_sport[(rowday == d_) & np.isin(S.spi, off)] = False
            notes.append(f'{d_[8:]} Nov: {", ".join(S.sports[j] for j in off)}')
    sp_r = show('losing sports switched off', entry_mask=(P > 0) & ok_sport)
    say('      switched off: ' + ('; '.join(notes) if notes else 'nothing'))
    say('      the base by sport (trades, won, $): ' + '; '.join(f'{S.sports[j]} {(csp == j).sum()}, {np.mean(cl[csp == j, 0] > 1e-9) * 100:.0f}%, ${cl[csp == j, 0].sum():+.2f}' for j in range(len(S.sports)) if (csp == j).sum() >= 20))
    # ---------------- 6: together
    say('')
    say('6. TOGETHER: only what beat the base in BOTH halves')
    b1, b2 = S.run(w0=W0, days=d1, entry_mask=P > 0)['final'], S.run(w0=W0, days=d2, entry_mask=P > 0)['final']
    say(f'   the base: ${base["final"]:.2f}, halves ${b1:.2f} / ${b2:.2f}')
    kw = dict(entry_mask=P > 0)
    picks = []
    good = [nm for nm, _ in shapes[:5] if sized[nm][1] > b1 and sized[nm][2] > b2]
    if good:
        nm = max(good, key=lambda k: min(sized[k][1] - b1, sized[k][2] - b2))
        kw['size_arr'] = np.maximum(5, np.round(10 * dict(shapes)[nm]))
        picks.append(f'confidence scaling "{nm}"')
    gst = [T for T in stops if stops[T][1] > b1 and stops[T][2] > b2]
    if gst:
        T = max(gst, key=lambda k: min(stops[k][1] - b1, stops[k][2] - b2))
        kw['stop_min'] = T
        picks.append(f'time stop {T} min')
    if sp_r[1] > b1 and sp_r[2] > b2:
        kw['entry_mask'] = (P > 0) & ok_sport
        picks.append('losing sports off')
    say('   chosen: ' + (', '.join(picks) if picks else 'nothing beat the base in both halves'))
    comb = show('THE COMBINATION', **kw)[0]
    say(f'   stress table (wallet after {nd} days from ${W0:.0f}: the base / the combination):')

    def stress(name, **more):
        ra, rb = S.run(w0=W0, entry_mask=P > 0, **more), S.run(w0=W0, **{**kw, **more})
        say(f'      {name:<70} ${ra["final"]:>7.2f} / ${rb["final"]:>7.2f}')
    stress('as tested')
    stress('every fill half a cent worse', haircut=0.005)
    stress('every fill 1c worse', haircut=0.01)
    stress('2 s reaction', L=2)
    stress('2 s reaction and half a cent worse', L=2, haircut=0.005)
    stress('bookmaker signals 6 s stale', flag_lag=6)
    stress('no part fills', whole_only=True)
    b2m = S.cfg(0, 2, 1) & S.live & np.isin(rowday, sc_days) & S.has & (spr >= 4.5) & (fam30 >= 0) & (P > 0)
    for frac in (0.1, 0.2):
        cut = np.quantile(k300[b2m & np.isfinite(k300)], 1 - frac)
        km = ~(b2m & (np.nan_to_num(k300, nan=-99) >= cut))
        ra = S.run(w0=W0, entry_mask=(P > 0) & km)
        rb = S.run(w0=W0, **{**kw, 'entry_mask': kw['entry_mask'] & km})
        say(f'      {"faster bots take our best " + str(int(frac * 100)) + "% of entry fills (hindsight)":<70} ${ra["final"]:>7.2f} / ${rb["final"]:>7.2f}')
    fa, fb = [], []
    for sd_ in range(10):
        km = np.random.default_rng(300 + sd_).random(len(S.Fm)) < 0.5
        fa.append(S.run(w0=W0, entry_mask=(P > 0) & km)['final'])
        fb.append(S.run(w0=W0, **{**kw, 'entry_mask': kw['entry_mask'] & km})['final'])
    say(f'      {"a random half of the entry fills (10 draws, median)":<70} ${np.median(fa):>7.2f} / ${np.median(fb):>7.2f}')
    np.savez(os.path.join(a.signals, stem + 'makerbetter_calls.npz'), P=P, c0=c0, model=np.array(use_m))
    with open(os.path.join(a.signals, stem + 'makerbetter.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
