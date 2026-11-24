"""PILOT: a WINNER model (owner, 23 Nov night: the value idea "needs a model too ... map score, the teams' track record,
maybe something from the suspicious-match work ... its own prediction for where the match is headed").

It predicts who WINS (not the next move), so a bet is held to the result: one fill, one fee. Rows = lab_sec.npz
(in-play decision seconds); the winner per game from Polymarket's last quotes (value.py).

INPUTS
  Polymarket   price (log-odds), spread, order book, activity, minutes played, sport; the game's own path: the price at
               the first in-play second, how far it has moved since, how far below its high / above its low.
  bookmakers   every family's gap to Polymarket and whether it quotes, the mean / widest gaps, the lead state, the
               bookmakers' own level now and at the start.
  score        the live score from the 1xBet family (linebet / melbet / paripesa; recorded since 22 Nov only): sets /
               maps / goals / points and the running set or half, seen from the token's side.
  record       each side's results in the games recorded BEFORE this one started (17 Nov on: thin), and for CS2 the
               Valve rank of both teams (cs2_activity) and whether it is a tier-1 game.
  suspicious   the pre-match "line moved on 3+ platforms" flag of the Suspicious page (line_moves: size, direction,
               platforms). cs2_activity's money figures are totals written AFTER the game, so they are left out (they
               would see the future).
MODELS (light: boosted trees, depth 4, <= 120 trees, at most 80 rows a game, each trained on the games that ended before
the day it is scored on, last fifth of those games for stopping and thresholds; 20-23 Nov). The trees START from
Polymarket's own price (the own view: from the bookmakers' level) and learn corrections; none are kept unless they
beat the bare price on the validation games:
  Polymarket only | + bookmakers | + score, record, suspicious | OWN VIEW: no Polymarket price at all.
SCORES, games weighing the same: log-loss (lower = better; shown as the gain over Polymarket's own price, x1000) with a
90% range resampling games; when a model disagrees with Polymarket by 5+ points, how far the result went its way.
THE BET: per game the first second (after 10 live minutes) where the model's chance beats the ask + fee by m (m chosen on
the validation games, total minus one SE): $100 at the ask, held to the result; next to the same seconds bet against it.

usage: python tools/signals/pilot_winner.py --db ~/swifteye/oddsfeed.db --signals ~/swifteye/research/lab
"""
import argparse
import datetime as dt
import os
import re
import sqlite3
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import features as F  # noqa: E402
import lab as LB  # noqa: E402
import mine as MN  # noqa: E402
import value as VL  # noqa: E402
import waves as W  # noqa: E402

C = MN.C
sig = lambda v: 1 / (1 + np.exp(-v))
norm = lambda s: re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()
THREE_WAY = set(W.THREE_WAY)
SCORE_BOOKS = ('linebet', 'melbet', 'paripesa')
PAIR = re.compile(r'(\d+)\s*[-:]\s*(\d+)')


class GBO(LB.GB):
    """lab.GB starting from a given margin per row (the market's own log-odds): the trees learn only corrections, and
    none are kept unless they beat the bare offset on the validation rows."""

    def fit_off(self, Xtr, ytr, otr, Xva, yva, ova, cols=None):
        import torch
        bce = torch.nn.functional.binary_cross_entropy_with_logits
        gen = torch.Generator().manual_seed(self.seed)
        call = torch.arange(Xtr.shape[1]) if cols is None else cols
        Ftr, Fva = otr.clone(), ova.clone()
        self.trees, best = [], (bce(Fva, yva).item(), 0)
        for it in range(self.n_trees):
            rows = torch.randint(0, len(ytr), (max(1, int(len(ytr) * self.row_frac)),), generator=gen)
            cc = call[torch.randperm(len(call), generator=gen)[:max(1, int(len(call) * self.col_frac))]]
            pr = torch.sigmoid(Ftr[rows])
            f, t, v = self._tree(Xtr[rows], ytr[rows] - pr, (pr * (1 - pr)).clamp_min(1e-4), cc)
            self.trees.append((f, t, v))
            Ftr += v[MN.Trees.leaf(Xtr, f, t)]
            Fva += v[MN.Trees.leaf(Xva, f, t)]
            if (it + 1) % 5 == 0:
                loss = bce(Fva, yva).item()
                if loss < best[0] - 1e-6:
                    best = (loss, it + 1)
                elif it + 1 - best[1] >= self.patience:
                    break
        self.trees = self.trees[:best[1]]
        return self

    def predict_off(self, Xb, off):
        F_ = off.clone()
        for f, t, v in self.trees:
            F_ += v[MN.Trees.leaf(Xb, f, t)]
        return F_.numpy()


