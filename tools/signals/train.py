"""Train and test the signal models on <db dir>/signals/dataset.npz (tools/signals/build.py).

  python tools/signals/train.py [--db PATH] [--epochs 14] [--hidden 256]

Walk-forward: games ordered by kick-off; the first 60% train, the next 20% choose the decision threshold
(validation), the last 20% are the test, never seen before. Two models with an up and a down head: logistic
regression and a neural network (2 hidden layers, GELU, dropout). Class-weighted cross-entropy.
The test is a trading backtest on executable prices: in each game and direction one position at a time,
entered 1 s after a signal at the ask, out at +1c net, at a 4c stop or after 120 s, fees paid (features.py);
compared with plain rules (superline ahead, three bookmaker families moving, a superline wave detected).
Writes <db dir>/signals/report.txt, model.pt and model.json (weights, scaler, thresholds, feature names)."""
import argparse
import json
import os
import sys
import time

import numpy as np
import torch
from scipy.stats import rankdata
from torch import nn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402


def auc(y, p):
    """Area under the ROC curve (Mann-Whitney, ties averaged)."""
    y = np.asarray(y).astype(bool)
    npos, nneg = y.sum(), (~y).sum()
    if not npos or not nneg:
        return float('nan')
    ranks = rankdata(p)  # ties get their average rank
    return float((ranks[y].sum() - npos * (npos + 1) / 2) / (npos * nneg))


class Net(nn.Module):
    def __init__(self, f, hidden, kind):
        super().__init__()
        if kind == 'linear':
            self.body = nn.Linear(f, 2)
        else:
            self.body = nn.Sequential(nn.Linear(f, hidden), nn.GELU(), nn.Dropout(0.15), nn.Linear(hidden, hidden), nn.GELU(), nn.Dropout(0.15), nn.Linear(hidden, 2))

    def forward(self, x):
        return self.body(x)


def predict(model, X, dev, bs=65536):
    model.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(X), bs):
            out.append(torch.sigmoid(model(torch.from_numpy(X[i:i + bs]).to(dev))).cpu().numpy())
    return np.concatenate(out)


def predict_raw(model, X, dev, bs=65536):
    model.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(X), bs):
            out.append(model(torch.from_numpy(X[i:i + bs]).to(dev)).squeeze(1).cpu().numpy())
    return np.concatenate(out)


def train(kind, Xtr, ytr, Xva, yva, dev, epochs, hidden, log):
    torch.manual_seed(7)
    model = Net(Xtr.shape[1], hidden, kind).to(dev)
    pos = ytr.mean(axis=0)
    lossf = nn.BCEWithLogitsLoss(pos_weight=torch.tensor((1 - pos) / pos, dtype=torch.float32, device=dev))
    opt = torch.optim.AdamW(model.parameters(), lr=3e-3 if kind == 'linear' else 1e-3, weight_decay=1e-4)
    Xt = torch.from_numpy(Xtr).to(dev)
    yt = torch.from_numpy(ytr.astype(np.float32)).to(dev)
    best, best_state = -1, None
    bs = 8192
    for ep in range(epochs):
        model.train()
        perm = torch.randperm(len(Xt), device=dev)
        for i in range(0, len(Xt), bs):
            b = perm[i:i + bs]
            opt.zero_grad()
            loss = lossf(model(Xt[b]), yt[b])
            loss.backward()
            opt.step()
        pv = predict(model, Xva, dev)
        a = [auc(yva[:, k], pv[:, k]) for k in (0, 1)]
        log(f'  {kind} epoch {ep + 1}: validation AUC up {a[0]:.4f} down {a[1]:.4f}')
        if np.nanmean(a) > best:
            best, best_state = np.nanmean(a), {k: v.detach().clone() for k, v in model.state_dict().items()}
    model.load_state_dict(best_state)
    return model


