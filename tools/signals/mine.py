"""Let the machine look for the signal: every in-play second, a per-game lead record, a 15-minute warm-up, boosted trees.

  python tools/signals/mine.py build --db X --signals DIR [--workers 20] [--limit N]
  python tools/signals/mine.py test  --signals DIR

Owner, 23 Nov: (1) the superline may be consistently early in some games and consistently late in others: measure
that per game and trade only where it leads; (2) no bets in the first 15 minutes of a game going live: measure the
bookmaker <-> Polymarket delay there first; (3) "signals must exist ... if x and y and z is 0.5*x and y is between
0.5 and 0.55, then it's an 80% safe signal in a sample of 50": a model that finds conjunctions of ranges, on all data.

BUILD: for every game (features.build's 1 s grid), every 3rd in-play decision second, both directions (buy the outcome
/ buy against it):
  X   the 99 causal inputs of the wave models (mirrored for the "against" direction at training time);
  H   the game's LEAD RECORD so far, causal, starting when the game goes live:
        rA_W / rB_W   correlation-like sums: Polymarket's step against the superline's move over the W s before it
                      (superline leads), and the superline's step against Polymarket's move before it (Polymarket
                      leads), W = 5 / 20 / 60 s; lead20 = rA_20 - rB_20, lead15 = the same over the last 15 min;
        slfirst_share of CUSUM waves both lines made, the share the superline was already in when Polymarket's began;
        share_sl_ahead / share_pm_ahead  time shares of the lead states;
        follow_share  of the "superline ahead" spells so far (known 60 s after each began), the share where
                      Polymarket then moved at least half the gap;
        w15_*         lead20, follow_share and slfirst_share as they stood at minute 15 of live play (the warm-up
                      measurement), held for the rest of the game; live_min.
  Y   dollars a $100 order makes (ask 1 s later, both taker fees, depth within 2c on both sides, 1c worse beyond the
      best level, a sale with no bid at the first real bid within 10 min else 0): h60 / h300 = sold into the bid
      after 1 / 5 min, tp5 = sold when the bid is 5c above the buy, else at 5 min.
TEST (19-23 Nov, every choice on earlier days, 20-min purge, one position a game):
  A  the lead record and the warm-up on two plain signals (superline ahead our way; a bookmaker jumped and Polymarket
     has not followed), then a rule "trade the signal only in games whose record is over a threshold";
  B  boosted regression trees (GPU, histogram splits = conjunctions of ranges) on every sample: predicted $ per $100;
     threshold on the last 20% before each day by total minus one standard error over games;
  C  the model's rules spelled out: a single tree's best leaves on 17-20 Nov and what they did on 21-23 Nov; and every
     "80%+ wins in 50+ samples" box a deep tree finds, on unseen days."""
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
import features as F  # noqa: E402
import waves as W  # noqa: E402
from booklead import fee, held  # noqa: E402

EVERY = 3


def pick_device():
    """Where the tree and network models run: LAB_DEVICE if set, else an NVIDIA card, else Apple's GPU (mps), else the CPU."""
    import torch
    d = os.environ.get('LAB_DEVICE')
    if d:
        return d
    if torch.cuda.is_available():
        return 'cuda'
    if getattr(torch.backends, 'mps', None) is not None and torch.backends.mps.is_available():
        return 'mps'
    return 'cpu'
NAMES = list(F.NAMES)
C = {k: i for i, k in enumerate(NAMES)}
HN = ['rA5', 'rB5', 'rA20', 'rB20', 'rA60', 'rB60', 'lead20', 'lead15', 'slfirst_share', 'n_wavefirst', 'share_sl_ahead', 'share_pm_ahead',
      'follow_share', 'n_spells', 'w15_lead20', 'w15_follow', 'w15_slfirst', 'live_min']
LAB = ['h60', 'h300', 'tp5', 'tp5_s3', 'tp5_s5', 'tp5_a2', 'tp5_t30', 'tp5_t60']
DAY = 86_400_000
NEG = ['x', 'gap_e', 'gap_z', 'lead_exp', 'sl_wave', 'pm_wave', 'ob_imb', 'ob_dimb', 'fam_gap_mean'] + \
      [k for k in NAMES if (k.endswith('_gap') or k.endswith('_r10') or k[:4] in ('pm_r', 'sl_r')) and not k.startswith('fam_')]
NEG = sorted(set(NEG))
SWAP = [('lead_up', 'lead_down'), ('sl_U', 'sl_D'), ('pm_U', 'pm_D'), ('fam_up5', 'fam_dn5'), ('fam_up30', 'fam_dn30')]
SWAPNEG = [('fam_gap_max', 'fam_gap_min'), ('fam_r10_max', 'fam_r10_min')]


