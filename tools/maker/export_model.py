"""Train the fill model on every recorded fill and write it out for the live bot.

  python tools/maker/export_model.py --signals DIR --out RUN_DIR/maker [--name makersteer_causal.npz]

DIR holds `makersteer.py build --causal --name makersteer_causal.npz` (inputs without hindsight: the only kind a live
bot can compute). The model is the one the evaluation settled on: five boosted-tree models (seeds 0-4, depth 4, 200
trees at most, learning rate 0.05) on the 173 inputs, target = cents a share a fill keeps after 5 min (cut at +-15),
trained on every in-play fill of every setting; the last fifth in time decides when to stop adding trees.
"The usual call" (the level above which the bot doubles its order) = the median positive call of the walk-forward
evaluation (makerbetter.py), not of this model on its own training data.
Before writing, the arrays are scored again in plain numpy and compared with the trained models."""
import argparse
import datetime as dt
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'signals'))
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import feat as FT  # noqa: E402
import makersteer as MS  # noqa: E402
import mine as MN  # noqa: E402
import model as MD  # noqa: E402

SEEDS = 5
GB = dict(loss='l2', depth=4, min_leaf=100, lam=50.0, row_frac=0.7, n_trees=200, lr=0.05)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--name', default='makersteer_causal.npz')
    a = ap.parse_args()
    import torch
    import lab as LB
    Z = np.load(os.path.join(a.signals, a.name))
    Fm, FX = Z['F'], Z['FX']
    nf = len(FT.FN)
    assert MS.FN[:nf] == FT.FN, 'the live input list is not the first columns of the fills'
    X = np.nan_to_num(FX[:, :nf]).astype(np.float64)
    tms, sg, q, m300, live = Fm[:, 2], Fm[:, 3], Fm[:, 4], Fm[:, 7], Fm[:, 10] > 0.5
    k300 = sg * (m300 - q) * 100
    use = np.isfinite(FX[:, 0]) & np.isfinite(k300) & live
    rows = np.flatnonzero(use)
    rows = rows[np.argsort(tms[rows], kind='stable')]
    cut = int(0.8 * len(rows))
    va, tr = rows[cut:], rows[:cut]
    tr = tr[tms[tr] + 600_000 < tms[va].min()]
    rng = np.random.default_rng(0)
    samp = rng.choice(rows, min(200_000, len(rows)), replace=False)
    edges = [np.unique(np.quantile(X[samp, j], np.linspace(0, 1, 65)[1:-1])) for j in range(nf)]
    Xb = np.zeros(X.shape, np.uint8)
    for j in range(nf):
        Xb[:, j] = np.searchsorted(edges[j], X[:, j], side='right')
    dev = MN.pick_device()
    tg = lambda v: torch.from_numpy(np.asarray(v)).to(dev)
    Xg = tg(Xb)
    y = tg(np.clip(np.nan_to_num(k300), -15, 15).astype(np.float32))
    depth = GB['depth']
    feats = np.zeros((SEEDS, GB['n_trees'], depth, 1 << (depth - 1)), np.int32)
    thrs = np.full((SEEDS, GB['n_trees'], depth, 1 << (depth - 1)), 255, np.int32)
    leaves = np.zeros((SEEDS, GB['n_trees'], 1 << depth), np.float64)
    base, ntrees = np.zeros(SEEDS), np.zeros(SEEDS, np.int64)
    pred = np.zeros(len(va))
    for s in range(SEEDS):
        m = LB.GB(**{**GB, 'seed': s}).fit(Xg[tg(tr)], y[tg(tr)], Xg[tg(va)], y[tg(va)])
        base[s], ntrees[s] = m.base, len(m.trees)
        for t, (f_, t_, v_) in enumerate(m.trees):
            for lv in range(depth):
                feats[s, t, lv, :1 << lv] = f_[lv].cpu().numpy()
                thrs[s, t, lv, :1 << lv] = t_[lv].cpu().numpy()
            leaves[s, t] = v_.cpu().numpy()
        pred += m.predict(Xg[tg(va)]) / SEEDS
        print(f'  seed {s}: {len(m.trees)} trees', flush=True)
    c0, src = 0.7, 'default'
    cp = os.path.join(a.signals, a.name.replace('.npz', '') + '_makerbetter_calls.npz')
    if os.path.exists(cp):
        C = np.load(cp)
        P = C['P']
        if len(P) == len(Fm):
            ci, spr = Fm[:, 0].astype(int), Fm[:, 11]
            fam30 = FX[:, MS.FN.index('fam_up30')] - FX[:, MS.FN.index('fam_dn30')]
            el = (ci == MS.CONFIGS.index((0, 2, 1))) & live & (spr >= 4.5) & (fam30 >= 0) & (P > 0)
            if el.sum() >= 100:
                c0, src = float(np.median(P[el])), f'walk-forward calls of {int(el.sum())} entry fills'
    os.makedirs(a.out, exist_ok=True)
    d = lambda v: dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%d %b %H:%M')
    meta = f'trained {dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")} UTC on {len(tr):,} fills ({d(tms[tr].min())} - {d(tms[tr].max())}), stopped on {len(va):,} ({d(tms[va].min())} - {d(tms[va].max())}); {a.name}; usual call {c0:.3f}c from {src}'
    path = MD.default_path(a.out)
    np.savez(path, names=np.array(FT.FN), edges=np.array(edges, dtype=object), feats=feats, thrs=thrs, leaves=leaves, base=base, ntrees=ntrees, c0=c0, meta=meta)
    M = MD.Model(path)
    chk = rng.choice(va, 300, replace=False)
    mine_ = np.array([M.call(X[i]) for i in chk])
    theirs = pred[np.searchsorted(va, chk)] if np.all(np.diff(va) > 0) else None
    order = np.argsort(va)
    theirs = pred[order][np.searchsorted(va[order], chk)]
    gap = np.abs(mine_ - theirs).max()
    rk = lambda v: np.argsort(np.argsort(v))
    print(f'{meta}')
    print(f'plain-numpy scoring against the trained models on 300 fills: largest gap {gap:.6f}c ({"OK" if gap < 1e-3 else "MISMATCH"})')
    print(f'on the held-back last fifth: rank correlation of the call with what a fill kept {np.corrcoef(rk(pred), rk(k300[va]))[0, 1]:+.3f}; fills it would keep (call > 0): {np.mean(pred > 0) * 100:.0f}%, '
          f'they kept {k300[va][pred > 0].mean():+.2f}c a share, the others {k300[va][pred <= 0].mean():+.2f}c')
    print(f'written: {path}')
    assert gap < 1e-3


if __name__ == '__main__':
    main()
