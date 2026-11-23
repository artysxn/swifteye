"""The bookmaker-leads trade (the owner's second drawing): build, a clean day-by-day test, and a dedicated model.

  python tools/signals/booklead.py build --db X --signals DIR [--workers 24]
  python tools/signals/booklead.py test  --signals DIR

BUILD: every instance of pattern_catchup.py --pattern booklead (a bookmaker family jumped, Polymarket has not followed
and sits on the wrong side) with the owner's exit: buy 1 s later (Yes at the ask, or No at 1 - bid), sell as soon as
the token's sell price reaches the bookmakers' level at the entry (Polymarket caught up), else at H seconds, for
H = 180 / 300 / 600; fees 5% p(1-p) each side. The plain exit (+3c net, else 180 s) is kept to compare.

TEST, for each day D from --first: everything is chosen on instances whose trade had ended before D, then applied to
D; one position at a time per game and outcome (an instance while one is open is skipped).
  RULE: trade when the gap (room to the bookmakers) >= R and, optionally, the general wave model's edge is over its
        threshold; R, the model requirement and the exit (H) chosen to maximise the total on the days before D.
  MODEL: a network trained only on these instances (before D, purged) predicts the net of each exit from the
        features at the second (the 99 inputs, as they are and mirrored by the direction, the room, the general
        model's edge); trade when the best exit's prediction >= tau, tau chosen on the last 20% of instances before D.
Pooled over the days, with a bootstrap range on the mean."""
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
import waves as W  # noqa: E402

HS = (180, 300, 600)
L = 1
EXITS = ['target_180', 'target_300', 'target_600', 'plain']


def fee(p):
    return 0.05 * p * (1 - p)


def held(lt, lv, times):
    k = np.searchsorted(lt, times, side='right') - 1
    return np.where(k >= 0, lv[np.clip(k, 0, None)], np.nan)


FILL_S = 600


def sales(lt, lb, la, d, e, n):
    """The token's sale price at e+1..e+n s: raw (NaN while nobody bids) and filled. A missing bid is almost always a
    token that collapsed to ~0 (checked 23 Nov: 280 of 287 sampled), so a planned sale with no bid happens at the first
    real bid within FILL_S s, else at 0. Before 23 Nov such trades were DROPPED, which hid losers (10% of gap >= 8
    trades at a 10-min exit, 22% at 20 min)."""
    secs = e + 1000 * np.arange(1, n + FILL_S + 1)
    hb, ha = held(lt, lb, secs), held(lt, la, secs)
    raw = hb if d > 0 else 1 - ha
    idx = np.where(np.isfinite(raw), np.arange(len(raw)), len(raw))
    nxt = np.minimum.accumulate(idx[::-1])[::-1][:n]
    ok = (nxt < len(raw)) & (nxt - np.arange(n) <= FILL_S)
    return raw[:n], np.where(ok, raw[np.minimum(nxt, len(raw) - 1)], 0.0)


