"""Three follow-ups on mine.npz (owner, 24 Nov): python tools/signals/followup.py --signals DIR

1. LEAD INSIDE A GAME. "When the superline is NOT leading in the first X minutes, is it leading by the end? Or at some
   point? Is there a correlation to how much % of the game the superline leads?" Per game, minute by minute: the
   rolling 15-minute lead (lead15: > +0.02 = superline leads, < -0.02 = Polymarket leads). Early lead = the record
   over the first X live minutes (lead20 at minute X). Then: leading at the end, at some point later, the share of
   later minutes; the share of the whole game each side leads; first half vs second half; and whether the share goes
   with the dollars the plain signals make in that game (whole game = hindsight; first half -> second half = usable).
2. RULES OF THE DAY. Per day: a model trained only on that day's early games (the first 35% by kick-off, samples
   before the cut), on earlier days, or on both, tested on the day's later games; the same for the "80%+ wins in 50+
   samples" boxes; by sport.
3. CASHING OUT LOSERS EARLY. The same entries under: +5c or 5 min (tp5); the same with a stop 3c / 5c under the price
   paid (s3 / s5) or 2c under the entry bid (a2); "not better than the entry bid after 30 / 60 s -> sell" (t30 / t60);
   and a plain 5-minute sale. On every sample, the two plain signals and the tree model's own picks."""
import argparse
import datetime as dt
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mine as MN  # noqa: E402

DAY = 86_400_000
DB = 0.02
QS = (0.9995, 0.999, 0.998, 0.995, 0.99, 0.98, 0.95)
SPORTS = ('football', 'cs2', 'tennis', 'basketball', 'dota2')
rk = lambda v: np.argsort(np.argsort(v))
spear = lambda a, b: np.corrcoef(rk(a), rk(b))[0, 1] if len(a) > 5 else float('nan')


