"""Train and test the wave models (the owner's three factors): python tools/signals/train_waves.py [--epochs 14]

MODEL A, one network with a shared body (factors 1 and 2; targets in wave_targets.py):
  hazard of a Polymarket up / down wave starting within 30 s        (cross-entropy)     -> when a wave comes
  10/50/90% quantiles of the capturable move up and down in 180 s   (pinball loss)      -> where it will go
  median time to that best price, up and down                        (pinball, minutes)  -> how long it runs
  inside a wave: median time and 10/50/90% of the points left to the peak (masked)       -> how long it lasts
Entry: buy up when q50(capturable up) - round-trip fees >= theta (down the same); theta chosen on validation.
MODEL B (factor 3): continuation value. Regression (Huber) of delta = (net the plain policy ends with) - (net if
sold now) on the market features (as they are and mirrored by the trade's direction) and the position (time held,
net now, best net so far, drawdown, entry spread). Sell at the first 10 s check where predicted delta < -m.
Walk-forward: games by kick-off, 60% train / 20% validation (thresholds) / 20% test. Writes
<db dir>/signals/waves_report.txt and wave_model.pt.

  --data DIR [DIR ...]  dataset folders (each from build_waves.py --db ...), trained on together; game ids are
                        kept apart per folder (a game recorded on two machines counts twice: build each
                        machine's database only once, or merge first with the Data page).
  --sport S[,S]         train and test on these sports only (esports = cs2, dota2, valorant, sc2, lol).
Results are also broken down by sport and by how busy Polymarket was (games differ)."""
import argparse
import os
import sys
import time

import numpy as np
import torch
from torch import nn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import train as T  # noqa: E402  (auc, backtest helpers)
from features import ESPORTS  # noqa: E402
import waves as W  # noqa: E402

Q = torch.tensor([0.1, 0.5, 0.9])


def pinball(pred, y, qs, mask=None):
    """Mean pinball loss of pred [n, len(qs)] for target y [n]."""
    e = y[:, None] - pred
    l = torch.maximum(qs * e, (qs - 1) * e)
    if mask is not None:
        l = l[mask]
    return l.mean() if l.numel() else pred.sum() * 0


class Body(nn.Module):
    def __init__(self, f, h, out):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(f, h), nn.GELU(), nn.Dropout(0.1), nn.Linear(h, h), nn.GELU(), nn.Dropout(0.1), nn.Linear(h, out))

    def forward(self, x):
        return self.net(x)


def run(model, X, dev, bs=65536):
    model.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(X), bs):
            out.append(model(torch.from_numpy(X[i:i + bs]).to(dev)).cpu().numpy())
    return np.concatenate(out)


def fit(model, Xtr, loss_fn, Xva, val_fn, epochs, dev, log, name, lr=1e-3):
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    Xt = torch.from_numpy(Xtr).to(dev)
    best, state = None, None
    for ep in range(epochs):
        model.train()
        perm = torch.randperm(len(Xt), device=dev)
        for i in range(0, len(Xt), 8192):
            b = perm[i:i + 8192]
            opt.zero_grad()
            loss = loss_fn(model(Xt[b]), b)
            loss.backward()
            opt.step()
        v = val_fn(run(model, Xva, dev))
        log(f'  {name} epoch {ep + 1}: validation loss {v:.4f}')
        if best is None or v < best:
            best, state = v, {k: t.detach().clone() for k, t in model.state_dict().items()}
    model.load_state_dict(state)
    return model


