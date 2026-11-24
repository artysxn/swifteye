"""Resting orders steered by what we know (24 Nov, PC; owner: "experiment with resting orders").

  python tools/signals/makersteer.py build --db X --signals DIR [--workers 20]     (DIR = the folder of paths.py and prints_fetch.py)
  python tools/signals/makersteer.py test  --signals DIR

makersim2.py (the Mac's night of 23 Nov) showed that an order resting one cent inside a wide spread keeps a little
(+0.5c a share at 2 s reaction, negative at 3 s) and that every fill is someone else choosing to trade with us. Today's
direction study (wavedir.py) showed the inputs call the next move 58% before it starts and 86% once it is under way.
A taker cannot use that (the round trip costs more than the move); a resting order can: it only has to decide WHICH
SIDE TO LEAVE IN THE BOOK. This tool measures exactly that, on Polymarket's real trades.

BUILD. Same fill rule as makersim2 (a real taker trade that reaches our price while we are first in line or that trades
through it; quotes decided on the book L seconds earlier; live recording only; 20 shares; one fill a side a minute).
Quotes one cent inside a spread of 2 / 3 / 5c+, L = 1 or 2 s, in a calm market (mid within 1c for 30 s, makersim2's
rule) or in any market. Each fill is stored with everything known at the second the quote was decided, seen from OUR
side of the fill (up = good for the position the fill gave us): the 99 inputs, the lead record, the lab's and the
direction study's inputs.
TEST. 1 the plain orders (the baseline, makersim2's numbers again); 2 each simple signal: fills where it agreed with
our side, disagreed, or had no view; 3 steering rules ("leave the bid only while ..."), each in both halves of the
week; 4 a model trained on fills of earlier days to predict what a fill keeps, used to pull the side it expects to
lose. Cents a share kept = our side's mark 5 min later (or 1 min, or the result) minus the fill price; nothing is
charged for getting out, so "at the result" is the only fully paid number.
LEAK CHECK: a public trade's stamp runs a few seconds behind the match and makersim2 places it within [stamp - 5, stamp].
If a trade really happened earlier than placed, inputs read "at the decision second" could contain moves made after it.
`build --lag 6 --name makersteer_lag6.npz` reads every input 6 s earlier than the decision (older than any placement
error); `test --name ...` reports it the same way."""
import argparse
import datetime as dt
import json
import os
import sys
import time
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import features as F  # noqa: E402
import labfeat as LF  # noqa: E402
import mine as MN  # noqa: E402
import paths as PT  # noqa: E402
import wavedir as WD  # noqa: E402
import waves as W  # noqa: E402
from booklead import held  # noqa: E402
from makersim import roll_range, shift  # noqa: E402
from makersim2 import align  # noqa: E402

C = MN.C
OUR = 20.0
CONFIGS = [(calm, k, L) for calm in (1, 0) for k in (2, 3, 5) for L in (1, 2)]
FN = MN.NAMES + MN.HN + LF.QN + WD.DN + ['q_spread', 'q_price', 'q_live_min', 'q_k', 'q_L', 'q_calm']
# REAL ORDER FLOW (added 24 Nov, last columns): Polymarket's public trades with sizes, seen from our side of the fill,
# counted up to FLOW_LAG s before the decision (older than any error in where a trade was placed): signed shares the
# takers bought minus sold over 30 / 120 / 600 s, shares traded and trades in 120 s, seconds since the last trade and
# its side. A live bot gets the same from the trade events of Polymarket's socket (price, size, side).
FLOW = ['fl_s30', 'fl_s120', 'fl_s600', 'fl_a120', 'fl_n120', 'fl_since', 'fl_lastside']
FN = FN + FLOW
FLOW_LAG = 6
DAY = 86_400_000
_G = {}