# ------------------------------------------------------------------ build
def lead_record(X, live, step_s):
    n = len(X)
    dpm = np.nan_to_num(X[:, C['pm_r1']]).astype(float)
    dsl = np.nan_to_num(X[:, C['sl_r1']]).astype(float)
    dpm[~live] = 0
    dsl[~live] = 0
    Ppm, Psl = np.cumsum(dpm), np.cumsum(dsl)
    vpm, vsl = np.cumsum(dpm ** 2), np.cumsum(dsl ** 2)

    def before(P, w):   # P[t-1] - P[t-1-w]: the line's move over the w steps before this one
        a = np.concatenate([np.zeros(1), P[:-1]])
        b = np.concatenate([np.zeros(w + 1), P[:-(w + 1)]]) if n > w + 1 else np.zeros(n)
        return a - b
    w15 = max(1, int(900 / step_s))
    roll = lambda v: v - (np.concatenate([np.zeros(w15), v[:-w15]]) if n > w15 else np.zeros(n))
    cols = {}
    for w_s in (5, 20, 60):
        w = max(1, int(w_s / step_s))
        A = np.cumsum(dpm * before(Psl, w))
        B = np.cumsum(dsl * before(Ppm, w))
        den = np.sqrt(vpm * vsl * w) + 1e-6
        cols[f'rA{w_s}'], cols[f'rB{w_s}'] = A / den, B / den
        if w_s == 20:
            cols['lead20'] = (A - B) / den
            cols['lead15'] = (roll(A) - roll(B)) / (np.sqrt(roll(vpm) * roll(vsl) * w) + 1e-6)
    sw, pw = X[:, C['sl_wave']], X[:, C['pm_wave']]
    sw1, pw1 = np.r_[0, sw[:-1]], np.r_[0, pw[:-1]]
    sl_first = np.cumsum((pw != 0) & (pw1 == 0) & live & (sw1 == pw))     # the superline was already in that wave
    pm_first = np.cumsum((sw != 0) & (sw1 == 0) & live & (pw1 == sw))
    cols['slfirst_share'] = (sl_first + 1) / (sl_first + pm_first + 2)
    cols['n_wavefirst'] = np.log1p(sl_first + pm_first)
    lt = np.cumsum(live) + 1e-9
    cols['share_sl_ahead'] = np.cumsum(((X[:, C['lead_up']] > 0.5) | (X[:, C['lead_down']] > 0.5)) & live) / lt
    cols['share_pm_ahead'] = np.cumsum((X[:, C['lead_pm']] > 0.5) & live) / lt
    x, ge = X[:, C['x']].astype(float), X[:, C['gap_e']].astype(float)
    st = np.where(X[:, C['lead_up']] > 0.5, 1, np.where(X[:, C['lead_down']] > 0.5, -1, 0))
    k60 = max(1, int(60 / step_s))
    on = np.flatnonzero((st != 0) & (np.r_[0, st[:-1]] != st) & live)
    on = on[on + k60 < n]
    tot, fol = np.zeros(n), np.zeros(n)
    if len(on):
        good = st[on] * (x[on + k60] - x[on]) >= 0.5 * np.abs(ge[on])
        np.add.at(tot, on + k60, 1.0)
        np.add.at(fol, on + k60, good.astype(float))
    tot, fol = np.cumsum(tot), np.cumsum(fol)
    cols['follow_share'] = (fol + 1) / (tot + 2)
    cols['n_spells'] = np.log1p(tot)
    live_min = np.cumsum(live) * step_s / 60.0
    k15 = int(np.searchsorted(live_min, 15.0))
    for name, src in (('w15_lead20', 'lead20'), ('w15_follow', 'follow_share'), ('w15_slfirst', 'slfirst_share')):
        v = np.zeros(n)
        if k15 < n:
            v[k15:] = cols[src][k15]
        cols[name] = v
    cols['live_min'] = live_min
    return np.nan_to_num(np.stack([cols[k] for k in HN], 1)).astype(np.float32)


