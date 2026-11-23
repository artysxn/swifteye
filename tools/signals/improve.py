"""Can rule + veto be trained to be better? Four levers, each tested clean day by day (19-23 Nov).

  python tools/signals/improve.py build --db X --signals DIR [--workers 24]
  python tools/signals/improve.py test  --signals DIR

BUILD
  1. More exits for every bookmaker-leads instance: sell when the token's sell price gets a fraction f of the way to
     the bookmakers' level at entry (f = 50 / 75 / 100 / 150%), else at H (300 / 600 / 1200 s). f100 with H 600 is
     the owner's exit (target_600), a check.
  2. The general wave model's 16 outputs at every instance and every more-bets candidate, each from the model trained
     on games that kicked off before that game's day (clean stacking: never a model that saw the game).

TEST, per day D, every choice made on what had ended before D (as booklead_veto.py):
  baseline   rule + veto exactly as booklead_veto.py (must reproduce its 195 trades);
  sizing     the same trades, stake proportional to the veto's predicted net (fractional Kelly: mean 1 on
             validation, 0.25x..3x), used on a day only if it beat equal stakes on the validation trades;
  exit grid  the rule may also pick any of the 12 new exits (on the same earlier days as before);
  veto v2    the veto may also use the general model's 16 outputs and a 5-network ensemble; the kind is still
             chosen on validation, so it falls back to the old veto when the new inputs do not help there;
  helper v2  the more-bets helper with a higher bar: a threshold per candidate type, taken only if its validation
             trades averaged >= +1c a share (room for live slippage), with the same new inputs;
  all        exit grid + veto v2 + sizing, then helper v2.
Ranges resample whole games; "vs baseline" is the paired difference of totals at the same money staked."""
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
import more_bets as MB  # noqa: E402
import train_waves as TW  # noqa: E402
import waves as W  # noqa: E402
from booklead import EXITS, L, fee, held, sales, take  # noqa: E402
from booklead_veto import Small, fit_net, fit_ridge  # noqa: E402
from booklead_veto import feats as feats1  # noqa: E402

FR = (50, 75, 100, 150)
HX = (300, 600, 1200)
NEW = [f'f{f}_{h}' for f in FR for h in HX]
DAY = 86_400_000


def hk(k):
    return 'hold_plain' if k == 'plain' else f'hold_{k}'


# ---------------------------------------------------------------- build
def exits_game(args):
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
        raw, sell = sales(lt, lb, la, d, e, max(HX))
        net = (sell - fee(sell) - (buy + fee(buy))) * 100
        res = {'j': j}
        for f in FR:
            lvl = p0 + d * f / 100 * room / 100
            target = lvl if d > 0 else 1 - lvl
            hit = np.flatnonzero(np.nan_to_num(raw, nan=-1) >= target - 1e-9)
            for H in HX:
                k = hit[0] if len(hit) and hit[0] < H else H - 1
                res[f'f{f}_{H}'] = float(net[k]) if np.isfinite(net[k]) else np.nan
                res[f'hold_f{f}_{H}'] = int(k + 1 + L)
        out.append(res)
    return out


