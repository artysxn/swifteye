"""A trained veto (and a relaxed gap) on top of the bookmaker-leads rule (python tools/signals/booklead_veto.py --signals DIR).

For each day D (19-23 Nov), using only what had ended before D:
  1. the RULE is chosen as in booklead.py test (gap R, general model OK or not, exit H; best total before D);
  2. a VETO model is trained on every bookmaker-leads instance with a gap >= 4 pt before D (purged), predicting the net of
     the rule's exit from a compact set of inputs known at that second (the gap, the general model's edge over its
     threshold, spread, order-book imbalance and recent Polymarket moves signed by the direction, how many families
     moved and how far, activity, volatility, price level, time in the game, sport). Two kinds: ridge (linear) and a
     small network; the kind and the veto threshold tau are chosen on the last 20% of those instances (the rule's trades
     there), and the veto is switched off for the day if no tau beats the rule alone there;
  3. RELAXED: the same model may also take gaps from 5 pt up (where the rule would not) when its prediction >= tau2,
     chosen the same way.
Then applied to day D, one position at a time per game and outcome. Pooled with bootstrap ranges; also what sizing the
stake by the prediction would have done (stake 0.5x / 1x / 2x by prediction tercile among kept trades)."""
import argparse
import datetime as dt
import os
import sys

import numpy as np
import torch
from torch import nn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from booklead import EXITS, take  # noqa: E402

SPORTS = ['tennis', 'basketball', 'football', 'cs2', 'dota2', 'amfootball']


def feats(D, idx, names):
    c = {k: i for i, k in enumerate(names)}
    X, d = D['X'][idx], D['dir'][idx].astype(np.float32)
    col = lambda k: X[:, c[k]]
    moved = np.where(d > 0, col('fam_up5'), col('fam_dn5'))
    jump = np.where(d > 0, col('fam_r10_max'), -col('fam_r10_min'))
    f = [D['room'][idx], np.nan_to_num(D['edge'][idx]), np.nan_to_num(D['edge'][idx] - D['th'][idx]), D['spread'][idx],
         col('ob_imb') * d, col('ob_dimb') * d, col('ob_on'), col('pm_r10') * d, col('pm_r30') * d, col('pm_r60') * d, moved, jump,
         col('fam_n'), col('ctx_pm_rate'), col('ctx_vol'), col('ctx_spread'), col('pm_quiet'), col('absx'), col('min_from_start'), col('inplay'),
         col('lead_up') if True else 0, col('gap_z') * d, col('sl_r10') * d]
    f += [(D['sport'][idx] == s).astype(np.float32) for s in SPORTS]
    return np.nan_to_num(np.stack(f, 1).astype(np.float32))


class Small(nn.Module):
    def __init__(self, f):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(f, 32), nn.GELU(), nn.Dropout(0.3), nn.Linear(32, 1))

    def forward(self, x):
        return self.net(x).squeeze(1)


def fit_ridge(X, y, lam=10.0):
    Xb = np.concatenate([X, np.ones((len(X), 1), np.float32)], 1)
    A = Xb.T @ Xb + lam * np.eye(Xb.shape[1])
    A[-1, -1] -= lam
    w = np.linalg.solve(A, Xb.T @ y)
    return lambda Z: np.concatenate([Z, np.ones((len(Z), 1), np.float32)], 1) @ w