def labels(bid, ask, sz, rows, d):
    if d > 0:
        tb, ta, tbs, tbd, tas, tad = bid, ask, sz[0], sz[2], sz[1], sz[3]
    else:
        tb, ta, tbs, tbd, tas, tad = 1 - ask, 1 - bid, sz[1], sz[3], sz[0], sz[2]
    n = len(tb)
    inf = lambda v: np.where(np.isfinite(v), v, np.inf)
    e = rows + 1
    buy = ta[e]
    okb = np.isfinite(buy) & (buy > 0.02) & (buy < 0.98)
    buy = np.where(okb, buy, 0.5)
    s_in = inf(tas[e])
    cap_in = np.maximum(inf(tad[e]), s_in)
    idx = np.where(np.isfinite(tb), np.arange(n), n)
    nxt = np.minimum.accumulate(idx[::-1])[::-1]

    def usd(k):
        j = nxt[k]
        good = (j < n) & (j - k <= 600)
        jj = np.minimum(j, n - 1)
        sell = np.where(good, np.nan_to_num(tb[jj]), 0.0)
        s_out = np.where(good, inf(tbs[jj]), np.inf)
        cap_out = np.where(good, np.maximum(inf(tbd[jj]), inf(tbs[jj])), np.inf)
        fb = fee(buy)
        nn = np.minimum(np.minimum(100.0 / (buy + fb), cap_in), cap_out)
        n1 = np.minimum(nn, s_in)
        cost = n1 * buy + (nn - n1) * (buy + 0.01) + nn * fb
        m1 = np.minimum(nn, s_out)
        proceeds = m1 * sell + (nn - m1) * np.maximum(sell - 0.01, 0.0) - nn * fee(sell)
        return np.where(okb, proceeds - cost, np.nan)
    out = np.full((len(rows), len(LAB)), np.nan, np.float32)
    out[:, 0] = usd(e + 60)
    out[:, 1] = usd(e + 300)
    win = np.lib.stride_tricks.sliding_window_view(np.nan_to_num(tb, nan=-1.0), 300)[e + 1]      # tb[e+1 : e+301]
    hit = win >= (buy + 0.05)[:, None] - 1e-9
    ftp = np.where(hit.any(1), hit.argmax(1), 10 ** 6)
    out[:, 2] = usd(e + 1 + np.minimum(ftp, 299))
    # Early cash-outs on the same trade (owner, 23 Nov: the take-profit version "doesn't cash out early enough"):
    # s3 / s5 = sell when the bid is 3c / 5c under the price paid; a2 = when the bid is 2c under the bid at entry;
    # t30 / t60 = if the bid is no better than at entry after 30 / 60 s, sell then. A missing bid counts as hit.
    bid0 = np.nan_to_num(tb[e], nan=0.0)
    for j, level in ((3, buy - 0.03), (4, buy - 0.05), (5, bid0 - 0.02)):
        st = win <= level[:, None] + 1e-9
        fst = np.where(st.any(1), st.argmax(1), 10 ** 6)
        out[:, j] = usd(e + 1 + np.minimum(np.minimum(ftp, fst), 299))
    for j, c in ((6, 30), (7, 60)):
        flat = win[:, c - 1] <= bid0 + 1e-9
        out[:, j] = usd(e + 1 + np.where(ftp < c, ftp, np.where(flat, c - 1, np.minimum(ftp, 299))))
    return out


def one(args):
    path, m, sport, start = args
    db = W.connect(path)
    pm, bk = W.load(db, m)
    try:
        book = db.execute('SELECT ts_ms, outcome, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND bid_size IS NOT NULL ORDER BY ts_ms', (m,)).fetchall()
    except Exception:
        book = None
    out = []
    for k, o in enumerate(['home', 'away'] if sport in W.THREE_WAY else ['home']):
        g = F.build(pm, bk, o, sport, start, sport in W.THREE_WAY, book, full=True)
        if g is None:
            continue
        ts, X = np.asarray(g['ts'], np.int64), g['X']
        n = len(ts)
        step_s = (ts[1] - ts[0]) / 1000.0
        live = X[:, C['inplay']] > 0.5
        if live.sum() * step_s < 1200:
            continue
        H = lead_record(X, live, step_s)
        rows = np.flatnonzero(g['evt'] & g['ok'] & live)[::EVERY]
        rows = rows[rows + 302 < n]
        if not len(rows) or abs(step_s - 1.0) > 1e-6:
            continue
        sz = [np.full(n, np.nan)] * 4
        bo = [r for r in (book or []) if r[1] == o]
        if bo:
            bt = np.array([r[0] for r in bo], np.int64)
            sz = [held(bt, np.array([np.nan if r[2 + j] is None else r[2 + j] for r in bo], float), ts) for j in range(4)]
        bid, ask = np.asarray(g['bid'], float), np.asarray(g['ask'], float)
        Y = np.stack([labels(bid, ask, sz, rows, 1), labels(bid, ask, sz, rows, -1)], 1)
        out.append({'t': ts[rows], 'game': np.full(len(rows), m, np.int32), 'outcome': np.full(len(rows), k, np.int8), 'sport': np.array([sport] * len(rows)),
                    'X': X[rows].astype(np.float32), 'H': H[rows], 'Y': Y.astype(np.float32)})
    return out


