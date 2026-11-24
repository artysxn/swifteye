"""Everything the fill model reads, for ONE game at ONE moment, from that game's rows up to that moment.

The live paper bot (paperbot.py) and the backtest (tools/signals/makersteer.py) must see the same numbers, so this file
does nothing of its own: it calls the research code (features.build with t_end, mine.lead_record, labfeat.extra,
wavedir.flow / wave_hist) in the order makersteer.one() does, and returns the row of the LAST grid second.
`parity.py` checks on recorded games that this row equals the backtest's row for the same second."""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'signals'))
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import features as F  # noqa: E402
import labfeat as LF  # noqa: E402
import mine as MN  # noqa: E402
import wavedir as WD  # noqa: E402
import waves as W  # noqa: E402
from booklead import held  # noqa: E402

C = MN.C
FN = MN.NAMES + MN.HN + LF.QN + WD.DN + ['q_spread', 'q_price', 'q_live_min', 'q_k', 'q_L', 'q_calm']
Q_K, Q_L, Q_CALM = 2.0, 1.0, 0.0          # the backtest's setting the wallet replays: one cent inside any spread of 2c+, 1 s late, any market
TWO_WAY = {'tennis', 'basketball', 'cs2', 'hockey', 'valorant', 'dota2', 'amfootball', 'lol', 'volleyball', 'baseball', 'mma'}


def grid(pm_rows, bk_rows, sport, start_ms, t_end=None):
    """pm_rows: (ts_ms, server_ts_ms, outcome, bid, ask, last, bid_size, ask_size, bid_depth, ask_depth), by time.
    bk_rows: (ts_ms, book, outcome, new_price) of the bookmakers' 1x2 changes, by time.
    Returns None, or the whole grid: ts, FE (seen from a buyer of the home token), FEm (from a seller), lm, X."""
    book = [(r[0], r[2], r[6], r[7], r[8], r[9]) for r in pm_rows if r[6] is not None]
    G = F.build([r[:5] for r in pm_rows], bk_rows, 'home', sport, start_ms, sport in W.THREE_WAY, book or None, full=True, t_end=t_end, causal=True)
    if G is None:
        return None
    tsf, X = np.asarray(G['ts'], np.int64), G['X']
    nf = len(tsf)
    live = X[:, C['inplay']] > 0.5
    Hh = MN.lead_record(X, live, 1.0)
    pr4 = [(q[0], q[3], q[4], q[5]) for q in pm_rows if q[2] == 'home']
    pt = np.array([q[0] for q in pr4], np.int64)
    bf = held(pt, np.array([np.nan if q[1] is None else q[1] for q in pr4], float), tsf)
    af = held(pt, np.array([np.nan if q[2] is None else q[2] for q in pr4], float), tsf)
    sz = [np.full(nf, np.nan)] * 4
    bo = [r for r in book if r[1] == 'home']
    if bo:
        bt = np.array([r[0] for r in bo], np.int64)
        sz = [held(bt, np.array([np.nan if r[2 + j] is None else r[2 + j] for r in bo], float), tsf) for j in range(4)]
    Q = LF.extra(X, bf, af, sz, G['pm_eps'], G['sl_eps'])
    p1, p2, pr5 = WD.wave_hist(G['pm_eps'], nf)
    s1, _, sr5 = WD.wave_hist(G['sl_eps'], nf)
    Dx = np.concatenate([WD.flow(pr4, tsf, (bf + af) / 2), np.stack([p1, p2, pr5, s1, sr5], 1).astype(np.float32)], 1)
    FE = np.concatenate([X, Hh, Q, Dx], 1)
    FEm = np.concatenate([MN.mirror(X), Hh, LF.mirror(Q), WD.mirror_d(Dx)], 1)
    return dict(ts=tsf, FE=FE, FEm=FEm, lm=Hh[:, MN.HN.index('live_min')], X=X)


def at(pm_rows, bk_rows, sport, start_ms, T):
    """What a bot knows about the game at T (ms): the model's inputs for a resting buy and a resting sale of the home
    token decided now, and the plain facts the rules read. Rows after T are ignored."""
    pm_rows = [r for r in pm_rows if r[0] <= T]
    bk_rows = [r for r in bk_rows if r[0] <= T]          # the bookmaker rows come from two tables and are not in one time order
    home = [r for r in pm_rows if r[2] == 'home']
    if not home or not bk_rows:
        return None
    bid, ask = home[-1][3], home[-1][4]
    if bid is None or ask is None or not (0 < bid < ask < 1):
        return None
    g = grid(pm_rows, bk_rows, sport, start_ms, t_end=T)
    if g is None:
        return None
    X = g['X'][-1]
    spread_c, lm = (ask - bid) * 100, float(g['lm'][-1])
    qb, qa = round(bid + 0.01, 4), round(ask - 0.01, 4)
    row_buy = np.concatenate([g['FE'][-1], [spread_c, qb, lm, Q_K, Q_L, Q_CALM]]).astype(np.float32)
    row_sell = np.concatenate([g['FEm'][-1], [spread_c, 1 - qa, lm, Q_K, Q_L, Q_CALM]]).astype(np.float32)
    return dict(T=int(T), grid_ts=int(g['ts'][-1]), bid=bid, ask=ask, spread_c=spread_c, mid=(bid + ask) / 2, inplay=bool(X[C['inplay']] > 0.5), live_min=lm,
                fam30=float(X[C['fam_up30']] - X[C['fam_dn30']]), lead=float(X[C['lead_up']] - X[C['lead_down']]), age_s=(T - pm_rows[-1][0]) / 1000.0, grid_s=len(g['ts']), books=int(X[C['fam_n']]),
                row_buy=row_buy, row_sell=row_sell, qb=qb, qa=qa)
