"""The model lab: every model retrained with better learners and inputs, each scored next to a no-model benchmark and
the number it needs for a profit, then one strategy that uses them all (owner, 24 Nov).

  python tools/signals/lab.py build --db X --signals DIR [--workers 22] [--limit N]
  python tools/signals/lab.py test  --signals DIR

BUILD (waves2.one with lab=True): lab_sec.npz = every 3rd in-play decision second; lab_evt.npz = every wave start.
Inputs: X (99 causal inputs), H (the game's lead record, mine.py), Q (labfeat.py: Hawkes-style intensities, order-flow
imbalance, the game's wave history), W (wave-to-wave record, events only).
MODELS, each trained on the days before the day it is scored on (10-min gap for second rows, 45-min for events):
  wave start      an up / a down Polymarket wave starts within 30 s. Old way = a network on X; new = boosted trees with
                  the logistic loss (Newton steps) on X, on X + H + Q, and a blend with a network on all inputs.
  next wave       any wave starts within 2 min (the "when does the next one begin" hazard at a longer horizon).
  wave length     inside a wave: seconds left to its peak (trees on log time; error against always guessing the median).
  superline right a superline wave has started and Polymarket is not in it: does Polymarket start one within 60 s.
  high wave       a wave start whose token can be sold 6c+ above the price paid within 5 min.
SCOREBOARD per model: the score, the same number with no model (AUC 0.5 / the base rate / the median guess), and where
it drives a trade the break-even: if its picks make W when the call is right and lose L when wrong, the picks need a
hit rate of L / (W + L); next to the hit rate they have.
COMBINED: a last model on the wave starts takes the inputs plus every model's own out-of-sample call (wave start our way
/ the other way, next wave, time left, superline right, high wave) and predicts dollars; picks = its top 3% (threshold
from earlier days), one position a game, sold after 5 / 10 min; next to the same model without the other models' calls."""
import argparse
import datetime as dt
import os
import sys
import time
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import labfeat as LF  # noqa: E402
import mine as MN  # noqa: E402
import waves as W  # noqa: E402
import waves2 as W2  # noqa: E402

DAY = 86_400_000