def overlap(a, b):
    A, B = set(norm(a).split()), set(norm(b).split())
    return len(A & B) / max(1, min(len(A), len(B)))


def score_series(c, m, home, away):
    """[(ts, top a, top b, sub a, sub b)] for the match, turned so a = the match's home side."""
    rows = []
    for book, ev, bh, ba in c.execute(f"SELECT book, book_event_id, home, away FROM book_events WHERE match_id = ? AND book IN ({','.join('?' * len(SCORE_BOOKS))})", (m, *SCORE_BOOKS)).fetchall():
        swap = overlap(bh, away) + overlap(ba, home) > overlap(bh, home) + overlap(ba, away)
        for ts, sc, det in c.execute('SELECT ts_ms, score, detail FROM book_scores WHERE book = ? AND book_event_id = ? ORDER BY ts_ms', (book, ev)):
            t = PAIR.search(sc or '')
            if not t:
                continue
            a, b = int(t.group(1)), int(t.group(2))
            sub = PAIR.findall((det or '').split('|')[0])
            sa, sb = (int(sub[-1][0]), int(sub[-1][1])) if sub else (0, 0)
            if swap:
                a, b, sa, sb = b, a, sb, sa
            rows.append((ts, a, b, sa, sb))
        if rows:
            break                                        # one book of the family is enough
    rows.sort()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    import torch
    torch.manual_seed(0)
    t0 = time.time()
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    S = np.load(os.path.join(a.signals, 'lab_sec.npz'))
    T, G0, SP = S['t'], S['game'].astype(np.int64), S['sport']
    KEY = G0 * 4 + VL.outcome_index(G0, T)
    X = S['X']
    order = np.lexsort((T, KEY))
    T, G0, SP, KEY, X = T[order], G0[order], SP[order], KEY[order], X[order]
    c = sqlite3.connect('file:' + a.db + '?mode=ro', uri=True, timeout=30)

    # ---------------- winners of every Polymarket game (labels, and the track record)
    info = {m: (sp, h, aw, st) for m, sp, h, aw, st in c.execute('SELECT id, sport, home, away, start_ms FROM matches')}
    pm_games = [m for (m,) in c.execute("SELECT DISTINCT match_id FROM book_events WHERE book = 'polymarket'")]
    res = VL.results(a.db, a.signals)                    # match -> (home won, away won) where known
    keys = np.unique(KEY)
    y_of = {}
    for k in keys:
        r = res.get(int(k) // 4)
        v = None if r is None else r[int(k) % 4]
        if v is not None:
            y_of[int(k)] = v
    y = np.array([y_of.get(int(k), -1) for k in KEY], np.float32)
    say(f'{len(T):,} in-play rows, {len(keys):,} game outcomes, winner known for {len(y_of):,}; results known for {sum(1 for v in res.values() if v[0] is not None):,} of {len(pm_games):,} Polymarket games ({time.time() - t0:.0f} s)')

    # ---------------- track record: each side's earlier results in this recording
    rec, hist = {}, {}
    for m in sorted((m for m in res if m in info and info[m][3]), key=lambda m: info[m][3]):
        sp, h, aw, st = info[m]
        hk, ak = (sp, norm(h)), (sp, norm(aw))
        past = lambda k: [w for (t_, w) in hist.get(k, []) if t_ + 4 * 3_600_000 < st]
        ph, pa = past(hk), past(ak)
        rec[m] = ((sum(ph) + 1) / (len(ph) + 2), len(ph), (sum(pa) + 1) / (len(pa) + 2), len(pa))
        hw, awn = res[m]
        if hw is not None:
            hist.setdefault(hk, []).append((st, float(hw)))
            a_won = awn if awn is not None else (1 - hw if sp not in THREE_WAY else None)
            if a_won is not None:
                hist.setdefault(ak, []).append((st, float(a_won)))
    vrs = {}
    for h, aw, st, vh, va, t1 in c.execute('SELECT home, away, start_ms, vrs_home, vrs_away, tier1 FROM cs2_activity'):
        vrs[(norm(h), norm(aw))] = (vh or 0, va or 0, t1 or 0)
    lm = {m: (o, mv, pl) for m, o, mv, pl in c.execute('SELECT match_id, outcome, move, platforms FROM line_moves')}

    # ---------------- per-row extra inputs
    n = len(T)
    first = np.r_[0, np.flatnonzero(KEY[1:] != KEY[:-1]) + 1]
    last = np.r_[first[1:], n]
    x = X[:, C['x']].astype(np.float64)
    gapm = np.nan_to_num(X[:, C['fam_gap_mean']].astype(np.float64))
    PATH = np.zeros((n, 6), np.float32)                  # x at the start, moved since, below the high, above the low, bookmakers now, bookmakers at the start
    SC = np.zeros((n, 5), np.float32)                    # has score, top diff, top sum, sub diff, sub sum
    RC = np.zeros((n, 7), np.float32)                    # own record, own games, other record, other games, own rank, other rank, tier 1
    SU = np.zeros((n, 3), np.float32)                    # flagged, move toward this token, platforms
    n_sc = 0
    for f_, l_ in zip(first, last):
        key = int(KEY[f_])
        m, k = key // 4, key % 4
        xs = x[f_:l_]
        PATH[f_:l_, 0], PATH[f_:l_, 1] = xs[0], xs - xs[0]
        PATH[f_:l_, 2], PATH[f_:l_, 3] = np.maximum.accumulate(xs) - xs, xs - np.minimum.accumulate(xs)
        PATH[f_:l_, 4], PATH[f_:l_, 5] = xs + gapm[f_:l_], xs[0] + gapm[f_]
        sgn = 1 if k == 0 else -1
        sp, h, aw, st = info.get(m, (None, '', '', None))
        ser = score_series(c, m, h, aw)
        if ser:
            n_sc += 1
            st_ = np.array([r[0] for r in ser], np.int64)
            j = np.searchsorted(st_, T[f_:l_], side='right') - 1
            has = j >= 0
            arr = np.array([r[1:] for r in ser], np.float32)[np.clip(j, 0, None)]
            SC[f_:l_, 0] = has
            SC[f_:l_, 1], SC[f_:l_, 2] = np.where(has, sgn * (arr[:, 0] - arr[:, 1]), 0), np.where(has, arr[:, 0] + arr[:, 1], 0)
            SC[f_:l_, 3], SC[f_:l_, 4] = np.where(has, sgn * (arr[:, 2] - arr[:, 3]), 0), np.where(has, arr[:, 2] + arr[:, 3], 0)
        if m in rec:
            rh, nh, ra, na = rec[m]
            own, other = ((rh, nh), (ra, na)) if k == 0 else ((ra, na), (rh, nh))
            RC[f_:l_, 0], RC[f_:l_, 1], RC[f_:l_, 2], RC[f_:l_, 3] = own[0], np.log1p(own[1]), other[0], np.log1p(other[1])
        v = vrs.get((norm(h), norm(aw)))
        if v:
            RC[f_:l_, 4], RC[f_:l_, 5], RC[f_:l_, 6] = (v[0], v[1], v[2]) if k == 0 else (v[1], v[0], v[2])
        if m in lm:
            o, mv, pl = lm[m]
            SU[f_:l_, 0], SU[f_:l_, 1], SU[f_:l_, 2] = 1, (mv or 0) * (1 if (o == 'home') == (k == 0) else -1), pl or 0
    say(f'live score found for {n_sc:,} of {len(first):,} game outcomes; a track record (1+ earlier game for a side) for {int(((RC[first, 1] > 0) | (RC[first, 3] > 0)).sum()):,}; flagged by the Suspicious page {int(SU[first, 0].sum()):,}; Valve rank for {int((RC[first, 4] > 0).sum()):,} ({time.time() - t0:.0f} s)')

    cols = lambda names: [C[k] for k in names]
    pm_cols = cols(['x', 'absx', 'spread', 'pm_quiet', 'pm_r60', 'pm_r180', 'ob_on', 'ob_imb', 'ob_dimb', 'ob_logdepth', 'ctx_pm_rate', 'ctx_spread', 'ctx_vol',
                    'sport_tennis', 'sport_basketball', 'sport_football', 'sport_esports', 'sport_hockey', 'three_way', 'min_from_start'])
    ctx_cols = cols(['sport_tennis', 'sport_basketball', 'sport_football', 'sport_esports', 'sport_hockey', 'three_way', 'min_from_start'])
    bk_cols = cols([f'{f}_{s}' for f in F.FAMS for s in ('gap', 'on')] + ['fam_n', 'fam_gap_mean', 'fam_gap_max', 'fam_gap_min', 'gap_e', 'gap_z', 'lead_stale', 'sl_r60', 'sl_r180', 'ctx_book_rate', 'ctx_gap_scale'])
    FE = np.concatenate([X[:, pm_cols], PATH[:, :4], X[:, bk_cols], PATH[:, 4:], SC, RC, SU, X[:, ctx_cols], X[:, [C['fam_n']]]], 1).astype(np.float32)
    nP, nB = len(pm_cols) + 4, len(bk_cols) + 2
    i_own0 = nP + nB + 5 + 7 + 3
    groups = {'Polymarket': np.arange(0, nP), 'bookmakers': np.arange(nP, nP + nB), 'score': np.arange(nP + nB, nP + nB + 5), 'record': np.arange(nP + nB + 5, nP + nB + 12),
              'suspicious': np.arange(nP + nB + 12, nP + nB + 15)}
    own_cols = np.r_[np.arange(nP + nB - 2, nP + nB), groups['score'], groups['record'], groups['suspicious'], np.arange(i_own0, FE.shape[1])]
    variants = (('Polymarket only', groups['Polymarket']), ('+ bookmakers', np.r_[groups['Polymarket'], groups['bookmakers']]),
                ('+ score, record, suspicious', np.arange(0, i_own0)), ('OWN VIEW (no Polymarket price)', own_cols))
    hs = X[:, C['spread']].astype(np.float64) / 200.0
    live10 = X[:, C['min_from_start']].astype(np.float64) * 60 >= 10
    del X

    p = np.clip(sig(x), 0.005, 0.995)
    pb = np.clip(sig(x + gapm), 0.005, 0.995)
    kidx = np.unique(KEY, return_inverse=True)[1]
    k_first_t, k_last_t = T[first], T[last - 1]
    known = y >= 0
    w_all = 1.0 / np.bincount(kidx)[kidx]
    ll = lambda q, yy: -(yy * np.log(q) + (1 - yy) * np.log(1 - q))
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    tg = lambda v: torch.from_numpy(np.asarray(v))
    rng = np.random.default_rng(0)
    Q = {name: np.full(n, np.nan, np.float32) for name, _ in variants}
    QV = {name: {} for name, _ in variants}              # validation predictions per day, for the bet threshold
    used = {g_: 0 for g_ in groups}
    kept = {name: [] for name, _ in variants}
    t_fit = 0.0
    Xb_all = None
    for dday in ('2026-11-20', '2026-11-21', '2026-11-22', '2026-11-23'):
        d0 = d_ms(dday)
        k_tr = np.flatnonzero(k_last_t + 3_600_000 < d0)
        k_te = np.flatnonzero((k_first_t >= d0) & (k_first_t < d0 + 86_400_000))
        k_tr = k_tr[np.argsort(k_first_t[k_tr], kind='stable')]
        if len(k_tr) < int(os.environ.get('PILOT_MIN_GAMES', 200)) or not len(k_te):
            continue
        cut = int(0.8 * len(k_tr))
        in_tr, in_va, in_te = np.zeros(len(first), bool), np.zeros(len(first), bool), np.zeros(len(first), bool)
        in_tr[k_tr[:cut]], in_va[k_tr[cut:]], in_te[k_te] = True, True, True
        r_tr, r_va, r_te = np.flatnonzero(in_tr[kidx] & known), np.flatnonzero(in_va[kidx] & known), np.flatnonzero(in_te[kidx] & known)

        def thin(r, per):                                # at most `per` rows a game, so long games do not swamp the short ones
            u = rng.random(len(r))
            o_ = np.lexsort((u, kidx[r]))
            rk = np.arange(len(r)) - np.r_[0, np.flatnonzero(np.diff(kidx[r][o_])) + 1][np.cumsum(np.r_[0, np.diff(kidx[r][o_]) != 0])]
            return np.sort(r[o_][rk < per])
        s_tr, s_va = thin(r_tr, 80), thin(r_va, 80)
        Xb_all = torch.from_numpy(LB.binned(FE, s_tr))
        yt = tg(y)
        off_pm = tg(np.clip(x, -5, 5).astype(np.float32))
        off_bk = tg(np.clip(x + gapm, -5, 5).astype(np.float32))
        for name, cc in variants:
            t1 = time.time()
            off = off_bk if name.startswith('OWN') else off_pm
            m = GBO(depth=4, n_trees=120, lr=0.05, min_leaf=300, lam=50.0, row_frac=0.5, col_frac=0.7, patience=25).fit_off(
                Xb_all[tg(s_tr)], yt[tg(s_tr)], off[tg(s_tr)], Xb_all[tg(s_va)], yt[tg(s_va)], off[tg(s_va)], cols=tg(cc.astype(np.int64)))
            t_fit = max(t_fit, time.time() - t1)
            kept[name].append(len(m.trees))
            Q[name][r_te] = sig(m.predict_off(Xb_all[tg(r_te)], off[tg(r_te)]))
            QV[name][dday] = (r_va, sig(m.predict_off(Xb_all[tg(r_va)], off[tg(r_va)])))
            if name.startswith('+ score'):
                for f_, _, _ in m.trees:
                    for lv in f_:
                        for j in lv.tolist():
                            for g_, idx in groups.items():
                                if idx[0] <= j <= idx[-1]:
                                    used[g_] += 1
        say(f'  {dday}: trained on {cut:,} game outcomes ({len(s_tr):,} rows), scored on {len(k_te):,} ({time.time() - t0:.0f} s)')
    say(f'  longest single training: {t_fit:.0f} s; trees kept per day (0 = nothing beat the market price on the validation games): ' + ', '.join(f'{nm.split(" (")[0]} {v}' for nm, v in kept.items()))

    te = np.flatnonzero(np.isfinite(Q['Polymarket only']) & known)
    if not len(te):
        say('no day could be scored (too few earlier games)')
        return
    w = w_all[te] / w_all[te].sum()
    yy = y[te].astype(np.float64)
    say()
    say(f'SCORES on {len(np.unique(KEY[te])):,} game outcomes of 20-23 Nov (games weigh the same). Log-loss of Polymarket\'s own price: {(w * ll(p[te], yy)).sum():.4f}')
    say('  gain over Polymarket\'s price, x1000 (+ = better than Polymarket) [90% range by game] | when it disagrees by 5+ pt: share of rows, and how far the result went ITS way')
    Gm = (KEY[te] // 4)
    for name, q in [('the bookmakers\' level', pb)] + [(nm, np.clip(Q[nm].astype(np.float64), 0.005, 0.995)) for nm, _ in variants]:
        dl = ll(p[te], yy) - ll(q[te], yy)
        lo, hi = VL.boot(Gm, lambda k: (w[k] * dl[k]).sum() / w[k].sum(), n=300)
        dis = np.abs(q[te] - p[te]) >= 0.05
        way = (np.sign(q[te] - p[te]) * (yy - p[te]))[dis]
        say(f'  {name:<32} {1000 * (w * dl).sum():+7.2f} [{1000 * lo:+.2f}..{1000 * hi:+.2f}] | disagrees {dis.mean() * 100:4.1f}% of rows, result went its way by {way.mean() * 100:+.1f} pt' if dis.sum() > 50 else
            f'  {name:<32} {1000 * (w * dl).sum():+7.2f} [{1000 * lo:+.2f}..{1000 * hi:+.2f}] | almost never disagrees by 5 pt')
    tot_used = max(1, sum(used.values()))
    say('  what the full model splits on: ' + ', '.join(f'{g_} {100 * v / tot_used:.0f}%' for g_, v in used.items()))
    for s_ in ('tennis', 'football', 'basketball', 'cs2'):
        mk = SP[te] == s_
        if mk.sum() > 2000:
            ws = w[mk] / w[mk].sum()
            say(f'    {s_:<11} gain x1000: ' + ', '.join(f'{nm.split(" (")[0]} {1000 * (ws * (ll(p[te][mk], yy[mk]) - ll(np.clip(Q[nm][te][mk].astype(np.float64), 0.005, 0.995), yy[mk]))).sum():+.2f}' for nm, _ in variants))

    say()
    say('THE BET: $100 at the ask when the model\'s chance beats ask + fee by m (m from the validation games), held to the result; one bet a game')

    def bets(rows, q, m_):
        """First qualifying row per game outcome: (row, side +1 buy the token / -1 buy against it)."""
        ask_y, ask_n = p[rows] + hs[rows], 1 - p[rows] + hs[rows]
        e_y = q - ask_y - 0.05 * ask_y * (1 - ask_y)
        e_n = (1 - q) - ask_n - 0.05 * ask_n * (1 - ask_n)
        okp = live10[rows] & (hs[rows] <= 0.015) & (p[rows] > 0.05) & (p[rows] < 0.95)
        side = np.where(e_y >= m_, 1, np.where(e_n >= m_, -1, 0)) * okp
        idx = np.flatnonzero(side != 0)
        if not len(idx):
            return np.array([], np.int64), np.array([], np.int64)
        g_ = (KEY[rows][idx] // 4)
        keep = idx[np.r_[True, g_[1:] != g_[:-1]]]
        return rows[keep], side[keep]

    def pay_of(r, side):
        paid = np.where(side > 0, p[r], 1 - p[r]) + hs[r]
        won = np.where(side > 0, y[r], 1 - y[r])
        return 100 * (won - paid - 0.05 * paid * (1 - paid)) / paid, won
    for name in ('+ bookmakers', '+ score, record, suspicious', 'OWN VIEW (no Polymarket price)'):
        R, SD = [], []
        for dday, (r_va, qv) in QV[name].items():
            best, m_best = -1e18, None
            for m_ in (0.02, 0.03, 0.05, 0.08):
                rv, sv = bets(r_va, qv, m_)
                if len(rv) >= 20:
                    pv_, _ = pay_of(rv, sv)
                    cautious = pv_.sum() - pv_.std() * np.sqrt(len(pv_))
                    if cautious > best:
                        best, m_best = cautious, m_
            if m_best is None or best <= 0:
                continue                                 # nothing made money on the validation games: no bets that day
            d0 = d_ms(dday)
            r_te = np.flatnonzero((T >= d0) & (T < d0 + 86_400_000) & np.isfinite(Q[name]) & known)
            rt, st_ = bets(r_te, Q[name][r_te].astype(np.float64), m_best)
            R.append(rt)
            SD.append(st_)
        if not R or not sum(len(r) for r in R):
            say(f'  {name:<32} no bets: no threshold made money on the validation games')
        else:
            r, sd = np.concatenate(R), np.concatenate(SD)
            pay, won = pay_of(r, sd)
            anti, _ = pay_of(r, -sd)
            lo, hi = VL.boot(KEY[r] // 4, lambda k: pay[k].mean(), n=1000)
            say(f'  {name:<32} {len(r):>5,} bets: ${pay.mean():+.2f} per $100 [{lo:+.2f}..{hi:+.2f}], win {won.mean() * 100:.0f}% at {np.mean(np.where(sd > 0, p[r], 1 - p[r]) + hs[r]) * 100:.0f}c paid | bet AGAINST the model on the same seconds: ${anti.mean():+.2f}')
        # the same rule with a fixed 5-point margin on every scored day (no threshold search), for reference
        r_all = np.flatnonzero(np.isfinite(Q[name]) & known)
        rt, st_ = bets(r_all, Q[name][r_all].astype(np.float64), 0.05)
        if len(rt) >= 20:
            pay, won = pay_of(rt, st_)
            anti, _ = pay_of(rt, -st_)
            lo, hi = VL.boot(KEY[rt] // 4, lambda k: pay[k].mean(), n=1000)
            say(f'      fixed 5-pt margin, every day  {len(rt):>5,} bets: ${pay.mean():+.2f} per $100 [{lo:+.2f}..{hi:+.2f}], win {won.mean() * 100:.0f}% | against: ${anti.mean():+.2f}')
    with open(os.path.join(a.signals, 'pilot_winner.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    print(f'done in {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main()
