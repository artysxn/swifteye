"""The steered resting orders with a small wallet, and what tuning is worth (24 Nov, PC; owner: "is there any code,
model improvement, or settings fine tuning that will make it work better? Assume a 175 dollar wallet").

  python tools/signals/makerwallet.py --signals DIR [--wallet 175] [--skip-models]

Needs makersteer.py build (the fills, and makersteer_states.npz = what a bot saw every in-play second of every game).

A. MODEL VARIANTS, each trained day by day on earlier days only and scored on the same fills: other targets (1 min,
   1 + 5 min), trained on the wide-spread / fast fills only, a short list of inputs, bigger trees, five seeds averaged,
   a yes / no target, a plain linear model. Compared on the fills inside spreads of 5c+ at 1 s: each keeps the same
   share of fills (its line comes from earlier days), cents a share and dollars, and the difference to the first model
   resampled by game.
B. A WALLET. An order resting in the book locks its money: a buy of S shares at q locks S x q, an offer locks
   S x (1 - q) unless we hold the shares. So a small wallet can only quote some of the games that qualify at a moment.
   Every fill is replayed in time order across all games: at that second every game that qualifies is listed (state one
   second earlier: book alive, price 10-90c, spread wide enough, the rule allows the side), the widest spreads get the
   money first, and the fill only happens if its order got money. A buy and a later sale in a game net out; what is
   left is held to the result and paid then (the money is back at the game's last quote). No fees (resting orders pay
   none). Reported: the wallet at the end, fills taken and missed for lack of money, the lowest point.
   GETTING OUT ("exit" mode): while we hold shares in a game, the order that would reduce the position always rests,
   one cent inside whatever the spread is (2c+), whatever the rule says, and costs no money (the shares back it). It
   fills on the same kind of real trade. Without it everything that is not netted by chance rides the result.
C. SETTINGS: spread 5 / 6 / 7 / 8c+, the rule, the order size (fixed shares or a share of the wallet), the cap per game,
   with or without the model. Chosen on 18-20 Nov, then run unchanged on 21-23 Nov from a fresh wallet (and the other
   way round), next to the plain default and to the best in hindsight."""
import argparse
import datetime as dt
import heapq
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import makersteer as MS  # noqa: E402
import mine as MN  # noqa: E402

DAY = 86_400_000
COMPACT = ['fam_up5', 'fam_dn5', 'fam_up30', 'fam_dn30', 'fam_n', 'fam_gap_mean', 'fam_gap_max', 'fam_gap_min', 'fam_r10_max', 'fam_r10_min', 'lead_up', 'lead_down', 'lead_pm', 'lead_age', 'gap_e', 'gap_z',
           'sl_r10', 'sl_r60', 'sl_r180', 'sl_wave', 'sl_U', 'sl_D', 'pm_r10', 'pm_r60', 'pm_r180', 'pm_U', 'pm_D', 'pm_wave', 'pm_quiet', 'spread', 'absx', 'ctx_pm_rate', 'ctx_book_rate', 'ctx_spread', 'ctx_vol',
           'sport_tennis', 'sport_basketball', 'sport_esports', 'sport_hockey', 'pm_prev1', 'sl_prev1', 'since_sl_alarm', 'since_pm_end', 'q_spread', 'q_price', 'q_live_min', 'q_k', 'q_L', 'q_calm']
