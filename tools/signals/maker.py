"""Limit orders instead of crossing the spread (python tools/signals/maker.py build|test --db X --signals DIR).

Polymarket's sports fee (5% p(1-p), venue.rs Fee::PM_SPORTS) is TAKER ONLY: a resting limit order pays no fee and no
half-spread. Four ways to trade every candidate in money.npz (bookmaker leads at every gap, the owner's 1st drawing,
superline lead), each in dollars per $100 order with the order book's depth:
  TT  buy at the ask 1 s after the signal, sell into the bid when it reaches the target, else at H (today's way);
  TM  buy at the ask; REST a sell at the target (the bookmakers' / superline's level at the signal): filled only when
      the token's best bid goes a tick (1c) PAST it (the price traded through: our offer would have been taken first),
      no fee; not filled by H -> sell into the bid at H (fee);
  MT  REST a buy at the token's bid 1 s after the signal for up to 30 s: filled only when the best ask drops a tick
      below it (sellers crossed our level), no fee, at our price; not filled -> no trade; then sell as TT;
  MM  both.
Target hits, give-ups and collapsed tokens (no bid: first real bid within 10 min, else 0) as booklead.py. Depth caps:
taker fills as money.py; a maker fill takes at most the depth within 2c on the crossing side at that second.
TEST: (1) every candidate by kind and variant; (2) rule + veto (improve.run_day) day by day 19-23 Nov with each
variant's exits, and with all of them offered (the rule picks on earlier days); (3) the money signal finder over all
kinds with all 12 exits; flat $100 orders, ranges by game."""
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
import improve as IM  # noqa: E402
import money as MO  # noqa: E402
import more_bets as MB  # noqa: E402
import waves as W  # noqa: E402
from booklead import FILL_S, HS, fee, held  # noqa: E402
from booklead_veto import fit_ridge  # noqa: E402

VARS = ['TT', 'TM', 'MT', 'MM']
KEYS = [f'usd_{v}_{H}' for v in VARS for H in HS]
W_IN, TICK, STAKE = 30, 0.01, 100.0
DAY = 86_400_000


def fin(x):
    return float(x) if np.isfinite(x) else np.inf


def trade(buy, taker_in, s_in, cap_in, sell, taker_out, s_out, cap_out):
    """Dollars made and spent on a $100 order; taker sides pay the fee and 1c more beyond the best level."""
    unit = buy + (fee(buy) if taker_in else 0.0)
    nn = min(STAKE / unit, cap_in, cap_out)
    if taker_in:
        n1 = min(nn, s_in)
        cost = n1 * buy + (nn - n1) * (buy + 0.01) + nn * fee(buy)
    else:
        cost = nn * buy
    if taker_out:
        m1 = min(nn, s_out)
        proceeds = m1 * sell + (nn - m1) * max(sell - 0.01, 0.0) - nn * fee(sell)
    else:
        proceeds = nn * sell
    return proceeds - cost, cost


