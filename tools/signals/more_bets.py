"""A helper for taking MORE bets on top of rule + veto (meta-labelling: simple signals propose, a model decides).

  python tools/signals/more_bets.py build --db X --signals DIR [--workers 24]
  python tools/signals/more_bets.py test  --signals DIR

CANDIDATES (each one per game / outcome / direction per minute, known at the second):
  booklead_small  the bookmaker leads (booklead pattern) with a gap of 3 to 8 points: the zone the rule leaves out;
  drawn           the owner's first drawing: Polymarket rising, a bookmaker jumping past it;
  superline_lead  the lead state turning "superline ahead" (lead.py), the green / red on the Trade review strip.
Each is valued with the owner's exit: buy 1 s later, sell when the token reaches the bookmakers' level at entry (for
superline_lead: the superline's level), else at 180 / 300 / 600 s; fees both sides.
TEST, day by day: a model (ridge or a small network, chosen on validation) trained on candidates whose trade ended
before the day predicts the net of each candidate; the threshold is chosen on the last 20% before the day so that the
EXTRA trades made money there (else no extras that day). On the day: rule + veto trades first (booklead_veto_kept.json),
then extra candidates in time order, skipped while any position is open in the same game (either outcome)."""
import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import train_waves as TW  # noqa: E402
import waves as W  # noqa: E402
from booklead import HS, build_game  # noqa: E402
from booklead_veto import fit_net, fit_ridge  # noqa: E402

TYPES = ['booklead_small', 'drawn', 'superline_lead']
EXIT = 'target_600'
SPORTS = ['tennis', 'basketball', 'football', 'cs2', 'dota2', 'amfootball']


def cmd_build(a):
    A = dict(np.load(os.path.join(a.signals, 'waves_A.npz'), allow_pickle=False))
    names = [str(x) for x in A['names']]
    c = {k: i for i, k in enumerate(names)}
    X = A['X']
    cands = []   # (type, row in waves_A, d, room)
    for t_, fname, lo, hi in (('booklead_small', 'pattern_booklead.json', 3, 8), ('drawn', 'pattern_catchup.json', 0, 1e9)):
        for r in json.load(open(os.path.join(a.signals, fname), encoding='utf-8')):
            if lo <= r['room'] < hi:
                cands.append((t_, r['i'], r['d'], r['room']))
    # superline_lead: the first second of each "superline ahead" spell, one per game / outcome / direction per minute
    same = np.r_[False, (A['game'][1:] == A['game'][:-1]) & (A['outcome'][1:] == A['outcome'][:-1])]
    for d, col in ((1, 'lead_up'), (-1, 'lead_down')):
        on = X[:, c[col]] > 0.5
        start = on & ~(np.r_[False, on[:-1]] & same)
        last = {}
        for i in np.flatnonzero(start):
            k = (int(A['game'][i]), int(A['outcome'][i]), d)
            if A['t'][i] - last.get(k, -10 ** 15) < 60_000:
                continue
            last[k] = int(A['t'][i])
            x, e = X[i, c['x']], X[i, c['gap_e']]
            room = abs(1 / (1 + np.exp(-(x + e))) - 1 / (1 + np.exp(-x))) * 100
            if room >= 1:
                cands.append(('superline_lead', int(i), d, float(room)))
    print(f'{len(cands)} candidates: ' + ', '.join(f'{t} {sum(1 for x in cands if x[0] == t)}' for t in TYPES), flush=True)
    # The general wave model's edge at each candidate (the day's walk-forward model).
    edge = np.full(len(cands), np.nan, np.float32)
    th = np.full(len(cands), np.nan, np.float32)
    by_day = defaultdict(list)
    for j, (_, i, d, _) in enumerate(cands):
        by_day[dt.datetime.fromtimestamp(A['start'][i] / 1000, dt.timezone.utc).strftime('%Y%m%d')].append(j)
    for day, js in by_day.items():
        p = os.path.join(a.signals, 'wf', day, 'wave_model.pt')
        if not os.path.exists(p):
            continue
        ck = torch.load(p, weights_only=False)
        net = TW.Body(len(ck['names']), ck['hidden'], ck['A']['net.6.weight'].shape[0])
        net.load_state_dict(ck['A'])
        net.eval()
        rows = np.array([cands[j][1] for j in js])
        with torch.no_grad():
            out = net(torch.from_numpy(np.clip((X[rows] - ck['mu']) / ck['sd'], -10, 10).astype(np.float32))).numpy()
        for k, j in enumerate(js):
            d = cands[j][2]
            edge[j] = np.sort(out[k, 2:5] if d > 0 else out[k, 5:8])[1] * 3 - A['fee_rt'][cands[j][1]]
            th[j] = ck['theta'].get('up' if d > 0 else 'down') or np.nan
    # The owner's exit for every candidate (booklead.build_game: target = Polymarket's mid moved by the room).
    oname = ['home', 'away']
    games = defaultdict(list)
    for j, (_, i, d, room) in enumerate(cands):
        p0 = 1 / (1 + np.exp(-X[i, c['x']]))
        games[int(A['game'][i])].append((j, oname[A['outcome'][i]], int(A['t'][i]), d, float(p0), float(room)))
    ex = {}
    with Pool(a.workers) as pool:
        for res in pool.imap_unordered(build_game, [(a.db, m, v) for m, v in games.items()], chunksize=4):
            for x in res:
                ex[x['j']] = x
    keep = [j for j in range(len(cands)) if j in ex]
    rows = np.array([cands[j][1] for j in keep])
    out = {'type': np.array([TYPES.index(cands[j][0]) for j in keep], np.int8), 'row': rows, 'X': X[rows], 'game': A['game'][rows].astype(np.int64),
           'outcome': A['outcome'][rows], 't': A['t'][rows], 'start': A['start'][rows], 'sport': A['sport'][rows],
           'dir': np.array([cands[j][2] for j in keep], np.int8), 'room': np.array([cands[j][3] for j in keep], np.float32),
           'edge': edge[keep], 'th': th[keep], 'spread': X[rows, c['spread']], 'names': A['names']}
    for H in HS:
        out[f'target_{H}'] = np.array([ex[j][f'target_{H}'] for j in keep], np.float32)
        out[f'hold_target_{H}'] = np.array([ex[j][f'hold_target_{H}'] for j in keep], np.int32)
    np.savez_compressed(os.path.join(a.signals, 'more_bets.npz'), **out)
    for k, t_ in enumerate(TYPES):
        v = out[EXIT][(out['type'] == k) & np.isfinite(out[EXIT])]
        print(f'  {t_:<16}{len(v):>7} candidates, your exit (10 min) {v.mean():+.2f}c a share, win {100 * np.mean(v > 0):.0f}%')


