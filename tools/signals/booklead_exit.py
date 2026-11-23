"""Exit helper for the bookmaker-leads rule + veto: sell early when a trade is not going to plan.

  python tools/signals/booklead_exit.py build --db X --signals DIR [--workers 24]
  python tools/signals/booklead_exit.py test  --signals DIR

The earlier exit models lost because they learned on other trades and another plan than they were judged on. This
one learns on the same kind of trade (bookmaker-leads instances with a gap >= 4 pt) under the same plan (the owner's
exit: sell when the token reaches the bookmakers' level at entry, else at 10 minutes). Every 10 s while a position is
held: the market's inputs at that second, and the position (time held, net if sold now, best net so far, drawdown,
the gap at entry, how far the sell price still is from the bookmakers' level, the general model's edge over its
threshold at entry, the entry spread); target delta = (the plan's end) - (sell now): a continuation value.
TEST, day by day: trained on positions whose plan had ended before the day (the last 20% validate), the patience rule
(sell when the predicted delta < -m for P s, after at least M s) chosen on the validation positions with a gap >= 8 pt
(like the rule's), off for the day if none beats the plan there; then applied to exactly the trades rule + veto took
that day (booklead_veto_kept.json), against those trades with the owner's exit."""
import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

import numpy as np
import torch
from torch import nn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import features as F  # noqa: E402
import waves as W  # noqa: E402
from exit_v2 import GRID_M, GRID_MIN, GRID_P, policy_value, prep  # noqa: E402
from exit_vs_hold import Net, book_rows, fee, held, run  # noqa: E402

H, CHECK, L = 600, 10, 1
PSCALE = np.array([H, 5, 5, 5, 5, 5, 3, 2], dtype=np.float32)


def build_game(args):
    path, m, sport, start, insts = args
    db = W.connect(path)
    pm, bk = W.load(db, m)
    br = book_rows(db, m)
    by = defaultdict(list)
    for x in pm:
        by[x[2]].append(x)
    out = []
    grids = {}
    for j, o, t, d, p0, room, edge_th, spread in insts:
        if o not in grids:
            grids[o] = F.build(pm, bk, o, sport, start, sport in W.THREE_WAY, br, full=True)
        g = grids[o]
        rows = sorted(((x[0], x[3]) for x in by.get(o, [])), key=lambda r: r[0])
        rows_a = sorted(((x[0], x[4]) for x in by.get(o, [])), key=lambda r: r[0])
        if g is None or not rows:
            continue
        e = t + L * 1000
        b_e, a_e = held(rows, np.array([e]))[0], held(rows_a, np.array([e]))[0]
        buy = a_e if d > 0 else 1 - b_e
        if not np.isfinite(buy) or not 0 < buy < 1:
            continue
        secs = e + 1000 * np.arange(1, H + 1)
        hb, ha = held(rows, secs), held(rows_a, secs)
        sell = hb if d > 0 else 1 - ha
        net = (sell - fee(sell) - (buy + fee(buy))) * 100
        target = p0 + d * room / 100 if d > 0 else 1 - (p0 + d * room / 100)
        hit = np.flatnonzero(np.nan_to_num(sell, nan=-1) >= target - 1e-9)
        end = hit[0] if len(hit) else H - 1
        cont = net[end]
        if not np.isfinite(cont):
            continue
        best = -1e9
        for k in range(CHECK, end + 1, CHECK):
            now = net[k - 1]
            if not np.isfinite(now):
                continue
            best = max(best, np.nanmax(net[:k]))
            gi = int(np.searchsorted(g['ts'], e + k * 1000, side='right') - 1)
            if gi < 0 or g['ts'][gi] < e + k * 1000 - 2000:      # past the end of the feature grid: no inputs
                continue
            to_target = (target - sell[k - 1]) * 100
            out.append((j, m, e + k * 1000, e, e + (end + 1) * 1000, d, g['X'][gi],
                        np.array([k, now, best, best - now, room, to_target, edge_th, spread], dtype=np.float32), now, cont))
    return out


def cmd_build(a):
    B = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    names = [str(x) for x in B['names']]
    xi = names.index('x')
    db = W.connect(a.db)
    meta = {m: (s, st) for m, s, _, _, st in W.games(db)}
    oname = ['home', 'away']
    games = defaultdict(list)
    for j in np.flatnonzero(B['room'] >= 4):
        p0 = 1 / (1 + np.exp(-B['X'][j, xi]))
        eth = float(np.nan_to_num(B['edge'][j] - B['th'][j]))
        games[int(B['game'][j])].append((int(j), oname[B['outcome'][j]], int(B['t'][j]), int(B['dir'][j]), float(p0), float(B['room'][j]), eth, float(B['spread'][j])))
    jobs = [(a.db, m, meta[m][0], meta[m][1], v) for m, v in games.items() if m in meta]
    rows = []
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(build_game, jobs, chunksize=2)):
            rows += r
            if (k + 1) % 400 == 0:
                print(f'  {k + 1} games, {len(rows)} checks', flush=True)
    d = {'j': np.array([r[0] for r in rows], np.int64), 'game': np.array([r[1] for r in rows], np.int64), 't': np.array([r[2] for r in rows], np.int64),
         't_entry': np.array([r[3] for r in rows], np.int64), 't_end': np.array([r[4] for r in rows], np.int64), 'dir': np.array([r[5] for r in rows], np.int8),
         'X': np.stack([r[6] for r in rows]).astype(np.float32), 'pos': np.stack([r[7] for r in rows]), 'now': np.array([r[8] for r in rows], np.float32),
         'cont': np.array([r[9] for r in rows], np.float32)}
    np.savez_compressed(os.path.join(a.signals, 'booklead_exit.npz'), **d)
    print(f"{len(d['t'])} checks of {len(np.unique(d['j']))} positions; holding the plan beat selling at {100 * np.mean(d['cont'] > d['now']):.0f}% of checks")