def build_game(args):
    path, m, insts = args
    db = W.connect(path)
    rows = db.execute('SELECT ts_ms, outcome, bid, ask FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)).fetchall()
    by = defaultdict(list)
    for t, o, b, a in rows:
        by[o].append((t, np.nan if b is None else b, np.nan if a is None else a))
    out = []
    for j, o, t, d, p0, room in insts:
        r = by.get(o)
        if not r:
            continue
        lt = np.array([x[0] for x in r], dtype=np.int64)
        lb = np.array([x[1] for x in r], dtype=float)
        la = np.array([x[2] for x in r], dtype=float)
        e = t + L * 1000
        b_e, a_e = held(lt, lb, np.array([e]))[0], held(lt, la, np.array([e]))[0]
        buy = a_e if d > 0 else 1 - b_e
        if not np.isfinite(buy) or not 0 < buy < 1:
            continue
        raw, sell = sales(lt, lb, la, d, e, max(HS))
        cost = buy + fee(buy)
        net = (sell - fee(sell) - cost) * 100
        target = p0 + d * room / 100 if d > 0 else 1 - (p0 + d * room / 100)   # the token's sell price at the bookmakers' level
        hit = np.flatnonzero(np.nan_to_num(raw, nan=-1) >= target - 1e-9)   # only a real bid reaches the target
        res = {'j': j}
        for H in HS:
            k = hit[0] if len(hit) and hit[0] < H else H - 1
            res[f'target_{H}'] = float(net[k]) if np.isfinite(net[k]) else np.nan
            res[f'hold_target_{H}'] = int(k + 1 + L)
        res['caught_up_600'] = bool(len(hit) and hit[0] < 600)
        out.append(res)
    return out


def cmd_build(a):
    R = json.load(open(os.path.join(a.signals, 'pattern_booklead.json'), encoding='utf-8'))
    A = np.load(os.path.join(a.signals, 'waves_A.npz'), allow_pickle=False)
    X = A['X']
    names = [str(x) for x in A['names']]
    xi = names.index('x')
    oname = ['home', 'away']
    games = defaultdict(list)
    for j, r in enumerate(R):
        p0 = 1 / (1 + np.exp(-X[r['i'], xi]))
        games[r['game']].append((j, oname[r['outcome']], r['t'], r['d'], float(p0), r['room']))
    ex = {}
    with Pool(a.workers) as pool:
        for k, res in enumerate(pool.imap_unordered(build_game, [(a.db, m, v) for m, v in games.items()], chunksize=4)):
            for x in res:
                ex[x['j']] = x
            if (k + 1) % 500 == 0:
                print(f'  {k + 1} games', flush=True)
    keep = [j for j in range(len(R)) if j in ex]
    d = {'X': X[[R[j]['i'] for j in keep]].astype(np.float32),
         'game': np.array([R[j]['game'] for j in keep], np.int64), 'outcome': np.array([R[j]['outcome'] for j in keep], np.int8),
         't': np.array([R[j]['t'] for j in keep], np.int64), 'dir': np.array([R[j]['d'] for j in keep], np.int8),
         'room': np.array([R[j]['room'] for j in keep], np.float32), 'edge': np.array([R[j]['edge'] for j in keep], np.float32),
         'th': np.array([np.nan if R[j]['th'] is None else R[j]['th'] for j in keep], np.float32),
         'spread': np.array([R[j]['spread'] for j in keep], np.float32), 'sport': np.array([R[j]['sport'] for j in keep]),
         'plain': np.array([R[j]['base'] for j in keep], np.float32), 'hold_plain': np.full(len(keep), 181, np.int32),
         'caught_up_600': np.array([ex[j]['caught_up_600'] for j in keep], bool)}
    for H in HS:
        d[f'target_{H}'] = np.array([ex[j][f'target_{H}'] for j in keep], np.float32)
        d[f'hold_target_{H}'] = np.array([ex[j][f'hold_target_{H}'] for j in keep], np.int32)
    d['names'] = A['names']
    np.savez_compressed(os.path.join(a.signals, 'booklead.npz'), **d)
    print(f"{len(keep)} instances; Polymarket caught up with the bookmakers within 10 min in {100 * d['caught_up_600'].mean():.0f}%")
    for k in EXITS:
        v = d[k][np.isfinite(d[k])]
        print(f'  exit {k:<11} mean {v.mean():+.2f}c a share, win {100 * np.mean(v > 0):.0f}%')


def take(D, idx, mask, exit_key):
    """One position at a time per (game, outcome): the instances (in idx, where mask) actually traded; their nets."""
    idx = idx[mask]
    idx = idx[np.lexsort((D['t'][idx], D['outcome'][idx], D['game'][idx]))]
    hold_key = 'hold_plain' if exit_key == 'plain' else f'hold_{exit_key}'
    busy, out = {}, []
    for i in idx:
        k = (int(D['game'][i]), int(D['outcome'][i]))
        if D['t'][i] < busy.get(k, -1) or not np.isfinite(D[exit_key][i]):
            continue
        busy[k] = int(D['t'][i]) + int(D[hold_key][i]) * 1000
        out.append(i)
    return np.array(out, dtype=np.int64)


class Net(nn.Module):
    def __init__(self, f, h=128, out=4):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(f, h), nn.GELU(), nn.Dropout(0.2), nn.Linear(h, h), nn.GELU(), nn.Dropout(0.2), nn.Linear(h, out))

    def forward(self, x):
        return self.net(x)