def backtest(sig, d, rows, k):
    """Trades taken where sig is True (rows: indexes into the dataset, any order), one position at a time per
    (game, outcome) for direction k. Returns (n, total net per share, mean, win share, trades' row indexes)."""
    rows = rows[sig[rows]]
    if not len(rows):
        return 0, 0.0, float('nan'), float('nan'), rows
    order = np.lexsort((d['t'][rows], d['outcome'][rows], d['game'][rows]))
    rows = rows[order]
    busy = {}
    taken = []
    for i in rows:
        key = (int(d['game'][i]), int(d['outcome'][i]))
        t = int(d['t'][i])
        if t < busy.get(key, -1):
            continue
        busy[key] = t + int(d['hold'][i, k]) * 1000
        taken.append(i)
    taken = np.array(taken, dtype=np.int64)
    nets = d['net'][taken, 2 * k + 1].astype(float)
    nets = nets[np.isfinite(nets)]
    return len(nets), float(nets.sum()), float(nets.mean()) if len(nets) else float('nan'), float((nets > 0).mean()) if len(nets) else float('nan'), taken


def pick_tau(p, d, rows, k, min_trades=40):
    """The threshold with the largest total net on these rows (validation), among those giving min_trades+."""
    best = (None, -1e9, 0)
    for q in (0.999, 0.998, 0.995, 0.99, 0.98, 0.97, 0.95, 0.93, 0.9, 0.85, 0.8, 0.7):
        tau = float(np.quantile(p[rows], q))
        n, tot, mean, win, _ = backtest(p >= tau, d, rows, k)
        if n >= min_trades and tot > best[1]:
            best = (tau, tot, n)
    return best[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db')
    ap.add_argument('--epochs', type=int, default=14)
    ap.add_argument('--hidden', type=int, default=256)
    ap.add_argument('--data', default='dataset.npz')
    ap.add_argument('--tag', default='')
    a = ap.parse_args()
    od = os.path.join(os.path.dirname(a.db or W.default_db()), 'signals')
    lines = []

    def log(s):
        print(s, flush=True)
        lines.append(s)

    t0 = time.time()
    d = dict(np.load(os.path.join(od, a.data), allow_pickle=False))
    names = [str(x) for x in d['names']]
    X, y = d['X'], d['y']
    games = np.unique(d['game'])
    gstart = {g: s for g, s in zip(d['game'], d['start'])}
    order = sorted(games, key=lambda g: gstart[g])
    n = len(order)
    split = {'train': set(order[:int(0.6 * n)]), 'val': set(order[int(0.6 * n):int(0.8 * n)]), 'test': set(order[int(0.8 * n):])}
    part = {k: np.flatnonzero(np.isin(d['game'], list(v))) for k, v in split.items()}
    log(f"{len(y)} decision seconds, {n} games, {X.shape[1]} features. Train {len(part['train'])} rows / {len(split['train'])} games, "
        f"validation {len(part['val'])} / {len(split['val'])}, test {len(part['test'])} / {len(split['test'])} (games by kick-off). "
        f"Wins: up {y[:, 0].mean():.3f}, down {y[:, 1].mean():.3f}. Label: take-profit, stop, horizon = {d['label'].tolist() if 'label' in d else 'see features.py'}.")
    mu = X[part['train']].mean(axis=0)
    sd = X[part['train']].std(axis=0)
    sd[sd < 1e-6] = 1.0
    Z = np.clip((X - mu) / sd, -10, 10).astype(np.float32)
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    log(f'device {dev}')

    preds, models = {}, {}
    for kind in ('linear', 'mlp'):
        m = train(kind, Z[part['train']], y[part['train']], Z[part['val']], y[part['val']], dev, a.epochs, a.hidden, log)
        models[kind] = m
        preds[kind] = predict(m, Z, dev)

    # Diagnostic: do the two heads tell direction, or only that a big move is coming (both high together)?
    for kind in ('linear', 'mlp'):
        pt = preds[kind][part['test']]
        log(f'{kind}: correlation of the up and down heads on the test games {np.corrcoef(pt[:, 0], pt[:, 1])[0, 1]:+.2f} (near +1 = it predicts volatility, not direction)')

    # A directional network: regression of the signed move r = (up net - down net) / 2 at the horizon, in cents
    # (the costs cancel, so r is Polymarket's mid move seen from the prices a trade gets). Huber loss.
    r = (d['net'][:, 0].astype(float) - d['net'][:, 2].astype(float)) / 2 * 100
    okr = np.isfinite(r)
    r = np.clip(np.nan_to_num(r), -20, 20).astype(np.float32)

    def fit_dir():
        torch.manual_seed(11)
        m = Net(Z.shape[1], a.hidden, 'mlp').to(dev)
        m.body[-1] = nn.Linear(a.hidden, 1).to(dev)
        opt = torch.optim.AdamW(m.parameters(), lr=1e-3, weight_decay=1e-4)
        tr = part['train'][okr[part['train']]]
        va = part['val'][okr[part['val']]]
        Xt, yt = torch.from_numpy(Z[tr]).to(dev), torch.from_numpy(r[tr]).to(dev)
        best, state = -1e9, None
        for ep in range(a.epochs):
            m.train()
            perm = torch.randperm(len(Xt), device=dev)
            for i in range(0, len(Xt), 8192):
                b = perm[i:i + 8192]
                opt.zero_grad()
                loss = nn.functional.huber_loss(m(Xt[b]).squeeze(1), yt[b], delta=3.0)
                loss.backward()
                opt.step()
            pv = predict_raw(m, Z[va], dev)
            ic = float(np.corrcoef(pv, r[va])[0, 1])
            log(f'  direction epoch {ep + 1}: validation correlation with the move {ic:+.4f}')
            if ic > best:
                best, state = ic, {k: v.detach().clone() for k, v in m.state_dict().items()}
        m.load_state_dict(state)
        return m

    mdir = fit_dir()
    pdir = predict_raw(mdir, Z, dev)
    te = part['test'][okr[part['test']]]
    log(f"direction network: correlation with the move on the test games {np.corrcoef(pdir[te], r[te])[0, 1]:+.4f}; "
        f"right direction on moves over 3c: {100 * np.mean(np.sign(pdir[te][np.abs(r[te]) > 3]) == np.sign(r[te][np.abs(r[te]) > 3])):.0f}%")
    # Its thresholds on validation: trade up when the predicted move is at least th_up cents, down when at most -th_dn.
    dir_tau = {}
    for k, dn in ((0, 'up'), (1, 'down')):
        s = pdir if k == 0 else -pdir
        best = (None, -1e9)
        for q in (0.999, 0.998, 0.995, 0.99, 0.98, 0.97, 0.95, 0.9):
            th = float(np.quantile(s[part['val']], q))
            n_, tot, *_ = backtest(s >= th, d, part['val'], k)
            if n_ >= 40 and tot > best[1]:
                best = (th, tot)
        dir_tau[dn] = best[0]
    preds['direction'] = None

    # Rules on the same rows (the plain math, for comparison).
    col = {nm: i for i, nm in enumerate(names)}
    prev_same = np.r_[False, (d['game'][1:] == d['game'][:-1]) & (d['outcome'][1:] == d['outcome'][:-1])]
    def onset(c):
        v = X[:, col[c]] > 0.5
        before = np.r_[False, v[:-1]] & prev_same
        return v & ~before
    rules = {
        'superline ahead (onset)': (onset('lead_up'), onset('lead_down')),
        '3+ families moved 1pt in 5 s': ((X[:, col['fam_up5']] >= 3) & (X[:, col['lead_pm']] < 0.5), (X[:, col['fam_dn5']] >= 3) & (X[:, col['lead_pm']] < 0.5)),
        'superline wave detected': ((X[:, col['sl_wave']] > 0.5) & (X[:, col['sl_wave_age']] == 0), (X[:, col['sl_wave']] < -0.5) & (X[:, col['sl_wave_age']] == 0)),
        'every decision second': (np.ones(len(y), bool), np.ones(len(y), bool)),
    }

    log('\nTest games (never seen in training or threshold choice):')
    log(f"{'signal':<34}{'dir':<6}{'AUC':>7}{'trades':>8}{'win':>7}{'mean net':>10}{'total':>9}")
    out_tau = {'direction': dir_tau}
    for k, dn in ((0, 'up'), (1, 'down')):
        th = dir_tau[dn]
        s = pdir if k == 0 else -pdir
        if th is None:
            log(f"{'direction net':<34}{dn:<6}{'':>7}   no threshold made money on validation")
        else:
            nt, tot, mean, win, _ = backtest(s >= th, d, part['test'], k)
            log(f"{'direction net' + f' ({th:+.1f}c move)':<34}{dn:<6}{'':>7}{nt:>8}{100 * win:>6.0f}%{100 * mean:>9.2f}c{100 * tot:>8.0f}c")
    for kind, p in preds.items():
        if p is None:
            continue
        out_tau[kind] = {}
        for k, dn in ((0, 'up'), (1, 'down')):
            tau = pick_tau(p[:, k], d, part['val'], k)
            out_tau[kind][dn] = tau
            a_ = auc(y[part['test'], k], p[part['test'], k])
            if tau is None:
                log(f"{kind:<34}{dn:<6}{a_:>7.3f}   no threshold made money on validation")
                continue
            nt, tot, mean, win, _ = backtest(p[:, k] >= tau, d, part['test'], k)
            log(f"{kind + f' (p >= {tau:.2f})':<34}{dn:<6}{a_:>7.3f}{nt:>8}{100 * win:>6.0f}%{100 * mean:>9.2f}c{100 * tot:>8.0f}c")
    for name, (su, sd_) in rules.items():
        for k, dn, s in ((0, 'up', su), (1, 'down', sd_)):
            nt, tot, mean, win, _ = backtest(s, d, part['test'], k)
            log(f"{name:<34}{dn:<6}{'':>7}{nt:>8}{100 * win:>6.0f}%{100 * mean:>9.2f}c{100 * tot:>8.0f}c")

    # By sport, the network on the test games.
    log('\nNetwork by sport (test):')
    p = preds['mlp']
    for sp in sorted(set(d['sport'][part['test']])):
        rows = part['test'][d['sport'][part['test']] == sp]
        bits = []
        for k, dn in ((0, 'up'), (1, 'down')):
            tau = out_tau['mlp'][dn]
            if tau is None:
                continue
            nt, tot, mean, win, _ = backtest(p[:, k] >= tau, d, rows, k)
            bits.append(f"{dn} AUC {auc(y[rows, k], p[rows, k]):.3f}, {nt} trades, mean {100 * mean:.2f}c" if nt else f"{dn} AUC {auc(y[rows, k], p[rows, k]):.3f}, no trades")
        log(f'  {sp:<12}{len(rows):>8} rows  ' + '; '.join(bits))

    # Which inputs the network leans on: AUC lost when one is shuffled (test rows, both heads averaged).
    rng = np.random.default_rng(3)
    rows = part['test'] if len(part['test']) <= 300_000 else rng.choice(part['test'], 300_000, replace=False)
    Zs = Z[rows].copy()
    base = predict(models['mlp'], Zs, dev)
    b0 = np.nanmean([auc(y[rows, k], base[:, k]) for k in (0, 1)])
    imp = []
    for j, nm in enumerate(names):
        keep = Zs[:, j].copy()
        Zs[:, j] = rng.permutation(keep)
        pp = predict(models['mlp'], Zs, dev)
        imp.append((b0 - np.nanmean([auc(y[rows, k], pp[:, k]) for k in (0, 1)]), nm))
        Zs[:, j] = keep
    imp.sort(reverse=True)
    log('\nWhat the network uses most (AUC lost when the input is shuffled):')
    for v, nm in imp[:20]:
        log(f'  {nm:<18}{v:+.4f}')

    torch.save({'state': models['mlp'].state_dict(), 'linear': models['linear'].state_dict(), 'direction': mdir.state_dict(), 'mu': mu, 'sd': sd, 'names': names, 'tau': out_tau, 'hidden': a.hidden}, os.path.join(od, f'model{a.tag}.pt'))
    js = {'names': names, 'mu': mu.tolist(), 'sd': sd.tolist(), 'tau': out_tau, 'hidden': a.hidden,
          'mlp': {k: v.cpu().numpy().tolist() for k, v in models['mlp'].state_dict().items()},
          'direction': {k: v.cpu().numpy().tolist() for k, v in mdir.state_dict().items()}}
    with open(os.path.join(od, f'model{a.tag}.json'), 'w', encoding='utf-8') as f:
        json.dump(js, f)
    log(f'\n{time.time() - t0:.0f} s; written to {od}')
    with open(os.path.join(od, f'report{a.tag}.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