def fit_net(X, y, dev):
    torch.manual_seed(2)
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


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--first', default='2026-11-19')
    ap.add_argument('--last', default='2026-11-23')
    a = ap.parse_args()
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    D = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    names = [str(x) for x in D['names']]
    end_t = {k: D['t'] + 1000 * (D['hold_plain'] if k == 'plain' else D[f'hold_{k}']) for k in EXITS}
    day = dt.datetime.fromisoformat(a.first).replace(tzinfo=dt.timezone.utc)
    last = dt.datetime.fromisoformat(a.last).replace(tzinfo=dt.timezone.utc)
    rule_v, veto_v, veto_removed, relax_v, sized, lines = [], [], [], [], [], []
    kept_log = []   # rule + veto trades: (booklead index, exit, day) for the exit helper (booklead_exit.py)
    while day <= last:
        d0, d1 = int(day.timestamp() * 1000), int((day + dt.timedelta(days=1)).timestamp() * 1000)
        today = np.flatnonzero((D['t'] >= d0) & (D['t'] < d1))
        if not len(today):
            day += dt.timedelta(days=1)
            continue
        # 1. the rule, chosen before D
        best = None
        for ek in EXITS:
            before = np.flatnonzero(end_t[ek] < d0)
            for R in (3, 4, 5, 6, 8, 10):
                for nm in (False, True):
                    mk = (D['room'][before] >= R) & ((D['edge'][before] >= D['th'][before]) if nm else True)
                    tk = take(D, before, mk, ek)
                    if len(tk) >= 30:
                        tot = float(np.sum(D[ek][tk]))
                        if best is None or tot > best[0]:
                            best = (tot, ek, R, nm)
        if best is None or best[0] <= 0:
            lines.append(f'{day:%d %b}: no rule made money before; no trades')
            day += dt.timedelta(days=1)
            continue
        _, ek, R, nm = best
        rule_mask = lambda idx: (D['room'][idx] >= R) & ((D['edge'][idx] >= D['th'][idx]) if nm else True)
        # 2. the veto: trained on gap >= 4 instances whose trade ended before D; tau on the last 20% (the rule's trades there)
        pool = np.flatnonzero((end_t[ek] < d0) & (D['room'] >= 4) & np.isfinite(D[ek]))
        pool = pool[np.argsort(D['t'][pool])]
        cut = int(0.8 * len(pool))
        tr, va = pool[:cut], pool[cut:]
        tr = tr[end_t[ek][tr] < D['t'][va].min()]
        F_tr, F_va, F_td = feats(D, tr, names), feats(D, va, names), feats(D, today, names)
        mu, sd = F_tr.mean(0), F_tr.std(0)
        sd[sd < 1e-6] = 1
        Z = lambda F: ((F - mu) / sd).astype(np.float32)
        y = np.clip(D[ek][tr], -30, 30)
        models = {'linear': fit_ridge(Z(F_tr), y), 'network': fit_net(Z(F_tr), y, dev)}
        va_rule = va[rule_mask(va)]
        base_va = float(np.sum(D[ek][take(D, va_rule, np.ones(len(va_rule), bool), ek)])) if len(va_rule) else 0.0
        choice = None
        for kind, f in models.items():
            pv = f(Z(feats(D, va_rule, names))) if len(va_rule) else np.zeros(0)
            for tau in (-3.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0):
                tk = take(D, va_rule, pv >= tau, ek) if len(va_rule) else np.zeros(0, np.int64)
                tot = float(np.sum(D[ek][tk])) if len(tk) else 0.0
                if len(tk) >= 10 and tot > base_va and (choice is None or tot > choice[0]):
                    choice = (tot, kind, tau)
        # 3. relaxed: gaps from 5 pt the rule leaves out, taken when the prediction >= tau2 (chosen on validation)
        relax = None
        va_rel = va[(D['room'][va] >= 5) & ~rule_mask(va)]
        for kind, f in models.items():
            pv = f(Z(feats(D, va_rel, names))) if len(va_rel) else np.zeros(0)
            for tau in (0.0, 0.5, 1.0, 2.0, 3.0, 4.0):
                tk = take(D, va_rel, pv >= tau, ek) if len(va_rel) else np.zeros(0, np.int64)
                tot = float(np.sum(D[ek][tk])) if len(tk) else 0.0
                if len(tk) >= 10 and tot > 0 and (relax is None or tot > relax[0]):
                    relax = (tot, kind, tau)
        # apply to D
        r_idx = today[rule_mask(today)]
        r_tk = take(D, r_idx, np.ones(len(r_idx), bool), ek)
        rule_v += list(D[ek][r_tk])
        if choice is not None:
            _, kind, tau = choice
            p_today = models[kind](Z(feats(D, r_idx, names)))
            v_tk = take(D, r_idx, p_today >= tau, ek)
            removed = np.setdiff1d(r_tk, v_tk)
            veto_v += list(D[ek][v_tk])
            kept_log += [(int(i), ek, day.strftime('%Y-%m-%d')) for i in v_tk]
            veto_removed += list(D[ek][removed])
            # sizing among kept: 0.5x / 1x / 2x by prediction tercile (cut points from validation)
            pv_keep = models[kind](Z(feats(D, va_rule, names)))
            q1, q2 = np.quantile(pv_keep, [1 / 3, 2 / 3]) if len(pv_keep) >= 3 else (0, 0)
            pk = models[kind](Z(feats(D, v_tk, names))) if len(v_tk) else np.zeros(0)
            sized += list(D[ek][v_tk] * np.where(pk >= q2, 2.0, np.where(pk >= q1, 1.0, 0.5)))
            vtxt = f'veto {kind}, keep if predicted >= {tau:+.1f}c: kept {len(v_tk)} ({np.mean(D[ek][v_tk]) if len(v_tk) else float("nan"):+.2f}c), removed {len(removed)} ({np.mean(D[ek][removed]) if len(removed) else float("nan"):+.2f}c)'
        else:
            veto_v += list(D[ek][r_tk])
            kept_log += [(int(i), ek, day.strftime('%Y-%m-%d')) for i in r_tk]
            sized += list(D[ek][r_tk])
            vtxt = 'veto off (no threshold beat the rule on validation)'
        if relax is not None:
            _, kind2, tau2 = relax
            x_idx = today[(D['room'][today] >= 5) & ~rule_mask(today)]
            x_tk = take(D, x_idx, models[kind2](Z(feats(D, x_idx, names))) >= tau2, ek) if len(x_idx) else np.zeros(0, np.int64)
            relax_v += list(D[ek][x_tk])
            xtxt = f'relaxed ({kind2}, >= {tau2:+.1f}c): {len(x_tk)} extra trades ({np.mean(D[ek][x_tk]) if len(x_tk) else float("nan"):+.2f}c)'
        else:
            xtxt = 'relaxed: off'
        lines.append(f'{day:%d %b}: rule gap >= {R}{" + model OK" if nm else ""}, {ek}: {len(r_tk)} trades ({np.mean(D[ek][r_tk]) if len(r_tk) else float("nan"):+.2f}c); {vtxt}; {xtxt}')
        day += dt.timedelta(days=1)

    def pooled(name, v):
        v = np.array(v, dtype=float)
        if not len(v):
            return f'  {name:<52} no trades'
        rng = np.random.default_rng(5)
        boot = [v[rng.integers(0, len(v), len(v))].mean() for _ in range(5000)]
        return f'  {name:<52}{len(v):>5} trades  mean {v.mean():+.2f}c (90% {np.quantile(boot, 0.05):+.2f}..{np.quantile(boot, 0.95):+.2f})  win {100 * np.mean(v > 0):.0f}%  total {v.sum():+.0f}c'
    out = ['Bookmaker-leads rule with a trained veto, day by day (everything chosen on earlier days only):'] + lines + ['', 'Pooled, 19-23 Nov:',
           pooled('the rule alone', rule_v), pooled('rule + veto', veto_v), pooled('   trades the veto removed', veto_removed),
           pooled('relaxed extra trades (gap 5-8, model says yes)', relax_v), pooled('rule + veto + relaxed', veto_v + relax_v),
           pooled('rule + veto, stake 0.5x / 1x / 2x by prediction', sized)]
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'booklead_veto.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    import json
    with open(os.path.join(a.signals, 'booklead_veto_kept.json'), 'w', encoding='utf-8') as f:
        json.dump(kept_log, f)


if __name__ == '__main__':
    main()
