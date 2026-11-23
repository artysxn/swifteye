"""Models trained on MONEY, not cents a share (owner, 23 Nov: "train the models further, better vetos, detect more signals").

  python tools/signals/money.py build --db X --signals DIR [--workers 24]
  python tools/signals/money.py test  --signals DIR

Why: the money backtest (backtest_sized.py) showed per-share results do not turn into dollars: a $ stake buys 100x more
shares of a 1c token than of a 50c one, and the order book cannot take them on the way in or out. Every model so far
learned cents a share.

BUILD: every candidate (all bookmaker-leads instances at every gap, the owner's 1st drawing, superline taking the lead)
labelled with what a $100 order really makes under the owner's exit (target = the bookmakers' / superline's level at
entry, else at H = 180 / 300 / 600 s): buy 1 s later; shares capped by the depth within 2c at the buy AND at the sale
(pm_quotes sizes), 1c worse beyond the best level; a sale with no bid goes at the first real bid within 10 min, else
0; fees 5% p(1-p) both fills. usd_H = dollars made on the $100 order, spent_H = dollars actually spent.

TEST, day by day 19-23 Nov, every choice on earlier days only:
  A  rule + veto as today (improve.py baseline), valued in dollars;
  B  MONEY RULE + VETO: the same machinery (improve.run_day: rule grid, veto kinds incl. the general model's outputs and
     an ensemble) but every choice and every label in dollars;
  C  MONEY SIGNAL FINDER: one model over all candidate kinds (ridge, network, 3-network ensemble; three outputs, one per
     exit), threshold and exit chosen on the last 20% before the day by total minus one standard error (> 0, >= 30
     trades), one position per game;
  D  B, then C's signals in games where B holds nothing.
Flat $100 orders; ranges resample whole games."""
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
import improve as IM  # noqa: E402
import more_bets as MB  # noqa: E402
import waves as W  # noqa: E402
from booklead import FILL_S, HS, L, fee, held  # noqa: E402
from booklead_veto import fit_ridge  # noqa: E402

STAKE = 100.0
EX = [f'usd_{H}' for H in HS]
DAY = 86_400_000
TYPES = ['booklead', 'drawn', 'superline_lead']


def money_game(args):
    path, m, insts = args
    db = W.connect(path)
    rows = db.execute('SELECT ts_ms, outcome, bid, ask, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms',
                      (m,)).fetchall()
    by = defaultdict(list)
    for r in rows:
        by[r[1]].append(r)
    cache, out = {}, []
    fin = lambda x: float(x) if np.isfinite(x) else np.inf
    for j, o, t, d, p0, room in insts:
        if o not in by:
            continue
        if o not in cache:
            arr = np.array([[x[0]] + [np.nan if v is None else v for v in x[2:]] for x in by[o]], dtype=float)
            cache[o] = (arr[:, 0].astype(np.int64),) + tuple(arr[:, k] for k in range(1, 7))
        lt, lb, la, bs, as_, bd, ad = cache[o]
        at = lambda v, times: held(lt, v, np.asarray(times))
        e = t + L * 1000
        b_e, a_e = at(lb, [e])[0], at(la, [e])[0]
        buy = a_e if d > 0 else 1 - b_e
        if not np.isfinite(buy) or not 0 < buy < 1:
            continue
        s_in = fin(at(as_ if d > 0 else bs, [e])[0])
        d_in = max(fin(at(ad if d > 0 else bd, [e])[0]), s_in)
        n = max(HS)
        secs = e + 1000 * np.arange(1, n + FILL_S + 1)
        raw = at(lb, secs) if d > 0 else 1 - at(la, secs)
        idx = np.where(np.isfinite(raw), np.arange(len(raw)), len(raw))
        nxt = np.minimum.accumulate(idx[::-1])[::-1]
        lvl = p0 + d * room / 100
        target = lvl if d > 0 else 1 - lvl
        hit = np.flatnonzero(np.nan_to_num(raw[:n], nan=-1) >= target - 1e-9)
        res = {'j': j}
        for H in HS:
            k = hit[0] if len(hit) and hit[0] < H else H - 1
            jj = nxt[k]
            if jj < len(raw) and jj - k <= FILL_S:
                sell, T = float(raw[jj]), int(secs[jj])
                s_out = fin(at(bs if d > 0 else as_, [T])[0])
                d_out = max(fin(at(bd if d > 0 else ad, [T])[0]), s_out)
            else:
                sell, s_out, d_out = 0.0, np.inf, np.inf
            want = STAKE / (buy + fee(buy))
            nn_ = min(want, d_in, d_out)
            n1, m1 = min(nn_, s_in), min(nn_, s_out)
            cost = n1 * buy + (nn_ - n1) * (buy + 0.01) + nn_ * fee(buy)
            proceeds = m1 * sell + (nn_ - m1) * max(sell - 0.01, 0.0) - nn_ * fee(sell)
            res[f'usd_{H}'] = proceeds - cost
            res[f'spent_{H}'] = cost
            res[f'net_{H}'] = (sell - fee(sell) - buy - fee(buy)) * 100
            res[f'hold_usd_{H}'] = int(k + 1 + L)
        out.append(res)
    return out