def heads_for(signals, X, start):
    out = None
    dn = (start // DAY).astype(np.int64)
    for d in np.unique(dn):
        day = dt.datetime.fromtimestamp(int(d) * 86400, dt.timezone.utc).strftime('%Y%m%d')
        p = os.path.join(signals, 'wf', day, 'wave_model.pt')
        if not os.path.exists(p):
            continue
        ck = torch.load(p, weights_only=False)
        k = ck['A']['net.6.weight'].shape[0]
        if out is None:
            out = np.full((len(X), k), np.nan, np.float32)
        net = TW.Body(len(ck['names']), ck['hidden'], k)
        net.load_state_dict(ck['A'])
        net.eval()
        rows = np.flatnonzero(dn == d)
        for i in range(0, len(rows), 100_000):
            r = rows[i:i + 100_000]
            with torch.no_grad():
                out[r] = net(torch.from_numpy(np.clip((X[r] - ck['mu']) / ck['sd'], -10, 10).astype(np.float32))).numpy()
    return out


def cmd_build(a):
    B = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    M = dict(np.load(os.path.join(a.signals, 'more_bets.npz'), allow_pickle=False))
    A = np.load(os.path.join(a.signals, 'waves_A.npz'), allow_pickle=False)
    g, s = A['game'], A['start']
    ug, first = np.unique(g, return_index=True)
    kick = dict(zip(ug.tolist(), s[first].tolist()))
    names = [str(x) for x in B['names']]
    xi = names.index('x')
    oname = ['home', 'away']
    games = defaultdict(list)
    for j in range(len(B['t'])):
        p0 = 1 / (1 + np.exp(-B['X'][j, xi]))
        games[int(B['game'][j])].append((j, oname[B['outcome'][j]], int(B['t'][j]), int(B['dir'][j]), float(p0), float(B['room'][j])))
    ex = {}
    with Pool(a.workers) as pool:
        for k, res in enumerate(pool.imap_unordered(exits_game, [(a.db, m, v) for m, v in games.items()], chunksize=4)):
            for x in res:
                ex[x['j']] = x
            if (k + 1) % 1000 == 0:
                print(f'  {k + 1} games', flush=True)
    n = len(B['t'])
    out = {}
    for key in NEW:
        out[key] = np.array([ex[j][key] if j in ex else np.nan for j in range(n)], np.float32)
        out[hk(key)] = np.array([ex[j][hk(key)] if j in ex else 600 for j in range(n)], np.int32)
    chk = np.isfinite(out['f100_600']) & np.isfinite(B['target_600'])
    print(f'check: f100_600 equals target_600 on {np.mean(np.abs(out["f100_600"][chk] - B["target_600"][chk]) < 1e-3) * 100:.1f}% of {chk.sum()} instances')
    bstart = np.array([kick.get(int(x), 0) for x in B['game']], np.int64)
    out['heads_bl'] = heads_for(a.signals, B['X'], bstart)
    out['heads_mb'] = heads_for(a.signals, M['X'], M['start'].astype(np.int64))
    np.savez_compressed(os.path.join(a.signals, 'improve.npz'), **out)
    for key in NEW:
        v = out[key][np.isfinite(out[key])]
        print(f'  exit {key:<10} all instances {v.mean():+.2f}c a share, win {100 * np.mean(v > 0):.0f}%')
    print(f"general model outputs: {np.isfinite(out['heads_bl'][:, 0]).mean() * 100:.0f}% of instances, {np.isfinite(out['heads_mb'][:, 0]).mean() * 100:.0f}% of candidates")


# ---------------------------------------------------------------- models
def fit_net_s(X, y, dev, seed):
    torch.manual_seed(seed)
    m = Small(X.shape[1]).to(dev)
    opt = torch.optim.AdamW(m.parameters(), lr=2e-3, weight_decay=1e-2)
    Xt, yt = torch.from_numpy(X).to(dev), torch.from_numpy(y.astype(np.float32)).to(dev)
    for ep in range(30):
        m.train()
        perm = torch.randperm(len(Xt), device=dev)
        for i in range(0, len(Xt), 512):
            b = perm[i:i + 512]
            opt.zero_grad()
            nn.functional.huber_loss(m(Xt[b]), yt[b], delta=3.0).backward()
            opt.step()
    m.eval()

    def pred(Z):
        with torch.no_grad():
            return m(torch.from_numpy(Z).to(dev)).cpu().numpy()
    return pred


def fit_ens(X, y, dev, n=5):
    ps = [fit_net_s(X, y, dev, 100 + s) for s in range(n)]
    return lambda Z: np.mean([p(Z) for p in ps], 0)


def fit(how, Z, y, dev):
    return fit_ridge(Z, y) if how == 'ridge' else fit_net(Z, y, dev) if how == 'net' else fit_ens(Z, y, dev)


def with_heads(F, H, d):
    h = np.nan_to_num(H)
    ok = np.isfinite(H[:, :1]).astype(np.float32)
    return np.concatenate([F, h, h * d[:, None], ok], 1).astype(np.float32)


V1 = {'linear': ('f1', 'ridge'), 'network': ('f1', 'net')}
V2 = dict(V1, **{'linear+': ('f2', 'ridge'), 'ensemble+': ('f2', 'ens')})


def standardiser(featfn, tr):
    F = featfn(tr)
    mu, sd = F.mean(0), F.std(0)
    sd[sd < 1e-6] = 1
    return lambda idx: ((featfn(idx) - mu) / sd).astype(np.float32)


# ---------------------------------------------------------------- rule + veto for one day
def run_day(D, names, d0, d1, exits, kinds, dev):
    today = np.flatnonzero((D['t'] >= d0) & (D['t'] < d1))
    end_t = {k: D['t'] + 1000 * D[hk(k)] for k in exits}
    best = None
    for ek in exits:
        before = np.flatnonzero(end_t[ek] < d0)
        for R in (3, 4, 5, 6, 8, 10):
            for nm in (False, True):
                mk = (D['room'][before] >= R) & ((D['edge'][before] >= D['th'][before]) if nm else True)
                tk = take(D, before, mk, ek)
                if len(tk) >= 30:
                    tot = float(np.sum(D[ek][tk]))
                    if best is None or tot > best[0]:
                        best = (tot, ek, R, nm)
    if best is None or best[0] <= 0 or not len(today):
        return None
    _, ek, R, nm = best
    rule_mask = lambda idx: (D['room'][idx] >= R) & ((D['edge'][idx] >= D['th'][idx]) if nm else True)
    pool = np.flatnonzero((end_t[ek] < d0) & (D['room'] >= 4) & np.isfinite(D[ek]))
    pool = pool[np.argsort(D['t'][pool])]
    cut = int(0.8 * len(pool))
    tr, va = pool[:cut], pool[cut:]
    tr = tr[end_t[ek][tr] < D['t'][va].min()]
    fsets = {'f1': lambda idx: feats1(D, idx, names),
             'f2': lambda idx: with_heads(feats1(D, idx, names), D['heads'][idx], D['dir'][idx].astype(np.float32))}
    Zs = {fs: standardiser(fsets[fs], tr) for fs in {v[0] for v in kinds.values()}}
    y = np.clip(D[ek][tr], -30, 30)
    models = {}
    for kind, (fs, how) in kinds.items():
        f = fit(how, Zs[fs](tr), y, dev)
        models[kind] = (lambda idx, f=f, fs=fs: f(Zs[fs](idx)) if len(idx) else np.zeros(0, np.float32))
    va_rule = va[rule_mask(va)]
    one = lambda idx: np.ones(len(idx), bool)
    base_va = float(np.sum(D[ek][take(D, va_rule, one(va_rule), ek)])) if len(va_rule) else 0.0
    choice = None
    for kind, m in models.items():
        pv = m(va_rule)
        for tau in (-3.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0):
            tk = take(D, va_rule, pv >= tau, ek) if len(va_rule) else np.zeros(0, np.int64)
            tot = float(np.sum(D[ek][tk])) if len(tk) else 0.0
            if len(tk) >= 10 and tot > base_va and (choice is None or tot > choice[0]):
                choice = (tot, kind, tau)
    r_idx = today[rule_mask(today)]
    r_tk = take(D, r_idx, one(r_idx), ek)
    res = {'ek': ek, 'R': R, 'nm': nm, 'rule': r_tk}
    if choice is None:
        res.update(kind='off', kept=r_tk, w=np.ones(len(r_tk)), w_terc=np.ones(len(r_tk)), sized=False)
        return res
    _, kind, tau = choice
    m = models[kind]
    v_tk = take(D, r_idx, m(r_idx) >= tau, ek)
    pk = m(v_tk)
    pv = m(va_rule)
    vk = take(D, va_rule, pv >= tau, ek)
    pvk = m(vk)
    s = float(np.mean(np.clip(pvk, 0.1, None))) if len(vk) else 1.0
    wfun = lambda p: np.clip(np.clip(p, 0.1, None) / s, 0.25, 3.0)
    w_va = wfun(pvk)
    sized = len(vk) >= 10 and np.sum(w_va * D[ek][vk]) / np.sum(w_va) > np.mean(D[ek][vk])
    q1, q2 = np.quantile(pv, [1 / 3, 2 / 3]) if len(pv) >= 3 else (0, 0)
    res.update(kind=kind, tau=tau, kept=v_tk, sized=bool(sized), w=wfun(pk) if sized else np.ones(len(v_tk)),
               w_terc=np.where(pk >= q2, 2.0, np.where(pk >= q1, 1.0, 0.5)))
    return res


# ---------------------------------------------------------------- helper v2
def helper_models(M, d0, okM, endM, dev):
    pool = np.flatnonzero(okM & (endM < d0))
    pool = pool[np.argsort(M['t'][pool])]
    cut = int(0.8 * len(pool))
    tr, va = pool[:cut], pool[cut:]
    tr = tr[endM[tr] < M['t'][va].min()]
    fsets = {'f1': lambda idx: MB.feats(M, idx),
             'f2': lambda idx: with_heads(MB.feats(M, idx), M['heads'][idx], M['dir'][idx].astype(np.float32))}
    Zs = {fs: standardiser(fsets[fs], tr) for fs in fsets}
    y = np.clip(M[MB.EXIT][tr], -30, 30)
    models = {}
    for kind, (fs, how) in V2.items():
        f = fit(how, Zs[fs](tr), y, dev)
        models[kind] = (lambda idx, f=f, fs=fs: f(Zs[fs](idx)) if len(idx) else np.zeros(0, np.float32))
    chosen = {}
    for k in range(len(MB.TYPES)):
        vk = va[M['type'][va] == k]
        best = None
        for kind, m in models.items():
            pv = m(vk)
            for tau in (0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0):
                tk = place(M, vk[pv >= tau], endM, {})
                if len(tk) < 20:
                    continue
                v = M[MB.EXIT][tk]
                if v.mean() >= 1.0 and (best is None or v.sum() > best[0]):
                    best = (float(v.sum()), kind, tau, len(tk), float(v.mean()))
        if best is not None:
            chosen[k] = best
    return models, chosen


def place(M, idx, endM, busy):
    """Chronological, one position per GAME (either outcome); busy = {game: [(start, end)]} already held. Only positions
    opened before the candidate block it (no foresight of a rule trade still to come)."""
    taken = []
    for i in idx[np.argsort(M['t'][idx], kind='stable')]:
        g = int(M['game'][i])
        if any(s <= M['t'][i] < e for s, e in busy.get(g, [])):
            continue
        busy.setdefault(g, []).append((int(M['t'][i]), int(endM[i])))
        taken.append(i)
    return np.array(taken, np.int64)


# ---------------------------------------------------------------- reporting
def summ(name, g, v, w=None, seed=9):
    g, v = np.asarray(g), np.asarray(v, float)
    if not len(v):
        return f'  {name:<50} no trades'
    w = np.ones(len(v)) if w is None else np.asarray(w, float)
    ug, inv = np.unique(g, return_inverse=True)
    svw, sw, sn = np.bincount(inv, v * w), np.bincount(inv, w), np.bincount(inv)
    rng = np.random.default_rng(seed)
    cb = []
    for _ in range(4000):
        k = rng.integers(0, len(ug), len(ug))
        cb.append(svw[k].sum() / sw[k].sum())
    mean = float(np.sum(v * w) / np.sum(w))
    return (f'  {name:<50}{len(v):>6} trades {len(ug):>4} games  {mean:+.2f}c a share (90% {np.quantile(cb, 0.05):+.2f}..{np.quantile(cb, 0.95):+.2f})'
            f'  win {100 * np.mean(v > 0):.0f}%  total {mean * len(v):+.0f}c')


def paired(name, A, B, seed=11):
    """A, B = (games, values, weights): difference of same-money totals A - B, resampling whole games."""
    allg = np.unique(np.concatenate([np.asarray(A[0]), np.asarray(B[0])]))
    pos = {int(x): i for i, x in enumerate(allg)}

    def sums(X):
        g, v, w = np.asarray(X[0]), np.asarray(X[1], float), np.asarray(X[2], float)
        ix = np.array([pos[int(x)] for x in g], np.int64) if len(g) else np.zeros(0, np.int64)
        return (np.bincount(ix, v * w, len(allg)), np.bincount(ix, w, len(allg)), np.bincount(ix, None, len(allg)))
    a, b = sums(A), sums(B)
    tot = lambda s, k: s[0][k].sum() * (s[2][k].sum() / s[1][k].sum()) if s[1][k].sum() > 0 else 0.0
    every = np.arange(len(allg))
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(4000):
        k = rng.integers(0, len(allg), len(allg))
        diffs.append(tot(a, k) - tot(b, k))
    diffs = np.array(diffs)
    return (f'  {name:<50} {tot(a, every) - tot(b, every):+6.0f}c (90% {np.quantile(diffs, 0.05):+.0f}..{np.quantile(diffs, 0.95):+.0f}),'
            f' better in {100 * np.mean(diffs > 0):.0f}% of resamples')


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    D = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    I = dict(np.load(os.path.join(a.signals, 'improve.npz'), allow_pickle=False))
    for k in NEW:
        D[k], D[hk(k)] = I[k], I[hk(k)]
    D['heads'] = I['heads_bl']
    M = dict(np.load(os.path.join(a.signals, 'more_bets.npz'), allow_pickle=False))
    M['heads'] = I['heads_mb']
    okM = np.isfinite(M[MB.EXIT])
    endM = M['t'] + 1000 * M[f'hold_{MB.EXIT}']
    names = [str(x) for x in D['names']]
    kept_ref = {(int(j), ek) for j, ek, _ in json.load(open(os.path.join(a.signals, 'booklead_veto_kept.json'), encoding='utf-8'))}
    V = {'base': [], 'sized': [], 'terc': [], 'exit': [], 'veto2': [], 'all': [], 'help_base': [], 'help_all': []}
    picks = defaultdict(list)   # variant -> (booklead index, exit, stake, day): for diagnostics (improve_check.py)
    lines = []
    day = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
    while day <= dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc):
        d0 = int(day.timestamp() * 1000)
        d1 = d0 + DAY
        runs = {'base': run_day(D, names, d0, d1, EXITS, V1, dev), 'exit': run_day(D, names, d0, d1, EXITS + NEW, V1, dev),
                'veto2': run_day(D, names, d0, d1, EXITS, V2, dev), 'all': run_day(D, names, d0, d1, EXITS + NEW, V2, dev)}
        txt = [f'{day:%d %b}:']
        for key, r in runs.items():
            if r is None:
                txt.append(f'{key}: no rule')
                continue
            ek, kept = r['ek'], r['kept']
            rows = [(int(D['game'][i]), float(D[ek][i]), 1.0, int(D['t'][i]), int(D['t'][i]) + 1000 * int(D[hk(ek)][i])) for i in kept]
            weighted = lambda ws: [(g, v, float(w), s, e) for (g, v, _, s, e), w in zip(rows, ws)]
            V[key] += weighted(r['w']) if key == 'all' else rows
            picks[key] += [(int(i), ek, float(w), f'{day:%Y-%m-%d}') for i, w in zip(kept, r['w'])]
            if key == 'base':
                V['sized'] += weighted(r['w'])
                V['terc'] += weighted(r['w_terc'])
                same = sum((int(i), ek) in kept_ref for i in kept)
                txt.append(f'base gap>={r["R"]}{"+model" if r["nm"] else ""} {ek}, veto {r["kind"]}: {len(kept)} trades ({same} as before), sizing {"on" if r["sized"] else "off"}')
            elif key == 'all':
                txt.append(f'all gap>={r["R"]}{"+model" if r["nm"] else ""} {ek}, veto {r["kind"]}, sizing {"on" if r["sized"] else "off"}: {len(kept)}')
            else:
                txt.append(f'{key} gap>={r["R"]}{"+model" if r["nm"] else ""} {ek}, veto {r["kind"]}: {len(kept)}')
        models, chosen = helper_models(M, d0, okM, endM, dev)
        txt.append('helper v2 ' + (', '.join(f'{MB.TYPES[k]} {c[1]} >= {c[2]:+.0f}c (val {c[3]} at {c[4]:+.2f}c)' for k, c in chosen.items()) or 'takes nothing'))
        todayM = np.flatnonzero(okM & (M['t'] >= d0) & (M['t'] < d1))
        cand = np.zeros(0, np.int64)
        for k, c in chosen.items():
            ik = todayM[M['type'][todayM] == k]
            cand = np.concatenate([cand, ik[models[c[1]](ik) >= c[2]]])
        for base_key, out_key in (('base', 'help_base'), ('all', 'help_all')):
            busy = {}
            for g, v, w, s, e in V[base_key]:
                if d0 <= s < d1:
                    busy.setdefault(g, []).append((s, e))
            tk = place(M, cand, endM, busy)
            V[out_key] += [(int(M['game'][i]), float(M[MB.EXIT][i]), 1.0, int(M['t'][i]), int(endM[i])) for i in tk]
            txt.append(f'+{len(tk)} extras on {base_key}')
        lines.append('  '.join(txt))
        print(lines[-1], flush=True)
        day += dt.timedelta(days=1)

    col = lambda key, j: [r[j] for r in V[key]]
    T = lambda key: (col(key, 0), col(key, 1), col(key, 2))
    plus = lambda k1, k2: tuple(x + y for x, y in zip(T(k1), T(k2)))
    out = ['Rule + veto: what training can still improve (19-23 Nov, every choice made on earlier days only)', ''] + lines + ['',
           'Each version (cents a share, fees paid, no settlement; ranges resample whole games):',
           summ('baseline: rule + veto', *T('base')),
           summ('sizing: stake by the veto\'s prediction (per $)', *T('sized')),
           summ('   (tercile 0.5x/1x/2x, for reference, per $)', *T('terc')),
           summ('exit grid: rule may pick 15 exits', *T('exit')),
           summ('veto v2: + general model outputs + ensemble', *T('veto2')),
           summ('all three', *T('all')),
           summ('helper v2 extras on the baseline', *T('help_base')),
           summ('baseline + helper v2', *plus('base', 'help_base')),
           summ('helper v2 extras on all three', *T('help_all')),
           summ('all three + helper v2', *plus('all', 'help_all')),
           '', 'Against the baseline (difference in total cents, same money staked):',
           paired('sizing', T('sized'), T('base')), paired('exit grid', T('exit'), T('base')),
           paired('veto v2', T('veto2'), T('base')), paired('all three', T('all'), T('base')),
           paired('baseline + helper v2', plus('base', 'help_base'), T('base')),
           paired('all three + helper v2', plus('all', 'help_all'), T('base'))]
    text = '\n'.join(out)
    print('\n'.join(out[len(lines) + 2:]))
    with open(os.path.join(a.signals, 'improve_trades.json'), 'w', encoding='utf-8') as f:
        json.dump(picks, f)
    with open(os.path.join(a.signals, 'improve.txt'), 'w', encoding='utf-8') as f:
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