def sim_game(args):
    path, m, insts = args
    db = W.connect(path)
    by = defaultdict(list)
    for r in db.execute('SELECT ts_ms, outcome, bid, ask, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)):
        by[r[1]].append(r)
    cache, out = {}, []
    n = max(HS) + W_IN
    for j, o, t, d, p0, room in insts:
        if o not in by:
            continue
        if o not in cache:
            arr = np.array([[x[0]] + [np.nan if v is None else v for v in x[2:]] for x in by[o]], dtype=float)
            cache[o] = (arr[:, 0].astype(np.int64),) + tuple(arr[:, k] for k in range(1, 7))
        lt, lb, la, bs, as_, bd, ad = cache[o]
        secs = t + 1000 + 1000 * np.arange(0, n + FILL_S + 1)        # index 0 = 1 s after the signal
        yb, ya, ybs, yas, ybd, yad = (held(lt, v, secs) for v in (lb, la, bs, as_, bd, ad))
        if d > 0:
            tb, ta, tbs, tbd, tas, tad = yb, ya, ybs, ybd, yas, yad
        else:
            tb, ta, tbs, tbd, tas, tad = 1 - ya, 1 - yb, yas, yad, ybs, ybd
        if not np.isfinite(ta[0]) or not 0 < ta[0] < 1:
            continue
        lvl = p0 + d * room / 100
        T = lvl if d > 0 else 1 - lvl
        idx = np.where(np.isfinite(tb), np.arange(len(tb)), len(tb))
        nxt = np.minimum.accumulate(idx[::-1])[::-1]
        res = {'j': j}
        for ein in 'TM':
            if ein == 'T':
                k0, buy, s_in, cap_in = 0, float(ta[0]), fin(tas[0]), max(fin(tad[0]), fin(tas[0]))
            else:
                b0 = tb[0]
                hits = np.flatnonzero(np.nan_to_num(ta[1:W_IN + 1], nan=9.0) <= b0 - TICK + 1e-9) if np.isfinite(b0) and b0 > 0 else []
                if not len(hits):
                    continue
                k0 = int(hits[0]) + 1
                buy, s_in, cap_in = float(b0), np.inf, max(fin(tad[k0]), fin(tas[k0]))
            for eout in 'TM':
                for H in HS:
                    seg = np.nan_to_num(tb[k0 + 1:k0 + H + 1], nan=-1.0)
                    hit = np.flatnonzero(seg >= (T - 1e-9 if eout == 'T' else T + TICK - 1e-9))
                    if len(hit):
                        kx = k0 + 1 + int(hit[0])
                        if eout == 'T':
                            sell, taker_out, s_out, cap_out = float(tb[kx]), True, fin(tbs[kx]), max(fin(tbd[kx]), fin(tbs[kx]))
                        else:
                            sell, taker_out, s_out, cap_out = float(T), False, np.inf, max(fin(tbd[kx]), fin(tbs[kx]))
                    else:
                        kx = k0 + H
                        jj = nxt[kx]
                        if jj < len(tb) and jj - kx <= FILL_S:
                            sell, s_out, cap_out = float(tb[jj]), fin(tbs[jj]), max(fin(tbd[jj]), fin(tbs[jj]))
                        else:
                            sell, s_out, cap_out = 0.0, np.inf, np.inf
                        taker_out = True
                    usd, spent = trade(buy, ein == 'T', s_in, cap_in, sell, taker_out, s_out, cap_out)
                    key = f'{ein}{eout}_{H}'
                    res[f'usd_{key}'] = usd
                    res[f'spent_{key}'] = spent
                    res[f'net_{key}'] = (sell - (fee(sell) if taker_out else 0) - buy - (fee(buy) if ein == 'T' else 0)) * 100
                    res[f'hold_usd_{key}'] = kx + 1
        out.append(res)
    return out


def cmd_build(a):
    U = dict(np.load(os.path.join(a.signals, 'money.npz'), allow_pickle=False))
    names = [str(x) for x in U['names']]
    p0 = 1 / (1 + np.exp(-U['X'][:, names.index('x')]))
    oname = ['home', 'away']
    games = defaultdict(list)
    for j in range(len(U['t'])):
        games[int(U['game'][j])].append((j, oname[U['outcome'][j]], int(U['t'][j]), int(U['dir'][j]), float(p0[j]), float(U['room'][j])))
    ex = {}
    with Pool(a.workers) as pool:
        for k, res in enumerate(pool.imap_unordered(sim_game, [(a.db, m, v) for m, v in games.items()], chunksize=4)):
            for x in res:
                ex[x['j']] = x
            if (k + 1) % 1000 == 0:
                print(f'  {k + 1} of {len(games)} games', flush=True)
    n = len(U['t'])
    out = {}
    for v in VARS:
        for H in HS:
            for f in ('usd', 'spent', 'net', 'hold_usd'):
                key = f'{f}_{v}_{H}'
                out[key] = np.array([ex[j].get(key, np.nan) if j in ex else np.nan for j in range(n)], np.float32)
            out[f'hold_usd_{v}_{H}'] = np.nan_to_num(out[f'hold_usd_{v}_{H}'], nan=W_IN).astype(np.int32)
    chk = np.isfinite(out['usd_TT_300']) & np.isfinite(U['usd_300'])
    print(f'check: TT_300 matches money.py usd_300 on {np.mean(np.abs(out["usd_TT_300"][chk] - U["usd_300"][chk]) < 0.01) * 100:.1f}% of {chk.sum()}')
    np.savez_compressed(os.path.join(a.signals, 'maker.npz'), **out)


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    U = dict(np.load(os.path.join(a.signals, 'money.npz'), allow_pickle=False))
    K = dict(np.load(os.path.join(a.signals, 'maker.npz'), allow_pickle=False))
    U.update(K)
    names = [str(x) for x in U['names']]
    out = ['Limit orders vs crossing the spread, dollars per $100 order (book depth, fees on taker fills only)', '',
           '(1) Every candidate, exit window 5 min (H 300): mean $ per $100 order, win %, and for limit buys the share filled']
    for k, t_ in enumerate(MO.TYPES):
        m = U['type'] == k
        for lo, hi in ((0, 4), (4, 8), (8, 15), (15, 100)):
            mm = m & (U['room'] >= lo) & (U['room'] < hi)
            if mm.sum() < 50:
                continue
            cells = []
            for v in VARS:
                x = U[f'usd_{v}_300'][mm]
                f = np.isfinite(x)
                cells.append(f'{v} ${np.nanmean(x):+6.2f} ({np.mean(x[f] > 0) * 100:.0f}%{"" if v[0] == "T" else f", filled {f.mean() * 100:.0f}%"})')
            out.append(f'  {t_:<15} gap {lo:>2}-{hi:<3} {mm.sum():>6}   ' + '   '.join(cells))
    out.append('')
    # (2) rule + veto, clean, per variant and with all offered
    nb = int(U['n_booklead'])
    isb = U['src'] < nb
    UB = {k: (v[isb] if isinstance(v, np.ndarray) and v.ndim >= 1 and len(v) == len(U['t']) else v) for k, v in U.items()}
    runs = {v: [f'usd_{v}_{H}' for H in HS] for v in VARS}
    runs['all four'] = KEYS
    R = {k: [] for k in runs}
    R['finder'] = []
    lines = []
    day = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
    okU = np.ones(len(U['t']), bool)
    while day <= dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc):
        d0 = int(day.timestamp() * 1000)
        d1 = d0 + DAY
        txt = [f'{day:%d %b}:']
        for name, keys in runs.items():
            r = IM.run_day(UB, names, d0, d1, keys, IM.V2, dev)
            if r is None:
                txt.append(f'{name}: no rule')
                continue
            ek = r['ek']
            R[name] += [(int(UB['game'][u]), float(UB[ek][u])) for u in r['kept']]
            txt.append(f'{name}: {ek} gap>={r["R"]}{"+m" if r["nm"] else ""} veto {r["kind"]} {len(r["kept"])} (${sum(UB[ek][u] for u in r["kept"]):+.0f})')
        best, pt, today, key = finder(U, d0, d1, dev, okU)
        if best is None:
            txt.append('finder: nothing passed')
        else:
            end = U['t'] + 1000 * U[f'hold_{key}']
            cand = today[(pt >= best[3]) & np.isfinite(U[key][today])]
            tk = IM.place({'t': U['t'], 'game': U['game']}, cand, end, {})
            R['finder'] += [(int(U['game'][i]), float(U[key][i])) for i in tk]
            txt.append(f'finder: {best[1]} {key} >= ${best[3]:+.0f}: {len(tk)} (${np.sum(U[key][tk]):+.0f})')
        lines.append('  '.join(txt))
        print(lines[-1], flush=True)
        day += dt.timedelta(days=1)

    def summ(name, rows):
        if not rows:
            return f'  {name:<40} no trades'
        g = np.array([r[0] for r in rows])
        v = np.array([r[1] for r in rows])
        ug, inv = np.unique(g, return_inverse=True)
        gs = np.bincount(inv, v)
        rng = np.random.default_rng(3)
        boot = [gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(4000)]
        return f'  {name:<40}{len(v):>5} trades {len(ug):>4} games  ${v.sum():+7.0f} (90% {np.quantile(boot, 0.05):+.0f}..{np.quantile(boot, 0.95):+.0f})  ${v.mean():+.2f} a trade, win {np.mean(v > 0) * 100:.0f}%'
    out += ['(2) Rule + veto day by day 19-23 Nov, every choice on earlier days (flat $100 orders); (3) the money signal finder:'] + lines + [''] + \
        [summ(f'rule + veto, {k}' if k in VARS else ('rule + veto, the rule picks among all' if k == 'all four' else 'signal finder, all kinds, all exits'), R[k])
         for k in list(runs) + ['finder']]
    text = '\n'.join(out)
    print('\n'.join(out[-(len(runs) + 1):]))
    with open(os.path.join(a.signals, 'maker.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


def finder(U, d0, d1, dev, ok):
    endmax = U['t'] + 1000 * np.max(np.stack([U[f'hold_{k}'] for k in KEYS]), 0)
    pool = np.flatnonzero(ok & (endmax < d0))
    pool = pool[np.argsort(U['t'][pool])]
    cut = int(0.8 * len(pool))
    tr, va = pool[:cut], pool[cut:]
    tr = tr[endmax[tr] < U['t'][va].min()]
    featfn = lambda idx: IM.with_heads(MB.feats(U, idx), U['heads'][idx], U['dir'][idx].astype(np.float32))
    Z = IM.standardiser(featfn, tr)
    Ztr = Z(tr)
    Y = np.clip(np.nan_to_num(np.stack([U[k][tr] for k in KEYS], 1), nan=0.0), -60, 60)    # an unfilled limit buy = no trade = $0
    nets = [MO.fit_multi(Ztr, Y, dev, 40 + s) for s in range(3)]
    models = {'linear': fit_ridge(Ztr, Y), 'network': nets[0], 'ensemble': lambda Zx: np.mean([f(Zx) for f in nets], 0)}
    Zva = Z(va)
    today = np.flatnonzero(ok & (U['t'] >= d0) & (U['t'] < d1))
    best = None
    for kind, f in models.items():
        pv = f(Zva)
        for e, key in enumerate(KEYS):
            end = U['t'] + 1000 * U[f'hold_{key}']
            fill = np.isfinite(U[key][va])
            for tau in (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0):
                tk = IM.place({'t': U['t'], 'game': U['game']}, va[(pv[:, e] >= tau) & fill], end, {})
                if len(tk) < 30:
                    continue
                sc = MO.score(U[key][tk])
                if sc > 0 and (best is None or sc > best[0]):
                    best = (sc, kind, e, tau)
    if best is None:
        return None, None, today, None
    return best, models[best[1]](Z(today))[:, best[2]] if len(today) else np.zeros(0), today, KEYS[best[2]]


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