RULES = ('none', 'fam30', 'fam30+lead', 'with30')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--wallet', type=float, default=175.0)
    ap.add_argument('--skip-models', action='store_true')
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    Z = np.load(os.path.join(a.signals, 'makersteer.npz'))
    Fm, FX = Z['F'], Z['FX']
    ci, gm, tms, sg, q, w, m60, m300, won, spi, inplay, spr, cls, raw = (Fm[:, j] for j in range(14))
    ci = ci.astype(int)
    k60, k300 = sg * (m60 - q) * 100, sg * (m300 - q) * 100
    kres = np.where(won >= 0, sg * (won - q) * 100, np.nan)
    fc = {k: i for i, k in enumerate(MS.FN)}
    has, live = np.isfinite(FX[:, 0]), inplay > 0.5
    rng = np.random.default_rng(0)
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    rowday = np.array([dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in tms])
    days_all = sorted(set(rowday.tolist()))
    w100 = np.minimum(100, raw)
    E5 = (ci == MS.CONFIGS.index((0, 5, 1))) & live & has
    calls_path = os.path.join(a.signals, 'makerwallet_calls.npz')

    def stat(v, g, ww):
        ok = np.isfinite(v)
        v, g, ww = v[ok], g[ok], ww[ok]
        if len(v) < 15:
            return f'{"-":>22}'
        ug, inv = np.unique(g, return_inverse=True)
        sums, cnts = np.bincount(inv, v * ww, len(ug)), np.bincount(inv, ww, len(ug))
        bs = [(lambda z: sums[z].sum() / max(cnts[z].sum(), 1e-9))(rng.integers(0, len(ug), len(ug))) for _ in range(400)]
        return f'{(v * ww).sum() / ww.sum():+5.2f} [{np.percentile(bs, 5):+.2f}..{np.percentile(bs, 95):+.2f}]'
    # ================= A: model variants
    if not a.skip_models or not os.path.exists(calls_path):
        import torch
        import lab as LB
        dev = MN.pick_device()
        tg = lambda v: torch.from_numpy(np.asarray(v)).to(dev)
        use = has & np.isfinite(k300) & np.isfinite(k60) & live
        base_rows = np.flatnonzero(use & (tms < d_ms(days_all[min(2, len(days_all) - 1)])))
        Xb = tg(LB.binned(np.nan_to_num(FX), base_rows))
        ccols = torch.tensor([fc[k] for k in COMPACT if k in fc], device=dev)
        clip = lambda v, c=15: np.clip(np.nan_to_num(v), -c, c).astype(np.float32)
        L1 = np.array([MS.CONFIGS[c][2] == 1 for c in ci])
        gb = dict(loss='l2', depth=4, min_leaf=100, lam=50.0, row_frac=0.7, n_trees=200, lr=0.05)
        variants = [('the first model (5-min mark, all inputs, all fills)', dict(y=clip(k300))),
                    ('target: the mark after 1 min', dict(y=clip(k60))),
                    ('target: the mean of the 1-min and 5-min marks', dict(y=clip((k60 + k300) / 2))),
                    ('trained on fills of 1 s reaction only', dict(y=clip(k300), sub=L1)),
                    ('trained on fills in spreads of 4c+ only', dict(y=clip(k300), sub=spr >= 3.5)),
                    ('a short list of 49 inputs', dict(y=clip(k300), cols=ccols)),
                    ('bigger trees (depth 6, 400 trees, slower learning)', dict(y=clip(k300), gb=dict(loss='l2', depth=6, min_leaf=200, lam=100.0, row_frac=0.5, n_trees=400, lr=0.03, patience=60))),
                    ('smaller trees (depth 3, strong shrinkage)', dict(y=clip(k300), gb=dict(loss='l2', depth=3, min_leaf=300, lam=200.0, row_frac=0.7, n_trees=300, lr=0.05))),
                    ('five seeds averaged', dict(y=clip(k300), seeds=5)),
                    ('short list + five seeds + 1-and-5-min target', dict(y=clip((k60 + k300) / 2), cols=ccols, seeds=5)),
                    ('yes / no target: "the fill keeps more than 0"', dict(y=(k300 > 0).astype(np.float32), gb=dict(loss='logistic', depth=4, min_leaf=100, lam=20.0, row_frac=0.7, n_trees=200, lr=0.05))),
                    ('a linear model on the short list', dict(y=clip(k300), linear=True))]
        PV, TH = np.full((len(Fm), len(variants)), np.nan, np.float32), {}
        t0 = time.time()
        for vi, (name, o) in enumerate(variants):
            yv = o['y']
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
                if 'sub' in o:
                    tr = tr[o['sub'][tr]]
                if o.get('linear'):
                    cc = [fc[k] for k in COMPACT if k in fc]
                    A_ = np.nan_to_num(FX[:, cc]).astype(np.float64)
                    mu, sd = A_[tr].mean(0), A_[tr].std(0) + 1e-6
                    Zs = np.clip((A_ - mu) / sd, -6, 6)
                    best = None
                    for lam in (10.0, 100.0, 1000.0, 10000.0):
                        beta = np.linalg.solve(Zs[tr].T @ Zs[tr] + lam * np.eye(len(cc)), Zs[tr].T @ (yv[tr] - yv[tr].mean()))
                        err = np.mean((Zs[va] @ beta + yv[tr].mean() - yv[va]) ** 2)
                        if best is None or err < best[0]:
                            best = (err, beta)
                    pt, pv = Zs[today] @ best[1] + yv[tr].mean(), Zs[va] @ best[1] + yv[tr].mean()
                else:
                    pt, pv = np.zeros(len(today)), np.zeros(len(va))
                    ns = o.get('seeds', 1)
                    for sd_ in range(ns):
                        m = LB.GB(**{**o.get('gb', gb), 'seed': sd_}).fit(Xb[tg(tr)], tg(yv)[tg(tr)], Xb[tg(va)], tg(yv)[tg(va)], cols=o.get('cols'))
                        pt += m.predict(Xb[tg(today)]) / ns
                        pv += m.predict(Xb[tg(va)]) / ns
                PV[today, vi] = pt
                e5v = E5[va]
                TH[(vi, dday)] = float(np.quantile(pv[e5v], 0.54)) if e5v.sum() >= 50 else float(np.quantile(pv, 0.54))
            print(f'  model {vi + 1} of {len(variants)} done ({time.time() - t0:.0f} s)', flush=True)
        sc_days = sorted({d_ for (_, d_) in TH})
        thr = np.full((len(Fm), len(variants)), np.nan, np.float32)
        for (vi, d_), v in TH.items():
            thr[rowday == d_, vi] = v
        np.savez(calls_path, PV=PV, thr=thr, names=np.array([n_ for n_, _ in variants]), days=np.array(sc_days))
    C_ = np.load(calls_path)
    PV, thr, vnames, sc_days = C_['PV'], C_['thr'], C_['names'].tolist(), C_['days'].tolist()
    scored = np.isfinite(PV).all(1) & np.isin(rowday, sc_days)
    nd = len(sc_days)
    b = E5 & scored
    usd = lambda mk, v=k300: np.nansum(v[mk] * w100[mk]) / 100 / nd
    say(f'A. MODEL VARIANTS, each trained on earlier days only; scored on {", ".join(x[8:] for x in sc_days)} Nov. Compared on the {b.sum():,} fills inside spreads of 5c+ at 1 s reaction (plain: {stat(k300[b], gm[b], w100[b])}, ${usd(b):+,.0f} a day).')
    say('   Each keeps the fills above ITS line from earlier days (the same share, about 46%). Cents a share after 5 min [90% by game]; $ a day at 5 min / held to the result, 100-share orders; and $ a day more than the first model [90% by game].')
    base_keep = b & (PV[:, 0] >= thr[:, 0])
    ug, inv = np.unique(gm[b], return_inverse=True)
    for vi, name in enumerate(vnames):
        keep = b & (PV[:, vi] >= thr[:, vi])
        rc = np.corrcoef(np.argsort(np.argsort(PV[scored & live, vi])), np.argsort(np.argsort(k300[scored & live])))[0, 1]
        dif = (keep[b].astype(float) - base_keep[b].astype(float)) * np.nan_to_num(k300[b]) * w100[b] / 100 / nd
        gs = np.bincount(inv, dif, len(ug))
        bs = [gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(1000)]
        say(f'   {name:<52} keeps {keep.sum():>5,}  {stat(k300[keep], gm[keep], w100[keep])}  ${usd(keep):>+5,.0f} / ${usd(keep, kres):>+5,.0f}   vs first ${dif.sum():>+5,.0f} [{np.percentile(bs, 5):+,.0f}..{np.percentile(bs, 95):+,.0f}]   rank corr {rc:+.3f}')
    ens = np.nanmean(np.stack([(PV[:, vi] - np.nanmean(PV[scored, vi])) / (np.nanstd(PV[scored, vi]) + 1e-9) for vi in (0, 2, 5, 8)], 1), 1)
    keep = np.zeros(len(Fm), bool)
    for d_ in sc_days:
        r = np.flatnonzero(b & (rowday == d_))
        prev = np.flatnonzero(b & (rowday < d_))
        line = np.quantile(ens[prev], 0.54) if len(prev) >= 200 else np.quantile(ens[r], 0.54)
        keep[r[ens[r] >= line]] = True
    say(f'   {"four of them together (mean of standardised calls)":<52} keeps {keep.sum():>5,}  {stat(k300[keep], gm[keep], w100[keep])}  ${usd(keep):>+5,.0f} / ${usd(keep, kres):>+5,.0f}')
    with open(os.path.join(a.signals, 'makerwallet.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    # ================= B: the wallet
    S = np.load(os.path.join(a.signals, 'makersteer_states.npz'))
    sgame, sec0, sn, soff, swon = S['game'], S['sec0'], S['n'], S['off'], S['won']
    sflags, sspread, sbid, sask = S['flags'], S['spread'], S['bid'], S['ask']
    gidx = {int(g): i for i, g in enumerate(sgame)}
    send = sec0 + sn + 60                                             # the money of a settled game is back a minute after its last quote
    NG = len(sgame)
    cand = np.flatnonzero((ci == MS.CONFIGS.index((0, 2, 1))) & live & np.isin(rowday, sc_days))
    cand = cand[np.argsort(tms[cand], kind='stable')]
    P0 = PV[:, 0]

    def rule_ok(fl, rule):
        up, dn, lu, ld = (fl >> 1) & 1, (fl >> 2) & 1, (fl >> 3) & 1, (fl >> 4) & 1
        if rule == 'none':
            return np.ones_like(up, bool), np.ones_like(up, bool)
        if rule == 'fam30':
            return dn == 0, up == 0
        if rule == 'fam30+lead':
            return (dn == 0) & (ld == 0), (up == 0) & (lu == 0)
        return up == 1, dn == 1                                        # with30
    EV = {}

    def events(minsp, rule):
        """Per candidate fill that its own setting allows: (fill row, eligible quotes at that second: game idx, side, price c, spread c)."""
        if (minsp, rule) in EV:
            return EV[(minsp, rule)]
        ev = []
        for f in cand:
            g_own = gidx.get(int(gm[f]), -1)
            if g_own < 0:
                continue
            if spr[f] < minsp - 0.5:
                ev.append((f, None, None, None, None, -1, g_own))
                continue
            u = int(tms[f] // 1000)
            idx = (u - 1) - sec0
            ok = (idx >= 0) & (idx < sn)
            gi = np.flatnonzero(ok)
            pos_ = soff[gi] + idx[gi]
            fl, sp_ = sflags[pos_], sspread[pos_]
            el = ((fl & 1) > 0) & (sp_ >= minsp) & (sp_ != 255)
            gi, pos_, fl, sp_ = gi[el], pos_[el], fl[el], sp_[el]
            okb, oka = rule_ok(fl, rule)
            g_ = np.concatenate([gi[okb], gi[oka]])
            sd = np.concatenate([np.ones(okb.sum(), np.int8), -np.ones(oka.sum(), np.int8)])
            pc = np.concatenate([sbid[pos_[okb]].astype(np.int16) + 1, sask[pos_[oka]].astype(np.int16) - 1])
            sc_ = np.concatenate([sp_[okb], sp_[oka]])
            me = gidx.get(int(gm[f]), -1)
            hit = np.flatnonzero((g_ == me) & (sd == int(sg[f])))
            if not len(hit):
                ev.append((f, None, None, None, None, -1, g_own))
                continue
            ev.append((f, g_, sd, pc, sc_, int(hit[0]), g_own))
        EV[(minsp, rule)] = ev
        return ev

    def run(minsp, rule, size, cap_mult, model, days, w0, detail=False, exit_any=False):
        """size: ('fix', shares) or ('pct', share of the wallet a side). Returns the final wallet and counts."""
        ev = events(minsp, rule)
        dset = set(days)
        cash, pos, mark, heap = w0, np.zeros(NG), np.full(NG, 0.5), []
        took = miss_cash = miss_cap = miss_model = exits = 0
        low, per_game, n_elig, need_all = w0, {}, [], []
        for f, g_, sd, pc, sc_, me, g_own in ev:
            if rowday[f] not in dset:
                continue
            u = int(tms[f] // 1000)
            while heap and heap[0][0] <= u:
                _, g0 = heapq.heappop(heap)
                pay = pos[g0] * swon[g0] if pos[g0] > 0 else -pos[g0] * (1 - swon[g0])
                cash += pay
                per_game[g0] = per_game.get(g0, 0.0) + pay
                pos[g0] = 0.0
            held = np.where(pos > 0, pos * mark, -pos * (1 - mark)).sum()
            eq = cash + held
            low = min(low, eq)
            sz = float(size[1]) if size[0] == 'fix' else max(5.0, 5.0 * np.floor(eq * size[1] / 0.5 / 5.0))
            cap = sz * cap_mult
            if exit_any and pos[g_own] != 0 and np.sign(pos[g_own]) == -int(sg[f]):      # this trade takes our exit order: no money needed, no rule
                sh, qf = min(abs(pos[g_own]), float(raw[f])), float(q[f])
                flow = sh * qf if sg[f] < 0 else sh * (1 - qf)
                cash += flow
                per_game[g_own] = per_game.get(g_own, 0.0) + flow
                pos[g_own] += int(sg[f]) * sh
                mark[g_own] = qf
                exits += 1
                continue
            if g_ is None:
                continue
            if model and not (P0[f] > 0):
                miss_model += 1
                continue
            pg = pos[g_]
            price = pc / 100.0
            within = np.abs(pg + sd * sz) <= cap + 1e-9
            need = np.where(sd > 0, sz * price, np.maximum(sz - np.maximum(pg, 0), 0) * (1 - price))
            if detail:
                n_elig.append(int(within.sum()))
                need_all.append(float(need[within].sum()))
            if not within[me]:
                miss_cap += 1
                continue
            order = np.lexsort((need, -sc_.astype(np.int32)))            # widest spread first, then the cheaper order
            order = order[within[order]]
            csum = np.cumsum(need[order])
            rank = int(np.flatnonzero(order == me)[0])
            if csum[rank] > cash + 1e-9:
                miss_cash += 1
                continue
            g0, s_, qf = int(g_[me]), int(sd[me]), float(q[f])
            sh = min(sz, float(raw[f]))
            if s_ > 0:
                cover = min(sh, max(-pos[g0], 0.0))
                flow = cover * (1 - qf) - (sh - cover) * qf
            else:
                red = min(sh, max(pos[g0], 0.0))
                flow = red * qf - (sh - red) * (1 - qf)
            cash += flow
            per_game[g0] = per_game.get(g0, 0.0) + flow
            if pos[g0] == 0:
                heapq.heappush(heap, (int(send[g0]), g0))
            pos[g0] += s_ * sh
            mark[g0] = qf
            took += 1
        while heap:
            _, g0 = heapq.heappop(heap)
            pay = pos[g0] * swon[g0] if pos[g0] > 0 else -pos[g0] * (1 - swon[g0])
            cash += pay
            per_game[g0] = per_game.get(g0, 0.0) + pay
            pos[g0] = 0.0
        r = dict(final=cash, took=took, miss_cash=miss_cash, miss_cap=miss_cap, miss_model=miss_model, low=low, exits=exits)
        if detail:
            r['per_game'] = per_game
            r['n_elig'], r['need_all'] = n_elig, need_all
        return r
    W0 = a.wallet
    say('')
    d1, d2 = [x for x in sc_days if x <= '2026-11-20'], [x for x in sc_days if x >= '2026-11-21']
    say(f'B. A ${W0:.0f} WALLET. Fills replayed in time order over all {NG} games; an order only exists if the wallet can pay for it at that second (widest spreads first); positions net out in a game and the rest is held to the result.')
    ev = events(5, 'fam30')
    r = run(5, 'fam30', ('fix', 20), 2, False, sc_days, W0, detail=True)
    say(f'   how much is on offer at once (spreads of 5c+, bookmaker rule): at the moment of a fill a median {np.median(r["n_elig"]):.0f} orders qualify across the games (90% of the time under {np.quantile(r["n_elig"], 0.9):.0f}); '
        f'to rest them all at 20 shares takes a median ${np.median(r["need_all"]):,.0f} (90% under ${np.quantile(r["need_all"], 0.9):,.0f}), at 5 shares a quarter of that.')

    def line(name, minsp, rule, size, capm, model, days, exit_any=False):
        r = run(minsp, rule, size, capm, model, days, W0, detail=True, exit_any=exit_any)
        pg = np.array(list(r['per_game'].values())) if r['per_game'] else np.zeros(1)
        bs = [pg[rng.integers(0, len(pg), len(pg))].sum() for _ in range(1000)]
        tot = r['took'] + r['miss_cash'] + r['miss_cap']
        say(f'   {name:<62} ${W0:.0f} -> ${r["final"]:>7,.2f} ({(r["final"] / W0 - 1) * 100:+5.0f}%, ${(r["final"] - W0) / len(days):+6.2f} a day; resampled by game ${W0 + np.percentile(bs, 5):,.0f}..${W0 + np.percentile(bs, 95):,.0f}) | '
            f'entries {r["took"]:>5,} of {tot:,} (no money {r["miss_cash"]:,}, at the cap {r["miss_cap"]:,}), exits {r["exits"]:,} | lowest ${r["low"]:,.0f} | games up {np.mean(pg > 0) * 100:.0f}%')
        return r
    say(f'   {len(sc_days)} days, spreads of 5c+, never against the bookmakers\' last 30 s, cap = 2 orders a game:')
    for ex in (False, True):
        say(f'   {"HELD TO THE RESULT (what is not netted by chance rides the game)" if not ex else "GETTING OUT (the exit order always rests while we hold shares)"}:')
        for sz in (5, 10, 20, 40):
            line(f'{sz} shares an order', 5, 'fam30', ('fix', sz), 2, False, sc_days, ex)
        for pct in (0.03, 0.05, 0.10, 0.20):
            line(f'{pct * 100:.0f}% of the wallet an order (grows with it)', 5, 'fam30', ('pct', pct), 2, False, sc_days, ex)
    say('   getting out, 10 shares an order, other choices:')
    line('no rule at all (both sides always)', 5, 'none', ('fix', 10), 2, False, sc_days, True)
    line('also never against a superline lead', 5, 'fam30+lead', ('fix', 10), 2, False, sc_days, True)
    line('only WITH the bookmakers\' last 30 s', 5, 'with30', ('fix', 10), 2, False, sc_days, True)
    line('bookmaker rule + the model must expect a gain', 5, 'fam30', ('fix', 10), 2, True, sc_days, True)
    line('no rule, the model must expect a gain', 5, 'none', ('fix', 10), 2, True, sc_days, True)
    for ms_ in (3, 4, 6, 7, 8):
        line(f'spreads of {ms_}c+ (bookmaker rule)', ms_, 'fam30', ('fix', 10), 2, False, sc_days, True)
    for cm in (1, 4, 8):
        line(f'cap = {cm} order{"s" if cm > 1 else ""} a game (bookmaker rule, 5c+)', 5, 'fam30', ('fix', 10), cm, False, sc_days, True)
    with open(os.path.join(a.signals, 'makerwallet.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    # ================= C: settings chosen on one half, run on the other
    say('')
    say(f'C. FINE TUNING (getting out; held to the result the same grid gave a rank correlation of +0.09 between the halves: pure luck of results): every setting run on {d1[0][8:]}-{d1[-1][8:]} Nov and on {d2[0][8:]}-{d2[-1][8:]} Nov, each from a fresh ${W0:.0f}.')
    grid = [(ms_, rl, sz, cm, md) for ms_ in (5, 6, 7, 8) for rl in RULES for sz in (('fix', 5), ('fix', 10), ('fix', 20), ('pct', 0.05), ('pct', 0.10), ('pct', 0.20)) for cm in (1, 2, 4) for md in (False, True)]
    R1, R2 = np.zeros(len(grid)), np.zeros(len(grid))
    t0 = time.time()
    for i, (ms_, rl, sz, cm, md) in enumerate(grid):
        R1[i] = run(ms_, rl, sz, cm, md, d1, W0, exit_any=True)['final']
        R2[i] = run(ms_, rl, sz, cm, md, d2, W0, exit_any=True)['final']
        if (i + 1) % 100 == 0:
            print(f'  {i + 1} of {len(grid)} settings ({time.time() - t0:.0f} s)', flush=True)
    nm = lambda g: f'spread {g[0]}c+, rule {g[1]}, {(str(g[2][1]) + " shares") if g[2][0] == "fix" else (f"{g[2][1] * 100:.0f}% of the wallet")}, cap {g[3]}, {"with" if g[4] else "no"} model'
    rk = lambda v: np.argsort(np.argsort(v))
    say(f'   {len(grid)} settings. Do the settings that did well in one half do well in the other? rank correlation {np.corrcoef(rk(R1), rk(R2))[0, 1]:+.2f}; settings that grew the wallet: first half {np.mean(R1 > W0) * 100:.0f}%, second half {np.mean(R2 > W0) * 100:.0f}%, both {np.mean((R1 > W0) & (R2 > W0)) * 100:.0f}%')
    dflt = grid.index((5, 'fam30', ('fix', 10), 2, False))
    b1, b2 = int(np.argmax(R1)), int(np.argmax(R2))
    say(f'   the default ({nm(grid[dflt])}): first half ${R1[dflt]:,.2f}, second half ${R2[dflt]:,.2f}')
    say(f'   best of the first half ({nm(grid[b1])}): ${R1[b1]:,.2f} there -> on the second half ${R2[b1]:,.2f}')
    say(f'   best of the second half ({nm(grid[b2])}): ${R2[b2]:,.2f} there -> on the first half ${R1[b2]:,.2f}')
    top1 = np.argsort(-R1)[:20]
    say(f'   the 20 best of the first half make a median ${np.median(R2[top1]):,.2f} on the second half (all settings: median ${np.median(R2):,.2f}); the 20 best of the second half make ${np.median(R1[np.argsort(-R2)[:20]]):,.2f} on the first (all: ${np.median(R1):,.2f})')
    say('   what each choice is worth, median wallet over all settings with it (first half / second half):')
    for title, key, vals in (('spread', 0, (5, 6, 7, 8)), ('rule', 1, RULES), ('size', 2, (('fix', 5), ('fix', 10), ('fix', 20), ('pct', 0.05), ('pct', 0.10), ('pct', 0.20))), ('cap', 3, (1, 2, 4)), ('model', 4, (False, True))):
        say(f'      {title}: ' + '; '.join(f'{v if not isinstance(v, tuple) else (str(v[1]) + (" sh" if v[0] == "fix" else " of wallet"))} ${np.median(R1[[i for i, g in enumerate(grid) if g[key] == v]]):,.0f} / ${np.median(R2[[i for i, g in enumerate(grid) if g[key] == v]]):,.0f}' for v in vals))
    both = np.minimum(R1, R2)
    say('   the settings whose WORSE half is best (robust choice), with both halves and the whole period run through:')
    for i in np.argsort(-both)[:6]:
        g = grid[i]
        rr = run(g[0], g[1], g[2], g[3], g[4], sc_days, W0, exit_any=True)
        say(f'      {nm(g):<70} ${R1[i]:>7,.2f} / ${R2[i]:>7,.2f} | {len(sc_days)} days straight ${rr["final"]:>8,.2f}, entries {rr["took"]:,}, exits {rr["exits"]:,}, lowest ${rr["low"]:,.0f}')
    with open(os.path.join(a.signals, 'makerwallet.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