def cmd_build(a):
    gl = W.games(W.connect(a.db))
    if a.limit:
        gl = gl[::max(1, len(gl) // a.limit)][:a.limit]
    print(f'{len(gl)} games', flush=True)
    parts, t0 = [], time.time()
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(one, [(a.db, m, sp, st) for m, sp, _, _, st in gl], chunksize=2)):
            parts += r
            if (k + 1) % 300 == 0:
                print(f'  {k + 1} of {len(gl)} games, {sum(len(p["t"]) for p in parts)} rows, {time.time() - t0:.0f} s', flush=True)
    D = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    np.savez(os.path.join(a.signals, 'mine.npz'), **D)
    y = D['Y']
    print(f'{len(D["t"])} rows x 2 directions from {len(np.unique(D["game"]))} games in {time.time() - t0:.0f} s; every sample: ' +
          ', '.join(f'{lab} ${np.nanmean(y[:, :, j]):+.2f}' for j, lab in enumerate(LAB)) + ' per $100')


# ------------------------------------------------------------------ trees (GPU)
class Trees:
    def __init__(self, depth=6, lr=0.08, n_trees=300, min_leaf=300, lam=100.0, row_frac=0.3, col_frac=0.6, patience=40, seed=0, bins=64):
        self.__dict__.update(depth=depth, lr=lr, n_trees=n_trees, min_leaf=min_leaf, lam=lam, row_frac=row_frac, col_frac=col_frac, patience=patience, seed=seed, B=bins)

    def _tree(self, Xb, g, cols):
        import torch
        dev, B = Xb.device, self.B
        n, Fc = len(g), len(cols)
        node = torch.zeros(n, dtype=torch.long, device=dev)
        ones = torch.ones(n, device=dev)
        ar = torch.arange(n, device=dev)
        feats, thrs = [], []
        for level in range(self.depth):
            nn_ = 1 << level
            hg = torch.zeros(nn_ * Fc * B, device=dev)
            hn = torch.zeros_like(hg)
            for a in range(0, Fc, 24):
                cc = cols[a:a + 24]
                idx = ((node[:, None] * Fc + torch.arange(a, a + len(cc), device=dev)[None, :]) * B + Xb[:, cc].long()).reshape(-1)
                hg.index_add_(0, idx, g[:, None].expand(-1, len(cc)).reshape(-1))
                hn.index_add_(0, idx, ones[:, None].expand(-1, len(cc)).reshape(-1))
            GL, NL = hg.view(nn_, Fc, B).cumsum(2), hn.view(nn_, Fc, B).cumsum(2)
            Gt, Nt = GL[:, :, -1:], NL[:, :, -1:]
            gain = GL ** 2 / (NL + self.lam) + (Gt - GL) ** 2 / (Nt - NL + self.lam) - Gt ** 2 / (Nt + self.lam)
            gain = torch.where((NL >= self.min_leaf) & (Nt - NL >= self.min_leaf), gain, torch.full_like(gain, -1.0)).view(nn_, -1)
            best = gain.argmax(1)
            ok = gain.gather(1, best[:, None]).squeeze(1) > 1e-9
            bf = cols[torch.div(best, B, rounding_mode='floor')]
            bb = torch.where(ok, best % B, torch.full_like(best, B))          # B = no split: everything goes left
            feats.append(bf)
            thrs.append(bb)
            node = node * 2 + (Xb[ar, bf[node]].long() > bb[node]).long()
        L = 1 << self.depth
        Gl = torch.zeros(L, device=dev).index_add_(0, node, g)
        Nl = torch.zeros(L, device=dev).index_add_(0, node, ones)
        return feats, thrs, self.lr * Gl / (Nl + self.lam)

    @staticmethod
    def leaf(Xb, feats, thrs):
        import torch
        n = Xb.shape[0]
        node = torch.zeros(n, dtype=torch.long, device=Xb.device)
        ar = torch.arange(n, device=Xb.device)
        for bf, bb in zip(feats, thrs):
            node = node * 2 + (Xb[ar, bf[node]].long() > bb[node]).long()
        return node

    def fit(self, Xtr, ytr, Xva=None, yva=None, cols=None):
        import torch
        dev = Xtr.device
        gen = torch.Generator(device=dev).manual_seed(self.seed)
        call = torch.arange(Xtr.shape[1], device=dev) if cols is None else cols
        self.base = ytr.mean()
        ptr = torch.full_like(ytr, self.base.item())
        pva = torch.full_like(yva, self.base.item()) if Xva is not None else None
        self.trees, best = [], (1e18, 0)
        for it in range(self.n_trees):
            rows = torch.randint(0, len(ytr), (max(1, int(len(ytr) * self.row_frac)),), device=dev, generator=gen) if self.row_frac < 1 else torch.arange(len(ytr), device=dev)
            cc = call[torch.randperm(len(call), device=dev, generator=gen)[:max(1, int(len(call) * self.col_frac))]] if self.col_frac < 1 else call
            f, t, v = self._tree(Xtr[rows], ytr[rows] - ptr[rows], cc)
            self.trees.append((f, t, v))
            ptr += v[self.leaf(Xtr, f, t)]
            if Xva is not None:
                pva += v[self.leaf(Xva, f, t)]
                if (it + 1) % 10 == 0:
                    loss = ((pva - yva) ** 2).mean().item()
                    if loss < best[0] - 1e-7:
                        best = (loss, it + 1)
                    elif it + 1 - best[1] >= self.patience:
                        break
        if Xva is not None:
            self.stopped = best[1]
            self.trees = self.trees[:max(best[1], 30)]
        return self

    def predict(self, Xb):
        import torch
        p = torch.full((Xb.shape[0],), self.base.item(), device=Xb.device)
        for f, t, v in self.trees:
            p += v[self.leaf(Xb, f, t)]
        return p


# ------------------------------------------------------------------ test
def mirror(X):
    Xm = X.copy()
    for k in NEG:
        Xm[:, C[k]] = -X[:, C[k]]
    for p, q in SWAP:
        Xm[:, C[p]], Xm[:, C[q]] = X[:, C[q]], X[:, C[p]]
    for p, q in SWAPNEG:
        Xm[:, C[p]], Xm[:, C[q]] = -X[:, C[q]], -X[:, C[p]]
    return Xm


def place(t, g, idx, hold=301_000):
    busy, out = {}, []
    for i in idx[np.argsort(t[idx], kind='stable')]:
        gi = int(g[i])
        if t[i] < busy.get(gi, -1):
            continue
        busy[gi] = int(t[i]) + hold
        out.append(i)
    return np.array(out, np.int64)


def gstats(v, g):
    v, g = np.asarray(v, float), np.asarray(g)
    if not len(v):
        return None
    ug, inv = np.unique(g, return_inverse=True)
    gs = np.bincount(inv, v)
    rng = np.random.default_rng(2)
    boot = np.array([gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(3000)])
    return {'n': len(v), 'games': len(ug), 'tot': v.sum(), 'lo': np.quantile(boot, 0.05), 'hi': np.quantile(boot, 0.95), 'mean': v.mean(), 'win': np.mean(v > 0) * 100,
            'cautious': v.sum() - gs.std() * np.sqrt(len(ug)) if len(ug) > 1 else -1e9}


def fmt(name, s):
    if s is None:
        return f'   {name:<54} no trades'
    return f'   {name:<54}{s["n"]:>6} trades {s["games"]:>4} games  ${s["tot"]:+9,.0f} (90% {s["lo"]:+,.0f}..{s["hi"]:+,.0f})  ${s["mean"]:+6.2f} per $100  win {s["win"]:.0f}%'


def cmd_test(a):
    import torch
    sys.stdout.reconfigure(encoding='utf-8')
    dev = 'cuda'
    fast = bool(os.environ.get('MINE_FAST'))
    D = np.load(os.path.join(a.signals, 'mine.npz'))
    X, H, Y, t0_, game0, sport0 = D['X'], D['H'], D['Y'], D['t'], D['game'], D['sport']
    n = len(t0_)
    FE = np.concatenate([np.concatenate([X, H], 1), np.concatenate([mirror(X), H], 1)])
    del X
    YY = np.concatenate([Y[:, 0], Y[:, 1]])
    T, G = np.concatenate([t0_, t0_]), np.concatenate([game0, game0]).astype(np.int64)
    SP = np.concatenate([sport0, sport0])
    FN = NAMES + HN
    fc = {k: i for i, k in enumerate(FN)}
    live_min = FE[:, fc['live_min']]
    okY = np.isfinite(YY).all(1)
    days = sorted({dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for x in T[::5000]})
    d_ms = lambda d: int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    test_days = [d for d in days if d >= '2026-11-19']
    out = [f'{n} in-play decision seconds x 2 directions = {2 * n} samples, {len(np.unique(G))} games, {days[0]} .. {days[-1]}; dollars per $100 order, fees and depth counted',
           '   every sample, no selection: ' + ', '.join(f'{lab} ${np.nanmean(YY[:, j]):+.2f}' for j, lab in enumerate(LAB)), '']
    say = lambda s: (out.append(s), print(s, flush=True))

    # ---------------- A: the lead record and the warm-up
    say('A. THE GAME\'S LEAD RECORD AND THE 15-MINUTE WARM-UP')
    # does the first 15 minutes say who leads later? one value a game: the warm-up record vs the last 15 minutes' record
    late = np.flatnonzero(live_min[:n] >= 45)[::-1]                      # rows of a game are in time order: the first seen backwards is its last
    li = late[np.unique(game0[late].astype(np.int64) * 4 + D['outcome'][late], return_index=True)[1]] if len(late) else np.zeros(0, np.int64)
    if len(li) > 50:
        w, l = FE[li, fc['w15_lead20']], FE[li, fc['lead15']]
        rk = lambda v: np.argsort(np.argsort(v))
        say(f'   persistence over {len(li)} games (45+ min live): rank correlation of the warm-up lead (first 15 min) with the lead of the last 15 min = '
            f'{np.corrcoef(rk(w), rk(l))[0, 1]:+.2f}; superline led in the warm-up in {np.mean(w > 0) * 100:.0f}% of games, of those it still led at the end in {np.mean(l[w > 0] > 0) * 100:.0f}% '
            f'(of the others: {np.mean(l[w <= 0] > 0) * 100:.0f}%)')
    sigs = {'superline ahead our way': FE[:, fc['lead_up']] > 0.5,
            'a bookmaker jumped, Polymarket has not followed': (FE[:, fc['fam_r10_max']] >= 0.12) & (FE[:, fc['pm_r10']] < 0.02) & (FE[:, fc['fam_gap_mean']] >= 0.12)}
    for sname, sm in sigs.items():
        S = np.flatnonzero(sm & okY)
        say(f'   signal "{sname}": {len(S)} samples; first 15 min live ${YY[S[live_min[S] < 15], 1].mean():+.2f}, after ${YY[S[live_min[S] >= 15], 1].mean():+.2f} per $100 (5-min sale)')
        S = S[live_min[S] >= 15]
        for feat in ('w15_lead20', 'lead20', 'lead15', 'follow_share', 'w15_follow', 'slfirst_share'):
            v = FE[S, fc[feat]]
            qs = np.quantile(v, [0.2, 0.4, 0.6, 0.8, 0.95])
            cells = []
            for lo, hi, lab in ((-1e9, qs[0], 'lowest 20%'), (qs[1], qs[2], 'middle'), (qs[3], 1e9, 'top 20%'), (qs[4], 1e9, 'top 5%')):
                mm = (v >= lo) & (v <= hi)
                cells.append(f'{lab} ${YY[S[mm], 1].mean():+.2f} / tp5 ${YY[S[mm], 2].mean():+.2f} (win {np.mean(YY[S[mm], 2] > 0) * 100:.0f}%)')
            say(f'      by {feat:<14} ' + '; '.join(cells))
        rows_c, lines = [], []
        for d in test_days:
            d0 = d_ms(d)
            before, today = S[T[S] + 320_000 < d0], S[(T[S] >= d0) & (T[S] < d0 + DAY)]
            best = None
            for feat in ('w15_lead20', 'lead20', 'lead15', 'follow_share', 'w15_follow', 'slfirst_share'):
                vb = FE[before, fc[feat]]
                for q in (0.5, 0.7, 0.8, 0.9, 0.95):
                    thr = np.quantile(vb, q) if len(vb) else 0
                    tk = place(T, G, before[vb >= thr])
                    for j in (1, 2):
                        if len(tk) >= 30:
                            s = gstats(YY[tk, j], G[tk])
                            if s['cautious'] > 0 and (best is None or s['cautious'] > best[0]):
                                best = (s['cautious'], feat, thr, j)
            if best is None:
                lines.append(f'{d[8:]} Nov none')
                continue
            _, feat, thr, j = best
            tk = place(T, G, today[FE[today, fc[feat]] >= thr])
            rows_c += [(YY[i, j], G[i]) for i in tk]
            lines.append(f'{d[8:]} Nov {feat}>={thr:.2f} {LAB[j]}: {len(tk)} ${YY[tk, j].sum():+.0f}')
        say('      clean rule (after 15 min, record over a threshold chosen on earlier days): ' + '; '.join(lines))
        say(fmt('   pooled', gstats([r[0] for r in rows_c], [r[1] for r in rows_c])))
    say('')

    # ---------------- B: boosted trees on every sample
    say('B. BOOSTED TREES ON EVERY SAMPLE (predicted $ per $100; chosen and thresholded on earlier days only)')
    first_d0 = d_ms(test_days[0])
    rng = np.random.default_rng(0)
    base_rows = np.flatnonzero(T < first_d0)
    samp = rng.choice(base_rows, min(300_000, len(base_rows)), replace=False)
    edges = []
    Xb = np.zeros(FE.shape, np.uint8)
    for j in range(FE.shape[1]):
        e = np.unique(np.quantile(FE[samp, j], np.linspace(0, 1, 65)[1:-1]))
        edges.append(e)
        Xb[:, j] = np.searchsorted(e, FE[:, j], side='right')
    Xg = torch.from_numpy(Xb).to(dev)
    del Xb
    Yc = torch.from_numpy(np.clip(np.nan_to_num(YY), -40, 40).astype(np.float32)).to(dev)
    cols_noH = torch.arange(len(NAMES), device=dev)
    cfg = dict(n_trees=40 if fast else 300, depth=6)
    variants = [('all inputs, 5-min sale', 1, None), ('all inputs, +5c or 5 min', 2, None), ('without the lead record, 5-min sale', 1, cols_noH)]
    res = {(v[0], w): [] for v in variants for w in ('any minute', 'after the 15-min warm-up')}
    rank = {v[0]: [] for v in variants}
    QS = (0.9998, 0.9995, 0.999, 0.998, 0.995, 0.99, 0.98)
    for d in test_days:
        d0 = d_ms(d)
        pool = np.flatnonzero(okY & (T + 1_200_000 < d0))
        pool = pool[np.argsort(T[pool], kind='stable')]
        cut = int(0.8 * len(pool))
        va = pool[cut:]
        tr = pool[:cut]
        tr = tr[T[tr] + 1_200_000 < T[va].min()] if len(va) else tr
        today = np.flatnonzero(okY & (T >= d0) & (T < d0 + DAY))
        if len(tr) < 20_000 or len(va) < 5_000 or not len(today):
            say(f'   {d}: too little earlier data, skipped')
            continue
        trg, vag, tdg = (torch.from_numpy(v).to(dev) for v in (tr, va, today))
        line = [f'   {d}: train {len(tr)}, validate {len(va)}, day {len(today)}']
        for name, j, cols in variants:
            t1 = time.time()
            m = Trees(**cfg).fit(Xg[trg], Yc[trg, j], Xg[vag], Yc[vag, j], cols=cols)
            pv, pt = m.predict(Xg[vag]).cpu().numpy(), m.predict(Xg[tdg]).cpu().numpy()
            order = np.argsort(-pt)
            k1, k2, k3 = max(1, len(pt) // 2000), max(1, len(pt) // 200), max(1, len(pt) // 20)
            rank[name].append([(YY[today[order[lo:hi]], j].sum(), hi - lo) for lo, hi in ((0, k1), (k1, k2), (k2, k3), (k3, len(pt)))])
            for wname, wmask in (('any minute', None), ('after the 15-min warm-up', 15.0)):
                vv = va if wmask is None else va[live_min[va] >= wmask]
                pvv = pv if wmask is None else pv[live_min[va] >= wmask]
                best = None
                for q in QS:
                    tau = np.quantile(pv, q)
                    tk = place(T, G, vv[pvv >= tau])
                    if len(tk) >= 30:
                        s = gstats(YY[tk, j], G[tk])
                        if s['cautious'] > 0 and (best is None or s['cautious'] > best[0]):
                            best = (s['cautious'], tau, len(tk), s['tot'])
                if best is None:
                    continue
                td = today if wmask is None else today[live_min[today] >= wmask]
                ptd = pt if wmask is None else pt[live_min[today] >= wmask]
                tk = place(T, G, td[ptd >= best[1]])
                res[(name, wname)] += [(YY[i, j], G[i]) for i in tk]
                if wmask is None:
                    line.append(f'{name}: {m.stopped} trees, pred >= ${best[1]:+.1f} (val {best[2]} trades ${best[3]:+.0f}) -> {len(tk)} trades ${YY[tk, j].sum():+.0f}')
            if not any(name in x for x in line[1:]):
                line.append(f'{name}: {m.stopped} trees, nothing passed on validation')
            line.append(f'[{time.time() - t1:.0f} s]')
        say(' | '.join(line))
    for (name, wname), rows_ in res.items():
        say(fmt(f'{name}, {wname}', gstats([r[0] for r in rows_], [r[1] for r in rows_])))
    say('   does the ranking mean anything on unseen days? realized $ per $100 by predicted rank within each day (every sample, not one a game):')
    for name, rr in rank.items():
        if rr:
            tot = np.array([[x[0] for x in day_] for day_ in rr]).sum(0)
            cnt = np.array([[x[1] for x in day_] for day_ in rr]).sum(0)
            say(f'      {name:<40} top 0.05% ${tot[0] / cnt[0]:+.2f} ({cnt[0]}) | next to 0.5% ${tot[1] / cnt[1]:+.2f} | next to 5% ${tot[2] / cnt[2]:+.2f} | the rest ${tot[3] / cnt[3]:+.2f}')
    say('')

    # ---------------- C: the rules spelled out
    say('C. THE RULES SPELLED OUT (found on games before 21 Nov; what they did on 21-23 Nov)')
    dsplit = d_ms('2026-11-21')
    tr = np.flatnonzero(okY & (T + 1_200_000 < dsplit))
    te = np.flatnonzero(okY & (T >= dsplit))
    if len(tr) > 20_000 and len(te) > 1000:
        trg, teg = torch.from_numpy(tr).to(dev), torch.from_numpy(te).to(dev)

        def rule_text(feats, thrs, leaf, depth):
            conds, node = [], 0
            for level in range(depth):
                bit = (leaf >> (depth - 1 - level)) & 1
                f, b = int(feats[level][node]), int(thrs[level][node])
                if b < len(edges[f]):
                    conds.append(f'{FN[f]} {">=" if bit else "<"} {edges[f][b]:.3g}')
                node = node * 2 + bit
            return ' AND '.join(conds)
        for title, depth, min_leaf, by in (('best 6 leaves of one tree (depth 5, 500+ samples a leaf), by average $', 5, 500, 'mean'),
                                           ('every box with 80%+ wins in 50+ samples (one deep tree, depth 9, +5c-or-5-min sale)', 9, 50, 'win')):
            j = 1 if by == 'mean' else 2
            m = Trees(depth=depth, lr=1.0, n_trees=1, min_leaf=min_leaf, lam=0.0, row_frac=1.0, col_frac=1.0)
            f, th, _ = m._tree(Xg[trg], Yc[trg, j] - Yc[trg, j].mean(), torch.arange(Xg.shape[1], device=dev))
            ltr, lte = Trees.leaf(Xg[trg], f, th).cpu().numpy(), Trees.leaf(Xg[teg], f, th).cpu().numpy()
            f_, th_ = [x.cpu().numpy() for x in f], [x.cpu().numpy() for x in th]
            L = 1 << depth
            ntr = np.bincount(ltr, minlength=L)
            mtr = np.bincount(ltr, YY[tr, j], L) / np.maximum(ntr, 1)
            wtr = np.bincount(ltr, YY[tr, j] > 0, L) / np.maximum(ntr, 1)
            nte = np.bincount(lte, minlength=L)
            mte = np.bincount(lte, YY[te, j], L) / np.maximum(nte, 1)
            wte = np.bincount(lte, YY[te, j] > 0, L) / np.maximum(nte, 1)
            say(f'   {title}:')
            if by == 'mean':
                for leaf in np.argsort(-np.where(ntr >= min_leaf, mtr, -1e9))[:6]:
                    say(f'      found: {ntr[leaf]} samples ${mtr[leaf]:+.2f} win {wtr[leaf] * 100:.0f}%  ->  unseen: {nte[leaf]} samples ${mte[leaf]:+.2f} win {wte[leaf] * 100:.0f}%   IF {rule_text(f_, th_, int(leaf), depth)}')
            else:
                box = np.flatnonzero((ntr >= 50) & (wtr >= 0.8))
                if len(box):
                    sel = np.isin(lte, box)
                    say(f'      {len(box)} boxes found ({ntr[box].sum()} samples, win {np.average(wtr[box], weights=ntr[box]) * 100:.0f}%, ${np.average(mtr[box], weights=ntr[box]):+.2f} per $100)  ->  '
                        f'unseen days: {sel.sum()} samples, win {np.mean(YY[te[sel], j] > 0) * 100 if sel.any() else float("nan"):.0f}%, ${YY[te[sel], j].mean() if sel.any() else float("nan"):+.2f} per $100')
                    for leaf in box[np.argsort(-ntr[box])][:4]:
                        say(f'      e.g. found {ntr[leaf]} samples win {wtr[leaf] * 100:.0f}% ${mtr[leaf]:+.2f} -> unseen {nte[leaf]} samples win {wte[leaf] * 100:.0f}% ${mte[leaf]:+.2f}   IF {rule_text(f_, th_, int(leaf), depth)}')
                else:
                    say('      none found')
    with open(os.path.join(a.signals, 'mine.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'test'])
    ap.add_argument('--db')
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=20)
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    (cmd_build if a.cmd == 'build' else cmd_test)(a)


if __name__ == '__main__':
    main()
