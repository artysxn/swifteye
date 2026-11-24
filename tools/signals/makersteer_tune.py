"""What makes the steered resting orders better? (24 Nov, PC; owner: "how can we improve it?")

  python tools/signals/makersteer_tune.py --signals DIR [--name makersteer.npz]

Works on the fills of makersteer.py build (no new data). The fill model is trained again day by day (earlier days
only), then, in play, on the days it is scored:
  1 HOW PICKY: leave a side only where the model's call is above a line (-1c .. +2c): fills, cents a share, dollars.
  2 RULE + MODEL together, and each alone.
  3 STAKE BY THE CALL: more shares where the model is surer (never more than the real trade's size).
  4 WHICH SPREADS pay: by the spread when the quote was decided.
  5 A REAL BOOK OF POSITIONS: fills taken in time order with a cap on the shares held per game, buys and sells of the
    same game netted, what is left held to the result. Dollars a day, the money tied up, and the worst day.
  6 REACTION TIME and MORE DAYS: what one second costs, and how the model's result grows with the days it has seen.
Cents a share kept = our side's mark 5 min after the fill minus the fill price (share-weighted, [90% by game])."""
import argparse
import datetime as dt
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import makersteer as MS  # noqa: E402
import mine as MN  # noqa: E402

DAY = 86_400_000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--name', default='makersteer.npz')
    a = ap.parse_args()
    import torch
    import lab as LB
    sys.stdout.reconfigure(encoding='utf-8')
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    Z = np.load(os.path.join(a.signals, a.name))
    Fm, FX = Z['F'], Z['FX']
    ci, gm, tms, sg, q, w, m60, m300, won, spi, inplay, spr, cls, raw = (Fm[:, j] for j in range(14))
    ci = ci.astype(int)
    k300 = sg * (m300 - q) * 100
    kres = np.where(won >= 0, sg * (won - q) * 100, np.nan)
    fc = {k: i for i, k in enumerate(MS.FN)}
    has = np.isfinite(FX[:, 0])
    live = inplay > 0.5
    rng = np.random.default_rng(0)
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    rowday = np.array([dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in tms])
    days_all = sorted(set(rowday.tolist()))
    dev = MN.pick_device()
    tg = lambda v: torch.from_numpy(np.asarray(v)).to(dev)
    use = has & np.isfinite(k300) & live
    y = np.clip(np.nan_to_num(k300), -15, 15).astype(np.float32)
    Xb = tg(LB.binned(np.nan_to_num(FX), np.flatnonzero(use & (tms < d_ms(days_all[min(2, len(days_all) - 1)])))))
    P = np.full(len(Fm), np.nan)
    ntrain = {}
    for dday in days_all[1:]:
        d0 = d_ms(dday)
        tr_all = np.flatnonzero(use & (tms + 600_000 < d0))
        today = np.flatnonzero(use & (tms >= d0) & (tms < d0 + DAY))
        if len(tr_all) < 3000 or not len(today):
            continue
        tr_all = tr_all[np.argsort(tms[tr_all], kind='stable')]
        cut = int(0.8 * len(tr_all))
        va, tr = tr_all[cut:], tr_all[:cut]
        tr = tr[tms[tr] + 600_000 < tms[va].min()]
        m = LB.GB(loss='l2', depth=4, min_leaf=100, lam=50.0, row_frac=0.7, n_trees=200, lr=0.05).fit(Xb[tg(tr)], tg(y)[tg(tr)], Xb[tg(va)], tg(y)[tg(va)])
        P[today] = m.predict(Xb[tg(today)])
        ntrain[dday] = len(tr)
    sc_days = sorted(ntrain)
    scored = np.isfinite(P) & np.isin(rowday, sc_days)
    nd = len(sc_days)
    np.save(os.path.join(a.signals, a.name.replace('.npz', '_calls.npy')), P)

    def stat(v, g, ww):
        ok = np.isfinite(v)
        v, g, ww = v[ok], g[ok], ww[ok]
        if len(v) < 15:
            return f'{"-":>22}'
        ug, inv = np.unique(g, return_inverse=True)
        sums, cnts = np.bincount(inv, v * ww, len(ug)), np.bincount(inv, ww, len(ug))
        bs = [(lambda z: sums[z].sum() / max(cnts[z].sum(), 1e-9))(rng.integers(0, len(ug), len(ug))) for _ in range(400)]
        return f'{(v * ww).sum() / ww.sum():+5.2f} [{np.percentile(bs, 5):+.2f}..{np.percentile(bs, 95):+.2f}]'
    w100 = np.minimum(100, raw)
    usd = lambda mk, ww, v=k300: np.nansum(v[mk] * ww[mk]) / 100 / nd
    cfg = lambda calm, k, L: (ci == MS.CONFIGS.index((calm, k, L))) & scored
    V = lambda k1, k2=None: FX[:, fc[k1]] - (FX[:, fc[k2]] if k2 else 0)
    fam30, lead, sl60 = V('fam_up30', 'fam_dn30'), V('lead_up', 'lead_down'), V('sl_r60')
    say(f'IMPROVING THE STEERED RESTING ORDERS: {int(Z["games"][0])} games, in play, scored on {", ".join(x[8:] for x in sc_days)} Nov ({nd} days). Orders of 100 shares (a fill = min(100, the real trade)); $ a day = these games only.')
    say('')
    say('1. HOW PICKY should the model be? Leave a side in the book only where its call is above the line.')
    for nm, b in (('one cent inside any spread of 2c+, 1 s late', cfg(0, 2, 1)), ('inside 5c+, 1 s late', cfg(0, 5, 1)), ('inside 2c+, 2 s late', cfg(0, 2, 2)), ('inside 5c+, 2 s late', cfg(0, 5, 2))):
        say(f'   {nm}: plain {b.sum():,} fills {stat(k300[b], gm[b], w100[b])}, ${usd(b, w100):+,.0f} a day at 5 min, ${usd(b, w100, kres):+,.0f} held to the result')
        for t in (-1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0):
            r = b & (P > t)
            if r.sum() >= 50:
                say(f'      call above {t:+.1f}c   {r.sum():>6,} fills ({r.sum() / b.sum() * 100:3.0f}%)  {stat(k300[r], gm[r], w100[r])}   ${usd(r, w100):>+6,.0f} a day at 5 min   ${usd(r, w100, kres):>+6,.0f} held to the result')
    say('')
    say('2. RULE AND MODEL TOGETHER (one cent inside any spread of 2c+, and inside 5c+; 1 s late)')
    for nm, b in (('inside 2c+', cfg(0, 2, 1)), ('inside 5c+', cfg(0, 5, 1))):
        for rn, mk in (('plain', b), ('never against the bookmakers\' last 30 s', b & (fam30 >= 0)), ('never against a superline lead', b & (lead >= 0)), ('the model expects a gain', b & (P > 0)),
                       ('model AND never against the bookmakers', b & (P > 0) & (fam30 >= 0)), ('model AND never against bookmakers, superline lead, superline 60 s', b & (P > 0) & (fam30 >= 0) & (lead >= 0) & (sl60 >= -1e-9)),
                       ('model OR (with the bookmakers\' last 30 s)', b & ((P > 0) | (fam30 > 0)))):
            if mk.sum() >= 50:
                say(f'   {nm}, {rn:<66}{mk.sum():>6,} fills  {stat(k300[mk], gm[mk], w100[mk])}   ${usd(mk, w100):>+6,.0f} a day at 5 min   ${usd(mk, w100, kres):>+6,.0f} held to the result')
    say('')
    say('3. STAKE BY THE CALL (inside 2c+, 1 s late, model expects a gain): shares = 100 x the call in cents, between 25 and 300, never more than the real trade')
    b = cfg(0, 2, 1) & (P > 0)
    wz = np.minimum(raw, 100 * np.clip(np.nan_to_num(P) / 1.0, 0.25, 3.0))
    scale = w100[b].sum() / max(wz[b].sum(), 1e-9)
    say(f'   flat 100 shares: {stat(k300[b], gm[b], w100[b])}, ${usd(b, w100):+,.0f} a day at 5 min, ${usd(b, w100, kres):+,.0f} at the result, {w100[b].sum() / nd:,.0f} shares a day')
    say(f'   by the call:     {stat(k300[b], gm[b], wz[b])}, ${usd(b, wz):+,.0f} a day at 5 min, ${usd(b, wz, kres):+,.0f} at the result, {wz[b].sum() / nd:,.0f} shares a day; at the SAME number of shares ${usd(b, wz) * scale:+,.0f} / ${usd(b, wz, kres) * scale:+,.0f}')
    say('')
    say('4. WHICH SPREADS PAY (1 s late, any market; the spread when the quote was decided)')
    b0 = cfg(0, 2, 1)
    for lo, hi, nm in ((2, 2.5, '2c'), (2.5, 3.5, '3c'), (3.5, 4.5, '4c'), (4.5, 6.5, '5-6c'), (6.5, 9.5, '7-9c'), (9.5, 99, '10c+')):
        mm = b0 & (spr >= lo) & (spr < hi)
        r = mm & (P > 0)
        if mm.sum() >= 80:
            say(f'   spread {nm:<5} plain {mm.sum():>6,} fills {stat(k300[mm], gm[mm], w100[mm])} ${usd(mm, w100):>+5,.0f} a day | model keeps {r.sum():>5,} {stat(k300[r], gm[r], w100[r])} ${usd(r, w100):>+5,.0f} a day, ${usd(r, w100, kres):>+5,.0f} at the result')
    say('')
    say('5. A REAL BOOK OF POSITIONS: fills in time order, at most CAP shares held in a game (a fill that would go past it is skipped), buys and sells of a game netted, the rest held to the result.')
    say('   $ a day [90% by game] | share of days above zero | money tied up: the most at one time in a game (median / 90% of games), and the sum of the games\' peaks on the busiest day')
    for pn, base in (('inside 2c+, 1 s, model expects a gain', cfg(0, 2, 1) & (P > 0)), ('inside 5c+, 1 s, never against the bookmakers', cfg(0, 5, 1) & (fam30 >= 0)), ('inside 5c+, 1 s, model expects a gain', cfg(0, 5, 1) & (P > 0)),
                     ('inside 5c+, 2 s, model expects a gain', cfg(0, 5, 2) & (P > 0)), ('inside 5c+, 1 s, plain', cfg(0, 5, 1))):
        idx = np.flatnonzero(base & (won >= 0))
        idx = idx[np.lexsort((tms[idx], gm[idx]))]
        for cap in (100, 300, 1000):
            pnl, peak, gday, taken = {}, {}, {}, 0
            g_prev, pos, cash, pk = None, 0.0, 0.0, 0.0
            for i in idx:
                g_ = int(gm[i])
                if g_ != g_prev:
                    if g_prev is not None:
                        pnl[g_prev], peak[g_prev] = cash + pos * wn, pk
                    g_prev, pos, cash, pk, wn = g_, 0.0, 0.0, 0.0, float(won[i])
                    gday[g_] = rowday[i]
                s_ = min(100.0, float(raw[i]))
                np_ = pos + sg[i] * s_
                if abs(np_) > cap + 1e-9:
                    continue
                pos = np_
                cash -= sg[i] * s_ * q[i]
                taken += 1
                pk = max(pk, pos * q[i] if pos > 0 else -pos * (1 - q[i]))
            if g_prev is not None:
                pnl[g_prev], peak[g_prev] = cash + pos * wn, pk
            gs = np.array(sorted(pnl))
            pv, kv = np.array([pnl[g_] for g_ in gs]), np.array([peak[g_] for g_ in gs])
            dv = np.array([gday[g_] for g_ in gs])
            bs = [pv[rng.integers(0, len(pv), len(pv))].sum() / nd for _ in range(1000)]
            per_day = np.array([pv[dv == x].sum() for x in sc_days])
            busiest = max(kv[dv == x].sum() for x in sc_days)
            say(f'   {pn:<46} cap {cap:>5}: {taken:>6,} of {len(idx):,} fills, ${pv.sum() / nd:>+6,.0f} a day [{np.percentile(bs, 5):+,.0f}..{np.percentile(bs, 95):+,.0f}] | {np.mean(per_day > 0) * 100:3.0f}% of days, worst day ${per_day.min():+,.0f} | '
                f'${np.median(kv):,.0f} / ${np.quantile(kv, 0.9):,.0f} a game, ${busiest:,.0f} on the busiest day | {pv.sum() / nd / max(busiest, 1) * 100:.1f}% a day on that money')
    say('')
    say('6. REACTION TIME AND MORE DAYS')
    for k in (2, 3, 5):
        a1, a2 = cfg(0, k, 1), cfg(0, k, 2)
        say(f'   inside {k}c+: plain 1 s {stat(k300[a1], gm[a1], w100[a1])}, 2 s {stat(k300[a2], gm[a2], w100[a2])} | model 1 s {stat(k300[a1 & (P > 0)], gm[a1 & (P > 0)], w100[a1 & (P > 0)])}, 2 s {stat(k300[a2 & (P > 0)], gm[a2 & (P > 0)], w100[a2 & (P > 0)])}')
    b = cfg(0, 2, 1)
    say('   the model by the day it is scored on (fills it had seen before | what its kept fills made, inside 2c+, 1 s | plain that day): ' + '; '.join(
        f'{x[8:]} Nov {ntrain[x]:,} | {stat(k300[b & (P > 0) & (rowday == x)], gm[b & (P > 0) & (rowday == x)], w100[b & (P > 0) & (rowday == x)])} | {stat(k300[b & (rowday == x)], gm[b & (rowday == x)], w100[b & (rowday == x)])}' for x in sc_days))
    imp_names = ('fam_up30', 'fam_dn30', 'lead_up', 'lead_down', 'gap_e', 'sl_r60', 'pm_r60', 'q_spread', 'q_price', 'q_live_min')
    say('   rank correlation of single inputs with what a fill kept (scored fills): ' + ', '.join(
        f'{nm} {np.corrcoef(np.argsort(np.argsort(FX[scored, fc[nm]])), np.argsort(np.argsort(k300[scored])))[0, 1]:+.3f}' for nm in imp_names) + f'; the model {np.corrcoef(np.argsort(np.argsort(P[scored])), np.argsort(np.argsort(k300[scored])))[0, 1]:+.3f}')
    with open(os.path.join(a.signals, a.name.replace('.npz', '_tune.txt')), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