def cmd_build(a):
    B = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    M = dict(np.load(os.path.join(a.signals, 'more_bets.npz'), allow_pickle=False))
    I = dict(np.load(os.path.join(a.signals, 'improve.npz'), allow_pickle=False))
    sel = np.flatnonzero(M['type'] >= 1)        # drawn, superline_lead (booklead_small = booklead rows already)
    U = {'X': np.concatenate([B['X'], M['X'][sel]]), 'heads': np.concatenate([I['heads_bl'], I['heads_mb'][sel]]),
         'type': np.concatenate([np.zeros(len(B['t']), np.int8), M['type'][sel].astype(np.int8)])}
    for k in ('game', 'outcome', 't', 'dir', 'room', 'edge', 'th', 'spread', 'sport'):
        U[k] = np.concatenate([B[k], M[k][sel]])
    U['names'] = B['names']
    names = [str(x) for x in B['names']]
    xi = names.index('x')
    p0 = 1 / (1 + np.exp(-U['X'][:, xi]))
    oname = ['home', 'away']
    games = defaultdict(list)
    for j in range(len(U['t'])):
        games[int(U['game'][j])].append((j, oname[U['outcome'][j]], int(U['t'][j]), int(U['dir'][j]), float(p0[j]), float(U['room'][j])))
    ex = {}
    with Pool(a.workers) as pool:
        for k, res in enumerate(pool.imap_unordered(money_game, [(a.db, m, v) for m, v in games.items()], chunksize=4)):
            for x in res:
                ex[x['j']] = x
            if (k + 1) % 1000 == 0:
                print(f'  {k + 1} of {len(games)} games', flush=True)
    keep = np.array(sorted(ex), np.int64)
    out = {k: (v[keep] if k != 'names' else v) for k, v in U.items()}
    for H in HS:
        for f in ('usd', 'spent', 'net', 'hold_usd'):
            key = f'{f}_{H}'
            out[key] = np.array([ex[j][key] for j in keep], np.int32 if f == 'hold_usd' else np.float32)
    out['src'] = keep            # index into the booklead.npz rows (first len(B) entries) or the appended candidates
    out['n_booklead'] = np.int64(len(B['t']))
    np.savez_compressed(os.path.join(a.signals, 'money.npz'), **out)
    print(f'{len(keep)} candidates labelled')
    for k, t_ in enumerate(TYPES):
        m = out['type'] == k
        for H in HS:
            v = out[f'usd_{H}'][m]
            print(f'  {t_:<15} H {H:>3}: {m.sum():>6}, ${v.mean():+.2f} per $100 order (mean spent ${out[f"spent_{H}"][m].mean():.0f}), win {np.mean(v > 0) * 100:.0f}%')


# ------------------------------------------------------------------ test
class Multi(nn.Module):
    def __init__(self, f, k):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(f, 64), nn.GELU(), nn.Dropout(0.3), nn.Linear(64, 64), nn.GELU(), nn.Dropout(0.3), nn.Linear(64, k))

    def forward(self, x):
        return self.net(x)