def part1(P, say):
    n, FE, fc, YY, H, key = P['n'], P['FE'], P['fc'], P['YY'], P['H'], P['key0']
    hc = {k: i for i, k in enumerate(MN.HN)}
    lm, l15, l20, sa, pa = (H[:, hc[k]].astype(float) for k in ('live_min', 'lead15', 'lead20', 'share_sl_ahead', 'share_pm_ahead'))
    cut = np.flatnonzero(np.r_[True, key[1:] != key[:-1], True])
    ng = len(cut) - 1
    gid = np.repeat(np.arange(ng), np.diff(cut))
    R = {k: np.full(ng, np.nan) for k in ('pct_sl', 'pct_pm', 'sl1', 'sl2', 'st1', 'st2', 'half', 'sa', 'pa')}
    E = {X: {k: np.full(ng, np.nan) for k in ('early', 'end', 'ever', 'share')} for X in (5, 10, 15, 30)}
    for g in range(ng):
        a, b = cut[g], cut[g + 1]
        if lm[b - 1] < 45:
            continue
        mg = np.arange(1, int(lm[b - 1]) + 1)
        idx = a + np.clip(np.searchsorted(lm[a:b], mg, side='right') - 1, 0, None)
        v15, v20 = l15[idx], l20[idx]
        for X, d in E.items():
            later = v15[X + 14:]
            if len(later) >= 10:
                d['early'][g], d['end'][g], d['ever'][g], d['share'][g] = v20[X - 1], later[-1] > DB, (later > DB).any(), (later > DB).mean()
        w = v15[14:]
        R['pct_sl'][g], R['pct_pm'][g] = (w > DB).mean(), (w < -DB).mean()
        h = len(mg) // 2
        if h - 14 >= 10:
            R['sl1'][g], R['sl2'][g], R['half'][g] = (v15[14:h] > DB).mean(), (v15[h:] > DB).mean(), h
            i_h, i_e = idx[h - 1], b - 1
            R['st1'][g] = sa[i_h] / max(sa[i_h] + pa[i_h], 1e-9)
            s2, p2 = sa[i_e] * lm[i_e] - sa[i_h] * lm[i_h], pa[i_e] * lm[i_e] - pa[i_h] * lm[i_h]
            R['st2'][g] = s2 / max(s2 + p2, 1e-9)
        R['sa'][g], R['pa'][g] = sa[b - 1], pa[b - 1]
    ok = np.isfinite(R['pct_sl'])
    say(f'1. LEAD INSIDE A GAME ({ok.sum()} games with 45+ minutes live; "leads" = rolling 15-minute lead over +0.02)')
    for X, d in E.items():
        m = np.isfinite(d['early'])
        no, yes = m & (d['early'] <= 0), m & (d['early'] > 0)
        f = lambda mm: (f'{mm.sum()} games: leading at the end {np.nanmean(d["end"][mm]) * 100:.0f}%, at some point later {np.nanmean(d["ever"][mm]) * 100:.0f}%, '
                        f'leads {np.nanmedian(d["share"][mm]) * 100:.0f}% of the later minutes (median)')
        say(f'   first {X:>2} min: superline NOT leading -> {f(no)}')
        say(f'                superline leading     -> {f(yes)}')
    q = lambda v, p: np.nanquantile(v[ok], p) * 100
    say(f'   share of a game (after minute 15) the superline leads: median {q(R["pct_sl"], 0.5):.0f}%, top quarter of games {q(R["pct_sl"], 0.75):.0f}%+, top tenth {q(R["pct_sl"], 0.9):.0f}%+; '
        f'it leads more than half the game in {np.mean(R["pct_sl"][ok] > 0.5) * 100:.0f}% of games. Polymarket leads: median {q(R["pct_pm"], 0.5):.0f}%, more than half the game in {np.mean(R["pct_pm"][ok] > 0.5) * 100:.0f}%')
    say(f'   time in the lead STATES (a visible gap): superline ahead {q(R["sa"], 0.5):.1f}% of the game (median), Polymarket ahead {q(R["pa"], 0.5):.1f}%')
    h = np.isfinite(R['sl1']) & np.isfinite(R['sl2'])
    say(f'   does it stay the same game-long? first half vs second half, {h.sum()} games: share of minutes the superline leads, rank correlation {spear(R["sl1"][h], R["sl2"][h]):+.2f}; '
        f'superline share of the lead states {spear(R["st1"][h], R["st2"][h]):+.2f}')
    top = h & (R['sl1'] >= np.nanquantile(R['sl1'][h], 0.8))
    say(f'   the fifth of games where it led most in the first half ({np.nanmedian(R["sl1"][top]) * 100:.0f}% of minutes): it led {np.nanmedian(R["sl2"][top]) * 100:.0f}% of the second half; all other games {np.nanmedian(R["sl2"][h & ~top]) * 100:.0f}%')
    live = FE[:, fc['live_min']]
    G2 = np.r_[gid, gid]
    sigs = {'superline ahead our way': FE[:, fc['lead_up']] > 0.5,
            'a bookmaker jumped, Polymarket has not followed': (FE[:, fc['fam_r10_max']] >= 0.12) & (FE[:, fc['pm_r10']] < 0.02) & (FE[:, fc['fam_gap_mean']] >= 0.12)}
    for sname, sm in sigs.items():
        S = np.flatnonzero(sm & P['okY'])
        for lab, val, sel in (('share of the WHOLE game the superline leads (hindsight)', R['pct_sl'][G2[S]], np.ones(len(S), bool)),
                              ('share of the FIRST HALF it leads -> trades in the second half (usable)', R['sl1'][G2[S]], live[S] > R['half'][G2[S]])):
            mm = np.isfinite(val) & sel
            if mm.sum() < 500:
                continue
            v, y5, yt = val[mm], YY[S[mm], 1], YY[S[mm], 2]
            qs = np.quantile(v, [0.2, 0.4, 0.6, 0.8, 0.95])
            cells = [f'{name} ${y5[b].mean():+.2f} (tp5 win {np.mean(yt[b] > 0) * 100:.0f}%)' for name, b in
                     (('lowest fifth', v <= qs[0]), ('middle', (v > qs[1]) & (v <= qs[2])), ('top fifth', v >= qs[3]), ('top 5%', v >= qs[4]))]
            say(f'   "{sname}" by {lab}: ' + '; '.join(cells) + f'; rank correlation with $ {spear(v[::7], y5[::7]):+.3f}')
    say('')