def feats(D, idx):
    names = [str(x) for x in D['names']]
    c = {k: i for i, k in enumerate(names)}
    X, d = D['X'][idx], D['dir'][idx].astype(np.float32)
    col = lambda k: X[:, c[k]]
    f = [D['room'][idx], np.nan_to_num(D['edge'][idx]), np.nan_to_num(D['edge'][idx] - D['th'][idx]), D['spread'][idx],
         col('ob_imb') * d, col('ob_dimb') * d, col('ob_on'), col('pm_r10') * d, col('pm_r30') * d, col('pm_r60') * d,
         np.where(d > 0, col('fam_up5'), col('fam_dn5')), np.where(d > 0, col('fam_r10_max'), -col('fam_r10_min')), col('fam_gap_mean') * d,
         col('fam_n'), col('ctx_pm_rate'), col('ctx_vol'), col('ctx_spread'), col('pm_quiet'), col('absx'), col('min_from_start'), col('inplay'),
         col('gap_z') * d, col('sl_r10') * d, col('sl_wave') * d, col('pm_wave') * d]
    f += [(D['type'][idx] == k).astype(np.float32) for k in range(len(TYPES))]
    f += [(D['sport'][idx] == s).astype(np.float32) for s in SPORTS]
    return np.nan_to_num(np.stack(f, 1).astype(np.float32))


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    D = dict(np.load(os.path.join(a.signals, 'more_bets.npz'), allow_pickle=False))
    B = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    kept = json.load(open(os.path.join(a.signals, 'booklead_veto_kept.json'), encoding='utf-8'))
    ok = np.isfinite(D[EXIT])
    end_t = D['t'] + 1000 * D[f'hold_{EXIT}']
    by_day = defaultdict(list)
    for j, ek, day in kept:
        by_day[day].append((j, ek))
    base_all, extra_all, extra_type, lines = [], [], [], []
    base_g, extra_g, iv = [], [], []   # game ids (clustered ranges) and (game, outcome, start, end) of every trade
    for day in sorted(by_day):
        d0 = int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        d1 = d0 + 86_400_000
        pool = np.flatnonzero(ok & (end_t < d0))
        pool = pool[np.argsort(D['t'][pool])]
        cut = int(0.8 * len(pool))
        tr, va = pool[:cut], pool[cut:]
        tr = tr[end_t[tr] < D['t'][va].min()]
        Ftr = feats(D, tr)
        mu, sd = Ftr.mean(0), Ftr.std(0)
        sd[sd < 1e-6] = 1
        Z = lambda F: ((F - mu) / sd).astype(np.float32)
        y = np.clip(D[EXIT][tr], -30, 30)
        models = {'linear': fit_ridge(Z(Ftr), y), 'network': fit_net(Z(Ftr), y, dev)}

        def run_day(idx, pred, tau, busy):
            taken = []
            for i in idx[np.argsort(D['t'][idx])]:
                k = int(D['game'][i])   # one position per game: the other outcome is the same bet mirrored
                if pred[i] < tau or any(s <= D['t'][i] < e for s, e in busy.get(k, [])):
                    continue
                busy.setdefault(k, []).append((int(D['t'][i]), int(end_t[i])))
                taken.append(i)
            return np.array(taken, np.int64)
        # threshold on validation: extras only (no rule + veto trades there), must make money
        best = None
        for kind, f in models.items():
            pv = np.full(len(D['t']), -1e9, np.float32)
            pv[va] = f(Z(feats(D, va)))
            for tau in (0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0):
                tk = run_day(va, pv, tau, {})
                tot = float(np.sum(D[EXIT][tk])) if len(tk) else 0.0
                if len(tk) >= 20 and tot > 0 and (best is None or tot > best[0]):
                    best = (tot, kind, tau, len(tk))
        # the day: rule + veto first, then extras where no position is open
        busy = {}
        base = []
        for j, ek in by_day[day]:
            base.append(float(B[ek][j]))
            base_g.append(int(B['game'][j]))
            iv.append((int(B['game'][j]), int(B['outcome'][j]), int(B['t'][j]), int(B['t'][j]) + 1000 * int(B[f'hold_{ek}'][j])))
            k = int(B['game'][j])
            busy.setdefault(k, []).append((int(B['t'][j]), int(B['t'][j]) + 1000 * int(B[f'hold_{ek}'][j])))
        base_all += base
        today = np.flatnonzero(ok & (D['t'] >= d0) & (D['t'] < d1))
        if best is not None and len(today):
            _, kind, tau, nv = best
            pv = np.full(len(D['t']), -1e9, np.float32)
            pv[today] = models[kind](Z(feats(D, today)))
            tk = run_day(today, pv, tau, busy)
            extra_all += list(D[EXIT][tk])
            extra_g += [int(g) for g in D['game'][tk]]
            iv += [(int(D['game'][i]), int(D['outcome'][i]), int(D['t'][i]), int(end_t[i])) for i in tk]
            extra_type += list(D['type'][tk])
            etxt = f'extras: {kind}, predicted >= {tau:+.1f}c (validation {nv} trades {best[0]:+.0f}c): {len(tk)} trades, {np.mean(D[EXIT][tk]) if len(tk) else float("nan"):+.2f}c'
        else:
            etxt = 'extras: none made money on validation, none taken'
        lines.append(f'{day}: rule + veto {len(base)} trades ({np.mean(base):+.2f}c); {etxt}')

    def pooled(name, v, g):
        v, g = np.array(v, dtype=float), np.array(g)
        if not len(v):
            return f'  {name:<44} no trades'
        rng = np.random.default_rng(9)
        boot = [v[rng.integers(0, len(v), len(v))].mean() for _ in range(5000)]
        ug, inv = np.unique(g, return_inverse=True)
        gs, gn = np.bincount(inv, v), np.bincount(inv)
        cb = []
        for _ in range(5000):   # resample whole games: trades in one game move together
            k = rng.integers(0, len(ug), len(ug))
            cb.append(gs[k].sum() / gn[k].sum())
        return (f'  {name:<44}{len(v):>6} trades {len(ug):>5} games  mean {v.mean():+.2f}c (90% {np.quantile(boot, 0.05):+.2f}..{np.quantile(boot, 0.95):+.2f}; '
                f'by game {np.quantile(cb, 0.05):+.2f}..{np.quantile(cb, 0.95):+.2f})  win {100 * np.mean(v > 0):.0f}%  total {v.sum():+.0f}c')
    et = np.array(extra_type)
    out = ['More-bets helper on top of rule + veto, day by day (trained and tuned only on earlier days):'] + lines + ['',
           pooled('rule + veto alone', base_all, base_g), pooled('extra trades from the helper', extra_all, extra_g)] + [
           pooled(f'   of which {t_}', list(np.array(extra_all)[et == k]), list(np.array(extra_g)[et == k])) for k, t_ in enumerate(TYPES) if np.any(et == k)] + [
           pooled('rule + veto + helper', base_all + extra_all, base_g + extra_g)]
    # positions open at the same time on BOTH outcomes of one game (home Up + away Up is a hedge, not two bets)
    by_g = defaultdict(list)
    for x in iv:
        by_g[x[0]].append(x)
    both = sum(1 for xs in by_g.values() for p in xs if any(q[1] != p[1] and q[2] < p[3] and p[2] < q[3] for q in xs))
    out.append(f'  trades overlapping a position on the other outcome of the same game: {both} of {len(iv)}')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'more_bets.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'test'])
    ap.add_argument('--db')
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=24)
    a = ap.parse_args()
    (cmd_build if a.cmd == 'build' else cmd_test)(a)


if __name__ == '__main__':
    main()