def inputs(D, idx, mu, sd):
    Z = np.clip((D['X'][idx] - mu) / sd, -10, 10)
    dd = D['dir'][idx].astype(np.float32)[:, None]
    extra = np.stack([D['room'][idx] / 5, np.nan_to_num(D['edge'][idx]) / 3, np.nan_to_num(D['edge'][idx] - D['th'][idx]) / 3, D['spread'][idx] / 2], 1)
    return np.concatenate([Z, Z * dd, extra, dd], 1).astype(np.float32)


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    D = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    n = len(D['t'])
    end_t = {k: D['t'] + 1000 * (D['hold_plain'] if k == 'plain' else D[f'hold_{k}']) for k in EXITS}
    day = dt.datetime.fromisoformat(a.first).replace(tzinfo=dt.timezone.utc)
    last = dt.datetime.fromisoformat(a.last).replace(tzinfo=dt.timezone.utc)
    rule_rows, model_rows, all_rows, lines = [], [], [], []
    while day <= last:
        d0, d1 = int(day.timestamp() * 1000), int((day + dt.timedelta(days=1)).timestamp() * 1000)
        today = np.flatnonzero((D['t'] >= d0) & (D['t'] < d1))
        if not len(today):
            day += dt.timedelta(days=1)
            continue
        # ---- the rule, chosen on what had ended before D
        best = None
        for ek in EXITS:
            before = np.flatnonzero(end_t[ek] < d0)
            for R in (3, 4, 5, 6, 8, 10):
                for need_model in (False, True):
                    mk = (D['room'][before] >= R) & ((D['edge'][before] >= D['th'][before]) if need_model else True)
                    tk = take(D, before, mk, ek)
                    if len(tk) < 30:
                        continue
                    tot = float(np.sum(D[ek][tk]))
                    if best is None or tot > best[0]:
                        best = (tot, ek, R, need_model, len(tk))
        if best is not None and best[0] > 0:
            _, ek, R, nm = best[:4]
            mk = (D['room'][today] >= R) & ((D['edge'][today] >= D['th'][today]) if nm else True)
            tk = take(D, today, mk, ek)
            rule_rows += [(int(i), float(D[ek][i])) for i in tk]
            rule_txt = f'rule room >= {R} pt{" + model OK" if nm else ""}, exit {ek} (chosen on {best[4]} earlier trades, {best[0]:+.0f}c): {len(tk)} trades, {np.mean(D[ek][tk]) if len(tk) else float("nan"):+.2f}c'
        else:
            rule_txt = 'rule: nothing made money before this day, no trades'
        # everything taken (as a floor): every instance, best-known exit target_300
        tk_all = take(D, today, np.ones(len(today), bool), 'target_300')
        all_rows += [(int(i), float(D['target_300'][i])) for i in tk_all]
        # ---- the dedicated model
        before = np.flatnonzero(np.max(np.stack([end_t[k] for k in EXITS]), 0) < d0)
        if len(before) < 2000:
            lines.append(f'{day:%d %b}: {rule_txt}; model: too few earlier instances ({len(before)})')
            day += dt.timedelta(days=1)
            continue
        order = before[np.argsort(D['t'][before])]
        cut = int(0.8 * len(order))
        tr, va = order[:cut], order[cut:]
        tr = tr[D['t'][tr] + 600_000 < D['t'][va].min()]          # purge: training trades end before validation
        mu, sd = D['X'][tr].mean(0), D['X'][tr].std(0)
        sd[sd < 1e-6] = 1
        Y = np.stack([np.clip(np.nan_to_num(D[k], nan=-5.0), -30, 30) for k in EXITS], 1).astype(np.float32)
        torch.manual_seed(1)
        net = Net(inputs(D, tr[:1], mu, sd).shape[1]).to(dev)
        opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-3)
        Xt, Yt = torch.from_numpy(inputs(D, tr, mu, sd)).to(dev), torch.from_numpy(Y[tr]).to(dev)
        for ep in range(25):
            net.train()
            perm = torch.randperm(len(Xt), device=dev)
            for i in range(0, len(Xt), 1024):
                b = perm[i:i + 1024]
                opt.zero_grad()
                nn.functional.huber_loss(net(Xt[b]), Yt[b], delta=3.0).backward()
                opt.step()
        net.eval()
        with torch.no_grad():
            pv = net(torch.from_numpy(inputs(D, va, mu, sd)).to(dev)).cpu().numpy()
            pt = net(torch.from_numpy(inputs(D, today, mu, sd)).to(dev)).cpu().numpy()
        bm = None
        for k, ek in enumerate(EXITS):
            for tau in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0):
                tk = take(D, va, pv[:, k] >= tau, ek)
                if len(tk) < 20:
                    continue
                tot = float(np.sum(D[ek][tk]))
                if bm is None or tot > bm[0]:
                    bm = (tot, k, ek, tau, len(tk))
        if bm is not None and bm[0] > 0:
            _, k, ek, tau = bm[:4]
            tk = take(D, today, pt[:, k] >= tau, ek)
            model_rows += [(int(i), float(D[ek][i])) for i in tk]
            model_txt = f'model: predicted {ek} >= {tau:+.1f}c (validation {bm[4]} trades {bm[0]:+.0f}c): {len(tk)} trades, {np.mean(D[ek][tk]) if len(tk) else float("nan"):+.2f}c'
        else:
            model_txt = 'model: no threshold made money on validation, no trades'
        lines.append(f'{day:%d %b}: {len(today)} instances; {rule_txt}; {model_txt}')
        day += dt.timedelta(days=1)

    def pooled(name, rows):
        if not rows:
            return f'  {name:<46} no trades'
        v = np.array([x[1] for x in rows])
        rng = np.random.default_rng(4)
        boot = [v[rng.integers(0, len(v), len(v))].mean() for _ in range(5000)]
        days = (D['t'][rows[-1][0]] - D['t'][rows[0][0]]) / 86_400_000 if len(rows) > 1 else 1
        return (f'  {name:<46}{len(v):>6} trades  mean {v.mean():+.2f}c a share (90% {np.quantile(boot, 0.05):+.2f}..{np.quantile(boot, 0.95):+.2f})  '
                f'win {100 * np.mean(v > 0):.0f}%  total {v.sum():+.0f}c')
    out = ['Bookmaker-leads trade, day by day: every choice made only on earlier days, applied to the next day.'] + lines + [
        '', 'Pooled over the test days:', pooled('every instance (your exit, 5 min)', all_rows), pooled('the rule (gap / model OK / exit chosen before)', rule_rows),
        pooled('the dedicated model', model_rows)]
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'booklead_test.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'test'])
    ap.add_argument('--db')
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=24)
    ap.add_argument('--first', default='2026-11-19')
    ap.add_argument('--last', default='2026-11-23')
    a = ap.parse_args()
    (cmd_build if a.cmd == 'build' else cmd_test)(a)


if __name__ == '__main__':
    main()