def trades(score, th, d, rows, val_key, hold_key):
    """One position at a time per (game, outcome): entries where score >= th; returns realized nets (cents)."""
    rows = rows[score[rows] >= th]
    rows = rows[np.lexsort((d['t'][rows], d['outcome'][rows], d['game'][rows]))]
    busy, taken = {}, []
    for i in rows:
        key = (int(d['game'][i]), int(d['outcome'][i]))
        if d['t'][i] < busy.get(key, -1):
            continue
        busy[key] = int(d['t'][i]) + int(d[hold_key][i]) * 1000
        taken.append(i)
    taken = np.array(taken, dtype=np.int64)
    v = d[val_key][taken].astype(float)
    return v[np.isfinite(v)], taken   # a few have no price at the horizon


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db')
    ap.add_argument('--epochs', type=int, default=14)
    ap.add_argument('--hidden', type=int, default=256)
    ap.add_argument('--data', nargs='*')
    ap.add_argument('--sport')
    ap.add_argument('--before', type=int, help='train only on games that kicked off before this (ms); the last 20%% of them validate, the rest after test')
    ap.add_argument('--no-exit', action='store_true', help='model A only (entries): skip the cash-out model and its data')
    ap.add_argument('--out', help='where the model and report go (default: the first --data folder, else <db dir>/signals)')
    a = ap.parse_args()
    out_dir = a.out or (a.data[0] if a.data else None)
    od = os.path.join(os.path.dirname(a.db or W.default_db()), 'signals')
    out_dir = out_dir or od
    lines = []

    def log(s):
        print(s, flush=True)
        lines.append(s)

    t0 = time.time()
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    def load(kind):
        parts = []
        for k, dd in enumerate(a.data or [od]):
            x = dict(np.load(os.path.join(dd, f'waves_{kind}.npz'), allow_pickle=False))
            x['game'] = x['game'].astype(np.int64) + k * 10_000_000   # games of different sources never mix
            parts.append(x)
        names_ = [str(v) for v in parts[0]['names']]
        for x in parts[1:]:
            if [str(v) for v in x['names']] != names_:
                raise SystemExit('the datasets were built with different features: rebuild them with the same features.py')
        out = {key: np.concatenate([x[key] for x in parts]) for key in parts[0] if key != 'names'}
        out['names'] = parts[0]['names']
        out['sport'] = np.array(['esports' if v in ESPORTS else v for v in out['sport']])
        if a.sport:
            keep = np.isin(out['sport'], a.sport.split(','))
            out = {key: (v[keep] if key != 'names' else v) for key, v in out.items()}
        return out

    A = load('A')
    B = load('B') if not a.no_exit else {k: np.zeros(0, dtype=np.int64) for k in ('game', 't')}
    gstart = dict(zip(A['game'], A['start']))
    order = sorted(gstart, key=lambda g: gstart[g])
    n = len(order)
    if a.before:   # rolling walk-forward: everything before the cut trains (last 20% validates), the rest tests
        pre = [g for g in order if gstart[g] < a.before]
        split = {'train': set(pre[:int(0.8 * len(pre))]), 'val': set(pre[int(0.8 * len(pre)):]), 'test': set(order[len(pre):])}
    else:
        split = {'train': set(order[:int(0.6 * n)]), 'val': set(order[int(0.6 * n):int(0.8 * n)]), 'test': set(order[int(0.8 * n):])}
    pa = {k: np.flatnonzero(np.isin(A['game'], list(v))) for k, v in split.items()}
    pb = {k: np.flatnonzero(np.isin(B['game'], list(v))) for k, v in split.items()}
    # Purge and embargo (no hindsight): a game that kicked off before a cut can run past it, and a row's labels look up
    # to L + H = 181 s ahead. So a training row (label included) must end before the first validation kick-off, and a
    # validation row before the test period (the first test kick-off, or --before): nothing the model learns from or is
    # tuned on comes from the time it is tested on.
    EMB = 200_000
    val_start = min(gstart[g] for g in split['val']) if split['val'] else None
    test_start = a.before or (min(gstart[g] for g in split['test']) if split['test'] else None)
    dropped = []
    for P_, D_ in ((pa, A), (pb, B)):
        n0 = len(P_['train']) + len(P_['val'])
        if val_start is not None:
            P_['train'] = P_['train'][D_['t'][P_['train']] + EMB < val_start]
        if test_start is not None:
            P_['val'] = P_['val'][D_['t'][P_['val']] + EMB < test_start]
        dropped.append(n0 - len(P_['train']) - len(P_['val']))
    log(f'Purged {dropped[0]} decision seconds and {dropped[1]} position checks that ran into a later period (games still going at a cut).')
    mu, sd = A['X'][pa['train']].mean(0), A['X'][pa['train']].std(0)
    sd[sd < 1e-6] = 1
    ZA = A['X']   # standardised in place (no copies of a 2 GB table); terciles below are unchanged by it
    ZA -= mu
    ZA /= sd
    np.clip(ZA, -10, 10, out=ZA)
    log(f"Model A: {len(A['t'])} decision seconds, {n} games (train {len(pa['train'])}, validation {len(pa['val'])}, test {len(pa['test'])} rows). "
        f"Wave starts within 30 s: up {A['up_soon'].mean():.3f}, down {A['dn_soon'].mean():.3f}; inside a wave {np.isfinite(A['rem_t']).mean():.3f}.")

    # ---- Model A
    clip = lambda v, lo, hi: np.clip(np.nan_to_num(v, nan=0.0), lo, hi).astype(np.float32)
    tgt = {'up': clip(A['up_soon'], 0, 1), 'dn': clip(A['dn_soon'], 0, 1), 'mu': clip(A['mfe_up'], -20, 40) / 3, 'md': clip(A['mfe_dn'], -20, 40) / 3,
           'tu': clip(A['t_up'], 0, 180) / 60, 'td': clip(A['t_dn'], 0, 180) / 60, 'rt': clip(A['rem_t'], 0, 600) / 60, 'ra': clip(A['rem_a'], -20, 40) / 3,
           'eu': clip(A['base_up'], -20, 20) / 3, 'ed': clip(A['base_dn'], -20, 20) / 3}
    inwave = np.isfinite(A['rem_t'])
    G = {k: torch.from_numpy(v).to(dev) for k, v in tgt.items()}
    Gw = torch.from_numpy(inwave).to(dev)
    tr = pa['train']
    pos = np.array([tgt['up'][tr].mean(), tgt['dn'][tr].mean()])
    pw = torch.tensor((1 - pos) / pos, dtype=torch.float32, device=dev)
    qs = Q.to(dev)
    q50 = torch.tensor([0.5], device=dev)
    bce = nn.BCEWithLogitsLoss(pos_weight=pw)
    trt = torch.from_numpy(tr).to(dev)

    def lossA(out, b, idx=None):
        i = trt[b] if idx is None else idx
        return (bce(out[:, 0:2], torch.stack([G['up'][i], G['dn'][i]], 1)) + pinball(out[:, 2:5], G['mu'][i], qs) + pinball(out[:, 5:8], G['md'][i], qs)
                + pinball(out[:, 8:9], G['tu'][i], q50) + pinball(out[:, 9:10], G['td'][i], q50)
                + pinball(out[:, 10:11], G['rt'][i], q50, Gw[i]) + pinball(out[:, 11:14], G['ra'][i], qs, Gw[i])
                + nn.functional.huber_loss(out[:, 14], G['eu'][i], delta=1.0) + nn.functional.huber_loss(out[:, 15], G['ed'][i], delta=1.0))

    vat = torch.from_numpy(pa['val']).to(dev)

    def valA(out):
        with torch.no_grad():
            return float(lossA(torch.from_numpy(out).to(dev), None, vat))

    torch.manual_seed(5)
    mA = fit(Body(ZA.shape[1], a.hidden, 16).to(dev), ZA[tr], lossA, ZA[pa['val']], valA, a.epochs, dev, log, 'model A')
    P = run(mA, ZA, dev)
    te = pa['test']
    h_up, h_dn = 1 / (1 + np.exp(-P[:, 0])), 1 / (1 + np.exp(-P[:, 1]))
    qu = np.sort(P[:, 2:5], axis=1) * 3
    qd = np.sort(P[:, 5:8], axis=1) * 3
    mfu, mfd = A['mfe_up'].astype(float), A['mfe_dn'].astype(float)
    log('\nFactor 1, when a wave comes (test games):')
    log(f"  up-wave start within 30 s: AUC {T.auc(A['up_soon'][te], h_up[te]):.3f}; down: AUC {T.auc(A['dn_soon'][te], h_dn[te]):.3f}; "
        f"correlation of the two {np.corrcoef(h_up[te], h_dn[te])[0, 1]:+.2f} (near +1 = only 'something will move', not which way)")
    tw = te[inwave[te]]
    base_rt = np.median(A['rem_t'][pa['train']][inwave[pa['train']]])
    log(f"  inside a wave, time left to the peak: median error {np.median(np.abs(P[tw, 10] * 60 - A['rem_t'][tw])):.0f} s "
        f"(always guessing the typical {base_rt:.0f} s: {np.median(np.abs(base_rt - A['rem_t'][tw])):.0f} s); "
        f"points left: median error {np.median(np.abs(np.sort(P[tw, 11:14], 1)[:, 1] * 3 - A['rem_a'][tw])):.2f} pt "
        f"(typical guess: {np.median(np.abs(np.median(A['rem_a'][pa['train']][inwave[pa['train']]]) - A['rem_a'][tw])):.2f} pt)")
    log('Factor 2, where it will go (test games; capturable move in 180 s, cents, spread included, fees not):')
    for nm, q, y in (('up', qu, mfu), ('down', qd, mfd)):
        med = np.median(y[pa['train']])
        log(f"  {nm}: median error {np.median(np.abs(q[te, 1] - y[te])):.2f}c (typical guess {np.median(np.abs(med - y[te])):.2f}c); "
            f"below the 10% line {100 * np.mean(y[te] < q[te, 0]):.0f}%, below the 90% line {100 * np.mean(y[te] < q[te, 2]):.0f}% (ideal 10% / 90%)")
    big = te[np.maximum(mfu[te], mfd[te]) >= 3 + A['fee_rt'][te]]
    log(f"  which way, where a move over costs + 3c came ({len(big)} seconds): right {100 * np.mean(np.sign(qu[big, 1] - qd[big, 1]) == np.sign(mfu[big] - mfd[big])):.0f}%")

    # Entries: predicted median capturable move minus fees, threshold on validation; exits by the plain policy.
    edge_up = qu[:, 1] - A['fee_rt']
    edge_dn = qd[:, 1] - A['fee_rt']
    # The wave-start hazards are directional: their difference says which way the next wave goes.
    haz_up, haz_dn = h_up - h_dn, h_dn - h_up
    log('\nEntries (test games; plain exit: sell at +3c net, else at 180 s; cents per trade, fees paid):')
    th = {}
    for nm, s, vk, hk in (('up', edge_up, 'base_up', 'hold_up'), ('down', edge_dn, 'base_dn', 'hold_dn'),
                          ('up, wave-start score', haz_up, 'base_up', 'hold_up'), ('down, wave-start score', haz_dn, 'base_dn', 'hold_dn')):
        best = (None, -1e9)
        for q in (0.999, 0.998, 0.995, 0.99, 0.98, 0.97, 0.95, 0.9, 0.8):
            c = float(np.quantile(s[pa['val']], q))
            v, _ = trades(s, c, A, pa['val'], vk, hk)
            if len(v) >= 40 and v.sum() > best[1]:
                best = (c, v.sum())
        th[nm] = best[0]
        extra = (('predicted move > fees', 0.0), ('every decision second', -1e9)) if 'wave' not in nm else ()
        for label, c in (('validation threshold', best[0]),) + extra:
            if c is None:
                log(f'  {nm:<23} {label:<24} no threshold made money on validation')
                continue
            v, _ = trades(s, c, A, te, vk, hk)
            log(f"  {nm:<23} {label:<24} {len(v):>6} trades, win {100 * np.mean(v > 0) if len(v) else 0:>3.0f}%, mean {np.mean(v) if len(v) else float('nan'):+.2f}c, total {v.sum():+.0f}c")

    # ---- More trades: take every trade the model expects to make money (expected net > theta), and the volume
    # curve: looser thresholds give more trades; the total is what matters while the last trades still pay.
    ev_up, ev_dn = P[:, 14] * 3, P[:, 15] * 3
    days = max((A['t'][te].max() - A['t'][te].min()) / 86_400_000, 1e-9)
    log(f'\nExpected-net entries (the model\'s predicted net of buying now, fees paid; test games over {days:.1f} days):')
    log(f"  {'threshold':<12}{'up trades':>10}{'/day':>7}{'mean':>8}{'total':>8}   {'down trades':>11}{'/day':>7}{'mean':>8}{'total':>8}")
    for c in (-1.0, -0.5, 0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0):
        bits = []
        for sc, vk, hk in ((ev_up, 'base_up', 'hold_up'), (ev_dn, 'base_dn', 'hold_dn')):
            v, _ = trades(sc, c, A, te, vk, hk)
            bits.append(f"{len(v):>10}{len(v) / days:>7.0f}{(np.mean(v) if len(v) else float('nan')):>+8.2f}{v.sum():>+8.0f}")
        log(f'  ev >= {c:<+6.2f}' + '   '.join(bits))
    for nm, sc, vk, hk in (('up', ev_up, 'base_up', 'hold_up'), ('down', ev_dn, 'base_dn', 'hold_dn')):
        best = (None, -1e9)
        for c in (-1.0, -0.5, 0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0):
            v, _ = trades(sc, c, A, pa['val'], vk, hk)
            if len(v) >= 40 and v.sum() > best[1]:
                best = (c, v.sum())
        th[f'ev {nm}'] = best[0]
        if best[0] is not None:
            v, _ = trades(sc, best[0], A, te, vk, hk)
            log(f"  {nm}: validation picks ev >= {best[0]:+.2f}: test {len(v)} trades ({len(v) / days:.0f}/day), mean {np.mean(v):+.2f}c, total {v.sum():+.0f}c")
        else:
            log(f'  {nm}: no expected-net threshold made money on validation')

    # ---- By kind of market: sport, and how busy Polymarket was over the last 10 minutes (terciles of train).
    names = [str(v) for v in A['names']]
    if 'ctx_pm_rate' in names:
        act = A['X'][:, names.index('ctx_pm_rate')]
        cuts = np.quantile(act[pa['train']], [1 / 3, 2 / 3])
        seg = np.digitize(act, cuts)
    else:
        seg = np.zeros(len(A['t']), dtype=int)
    log('\nBy kind of market (test games; entries on the predicted move with the validation thresholds):')
    segs = [(f'sport {sp}', A['sport'] == sp) for sp in sorted(set(A['sport'][te]))]
    segs += [(f'Polymarket {nm}', seg == k) for k, nm in enumerate(('quiet (fewest changes)', 'medium', 'busy (most changes)'))]
    for label, mask in segs:
        rows = te[mask[te]]
        if len(rows) < 500:
            continue
        bits = [f"wave start AUC up {T.auc(A['up_soon'][rows], h_up[rows]):.2f} / down {T.auc(A['dn_soon'][rows], h_dn[rows]):.2f}"]
        for nm, sc, vk, hk in (('up', edge_up, 'base_up', 'hold_up'), ('down', edge_dn, 'base_dn', 'hold_dn')):
            if th.get(nm) is None:
                continue
            v, _ = trades(sc, th[nm], A, rows, vk, hk)
            bits.append(f"{nm} {len(v)} trades {np.mean(v) if len(v) else float('nan'):+.2f}c")
        log(f'  {label:<34}{len(rows):>8} s  ' + '; '.join(bits))

    if a.no_exit:
        torch.save({'A': mA.state_dict(), 'mu': mu, 'sd': sd, 'names': [str(x) for x in A['names']], 'hidden': a.hidden, 'theta': th},
                   os.path.join(out_dir, 'wave_model.pt'))
        log(f'\n{time.time() - t0:.0f} s; model A only; written to {out_dir}')
        with open(os.path.join(out_dir, 'waves_report.txt'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        return

    # ---- Model B: cash out early
    ZB = np.clip((B['X'] - mu) / sd, -10, 10).astype(np.float32)
    dB = B['dir'].astype(np.float32)[:, None]
    ps = np.array([180, 5, 5, 5, 2], dtype=np.float32)
    XB = np.concatenate([ZB, ZB * dB, B['pos'] / ps, dB], axis=1).astype(np.float32)
    delta = np.clip(B['cont'] - B['now'], -30, 30).astype(np.float32)
    log(f"\nModel B: {len(delta)} checks of open positions ({len(np.unique(np.stack([B['game'], B['outcome'], B['t_entry'], B['dir']], 1), axis=0))} positions); "
        f"holding beat selling at {100 * np.mean(delta > 0):.0f}% of checks; mean (plain end - sell now) {delta.mean():+.2f}c.")
    yB = torch.from_numpy(delta).to(dev)
    trb = torch.from_numpy(pb['train']).to(dev)
    vab = torch.from_numpy(pb['val']).to(dev)

    def lossB(out, b, idx=None):
        i = trb[b] if idx is None else idx
        return nn.functional.huber_loss(out[:, 0], yB[i], delta=3.0)

    def valB(out):
        with torch.no_grad():
            return float(lossB(torch.from_numpy(out).to(dev), None, vab))

    torch.manual_seed(9)
    mB = fit(Body(XB.shape[1], a.hidden, 1).to(dev), XB[pb['train']], lossB, XB[pb['val']], valB, a.epochs, dev, log, 'model B')
    pred = run(mB, XB, dev)[:, 0]

    def policy(rows, m=None, stop=None):
        """Mean net per position: sell at the first check where the rule fires, else the plain policy's end."""
        key = np.stack([B['game'][rows], B['outcome'][rows], B['t_entry'][rows], B['dir'][rows]], 1)
        o = np.lexsort((B['pos'][rows, 0], key[:, 3], key[:, 2], key[:, 1], key[:, 0]))
        rows, key = rows[o], key[o]
        start = np.r_[True, (key[1:] != key[:-1]).any(1)]
        res, sold, cur, done = [], 0, None, False
        for j, r in enumerate(rows):
            if start[j]:
                if cur is not None:
                    res.append(cur)
                cur, done = float(B['cont'][r]), False
            if done:
                continue
            fire = (m is not None and pred[r] < -m) or (stop is not None and B['now'][r] <= -stop)
            if fire:
                cur, done = float(B['now'][r]), True
                sold += 1
        if cur is not None:
            res.append(cur)
        res = np.array(res)
        return res.mean(), len(res), sold

    best = (None, -1e9)
    for m in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0):
        v, _, _ = policy(pb['val'], m=m)
        if v > best[1]:
            best = (m, v)
    mb = best[0]
    te_b = pb['test']
    plain, npos, _ = policy(te_b)
    stp, _, s_stp = policy(te_b, stop=4.0)
    mod, _, s_mod = policy(te_b, m=mb)
    log(f"\nFactor 3, cash out early (test games, {npos} positions entered at sampled decision seconds, both ways; cents per position, fees paid):")
    log(f"  plain (sell at +3c net or at 180 s)           mean {plain:+.2f}c")
    log(f"  plus a fixed 4c stop                          mean {stp:+.2f}c  (sold early {s_stp})")
    log(f"  plus the cash-out model (sell if it predicts holding loses over {mb:.1f}c)  mean {mod:+.2f}c  (sold early {s_mod})")
    for sp in sorted(set(B['sport'][te_b])):
        rows = te_b[B['sport'][te_b] == sp]
        if len(rows) < 300:
            continue
        p0, n0, _ = policy(rows)
        p1, _, s1 = policy(rows, m=mb)
        log(f'    {sp:<12} {n0:>6} positions: plain {p0:+.2f}c, with the cash-out model {p1:+.2f}c')
    # The same on positions the entry model would have taken.
    A_key = {(int(g), int(o), int(t)): i for i, (g, o, t) in enumerate(zip(A['game'], A['outcome'], A['t']))}
    sel = []
    for r in te_b:
        i = A_key.get((int(B['game'][r]), int(B['outcome'][r]), int(B['t_entry'][r])))
        if i is None:
            continue
        e = haz_up[i] if B['dir'][r] > 0 else haz_dn[i]
        c = th['up, wave-start score'] if B['dir'][r] > 0 else th['down, wave-start score']
        if c is not None and e >= c:
            sel.append(r)
    sel = np.array(sel, dtype=np.int64)
    if len(sel):
        p1, n1, _ = policy(sel)
        p2, _, s2 = policy(sel, m=mb)
        log(f"  on the positions the entry model would take ({n1}): plain {p1:+.2f}c, with the cash-out model {p2:+.2f}c (sold early {s2})")
    torch.save({'A': mA.state_dict(), 'B': mB.state_dict(), 'mu': mu, 'sd': sd, 'names': [str(x) for x in A['names']], 'hidden': a.hidden,
                'theta': th, 'exit_margin': mb}, os.path.join(out_dir, 'wave_model.pt'))
    log(f'\n{time.time() - t0:.0f} s; written to {out_dir}')
    with open(os.path.join(out_dir, 'waves_report.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