def one(args):
    db_path, sig, m, lag, causal = args
    if 'M' not in _G:
        _G['M'], _G['A'] = PT.load(sig)
        _G['idx'] = {int(_G['M']['match'][i]): i for i in range(len(_G['M']['key'])) if _G['M']['k'][i] == 0}
        _G['sports'] = sorted(set(_G['M']['sport'].tolist()))
    M, A = _G['M'], _G['A']
    try:
        g = json.load(open(os.path.join(sig, 'pm_trades', f'{m}.json')))
    except Exception:
        return None
    if m not in _G['idx'] or not g.get('trades'):
        return None
    i = _G['idx'][m]
    o, n, t0s = int(M['off'][i]), int(M['n'][i]), int(M['t0'][i]) // 1000
    won, sport, start = float(M['won'][i]), str(M['sport'][i]), int(M['start'][i])
    bid, ask, mid, fresh, mark = PT.book(A, o, n, won, maxage=15.0)
    spread = ask - bid
    s0 = int(max(0, (start - int(M['t0'][i])) // 1000)) if start else 0
    tr = np.array([(t[0], t[1], t[2], t[3], 1 if (t[4] or '').upper() == 'BUY' else -1) for t in g['trades']], float)
    ts = tr[:, 0].astype(np.int64)
    keep = (ts >= t0s + 40) & (ts < t0s + n - 2)
    tr, ts = tr[keep], ts[keep]
    if not len(tr):
        return None
    home = tr[:, 1] > 0.5
    side = np.where(home, tr[:, 4], -tr[:, 4]).astype(np.int64)
    p = np.where(home, tr[:, 2], 1 - tr[:, 2])
    size = tr[:, 3]
    order = np.argsort(ts, kind='stable')
    ts, side, p, size = ts[order], side[order], p[order], size[order]
    u = align(ts, side, p, bid, ask, t0s, n)
    okb = fresh[u] & fresh[u - 1]
    ts, side, p, size, u = ts[okb], side[okb], p[okb], size[okb], u[okb]
    if len(u):
        sv_, av_, cn_ = np.zeros(n), np.zeros(n), np.zeros(n)
        np.add.at(sv_, u, side * size)
        np.add.at(av_, u, size)
        np.add.at(cn_, u, 1.0)
        cs_s, cs_a, cs_n = (np.concatenate([[0.0], np.cumsum(v_)]) for v_ in (sv_, av_, cn_))
        last_tr = np.maximum.accumulate(np.where(cn_ > 0, np.arange(n), -10 ** 9))
        last_sd = np.sign(sv_)[np.clip(last_tr, 0, None)] * (last_tr >= 0)
    slog = lambda x_: np.sign(x_) * np.log1p(abs(x_))

    def flow_feats(dsec, sgn):
        e = dsec - FLOW_LAG
        if e < 0:
            return [0.0] * len(FLOW)
        wsum = lambda cs, w_: cs[e + 1] - cs[max(e - w_ + 1, 0)]
        return [slog(sgn * wsum(cs_s, 30)), slog(sgn * wsum(cs_s, 120)), slog(sgn * wsum(cs_s, 600)), np.log1p(wsum(cs_a, 120)), wsum(cs_n, 120),
                np.log1p(min(e - last_tr[e], 3600)) if last_tr[e] >= 0 else np.log1p(3600), sgn * last_sd[e]]
    if not len(u):
        return None
    # ---- everything known each second, on the feature grid of the home token
    db = W.connect(db_path)
    pmr = db.execute('SELECT ts_ms, server_ts_ms, outcome, bid, ask, last FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)).fetchall()
    bk = []
    for table in ('changes', 'changes_research'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            bk += db.execute(f"SELECT ts_ms, book, outcome, new_price FROM {table} WHERE match_id = ? AND market = '1x2' AND book NOT IN ('polymarket', 'kalshi') ORDER BY ts_ms", (m,)).fetchall()
    try:
        book = db.execute('SELECT ts_ms, outcome, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND bid_size IS NOT NULL ORDER BY ts_ms', (m,)).fetchall()
    except Exception:
        book = None
    G = F.build([r[:5] for r in pmr], bk, 'home', sport, start, sport in W.THREE_WAY, book, full=True, causal=causal)
    if G is None:
        return None
    tsf, X = np.asarray(G['ts'], np.int64), G['X']
    nf = len(tsf)
    live = X[:, C['inplay']] > 0.5
    Hh = MN.lead_record(X, live, 1.0)
    pr4 = [(q[0], q[3], q[4], q[5]) for q in pmr if q[2] == 'home']
    pt = np.array([q[0] for q in pr4], np.int64)
    bf = held(pt, np.array([np.nan if q[1] is None else q[1] for q in pr4], float), tsf)
    af = held(pt, np.array([np.nan if q[2] is None else q[2] for q in pr4], float), tsf)
    sz = [np.full(nf, np.nan)] * 4
    bo = [r for r in (book or []) if r[1] == 'home']
    if bo:
        bt = np.array([r[0] for r in bo], np.int64)
        sz = [held(bt, np.array([np.nan if r[2 + j] is None else r[2 + j] for r in bo], float), tsf) for j in range(4)]
    Q = LF.extra(X, bf, af, sz, G['pm_eps'], G['sl_eps'])
    p1, p2, pr5 = WD.wave_hist(G['pm_eps'], nf)
    s1, _, sr5 = WD.wave_hist(G['sl_eps'], nf)
    Dx = np.concatenate([WD.flow(pr4, tsf, (bf + af) / 2), np.stack([p1, p2, pr5, s1, sr5], 1).astype(np.float32)], 1)
    FE = np.concatenate([X, Hh, Q, Dx], 1)
    FEm = np.concatenate([MN.mirror(X), Hh, LF.mirror(Q), WD.mirror_d(Dx)], 1)
    sec_end = t0s * 1000 + 1000 * np.arange(n, dtype=np.int64) + 999
    fi = np.searchsorted(tsf, sec_end, side='right') - 1
    okf = (fi >= 0) & (sec_end - tsf[np.clip(fi, 0, None)] < 3000)
    lm = Hh[:, MN.HN.index('live_min')]
    # ---- quotes and fills
    base0 = fresh & np.isfinite(spread) & (spread > 0) & (mid >= 0.10) & (mid <= 0.90)
    calm0 = base0 & (roll_range(mid) <= 0.0101)
    pb, pa = np.r_[np.nan, bid[:-1]], np.r_[np.nan, ask[:-1]]
    sp_i = _G['sports'].index(sport)
    Fm, FX = [], []
    for ci, (calm, par, L) in enumerate(CONFIGS):
        cal = shift(calm0 if calm else base0, L, False)
        ok = cal & (shift(spread, L, np.nan) >= par / 100 - 1e-9)
        qb = np.where(ok, shift(bid, L, np.nan) + 0.01, np.nan)
        qa = np.where(ok, shift(ask, L, np.nan) - 0.01, np.nan)
        for sgn, qarr, prev in ((1, qb, pb), (-1, qa, pa)):
            q = qarr[u]
            tk = side == -sgn
            reach = tk & np.isfinite(q) & (sgn * (q - p) >= -1e-9)
            first = sgn * (q - prev[u]) > 1e-9
            through = sgn * (q - p) > 1e-9
            last = -10 ** 9
            for j in np.flatnonzero(reach & (first | through)):
                k = int(u[j])
                if k - last < 60:
                    continue
                last = k
                dsec = max(k - L, 0)
                fsec = max(dsec - lag, 0)
                Fm.append((ci, m, (t0s + k) * 1000, sgn, q[j], min(OUR, size[j]), float(mark(np.array([k]), 60)[0]), float(mark(np.array([k]), 300)[0]), won, sp_i, float(k >= s0),
                           spread[dsec] * 100, 0.0 if first[j] else 1.0, size[j]))
                if okf[fsec]:
                    r = int(fi[fsec])
                    FX.append(np.concatenate([(FE if sgn > 0 else FEm)[r], [spread[dsec] * 100, q[j] if sgn > 0 else 1 - q[j], lm[r], par, L, calm], flow_feats(dsec, sgn)]))
                else:
                    FX.append(np.full(len(FN), np.nan))
    if not Fm:
        return None
    # ---- the state of every in-play second (for the wallet test, makerwallet.py): what a bot would see when deciding
    def sign_bits(v, pos_bit, neg_bit):
        x_ = np.where(okf, v[np.clip(fi, 0, None)], 0.0)
        return ((x_ > 1e-9).astype(np.uint8) << pos_bit) | ((x_ < -1e-9).astype(np.uint8) << neg_bit)
    flags = (base0 & (np.arange(n) >= s0)).astype(np.uint8)
    flags |= sign_bits(X[:, C['fam_up30']] - X[:, C['fam_dn30']], 1, 2) | sign_bits(X[:, C['lead_up']] - X[:, C['lead_down']], 3, 4) | sign_bits(X[:, C['sl_r60']], 5, 6)
    flags |= calm0.astype(np.uint8) << 7
    cents = lambda v: np.clip(np.nan_to_num(np.round(v * 100), nan=0.0), 0, 255).astype(np.uint8)
    st = (m, t0s + s0, n - s0, won, sp_i, flags[s0:], np.where(np.isfinite(spread), cents(spread), 255).astype(np.uint8)[s0:], cents(bid)[s0:], cents(ask)[s0:])
    return np.array(Fm, float), np.array(FX, np.float32), st


def cmd_build(a):
    import glob
    ms = sorted(int(os.path.basename(f)[:-5]) for f in glob.glob(os.path.join(a.signals, 'pm_trades', '*.json')))
    print(f'{len(ms)} games with public trades', flush=True)
    Fm, FX, ST, t0, done = [], [], [], time.time(), 0
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(one, [(a.db, a.signals, m, a.lag, a.causal) for m in ms], chunksize=2)):
            if r is not None:
                Fm.append(r[0])
                FX.append(r[1])
                ST.append(r[2])
                done += 1
            if (k + 1) % 200 == 0:
                print(f'  {k + 1} of {len(ms)} games, {sum(len(x) for x in Fm):,} fills, {time.time() - t0:.0f} s', flush=True)
    M, _ = PT.load(a.signals)
    np.savez(os.path.join(a.signals, a.name), F=np.concatenate(Fm), FX=np.concatenate(FX), sports=np.array(sorted(set(M['sport'].tolist()))), games=np.array([done, len(ms)]))
    if a.lag == 0:        # per-second states: game, first in-play second (epoch s), seconds, result, sport; then flags / spread / bid / ask in cents, one after another
        ln = np.array([x[2] for x in ST], np.int64)
        np.savez(os.path.join(a.signals, a.name.replace('.npz', '_states.npz')), game=np.array([x[0] for x in ST], np.int64), sec0=np.array([x[1] for x in ST], np.int64), n=ln,
                 off=np.r_[0, np.cumsum(ln)[:-1]], won=np.array([x[3] for x in ST]), sport=np.array([x[4] for x in ST], np.int16),
                 flags=np.concatenate([x[5] for x in ST]), spread=np.concatenate([x[6] for x in ST]), bid=np.concatenate([x[7] for x in ST]), ask=np.concatenate([x[8] for x in ST]))
    print(f'{done} games with fills, {sum(len(x) for x in Fm):,} fills over {len(CONFIGS)} settings, {time.time() - t0:.0f} s')


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    Z = np.load(os.path.join(a.signals, a.name))
    txt = os.path.join(a.signals, a.name.replace('.npz', '.txt'))
    Fm, FX, sports = Z['F'], Z['FX'], Z['sports'].tolist()
    ci, gm, tms, sg, q, w, m60, m300, won, spi, inplay, spr, cls = (Fm[:, j] for j in range(13))
    raw = Fm[:, 13]
    ci = ci.astype(int)
    k60, k300 = sg * (m60 - q) * 100, sg * (m300 - q) * 100
    kres = np.where(won >= 0, sg * (won - q) * 100, np.nan)
    fc = {k: i for i, k in enumerate(FN)}
    has = np.isfinite(FX[:, 0])
    rng = np.random.default_rng(0)
    days_all = sorted({dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in tms})
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    half = d_ms('2026-11-21')
    ngames, nall = int(Z['games'][0]), int(Z['games'][1])

    def stat(v, g, ww, n_min=15):
        ok = np.isfinite(v)
        v, g, ww = v[ok], g[ok], ww[ok]
        if len(v) < n_min:
            return f'{"-":>22}'
        ug, inv = np.unique(g, return_inverse=True)
        sums, cnts = np.bincount(inv, v * ww, len(ug)), np.bincount(inv, ww, len(ug))
        bs = [(lambda z: sums[z].sum() / max(cnts[z].sum(), 1e-9))(rng.integers(0, len(ug), len(ug))) for _ in range(400)]
        return f'{(v * ww).sum() / ww.sum():+5.2f} [{np.percentile(bs, 5):+.2f}..{np.percentile(bs, 95):+.2f}]'
    cname = lambda c: f'{"calm" if CONFIGS[c][0] else "any "} market, inside {CONFIGS[c][1]}c+, {CONFIGS[c][2]} s late'
    say(f'RESTING ORDERS STEERED BY WHAT WE KNOW: {ngames} games with real trades (of {nall} fetched), {len(Fm):,} simulated fills over {len(CONFIGS)} settings, {days_all[0][8:]}-{days_all[-1][8:]} Nov; '
        f'the inputs are known for {has.mean() * 100:.0f}% of fills.')
    say('Cents a share KEPT (our side\'s mark later minus the fill price), share-weighted, [90% range by game]. Nothing is charged for getting out.')
    say('')
    say('1. THE PLAIN ORDERS, in play (both sides always in the book)')
    say(f'   {"":<36}{"fills":>6} {"games":>6}   after 1 min            | after 5 min            | at the result')
    live = inplay > 0.5
    for c in range(len(CONFIGS)):
        r = (ci == c) & live
        if r.sum() >= 30:
            say(f'   {cname(c):<36}{r.sum():>6,} {len(np.unique(gm[r])):>6,}   {stat(k60[r], gm[r], w[r])} | {stat(k300[r], gm[r], w[r])} | {stat(kres[r], gm[r], w[r])}')
    say('   before the start:')
    for c in range(len(CONFIGS)):
        r = (ci == c) & ~live
        if r.sum() >= 30:
            say(f'   {cname(c):<36}{r.sum():>6,} {len(np.unique(gm[r])):>6,}   {stat(k60[r], gm[r], w[r])} | {stat(k300[r], gm[r], w[r])} | {stat(kres[r], gm[r], w[r])}')
    # ---------------- 2: simple signals
    V = lambda k1, k2=None: FX[:, fc[k1]] - (FX[:, fc[k2]] if k2 else 0)
    sigs = (('bookmaker families moved in 5 s', V('fam_up5', 'fam_dn5')), ('bookmaker families moved in 30 s', V('fam_up30', 'fam_dn30')), ('the superline is "ahead"', V('lead_up', 'lead_down')),
            ('the superline is above / below Polymarket', V('gap_e')), ('all bookmakers\' mean gap to Polymarket', V('fam_gap_mean')), ('the superline is in a wave', V('sl_wave')),
            ('the superline\'s move, 10 s', V('sl_r10')), ('the superline\'s move, 60 s', V('sl_r60')), ('order book: more size on our side (best level)', V('ob_imb')), ('order book within 2c', V('ob_dimb')),
            ('order-flow imbalance, 10 s', V('ofi_10')), ('order-flow imbalance, 60 s', V('ofi_60')), ('trade prints, 30 s', V('tf_sgn_30')), ('trade prints, 120 s', V('tf_sgn_120')),
            ('Polymarket\'s move, 10 s', V('pm_r10')), ('Polymarket\'s move, 60 s', V('pm_r60')), ('Polymarket\'s move, 180 s', V('pm_r180')), ('Polymarket\'s last wave', V('pm_prev1')),
            ('Polymarket is moving now (its CUSUM)', V('pm_U', 'pm_D')))
    for title, base in (('calm market, inside 2c+, 2 s late (most fills)', (ci == CONFIGS.index((1, 2, 2))) & live), ('any market, inside 2c+, 2 s late', (ci == CONFIGS.index((0, 2, 2))) & live),
                        ('any market, inside 5c+, 2 s late', (ci == CONFIGS.index((0, 5, 2))) & live)):
        b = base & has
        if b.sum() < 200:
            continue
        say('')
        say(f'2. EACH SIGNAL, {title}: {b.sum():,} fills, all {stat(k300[b], gm[b], w[b])} after 5 min. "With us" = the signal pointed the way our filled order needs.')
        say(f'   {"":<48}{"with us":>8}  kept 5 min            {"against us":>10}  kept 5 min            {"no view":>8}  kept 5 min           | with us minus against, at the result')
        for name, v in sigs:
            pos, neg, zero = b & (v > 1e-9), b & (v < -1e-9), b & (np.abs(v) <= 1e-9)
            dres = (np.nansum(kres[pos] * w[pos]) / max(w[pos][np.isfinite(kres[pos])].sum(), 1e-9)) - (np.nansum(kres[neg] * w[neg]) / max(w[neg][np.isfinite(kres[neg])].sum(), 1e-9)) if pos.sum() >= 30 and neg.sum() >= 30 else float('nan')
            say(f'   {name:<48}{pos.sum():>8,}  {stat(k300[pos], gm[pos], w[pos])} {neg.sum():>10,}  {stat(k300[neg], gm[neg], w[neg])} {zero.sum():>8,}  {stat(k300[zero], gm[zero], w[zero])} | {dres:+.2f}')
    # ---------------- 3: steering rules, both halves
    say('')
    say('3. STEERING RULES: leave an order in the book only while the rule allows that side. Kept 5 min, cents a share; fills kept of all; and the same in each half of the week.')
    rules = (('never against the bookmakers\' last 30 s', V('fam_up30', 'fam_dn30') >= 0), ('only WITH the bookmakers\' last 30 s', V('fam_up30', 'fam_dn30') > 0),
             ('never against a superline lead', V('lead_up', 'lead_down') >= 0), ('never against the superline\'s 60 s move', V('sl_r60') >= -1e-9),
             ('never against the order book (best level)', V('ob_imb') >= 0), ('never against the 60 s order flow', V('ofi_60') >= 0),
             ('only after Polymarket moved AGAINST us in 60 s (the pull-back)', V('pm_r60') < -1e-9), ('only after Polymarket moved OUR way in 60 s', V('pm_r60') > 1e-9),
             ('never against bookmakers 30 s AND never against the order book', (V('fam_up30', 'fam_dn30') >= 0) & (V('ob_imb') >= 0)),
             ('with the gap to all bookmakers (they sit on our side of Polymarket)', V('fam_gap_mean') > 0))
    for c in (CONFIGS.index((1, 2, 2)), CONFIGS.index((0, 2, 2)), CONFIGS.index((1, 3, 2)), CONFIGS.index((0, 3, 2)), CONFIGS.index((0, 5, 2)), CONFIGS.index((0, 5, 1))):
        b = (ci == c) & live & has
        if b.sum() < 200:
            continue
        e_, l_ = b & (tms < half), b & (tms >= half)
        say(f'   {cname(c)}: plain {b.sum():,} fills {stat(k300[b], gm[b], w[b])} | 17-20 Nov {stat(k300[e_], gm[e_], w[e_])} | 21-23 Nov {stat(k300[l_], gm[l_], w[l_])} | at the result {stat(kres[b], gm[b], w[b])}')
        for name, allow in rules:
            r = b & allow
            if r.sum() < 30:
                continue
            say(f'      {name:<64}{r.sum():>6,} ({r.sum() / b.sum() * 100:3.0f}%) {stat(k300[r], gm[r], w[r])} | {stat(k300[r & e_], gm[r & e_], w[r & e_])} | {stat(k300[r & l_], gm[r & l_], w[r & l_])} | {stat(kres[r], gm[r], w[r])}')
    with open(txt, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    # ---------------- 4: a model on fills
    import torch
    import lab as LB
    dev = MN.pick_device()
    tg = lambda v: torch.from_numpy(np.asarray(v)).to(dev)
    say('')
    say('4. A MODEL ON FILLS: trees trained on the fills of earlier days (all settings together) predict what a fill keeps after 5 min; a side is pulled when the model expects it to lose.')
    use = has & np.isfinite(k300) & live
    y = np.clip(np.nan_to_num(k300), -15, 15).astype(np.float32)
    Xb = tg(LB.binned(np.nan_to_num(FX), np.flatnonzero(use & (tms < d_ms(days_all[min(2, len(days_all) - 1)])))))
    P = np.full(len(Fm), np.nan)
    TH = {}
    for dday in days_all[1:]:
        d0 = d_ms(dday)
        tr_all = np.flatnonzero(use & (tms + 600_000 < d0))
        today = np.flatnonzero(use & (tms >= d0) & (tms < d0 + DAY))
        if len(tr_all) < 3000 or not len(today):
            continue
        tr_all = tr_all[np.argsort(tms[tr_all], kind='stable')]
        cut = int(0.8 * len(tr_all))
        va, tr = tr_all[cut:], tr_all[:cut]
        tr = tr[tms[tr] + 600_000 < tms[va].min()]
        m = LB.GB(loss='l2', depth=4, min_leaf=100, lam=50.0, row_frac=0.7, n_trees=200, lr=0.05).fit(Xb[tg(tr)], tg(y)[tg(tr)], Xb[tg(va)], tg(y)[tg(va)])
        P[today] = m.predict(Xb[tg(today)])
        pv = m.predict(Xb[tg(va)])
        TH[dday] = {qq: np.quantile(pv, qq) for qq in (0.25, 0.5, 0.75)}
    sc_days = sorted(TH)
    rowday = np.array([dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in tms])
    scored = np.isfinite(P) & np.isin(rowday, sc_days)
    if scored.sum() >= 500:
        say(f'   scored on {", ".join(x[8:] for x in sc_days)} Nov: {scored.sum():,} fills; rank correlation of its call with what the fill kept: {np.corrcoef(np.argsort(np.argsort(P[scored])), np.argsort(np.argsort(k300[scored])))[0, 1]:+.3f}')
        thr = lambda qq: np.array([TH[d_][qq] if d_ in TH else np.nan for d_ in rowday])
        for c in range(len(CONFIGS)):
            b = (ci == c) & scored
            if b.sum() < 150:
                continue
            say(f'   {cname(c)}: plain {b.sum():,} fills {stat(k300[b], gm[b], w[b])}, at the result {stat(kres[b], gm[b], w[b])}')
            for nm, keep_ in (('keep where the model expects a gain (call > 0)', P > 0), ('keep its better half (median of earlier days)', P >= thr(0.5)), ('keep its best quarter', P >= thr(0.75))):
                r, x_ = b & keep_, b & ~keep_
                if r.sum() >= 30 and x_.sum() >= 30:
                    say(f'      {nm:<48}{r.sum():>6,} kept {stat(k300[r], gm[r], w[r])}, at the result {stat(kres[r], gm[r], w[r])} | the {x_.sum():,} pulled would have kept {stat(k300[x_], gm[x_], w[x_])}')
    else:
        say('   too few fills on days with enough earlier fills to train on')
    # ---------------- 5: dollars, order size, sports
    say('')
    nd = max(len(sc_days), 1)
    say(f'5. IN DOLLARS, on the days the model is scored ({nd} days), in play, in THESE {ngames} games (about a third of the two-way games recorded). An order of S shares takes min(S, the real trade\'s size).')
    say(f'   {"":<66}{"fills":>7} | S = 20: $ a day at 5 min / at the result | S = 100                  | S = 500                  | cents a share, S = 100 [90% by game]')
    fam30 = V('fam_up30', 'fam_dn30')
    pol = []
    for c_, nm in ((CONFIGS.index((0, 5, 1)), 'inside 5c+, 1 s'), (CONFIGS.index((0, 5, 2)), 'inside 5c+, 2 s'), (CONFIGS.index((0, 3, 1)), 'inside 3c+, 1 s'), (CONFIGS.index((0, 3, 2)), 'inside 3c+, 2 s'),
                   (CONFIGS.index((0, 2, 1)), 'inside 2c+, 1 s'), (CONFIGS.index((0, 2, 2)), 'inside 2c+, 2 s')):
        b = (ci == c_) & scored
        pol += [(f'{nm}: plain', b), (f'{nm}: never against the bookmakers\' last 30 s', b & (fam30 >= 0)), (f'{nm}: the model expects a gain', b & (P > 0))]
    for nm, mk in pol:
        if mk.sum() < 30:
            continue
        cells = []
        for S_ in (20, 100, 500):
            ws = np.minimum(S_, raw)
            cells.append(f'{np.nansum(k300[mk] * ws[mk]) / 100 / nd:>+9.0f} / {np.nansum(kres[mk] * ws[mk]) / 100 / nd:>+9.0f}')
        w100 = np.minimum(100, raw)
        say(f'   {nm:<66}{mk.sum():>7,} | {cells[0]:<38} | {cells[1]:<24} | {cells[2]:<24} | {stat(k300[mk], gm[mk], w100[mk])}')
    say(f'   the real trades that filled us: median {np.median(raw[scored]):.0f} shares, 75% under {np.quantile(raw[scored], 0.75):.0f}, 90% under {np.quantile(raw[scored], 0.9):.0f}')
    bm = (ci == CONFIGS.index((0, 2, 1))) & scored & (P > 0)
    say('   the model\'s kept fills (inside 2c+, 1 s late) by sport, cents a share after 5 min / at the result: ' + '; '.join(
        f'{sports[j]} {stat(k300[bm & (spi == j)], gm[bm & (spi == j)], w[bm & (spi == j)])} / {stat(kres[bm & (spi == j)], gm[bm & (spi == j)], w[bm & (spi == j)])} ({int((bm & (spi == j)).sum())})'
        for j in range(len(sports)) if (bm & (spi == j)).sum() >= 60))
    say('   and by day: ' + '; '.join(f'{d_[8:]} Nov {stat(k300[bm & (rowday == d_)], gm[bm & (rowday == d_)], w[bm & (rowday == d_)])} ({int((bm & (rowday == d_)).sum())})' for d_ in sc_days))
    pr_ = FX[:, fc['q_price']]
    say('   and by the price of what we were filled in: ' + '; '.join(f'{lo * 100:.0f}-{hi * 100:.0f}c {stat(k300[bm & (pr_ >= lo) & (pr_ < hi)], gm[bm & (pr_ >= lo) & (pr_ < hi)], w[bm & (pr_ >= lo) & (pr_ < hi)])}'
                                                                 for lo, hi in ((0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.91))))
    with open(txt, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'test'])
    ap.add_argument('--db')
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=20)
    ap.add_argument('--lag', type=int, default=0, help='read every input this many seconds before the decision (leak check)')
    ap.add_argument('--name', default='makersteer.npz')
    ap.add_argument('--causal', action='store_true', help='bookmaker lines without hindsight (waves.book_lines causal=True): what a live bot can compute; tools/maker needs this')
    a = ap.parse_args()
    (cmd_build if a.cmd == 'build' else cmd_test)(a)


if __name__ == '__main__':
    main()