def fit_split(idx, j, Xg, Yc, T, tg, min_leaf=300):
    idx = idx[np.argsort(T[idx], kind='stable')]
    cut = int(0.8 * len(idx))
    va, tr = idx[cut:], idx[:cut]
    tr = tr[T[tr] + 1_200_000 < T[va].min()]
    m = MN.Trees(depth=6, n_trees=300, min_leaf=min_leaf).fit(Xg[tg(tr)], Yc[tg(tr), j], Xg[tg(va)], Yc[tg(va), j])
    return m, va, m.predict(Xg[tg(va)]).cpu().numpy()


def pick_tau(pv, va, j, P, min_trades):
    best = None
    for q in QS:
        tau = np.quantile(pv, q)
        tk = MN.place(P['T'], P['G'], va[pv >= tau])
        if len(tk) >= min_trades:
            s = MN.gstats(P['YY'][tk, j], P['G'][tk])
            if s['cautious'] > 0 and (best is None or s['cautious'] > best[0]):
                best = (s['cautious'], tau)
    return None if best is None else best[1]


def part2(P, Xg, Yc, edges, days, say, dev):
    import torch
    T, G, SP, okY, YY, FE, fc, FN = (P[k] for k in ('T', 'G', 'SP', 'okY', 'YY', 'FE', 'fc', 'FN'))
    tg = lambda v: torch.from_numpy(v).to(dev)
    ug, inv = np.unique(G, return_inverse=True)
    gstart = np.full(len(ug), 2 ** 62, np.int64)
    np.minimum.at(gstart, inv, T - (FE[:, fc['live_min']].astype(np.float64) * 60000).astype(np.int64))
    S0 = gstart[inv]
    d_ms = lambda d: int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    V = ('earlier days', 'the day\'s early games', 'both')
    rank = {v: np.zeros((3, 2)) for v in V}
    trades = {v: [] for v in V}
    by_sport = {s: {'rank': np.zeros(2), 'trades': [], 'all': np.zeros(2)} for s in SPORTS}
    boxes = {v: np.zeros(5) for v in V[:2]}     # boxes, found samples, found wins, unseen samples, unseen wins... + $
    box_usd = {v: 0.0 for v in V[:2]}
    base = np.zeros(2)
    say('2. RULES OF THE DAY: learned from a day\'s early games (first 35% by kick-off) vs from earlier days, tested on that day\'s LATER games')
    for d in days:
        d0 = d_ms(d)
        gs = gstart[(gstart >= d0) & (gstart < d0 + DAY)]
        if len(gs) < 40:
            continue
        cutoff = int(np.quantile(gs, 0.35))
        late = np.flatnonzero(okY & (S0 >= cutoff) & (S0 < d0 + DAY))
        sets = {V[0]: np.flatnonzero(okY & (T + 1_200_000 < d0)), V[1]: np.flatnonzero(okY & (S0 >= d0) & (S0 < cutoff) & (T + 1_200_000 < cutoff))}
        sets[V[2]] = np.concatenate([sets[V[0]], sets[V[1]]])
        if len(late) < 5000 or len(sets[V[1]]) < 20_000 or len(sets[V[0]]) < 20_000:
            say(f'   {d}: too little data (early-game samples {len(sets[V[1]])}), skipped')
            continue
        base += (YY[late, 1].sum(), len(late))
        line = [f'   {d}: early-game samples {len(sets[V[1]])}, later-game samples {len(late)}']
        lg = tg(late)
        for v in V:
            m, va, pv = fit_split(sets[v], 1, Xg, Yc, T, tg)
            pt = m.predict(Xg[lg]).cpu().numpy()
            order = np.argsort(-pt)
            k1, k2 = max(1, len(pt) // 2000), max(1, len(pt) // 200)
            for b, (lo, hi) in enumerate(((0, k1), (k1, k2), (k2, len(pt)))):
                rank[v][b] += (YY[late[order[lo:hi]], 1].sum(), hi - lo)
            tau = pick_tau(pv, va, 1, P, 30 if v != V[1] else 15)
            if tau is not None:
                tk = MN.place(T, G, late[pt >= tau])
                trades[v] += [(YY[i, 1], G[i]) for i in tk]
                line.append(f'{v}: {len(tk)} trades ${YY[tk, 1].sum():+.0f}')
            else:
                line.append(f'{v}: nothing passed')
            if v == V[2]:
                for s in SPORTS:
                    ms = SP[late] == s
                    if ms.sum() < 2000:
                        continue
                    o = np.argsort(-pt[ms])[:max(1, ms.sum() // 200)]
                    by_sport[s]['rank'] += (YY[late[ms][o], 1].sum(), len(o))
                    by_sport[s]['all'] += (YY[late[ms], 1].sum(), ms.sum())
                    if tau is not None:
                        tk = MN.place(T, G, late[ms][pt[ms] >= tau])
                        by_sport[s]['trades'] += [(YY[i, 1], G[i]) for i in tk]
        for v in V[:2]:      # the "80%+ wins in 50+ samples" boxes of a deep tree (tp5), found on this set, tested on the later games
            tr = sets[v]
            if len(tr) > 400_000:
                tr = tr[np.argsort(T[tr])][-400_000:]
            mt = MN.Trees(depth=9, lr=1.0, n_trees=1, min_leaf=50, lam=0.0, row_frac=1.0, col_frac=1.0)
            f, th, _ = mt._tree(Xg[tg(tr)], Yc[tg(tr), 2] - Yc[tg(tr), 2].mean(), torch.arange(Xg.shape[1], device=dev))
            ltr, lte = MN.Trees.leaf(Xg[tg(tr)], f, th).cpu().numpy(), MN.Trees.leaf(Xg[lg], f, th).cpu().numpy()
            L = 1 << 9
            ntr = np.bincount(ltr, minlength=L)
            wtr = np.bincount(ltr, YY[tr, 2] > 0, L)
            box = np.flatnonzero((ntr >= 50) & (wtr / np.maximum(ntr, 1) >= 0.8))
            sel = np.isin(lte, box)
            boxes[v] += (len(box), ntr[box].sum(), wtr[box].sum(), sel.sum(), (YY[late[sel], 2] > 0).sum())
            box_usd[v] += YY[late[sel], 2].sum()
        say(' | '.join(line))
    if base[1]:
        say(f'   every later-game sample, no selection: ${base[0] / base[1]:+.2f} per $100')
        for v in V:
            r = rank[v]
            say(f'   model from {v:<22} top 0.05% of its picks ${r[0, 0] / max(r[0, 1], 1):+.2f} ({int(r[0, 1])} samples) | next to 0.5% ${r[1, 0] / max(r[1, 1], 1):+.2f} | the rest ${r[2, 0] / max(r[2, 1], 1):+.2f}')
            say(MN.fmt(f'   trades, model from {v}', MN.gstats([x[0] for x in trades[v]], [x[1] for x in trades[v]])))
        for v in V[:2]:
            b = boxes[v]
            say(f'   "80%+ wins in 50+ samples" boxes found on {v}: {int(b[0])} boxes, {int(b[1])} samples, {b[2] / max(b[1], 1) * 100:.0f}% wins -> on the later games: {int(b[3])} samples, '
                f'{b[4] / max(b[3], 1) * 100:.0f}% wins, ${box_usd[v] / max(b[3], 1):+.2f} per $100')
        say('   by sport (model from both; later games):')
        for s in SPORTS:
            x = by_sport[s]
            if x['all'][1]:
                st = MN.gstats([t[0] for t in x['trades']], [t[1] for t in x['trades']])
                say(f'      {s:<11} every sample ${x["all"][0] / x["all"][1]:+.2f} | model\'s top 0.5% ${x["rank"][0] / max(x["rank"][1], 1):+.2f} ({int(x["rank"][1])}) | trades: ' +
                    (f'{st["n"]} in {st["games"]} games ${st["tot"]:+,.0f} (90% {st["lo"]:+,.0f}..{st["hi"]:+,.0f}), ${st["mean"]:+.2f} per $100, win {st["win"]:.0f}%' if st else 'none'))
    say('')


def part3(P, Xg, Yc, days, say, dev):
    import torch
    T, G, okY, YY, FE, fc = (P[k] for k in ('T', 'G', 'okY', 'YY', 'FE', 'fc'))
    tg = lambda v: torch.from_numpy(v).to(dev)
    d_ms = lambda d: int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    EX = [(2, '+5c or 5 min (tp5)'), (3, 'tp5 + stop 3c under the buy'), (4, 'tp5 + stop 5c under the buy'), (5, 'tp5 + stop 2c under the entry bid'),
          (6, 'tp5 + out at 30 s if not better'), (7, 'tp5 + out at 60 s if not better'), (1, 'plain sale after 5 min')]
    picks, clean, picks_h = [], [], []
    for d in days:
        d0 = d_ms(d)
        tr = np.flatnonzero(okY & (T + 1_200_000 < d0))
        today = np.flatnonzero(okY & (T >= d0) & (T < d0 + DAY))
        if len(tr) < 50_000 or not len(today):
            continue
        m, va, pv = fit_split(tr, 2, Xg, Yc, T, tg)
        pt = m.predict(Xg[tg(today)]).cpu().numpy()
        tau = np.quantile(pv, 0.998)                     # the threshold comes from earlier days, never from the day itself
        pk = MN.place(T, G, today[pt >= tau])
        picks += list(pk)
        vk = MN.place(T, G, va[pv >= tau])
        j = max(EX, key=lambda e: YY[vk, e[0]].sum())[0] if len(vk) else 2
        clean += [(YY[i, j], G[i]) for i in pk]
        m2, _, pv2 = fit_split(tr, 1, Xg, Yc, T, tg)        # the same, trained on the plain 5-minute sale
        pt2 = m2.predict(Xg[tg(today)]).cpu().numpy()
        picks_h += list(MN.place(T, G, today[pt2 >= np.quantile(pv2, 0.998)]))
    picks, picks_h = np.array(picks, np.int64), np.array(picks_h, np.int64)
    sets = [('every sample', np.flatnonzero(okY)), ('superline ahead our way', np.flatnonzero(okY & (FE[:, fc['lead_up']] > 0.5))),
            ('bookmaker jumped, Polymarket not followed', np.flatnonzero(okY & (FE[:, fc['fam_r10_max']] >= 0.12) & (FE[:, fc['pm_r10']] < 0.02) & (FE[:, fc['fam_gap_mean']] >= 0.12))),
            (f'picks of the model trained on +5c-or-5-min (top 0.2%, threshold from earlier days, one a game: {len(picks)})', picks),
            (f'picks of the model trained on the plain 5-minute sale ({len(picks_h)})', picks_h)]
    say('3. CASHING OUT LOSERS EARLY: the same entries, different exits ($ per $100 order / share of trades won)')
    for name, idx in sets:
        if not len(idx):
            continue
        say(f'   {name}:')
        say('      ' + ' | '.join(f'{lab} ${YY[idx, j].mean():+.2f} / {np.mean(YY[idx, j] > 0) * 100:.0f}%' for j, lab in EX))
    if len(picks):
        tp = YY[picks, 2]
        W, Lo = tp > 0, tp <= 0
        say(f'   the model\'s picks under tp5: {W.mean() * 100:.0f}% win ${tp[W].mean():+.2f} each, {Lo.mean() * 100:.0f}% lose ${tp[Lo].mean():+.2f} each')
        for j, lab in EX[1:6]:
            v = YY[picks, j]
            say(f'      {lab}: of the winners {np.mean(v[W] <= 0) * 100:.0f}% are cut before they win (they then make ${v[W][v[W] <= 0].mean() if (v[W] <= 0).any() else 0:+.2f}); '
                f'the losers end at ${v[Lo].mean():+.2f} instead of ${tp[Lo].mean():+.2f}; net change ${v.mean() - tp.mean():+.2f} per $100')
        say(MN.fmt('   exit chosen on earlier days\' picks, applied to the day', MN.gstats([x[0] for x in clean], [x[1] for x in clean])))
        say(MN.fmt('   the same picks with tp5', MN.gstats(YY[picks, 2], G[picks])))
    dstr = lambda i: dt.datetime.fromtimestamp(T[i] / 1000, dt.timezone.utc).strftime('%d')
    for name, pk in (('picks of the +5c-or-5-min model', picks), ('picks of the 5-minute-sale model', picks_h)):
        if len(pk):
            sp, dd = P['SP'][pk], np.array([dstr(i) for i in pk])
            say(MN.fmt(f'   {name}, plain 5-minute sale', MN.gstats(YY[pk, 1], G[pk])))
            say('      by day: ' + '; '.join(f'{d} Nov {np.sum(dd == d)} ${YY[pk[dd == d], 1].mean():+.2f}' for d in sorted(set(dd))))
            say('      by sport: ' + '; '.join(f'{x} {np.sum(sp == x)} ${YY[pk[sp == x], 1].mean():+.2f} (win {np.mean(YY[pk[sp == x], 1] > 0) * 100:.0f}%)' for x in SPORTS if np.sum(sp == x) >= 20))
            v = np.sort(YY[pk, 1])[::-1]
            say(f'      median trade ${np.median(v):+.2f}; the 20 best trades make ${v[:20].sum():+,.0f} of ${v.sum():+,.0f}; token price at entry: median {np.median(1 / (1 + np.exp(-FE[pk, fc["x"]]))) * 100:.0f}c')


def main():
    import torch
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--parts', default='123')
    a = ap.parse_args()
    dev = 'cuda'
    D = np.load(os.path.join(a.signals, 'mine.npz'))
    X, H, Y, t0, game0, sport0, out0 = D['X'], D['H'], D['Y'], D['t'], D['game'], D['sport'], D['outcome']
    n = len(t0)
    FE = np.concatenate([np.concatenate([X, H], 1), np.concatenate([MN.mirror(X), H], 1)])
    del X
    FN = MN.NAMES + MN.HN
    P = dict(n=n, FE=FE, YY=np.concatenate([Y[:, 0], Y[:, 1]]), T=np.concatenate([t0, t0]), G=np.concatenate([game0, game0]).astype(np.int64),
             SP=np.concatenate([sport0, sport0]), FN=FN, fc={k: i for i, k in enumerate(FN)}, H=H, key0=game0.astype(np.int64) * 4 + out0)
    P['okY'] = np.isfinite(P['YY']).all(1)
    T = P['T']
    days = sorted({dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for x in T[::5000]})
    test_days = [d for d in days if d >= '2026-11-19']
    out = []
    say = lambda s: (out.append(s), print(s, flush=True))
    if '1' in a.parts:
        part1(P, say)
    if '2' in a.parts or '3' in a.parts:
        first = int(dt.datetime.fromisoformat(test_days[0]).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        rng = np.random.default_rng(0)
        base_rows = np.flatnonzero(T < first)
        samp = rng.choice(base_rows, min(300_000, len(base_rows)), replace=False)
        edges, Xb = [], np.zeros(FE.shape, np.uint8)
        for j in range(FE.shape[1]):
            e = np.unique(np.quantile(FE[samp, j], np.linspace(0, 1, 65)[1:-1]))
            edges.append(e)
            Xb[:, j] = np.searchsorted(e, FE[:, j], side='right')
        Xg = torch.from_numpy(Xb).to(dev)
        del Xb
        Yc = torch.from_numpy(np.clip(np.nan_to_num(P['YY']), -40, 40).astype(np.float32)).to(dev)
        if '2' in a.parts:
            part2(P, Xg, Yc, edges, test_days, say, dev)
        if '3' in a.parts:
            part3(P, Xg, Yc, test_days, say, dev)
    with open(os.path.join(a.signals, 'followup.txt' if a.parts == '123' else f'followup_{a.parts}.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
