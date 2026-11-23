"""What if every trade must aim for at least W cents of profit? (python tools/signals/min_win.py --signals DIR)

The bots' `min_edge` (snipe.rs Gate, adaptive.rs Policy): the expected profit per share IF Polymarket reaches the target
(catch-up 100%), after the half spread both ways, 1c exit slippage and the 5% p(1-p) taker fee on both fills:
    entry = token mid + spread/2; exit = target - spread/2 - 1c; edge = exit - entry - fee(entry) - fee(exit).
The live plan (sims.json 'snipe-plan', pinned) uses 4c. Here W = 0 / 4 / 5 / 6 / 8 / 10c on:
  1. every candidate in money.npz (bookmaker leads, the 1st drawing, superline lead): $ per $100 order with book depth,
     owner's exit at 5 and 10 min;
  2. clean rule + veto (improve.run_day) day by day 19-23 Nov on the candidates that clear W;
  3. the bookmaker jumps Polymarket had not followed (faster_events.npz), today's timing and an ideal feed.
Target: the bookmakers' / superline's level at the signal (money.npz), the bookmaker's jump (faster_events)."""
import argparse
import datetime as dt
import os
import sys

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import improve as IM  # noqa: E402
from booklead import fee  # noqa: E402

WS = (0, 4, 5, 6, 8, 10)
DAY = 86_400_000


def edge_c(mid_tok, spread_c, room_pt):
    entry = np.clip(mid_tok + spread_c / 200, 0.01, 0.99)
    exit_ = np.clip(mid_tok + room_pt / 100 - spread_c / 200 - 0.01, 0.0, 1.0)
    return (exit_ - entry - fee(entry) - fee(exit_)) * 100


def summ(v, g):
    v, g = np.asarray(v, float), np.asarray(g)
    if not len(v):
        return 'no trades'
    ug, inv = np.unique(g, return_inverse=True)
    gs = np.bincount(inv, v)
    rng = np.random.default_rng(2)
    boot = [gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(3000)]
    return f'{len(v):>6} trades  ${v.sum():+9,.0f} (90% {np.quantile(boot, 0.05):+,.0f}..{np.quantile(boot, 0.95):+,.0f})  ${v.mean():+6.2f} per $100  win {np.mean(v > 0) * 100:.0f}%'


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    U = dict(np.load(os.path.join(a.signals, 'money.npz'), allow_pickle=False))
    names = [str(x) for x in U['names']]
    p0 = 1 / (1 + np.exp(-U['X'][:, names.index('x')]))
    tok = np.where(U['dir'] > 0, p0, 1 - p0)
    E = edge_c(tok, U['spread'], U['room'])
    out = ['Minimum expected win per share (the bots\' min_edge), dollars per $100 order with book depth, fees paid', '',
           '1. Every candidate, all days (no model; one row per signal, overlaps not removed):']
    TY = ['bookmaker leads', 'your 1st drawing', 'superline lead']
    for k, t_ in enumerate(TY):
        out.append(f'   {t_}:')
        for w in WS:
            m = (U['type'] == k) & (E >= w)
            out.append(f'     W >= {w:>2}c  {m.sum():>6} signals ({m.sum() / max((U["type"] == k).sum(), 1) * 100:4.1f}%)  '
                       f'5 min ${U["usd_300"][m].mean() if m.any() else float("nan"):+6.2f}  10 min ${U["usd_600"][m].mean() if m.any() else float("nan"):+6.2f} per $100, '
                       f'win {np.mean(U["usd_600"][m] > 0) * 100 if m.any() else float("nan"):.0f}%')
    # 2. clean rule + veto per W
    out += ['', '2. Rule + veto, day by day 19-23 Nov, only signals that clear W (rule, exit and veto chosen on earlier days):']
    nb = int(U['n_booklead'])
    for w in WS:
        keep = (U['src'] < nb) & (E >= w)
        UB = {k: (v[keep] if isinstance(v, np.ndarray) and v.ndim >= 1 and len(v) == len(U['t']) else v) for k, v in U.items()}
        rows = []
        for day in range(19, 24):
            d0 = int(dt.datetime(2026, 11, day, tzinfo=dt.timezone.utc).timestamp() * 1000)
            r = IM.run_day(UB, names, d0, d0 + DAY, ['usd_180', 'usd_300', 'usd_600'], IM.V2, dev)
            if r is not None:
                rows += [(int(UB['game'][u]), float(UB[r['ek']][u])) for u in r['kept']]
        out.append(f'   W >= {w:>2}c  ' + summ([x[1] for x in rows], [x[0] for x in rows]))
        print(out[-1], flush=True)
    # 3. bookmaker jumps Polymarket had not followed
    F = dict(np.load(os.path.join(a.signals, 'faster_events.npz'), allow_pickle=False))
    out += ['', '3. Bookmaker jumps Polymarket had not followed yet (all days; sell into the bid at the bookmaker\'s level, else 5 min):']
    for k, lab in ((2, 'today (receive + 1 s)'), (0, 'ideal feed (publish + 0.25 s)')):
        el = F[f'ok{k}'] & (F[f'eligible{k}'] == 1)
        sp = F[f'spread{k}']
        mid = F[f'price{k}'] - sp / 200
        Ej = edge_c(mid, sp, F['size'])
        out.append(f'   {lab}:')
        for w in WS:
            m = el & (Ej >= w)
            out.append(f'     W >= {w:>2}c  ' + summ(F[f'usd_T{k}'][m], F['game'][m]))
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'min_win.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