def inputs(Z, pos, dd):
    dd = dd.astype(np.float32)[:, None]
    return np.concatenate([Z, Z * dd, pos / PSCALE, dd], 1).astype(np.float32)


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    P = dict(np.load(os.path.join(a.signals, 'booklead_exit.npz'), allow_pickle=False))
    B = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    kept = json.load(open(os.path.join(a.signals, 'booklead_veto_kept.json'), encoding='utf-8'))
    by_day = defaultdict(list)
    for j, ek, day in kept:
        by_day[day].append((j, ek))
    rows_of = defaultdict(list)
    for r, j in enumerate(P['j']):
        rows_of[int(j)].append(r)
    base_all, helper_all, sold_info, lines = [], [], [], []
    for day in sorted(by_day):
        d0 = int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        trades = by_day[day]
        before = np.flatnonzero(P['t_end'] < d0)
        order = before[np.argsort(P['t_entry'][before])]
        cut = int(0.8 * len(order))
        tr, va = order[:cut], order[cut:]
        tr = tr[P['t_end'][tr] < P['t_entry'][va].min()]
        mu, sd = P['X'][tr].mean(0), P['X'][tr].std(0)
        sd[sd < 1e-6] = 1
        Z = lambda idx: np.clip((P['X'][idx] - mu) / sd, -10, 10).astype(np.float32)
        Xtr = inputs(Z(tr), P['pos'][tr], P['dir'][tr])
        ytr = np.clip(P['cont'][tr] - P['now'][tr], -30, 30).astype(np.float32)
        torch.manual_seed(4)
        net = Net(Xtr.shape[1]).to(dev)
        opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-3)
        Xt, yt = torch.from_numpy(Xtr).to(dev), torch.from_numpy(ytr).to(dev)
        for ep in range(10):
            net.train()
            perm = torch.randperm(len(Xt), device=dev)
            for i in range(0, len(Xt), 2048):
                b = perm[i:i + 2048]
                opt.zero_grad()
                nn.functional.huber_loss(net(Xt[b]), yt[b], delta=3.0).backward()
                opt.step()
        net.eval()
        va8 = va[P['pos'][va][:, 4] >= 8]                               # validation positions like the rule's (gap >= 8)
        PD = {'game': P['game'], 't_entry': P['t_entry'], 'dir': P['dir'], 'pos': P['pos'], 'cont': P['cont'], 'now': P['now']}
        ctx = prep(PD, va8)
        pv = run(net, inputs(Z(va8), P['pos'][va8], P['dir'][va8]), dev)
        plan = policy_value(np.full(len(va8), 1e9), ctx, 0, 0, 0)
        best = (None, plan)
        for m in GRID_M:
            for M in GRID_MIN:
                for Pp in GRID_P:
                    v = policy_value(pv, ctx, m, M, Pp)
                    if v > best[1]:
                        best = ((m, M, Pp), v)
        rule = best[0]
        b_day, h_day, early = [], [], 0
        for j, ek in trades:
            base = float(B[ek][j])
            b_day.append(base)
            val = base
            rs = sorted(rows_of.get(j, []), key=lambda r: P['pos'][r, 0])
            if rule is not None and rs and ek == 'target_600':
                rs = np.array(rs)
                pr = run(net, inputs(Z(rs), P['pos'][rs], P['dir'][rs]), dev)
                m_, M_, P_ = rule
                below = None
                for q, r in enumerate(rs):
                    held_s = P['pos'][r, 0]
                    if pr[q] < -m_:
                        below = held_s if below is None else below
                        if held_s >= M_ and held_s - below >= P_:
                            val = float(P['now'][r])
                            early += 1
                            sold_info.append((held_s, base, val))
                            break
                    else:
                        below = None
            h_day.append(val)
        base_all += b_day
        helper_all += h_day
        rtxt = 'off (nothing beat the plan on validation)' if rule is None else f'sell when holding is predicted to lose over {rule[0]:.0f}c for {rule[2]} s, after {rule[1]} s (validation {best[1]:+.2f}c vs plan {plan:+.2f}c)'
        lines.append(f'{day}: {len(trades)} rule + veto trades; helper {rtxt}: sold early {early}; plan {np.mean(b_day):+.2f}c, with helper {np.mean(h_day):+.2f}c')

    def pooled(name, v):
        v = np.array(v, dtype=float)
        rng = np.random.default_rng(6)
        boot = [v[rng.integers(0, len(v), len(v))].mean() for _ in range(5000)]
        return f'  {name:<40}{len(v):>5} trades  mean {v.mean():+.2f}c (90% {np.quantile(boot, 0.05):+.2f}..{np.quantile(boot, 0.95):+.2f})  win {100 * np.mean(v > 0):.0f}%  total {v.sum():+.0f}c'
    diff = np.array(helper_all) - np.array(base_all)
    rng = np.random.default_rng(8)
    boot = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(5000)]
    out = ['Exit helper on the rule + veto trades, day by day (trained only on earlier days):'] + lines + ['',
           pooled('rule + veto, your exit', base_all), pooled('rule + veto + exit helper', helper_all),
           f'  helper minus your exit {diff.mean():+.2f}c a trade (90% {np.quantile(boot, 0.05):+.2f}..{np.quantile(boot, 0.95):+.2f}); sold early {len(sold_info)} trades'
           + (f' (median after {np.median([s[0] for s in sold_info]):.0f} s): your exit made {np.mean([s[1] for s in sold_info]):+.2f}c there, the helper {np.mean([s[2] for s in sold_info]):+.2f}c' if sold_info else '')]
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'booklead_exit.txt'), 'w', encoding='utf-8') as f:
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