def cmd_build(a):
    gl = W.games(W.connect(a.db))
    if a.limit:
        gl = gl[::max(1, len(gl) // a.limit)][:a.limit]
    ev, sec, t0 = [], [], time.time()
    with Pool(a.workers) as pool:
        for k, (r, f) in enumerate(pool.imap_unordered(W2.one, [(a.db, m, sp, st, 0, True) for m, sp, _, _, st in gl], chunksize=2)):
            ev += r
            sec += f.get('SEC', [])
            if (k + 1) % 500 == 0:
                print(f'  {k + 1} of {len(gl)} games, {time.time() - t0:.0f} s', flush=True)
    E = {key: np.concatenate([p[key] for p in ev]) for key in ev[0]}
    S = {key: np.concatenate([p[key] for p in sec]) for key in sec[0]}
    np.savez(os.path.join(a.signals, 'lab_evt.npz'), **E)
    np.savez(os.path.join(a.signals, 'lab_sec.npz'), **S)
    print(f'{len(S["t"])} second rows and {len(E["t"])} wave starts from {len(np.unique(S["game"]))} games in {time.time() - t0:.0f} s')


class GB:
    """Histogram gradient boosting on the GPU with Newton steps: loss 'logistic' (gradient y - p, hessian p(1-p)) or 'l2'."""

    def __init__(self, loss='logistic', depth=6, lr=0.1, n_trees=200, min_leaf=200, lam=5.0, row_frac=0.25, col_frac=0.6, patience=30, seed=0, bins=64):
        self.__dict__.update(loss=loss, depth=depth, lr=lr, n_trees=n_trees, min_leaf=min_leaf, lam=lam, row_frac=row_frac, col_frac=col_frac, patience=patience, seed=seed, B=bins)

    def _tree(self, Xb, g, h, cols):
        import torch
        dev, B = Xb.device, self.B
        n, Fc = len(g), len(cols)
        node = torch.zeros(n, dtype=torch.long, device=dev)
        ones = torch.ones(n, device=dev)
        ar = torch.arange(n, device=dev)
        feats, thrs = [], []
        for level in range(self.depth):
            nn_ = 1 << level
            hg, hh, hn = (torch.zeros(nn_ * Fc * B, device=dev) for _ in range(3))
            for a in range(0, Fc, 24):
                cc = cols[a:a + 24]
                idx = ((node[:, None] * Fc + torch.arange(a, a + len(cc), device=dev)[None, :]) * B + Xb[:, cc].long()).reshape(-1)
                for acc, v in ((hg, g), (hh, h), (hn, ones)):
                    acc.index_add_(0, idx, v[:, None].expand(-1, len(cc)).reshape(-1))
            GL, HL, NL = (v.view(nn_, Fc, B).cumsum(2) for v in (hg, hh, hn))
            Gt, Ht, Nt = GL[:, :, -1:], HL[:, :, -1:], NL[:, :, -1:]
            gain = GL ** 2 / (HL + self.lam) + (Gt - GL) ** 2 / (Ht - HL + self.lam) - Gt ** 2 / (Ht + self.lam)
            gain = torch.where((NL >= self.min_leaf) & (Nt - NL >= self.min_leaf), gain, torch.full_like(gain, -1.0)).view(nn_, -1)
            best = gain.argmax(1)
            ok = gain.gather(1, best[:, None]).squeeze(1) > 1e-12
            bf = cols[torch.div(best, B, rounding_mode='floor')]
            bb = torch.where(ok, best % B, torch.full_like(best, B))
            feats.append(bf)
            thrs.append(bb)
            node = node * 2 + (Xb[ar, bf[node]].long() > bb[node]).long()
        L = 1 << self.depth
        Gl = torch.zeros(L, device=dev).index_add_(0, node, g)
        Hl = torch.zeros(L, device=dev).index_add_(0, node, h)
        return feats, thrs, self.lr * Gl / (Hl + self.lam)

    def fit(self, Xtr, ytr, Xva, yva, cols=None):
        import torch
        dev = Xtr.device
        gen = torch.Generator().manual_seed(self.seed)      # a CPU generator works on every device
        call = torch.arange(Xtr.shape[1], device=dev) if cols is None else cols
        m = ytr.mean().clamp(1e-4, 1 - 1e-4) if self.loss == 'logistic' else ytr.mean()
        self.base = (torch.log(m / (1 - m)) if self.loss == 'logistic' else m).item()
        Ftr, Fva = torch.full_like(ytr, self.base), torch.full_like(yva, self.base)
        self.trees, best = [], (1e18, 0)
        for it in range(self.n_trees):
            rows = torch.randint(0, len(ytr), (max(1, int(len(ytr) * self.row_frac)),), generator=gen).to(dev)
            cc = call[torch.randperm(len(call), generator=gen)[:max(1, int(len(call) * self.col_frac))].to(dev)]
            if self.loss == 'logistic':
                p = torch.sigmoid(Ftr[rows])
                g, h = ytr[rows] - p, (p * (1 - p)).clamp_min(1e-4)
            else:
                g, h = ytr[rows] - Ftr[rows], torch.ones(len(rows), device=dev)
            f, t, v = self._tree(Xtr[rows], g, h, cc)
            self.trees.append((f, t, v))
            Ftr += v[MN.Trees.leaf(Xtr, f, t)]
            Fva += v[MN.Trees.leaf(Xva, f, t)]
            if (it + 1) % 10 == 0:
                loss = (torch.nn.functional.binary_cross_entropy_with_logits(Fva, yva) if self.loss == 'logistic' else ((Fva - yva) ** 2).mean()).item()
                if loss < best[0] - 1e-7:
                    best = (loss, it + 1)
                elif it + 1 - best[1] >= self.patience:
                    break
        self.trees = self.trees[:max(best[1], 20)]
        return self

    def predict(self, Xb):
        import torch
        F_ = torch.full((Xb.shape[0],), self.base, device=Xb.device)
        for f, t, v in self.trees:
            F_ += v[MN.Trees.leaf(Xb, f, t)]
        return F_.cpu().numpy()


def mlp_fit(Xtr, Ytr, Xva, Yva):
    """The old way: a small network, binary cross-entropy on several targets at once. Returns a predictor of logits."""
    import torch
    from torch import nn
    torch.manual_seed(0)
    mu, sd = Xtr.mean(0, keepdim=True), Xtr.std(0, keepdim=True).clamp_min(1e-6)
    Z = lambda v: ((v - mu) / sd).clamp(-8, 8)
    net = nn.Sequential(nn.Linear(Xtr.shape[1], 256), nn.GELU(), nn.Dropout(0.2), nn.Linear(256, 128), nn.GELU(), nn.Linear(128, Ytr.shape[1])).to(Xtr.device)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-4)
    best, state = 1e18, None
    for ep in range(6):
        net.train()
        perm = torch.randperm(len(Xtr), device=Xtr.device)
        for i in range(0, len(perm), 2048):
            b = perm[i:i + 2048]
            opt.zero_grad()
            nn.functional.binary_cross_entropy_with_logits(net(Z(Xtr[b])), Ytr[b]).backward()
            opt.step()
        net.eval()
        with torch.no_grad():
            lv = np.mean([nn.functional.binary_cross_entropy_with_logits(net(Z(Xva[i:i + 100_000])), Yva[i:i + 100_000]).item() for i in range(0, len(Xva), 100_000)])
        if lv < best:
            best, state = lv, {k: v.clone() for k, v in net.state_dict().items()}
    net.load_state_dict(state)
    net.eval()

    def pred(Xv):
        with torch.no_grad():
            return torch.cat([net(Z(Xv[i:i + 100_000])) for i in range(0, len(Xv), 100_000)]).cpu().numpy()
    return pred


def auc(score, pos):
    pos = np.asarray(pos, bool)
    if pos.sum() == 0 or (~pos).sum() == 0:
        return float('nan')
    r = np.argsort(np.argsort(score, kind='stable'), kind='stable') + 1.0
    return (r[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * (~pos).sum())


def binned(FE, base_rows):
    rng = np.random.default_rng(0)
    samp = rng.choice(base_rows, min(200_000, len(base_rows)), replace=False)
    Xb = np.zeros(FE.shape, np.uint8)
    for j in range(FE.shape[1]):
        e = np.unique(np.quantile(FE[samp, j], np.linspace(0, 1, 65)[1:-1]))
        Xb[:, j] = np.searchsorted(e, FE[:, j], side='right')
    return Xb


def breakeven(pay, right):
    """Picks make `pay`; `right` says where the call was correct. (W, L, hit rate needed, hit rate had)."""
    pay, right = np.asarray(pay, float), np.asarray(right, bool)
    if right.sum() < 5 or (~right).sum() < 5:
        return None
    Wn, Ls = pay[right].mean(), -pay[~right].mean()
    need = Ls / (Wn + Ls) if Wn > 0 and Ls > 0 else (0.0 if Ls <= 0 else float('inf'))
    return Wn, Ls, need, right.mean()


def cmd_test(a):
    import torch
    sys.stdout.reconfigure(encoding='utf-8')
    dev = MN.pick_device()
    print(f'running on {dev}', flush=True)
    tg = lambda v: torch.from_numpy(v).to(dev)
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    out = []
    say = lambda s: (out.append(s), print(s, flush=True))
    S = np.load(os.path.join(a.signals, 'lab_sec.npz'))
    E = np.load(os.path.join(a.signals, 'lab_evt.npz'))
    nx = S['X'].shape[1]
    FS = np.concatenate([S['X'], S['H'], S['Q']], 1)
    Ts, Gs = S['t'], S['game'].astype(np.int64)
    up, dn, any120, rem_t = S['up30'].astype(np.float32), S['dn30'].astype(np.float32), S['any120'].astype(np.float32), S['rem_t']
    Ys = S['Y']
    Te, Ge, de, We, Ye, TGe, SPe = E['t'], E['game'].astype(np.int64), E['d'], E['W'], E['Y'], E['TG'], E['sport']
    FE_raw = np.concatenate([E['X'], E['H'], E['Q']], 1)                       # same columns as the second rows: the per-second models read these
    Xm, Qm = E['X'].copy(), E['Q'].copy()
    Xm[de < 0], Qm[de < 0] = MN.mirror(E['X'][de < 0]), LF.mirror(E['Q'][de < 0])
    FE_m = np.concatenate([Xm, E['H'], Qm, We], 1)                             # seen from the trade's side, plus the wave-to-wave record
    days = sorted({dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in Ts[::2000]})
    first = d_ms(days[1])
    Xs_b = tg(binned(FS, np.flatnonzero(Ts < first)))
    edges_rows = np.flatnonzero(Te < first)
    Xe_raw_b = tg(binned(np.concatenate([FS[np.flatnonzero(Ts < first)][:200_000], FE_raw]), np.arange(min(200_000, int((Ts < first).sum()))))[min(200_000, int((Ts < first).sum())):])
    Xe_m_b = tg(binned(FE_m, edges_rows))
    Xs_f = tg(np.nan_to_num(FS).astype(np.float32))
    okE = np.isfinite(Ye).all(1) & np.isfinite(TGe[:, 0]) & np.isfinite(TGe[:, 4])
    high = (TGe[:, 0] >= 6).astype(np.float32)
    fol = np.nan_to_num(TGe[:, 8]).astype(np.float32)
    sl_open = (We[:, 9] == 0) & (We[:, 8] == 0)                                 # a superline wave start with Polymarket not in that wave
    S1 = np.full((len(Te), 6), np.nan, np.float32)                              # the other models' calls at each wave start, made out of sample
    sc = {k: [] for k in ('mlpX_up', 'mlpX_dn', 'gbX_up', 'gb_up', 'gb_dn', 'blend_up', 'blend_dn', 'any', 'len', 'len_naive', 'lenX', 'fol', 'high')}
    picks = {k: [] for k in ('start', 'fol', 'high')}
    base = {k: [] for k in ('up', 'dn', 'any', 'fol', 'high')}
    topp = {k: [] for k in ('up', 'dn', 'any', 'fol', 'high')}

    def split(idx, T, gap):
        idx = idx[np.argsort(T[idx], kind='stable')]
        cut = int(0.8 * len(idx))
        va, tr = idx[cut:], idx[:cut]
        return tr[T[tr] + gap < T[va].min()], va
    t00 = time.time()
    for dday in days[1:]:
        d0 = d_ms(dday)
        scored = dday >= '2026-11-19'
        tr_all, today = np.flatnonzero(Ts + 600_000 < d0), np.flatnonzero((Ts >= d0) & (Ts < d0 + DAY))
        ev_today = np.flatnonzero((Te >= d0) & (Te < d0 + DAY))
        if len(tr_all) < 50_000 or not len(today):
            continue
        tr, va = split(tr_all, Ts, 600_000)
        trg, vag, tdg, evg = tg(tr), tg(va), tg(today), tg(ev_today)
        P = {}
        for name, y in (('up', up), ('dn', dn), ('any', any120)):
            yt = tg(y)
            m = GB().fit(Xs_b[trg], yt[trg], Xs_b[vag], yt[vag])
            P[name] = m.predict(Xs_b[tdg])
            S1[ev_today, {'up': 0, 'dn': 1, 'any': 2}[name]] = m.predict(Xe_raw_b[evg])
            if scored:
                pv = m.predict(Xs_b[vag])
                sel = P[name] >= np.quantile(pv, 0.99)
                base[name].append((y[today].sum(), len(today)))
                topp[name].append((y[today][sel].sum(), sel.sum()))
                if name != 'any':
                    j = 0 if name == 'up' else 1
                    okm = np.isfinite(Ys[today][:, j, 1])
                    picks['start'] += [(Ys[i, j, 1], Ys[i, j, 2], y[i]) for i in today[sel & okm]]
        iw_tr, iw_va, iw_td = tr[np.isfinite(rem_t[tr])], va[np.isfinite(rem_t[va])], today[np.isfinite(rem_t[today])]
        if len(iw_tr) > 5000 and len(iw_va) > 500:
            lt = tg(np.log1p(np.nan_to_num(rem_t)).astype(np.float32))
            mlen = GB(loss='l2', min_leaf=100).fit(Xs_b[tg(iw_tr)], lt[tg(iw_tr)], Xs_b[tg(iw_va)], lt[tg(iw_va)])
            S1[ev_today, 3] = mlen.predict(Xe_raw_b[evg])
            if scored and len(iw_td):
                sc['len'].append((np.abs(np.expm1(mlen.predict(Xs_b[tg(iw_td)])) - rem_t[iw_td]), None))
                sc['len_naive'].append((np.abs(np.median(rem_t[iw_tr]) - rem_t[iw_td]), None))
                mx = GB(loss='l2', min_leaf=100).fit(Xs_b[tg(iw_tr)], lt[tg(iw_tr)], Xs_b[tg(iw_va)], lt[tg(iw_va)], cols=torch.arange(nx, device=dev))
                sc['lenX'].append((np.abs(np.expm1(mx.predict(Xs_b[tg(iw_td)])) - rem_t[iw_td]), None))
        if scored:
            Y2 = tg(np.stack([up, dn], 1))
            pX = mlp_fit(Xs_f[trg][:, :nx], Y2[trg], Xs_f[vag][:, :nx], Y2[vag])(Xs_f[tdg][:, :nx])
            pA = mlp_fit(Xs_f[trg], Y2[trg], Xs_f[vag], Y2[vag])(Xs_f[tdg])
            yt = tg(up)
            gX = GB().fit(Xs_b[trg], yt[trg], Xs_b[vag], yt[vag], cols=torch.arange(nx, device=dev)).predict(Xs_b[tdg])
            n_ = len(today)
            for key, s_, y in (('mlpX_up', pX[:, 0], up), ('mlpX_dn', pX[:, 1], dn), ('gbX_up', gX, up), ('gb_up', P['up'], up), ('gb_dn', P['dn'], dn),
                               ('blend_up', P['up'] + pA[:, 0], up), ('blend_dn', P['dn'] + pA[:, 1], dn), ('any', P['any'], any120)):
                sc[key].append((auc(s_, y[today] > 0.5), n_))
        # the two models on wave starts (inputs seen from the trade's side)
        etr_all = np.flatnonzero(okE & (Te + 2_700_000 < d0))
        if len(etr_all) > 8_000 and len(ev_today):
            for name, y, mask, col in (('fol', fol, sl_open, 4), ('high', high, np.ones(len(Te), bool), 5)):
                etr, eva = split(etr_all[mask[etr_all]], Te, 2_700_000)
                yt = tg(y)
                m = GB(min_leaf=100).fit(Xe_m_b[tg(etr)], yt[tg(etr)], Xe_m_b[tg(eva)], yt[tg(eva)])
                pe = m.predict(Xe_m_b[evg])
                S1[ev_today, col] = pe
                if scored:
                    use = mask[ev_today] & okE[ev_today]
                    sc[name].append((auc(pe[use], y[ev_today][use] > 0.5), int(use.sum())))
                    sel = use & (pe >= np.quantile(m.predict(Xe_m_b[tg(eva)]), 0.9 if name == 'fol' else 0.97))
                    base[name].append((y[ev_today][use].sum(), use.sum()))
                    topp[name].append((y[ev_today][sel].sum(), sel.sum()))
                    picks[name] += [(Ye[i, 1], Ye[i, 3], y[i]) for i in ev_today[sel]]
        print(f'  {dday}: models trained ({time.time() - t00:.0f} s)', flush=True)
    wavg = lambda L: np.average([x[0] for x in L], weights=[x[1] for x in L]) if L else float('nan')
    rate = lambda L: sum(x[0] for x in L) / max(sum(x[1] for x in L), 1) * 100
    med = lambda L: np.median(np.concatenate([x[0] for x in L])) if L else float('nan')
    say('SCOREBOARD (19-23 Nov, every model trained only on the days before the day it is scored on). AUC: 0.50 = no model, 1.00 = perfect.')
    say(f'  WAVE START, up within 30 s    no model 0.50 | old network {wavg(sc["mlpX_up"]):.3f} | trees, old inputs {wavg(sc["gbX_up"]):.3f} | trees, all inputs {wavg(sc["gb_up"]):.3f} | trees + network {wavg(sc["blend_up"]):.3f}')
    say(f'  WAVE START, down within 30 s  no model 0.50 | old network {wavg(sc["mlpX_dn"]):.3f} | trees, all inputs {wavg(sc["gb_dn"]):.3f} | trees + network {wavg(sc["blend_dn"]):.3f}')
    say(f'      how often it happens: up {rate(base["up"]):.1f}%, down {rate(base["dn"]):.1f}% of seconds (= a random pick); in the model\'s top 1% of seconds: up {rate(topp["up"]):.0f}%, down {rate(topp["dn"]):.0f}%')
    say(f'  NEXT WAVE within 2 min        no model 0.50 | trees, all inputs {wavg(sc["any"]):.3f}; happens after {rate(base["any"]):.0f}% of seconds, after {rate(topp["any"]):.0f}% of the model\'s top 1%')
    say(f'  WAVE LENGTH (seconds left to the peak, median error): no model (always the median) {med(sc["len_naive"]):.0f} s | trees, old inputs {med(sc["lenX"]):.0f} s | trees, all inputs {med(sc["len"]):.0f} s')
    say(f'  SUPERLINE RIGHT (Polymarket follows within 60 s): no model 0.50 | trees {wavg(sc["fol"]):.3f}; happens in {rate(base["fol"]):.0f}% of superline waves, in {rate(topp["fol"]):.0f}% of the model\'s top 10%')
    say(f'  HIGH WAVE                     no model 0.50 | trees {wavg(sc["high"]):.3f}; {rate(base["high"]):.0f}% of wave starts are high, {rate(topp["high"]):.0f}% of the model\'s top 3%')
    say('')
    say('WHAT EACH NEEDS FOR A PROFIT (its picks bought and sold after 5 min; $ per $100 with fees and depth):')
    for key, name in (('start', 'wave start, top 1% of seconds (buy that side)'), ('fol', 'superline right, top 10%'), ('high', 'high wave, top 3%')):
        pk = picks[key]
        if len(pk) < 30:
            continue
        pay, pay_tp, right = np.array([x[0] for x in pk]), np.array([x[1] for x in pk]), np.array([x[2] for x in pk]) > 0.5
        be = breakeven(pay, right)
        if be:
            Wn, Ls, need, had = be
            say(f'  {name:<46} {len(pk):>6} picks: right -> ${Wn:+.2f}, wrong -> ${-Ls:+.2f}; needs to be right {("%.0f%%" % (need * 100)) if np.isfinite(need) else "more than always"} of the time, is right {had * 100:.0f}%; '
                f'makes ${pay.mean():+.2f} per $100 (with +5c take-profit ${pay_tp.mean():+.2f})')
    say('')
    with open(os.path.join(a.signals, 'lab.txt'), 'w', encoding='utf-8') as fh:      # the scoreboard is safe on disk before the last step
        fh.write('\n'.join(out) + '\n')
    # ---------------- the combined strategy
    say('COMBINED STRATEGY: a last model on the wave starts reads the inputs AND the other models\' calls, predicts dollars; top 3%, one position a game')
    side_same = np.where(de > 0, S1[:, 0], S1[:, 1])
    side_opp = np.where(de > 0, S1[:, 1], S1[:, 0])
    Z1 = np.stack([side_same, side_opp, S1[:, 2], S1[:, 3], S1[:, 4], S1[:, 5]], 1)
    have = np.isfinite(Z1).all(1)
    FC = np.concatenate([FE_m, np.nan_to_num(Z1)], 1)
    base_c = np.flatnonzero(have & (Te < d_ms(days[min(3, len(days) - 1)])))
    if len(base_c) < 1000:
        base_c = np.flatnonzero(have)
    if len(base_c) < 1000:
        say('  too few wave starts with every model\'s call to train the combined model')
        with open(os.path.join(a.signals, 'lab.txt'), 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(out) + '\n')
        return
    Xc_b = tg(binned(FC, base_c))
    Yc = tg(np.clip(np.nan_to_num(Ye), -40, 40).astype(np.float32))
    nF = FE_m.shape[1]
    res = {(v, j): [] for v in ('with the other models', 'without them') for j in (1, 2)}
    for dday in [x for x in days if x >= '2026-11-20']:
        d0 = d_ms(dday)
        tr_all = np.flatnonzero(okE & have & (Te + 2_700_000 < d0))
        today = np.flatnonzero(okE & have & (Te >= d0) & (Te < d0 + DAY))
        if len(tr_all) < 20_000 or not len(today):
            continue
        tr, va = split(tr_all, Te, 2_700_000)
        for vname, cols in (('with the other models', None), ('without them', torch.arange(nF, device=dev))):
            for j in (1, 2):
                m = GB(loss='l2', min_leaf=100, lam=50.0, n_trees=300, lr=0.08, row_frac=0.5).fit(Xc_b[tg(tr)], Yc[tg(tr), j], Xc_b[tg(va)], Yc[tg(va), j], cols=cols)
                pt = m.predict(Xc_b[tg(today)])
                tk = MN.place(Te, Ge, today[pt >= np.quantile(m.predict(Xc_b[tg(va)]), 0.97)], hold=601_000)
                res[(vname, j)] += [(Ye[i, j], Ge[i]) for i in tk]
    for (vname, j), rows in res.items():
        if rows:
            v = np.array([x[0] for x in rows])
            st = MN.gstats(v, [x[1] for x in rows])
            Wn, Ls = v[v > 0].mean() if (v > 0).any() else 0, -v[v <= 0].mean() if (v <= 0).any() else 0
            say(MN.fmt(f'{vname}, sold after {"5" if j == 1 else "10"} min', st) + f' | wins ${Wn:+.2f}, losses ${-Ls:+.2f}: needs {Ls / max(Wn + Ls, 1e-9) * 100:.0f}% wins')
    allv = Ye[okE, 1]
    say(f'  no model (every wave start, sold after 5 min): ${allv.mean():+.2f} per $100, win {np.mean(allv > 0) * 100:.0f}%')
    with open(os.path.join(a.signals, 'lab.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'test'])
    ap.add_argument('--db')
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=22)
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    (cmd_build if a.cmd == 'build' else cmd_test)(a)


if __name__ == '__main__':
    main()