def fit_multi(X, Y, dev, seed):
    torch.manual_seed(seed)
    m = Multi(X.shape[1], Y.shape[1]).to(dev)
    opt = torch.optim.AdamW(m.parameters(), lr=2e-3, weight_decay=1e-2)
    Xt, Yt = torch.from_numpy(X).to(dev), torch.from_numpy(Y.astype(np.float32)).to(dev)
    for ep in range(20):
        m.train()
        perm = torch.randperm(len(Xt), device=dev)
        for i in range(0, len(Xt), 1024):
            b = perm[i:i + 1024]
            opt.zero_grad()
            nn.functional.huber_loss(m(Xt[b]), Yt[b], delta=5.0).backward()
            opt.step()
    m.eval()

    def pred(Z):
        with torch.no_grad():
            return np.concatenate([m(torch.from_numpy(Z[i:i + 200_000]).to(dev)).cpu().numpy() for i in range(0, len(Z), 200_000)])
    return pred


def score(v):
    """Total minus one standard error of the total: a cautious pick on validation."""
    v = np.asarray(v, float)
    return v.sum() - v.std() * np.sqrt(len(v)) if len(v) > 1 else -np.inf


def finder_day(U, d0, d1, dev, ok):
    """C: one model over every candidate kind; returns (chosen, predictions for today's rows, today's rows)."""
    endmax = U['t'] + 1000 * np.max(np.stack([U[f'hold_{k}'] for k in EX]), 0)
    pool = np.flatnonzero(ok & (endmax < d0))
    pool = pool[np.argsort(U['t'][pool])]
    cut = int(0.8 * len(pool))
    tr, va = pool[:cut], pool[cut:]
    tr = tr[endmax[tr] < U['t'][va].min()]
    featfn = lambda idx: IM.with_heads(MB.feats(U, idx), U['heads'][idx], U['dir'][idx].astype(np.float32))
    Z = IM.standardiser(featfn, tr)
    Ztr = Z(tr)
    Y = np.clip(np.stack([U[k][tr] for k in EX], 1), -60, 60)
    ridge = fit_ridge(Ztr, Y)
    nets = [fit_multi(Ztr, Y, dev, 20 + s) for s in range(3)]
    models = {'linear': ridge, 'network': nets[0], 'ensemble': lambda Zx: np.mean([f(Zx) for f in nets], 0)}
    Zva = Z(va)
    today = np.flatnonzero(ok & (U['t'] >= d0) & (U['t'] < d1))
    Ztd = Z(today) if len(today) else np.zeros((0, Ztr.shape[1]), np.float32)
    best = None
    for kind, f in models.items():
        pv = f(Zva)
        for e, key in enumerate(EX):
            end = U['t'] + 1000 * U[f'hold_{key}']
            for tau in (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0):
                tk = IM.place({'t': U['t'], 'game': U['game']}, va[pv[:, e] >= tau], end, {})
                if len(tk) < 30:
                    continue
                sc = score(U[key][tk])
                if sc > 0 and (best is None or sc > best[0]):
                    best = (sc, kind, e, tau, len(tk), float(U[key][tk].sum()))
    if best is None:
        return None, None, today
    return best, models[best[1]](Ztd)[:, best[2]] if len(today) else np.zeros(0), today


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    U = dict(np.load(os.path.join(a.signals, 'money.npz'), allow_pickle=False))
    names = [str(x) for x in U['names']]
    nb = int(U['n_booklead'])
    isb = U['src'] < nb
    UB = {k: (v[isb] if isinstance(v, np.ndarray) and v.ndim >= 1 and len(v) == len(U['t']) else v) for k, v in U.items()}
    ub_of_src = {int(s): i for i, s in enumerate(UB['src'])}
    ok = np.all(np.stack([np.isfinite(U[k]) for k in EX]), 0)
    picks = json.load(open(os.path.join(a.signals, 'improve_trades.json'), encoding='utf-8'))['base']
    R = {k: [] for k in ('A', 'B', 'C', 'D_extra')}
    lines = []
    day = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
    while day <= dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc):
        d0 = int(day.timestamp() * 1000)
        d1 = d0 + DAY
        txt = [f'{day:%d %b}:']
        # A: today's rule + veto, in dollars
        nA = 0
        for i, ek, w, dd in picks:
            if dd == f'{day:%Y-%m-%d}' and i in ub_of_src and ek.startswith('target_'):
                u = ub_of_src[i]
                key = 'usd_' + ek.split('_')[1]
                R['A'].append((int(UB['game'][u]), float(UB[key][u]), float(UB['spent_' + ek.split('_')[1]][u])))
                nA += 1
        txt.append(f'A {nA}')
        # B: money rule + veto
        r = IM.run_day(UB, names, d0, d1, EX, IM.V2, dev)
        busy = {}
        if r is None:
            txt.append('B: no rule made money before')
        else:
            ek = r['ek']
            H = ek.split('_')[1]
            for u in r['kept']:
                R['B'].append((int(UB['game'][u]), float(UB[ek][u]), float(UB['spent_' + H][u])))
                busy.setdefault(int(UB['game'][u]), []).append((int(UB['t'][u]), int(UB['t'][u]) + 1000 * int(UB[f'hold_{ek}'][u])))
            txt.append(f'B gap>={r["R"]}{"+model" if r["nm"] else ""} {ek} veto {r["kind"]}: {len(r["kept"])} (${sum(UB[ek][u] for u in r["kept"]):+.0f})')
        # C: money signal finder over every kind
        best, pt, today = finder_day(U, d0, d1, dev, ok)
        if best is None:
            txt.append('C: nothing passed on validation')
        else:
            _, kind, e, tau, nv, totv = best
            key = EX[e]
            end = U['t'] + 1000 * U[f'hold_{key}']
            cand = today[pt >= tau]
            for name_, b0 in (('C', {}), ('D_extra', busy)):
                tk = IM.place({'t': U['t'], 'game': U['game']}, cand, end, {g: list(v) for g, v in b0.items()})
                R[name_] += [(int(U['game'][i]), float(U[key][i]), float(U['spent_' + key.split('_')[1]][i]), int(U['type'][i])) for i in tk]
                if name_ == 'C':
                    txt.append(f'C {kind} {key} >= ${tau:+.0f} (val {nv} trades ${totv:+.0f}): {len(tk)} (${sum(U[key][tk]):+.0f})')
        lines.append('  '.join(txt))
        print(lines[-1], flush=True)
        day += dt.timedelta(days=1)

    def summ(name, rows):
        if not rows:
            return f'  {name:<44} no trades'
        g = np.array([r[0] for r in rows])
        v = np.array([r[1] for r in rows])
        sp = np.array([r[2] for r in rows])
        ug, inv = np.unique(g, return_inverse=True)
        gs = np.bincount(inv, v)
        rng = np.random.default_rng(3)
        boot = [gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(4000)]
        return (f'  {name:<44}{len(v):>5} trades {len(ug):>4} games  ${v.sum():+8.0f} (90% {np.quantile(boot, 0.05):+.0f}..{np.quantile(boot, 0.95):+.0f})  '
                f'${v.mean():+.2f} a trade, spent ${sp.mean():.0f} of $100 on average, win {np.mean(v > 0) * 100:.0f}%')
    D_all = R['B'] + [r[:3] for r in R['D_extra']]
    out = ['Money-trained models, day by day (19-23 Nov; every choice on earlier days only; flat $100 orders, book-depth fills both sides):', ''] + lines + ['',
           summ('A  rule + veto as today', R['A']), summ('B  money rule + veto', R['B']), summ('C  money signal finder (all kinds)', [r[:3] for r in R['C']])] + [
           summ(f'     of which {t_}', [r[:3] for r in R['C'] if r[3] == k]) for k, t_ in enumerate(TYPES) if any(r[3] == k for r in R['C'])] + [
           summ('D  B + C in games B leaves free', D_all), summ('     the extra C trades', [r[:3] for r in R['D_extra']])]
    text = '\n'.join(out)
    print('\n'.join(out[len(lines) + 2:]))
    with open(os.path.join(a.signals, 'money.txt'), 'w', encoding='utf-8') as f:
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
