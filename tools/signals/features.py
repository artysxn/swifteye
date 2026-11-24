"""Signal features and labels on a 1-second grid, for one (game, outcome).

Everything at second i uses data up to i only (receive clock: what a bot here could have seen). Inputs are
the same lines the wave tools use (tools/waves: Polymarket mid, margin-free bookmaker lines, the superline,
the lead state and the trend waves), so the model sees what the viewer shows, plus each bookmaker family.

LABELS (executable, as the bots trade): a signal at second i is acted on L = 1 s later. Up = buy the outcome's
Yes at the ask; down = buy its No at 1 - the Yes bid. Fees 5% x p(1-p) per share on the buy and on the sale.
Within H = 120 s, the trade is a WIN (label 1) if its net (sale at the bid of the token bought, minus what it
cost, both fees paid) reaches +TP = 1c before the token's sell price falls STOP = 4c below where it stood at
the buy; else 0 (stopped, or neither by H). Also kept: the net at H and the trade's own net (exit at the
take-profit, the stop or H), for P&L. `evt` marks seconds where any line changed: where a bot decides.
"""
import math
import os
import sys
import warnings

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import lead as L  # noqa: E402
import waves as W  # noqa: E402

FAMS = ['betby', 'kambi', '1xbet', 'm88', 'fonbet', 'leon', 'olimp', 'pinnacle', 'marathon', 'thunderpick', 'betcity', 'altenar']
SPORTS = ['tennis', 'basketball', 'football', 'esports', 'hockey']
ESPORTS = {'cs2', 'dota2', 'valorant', 'sc2', 'lol'}
WIN = (1, 3, 10, 30, 60, 180)        # return windows, s
LAT, HOR, TP, STOP = 1, 120, 0.01, 0.04
FEE = 0.05
MAX_SPREAD = 0.06
STEP = 1000


def fee(p):
    return FEE * p * (1 - p)


def names():
    n = ['x', 'absx', 'spread', 'pm_quiet']
    n += [f'pm_r{w}' for w in WIN] + [f'sl_r{w}' for w in WIN]
    n += ['gap_e', 'gap_z', 'lead_level', 'lead_up', 'lead_down', 'lead_pm', 'lead_stale', 'lead_age', 'lead_exp']
    n += ['sl_U', 'sl_D', 'pm_U', 'pm_D', 'sl_wave', 'sl_wave_age', 'pm_wave', 'pm_wave_age', 'sl_pi']
    for f in FAMS:
        n += [f'{f}_gap', f'{f}_r10', f'{f}_on']
    n += ['fam_n', 'fam_up5', 'fam_dn5', 'fam_up30', 'fam_dn30', 'fam_gap_mean', 'fam_gap_max', 'fam_gap_min', 'fam_r10_max', 'fam_r10_min']
    n += ['ob_on', 'ob_imb', 'ob_dimb', 'ob_logdepth', 'ob_imb_r10', 'ob_dimb_r10']
    n += ['ctx_pm_rate', 'ctx_book_rate', 'ctx_spread', 'ctx_vol', 'ctx_gap_scale']
    n += [f'sport_{s}' for s in SPORTS] + ['three_way', 'inplay', 'min_from_start']
    return n


NAMES = names()


def _ret(x, w):
    r = np.full(len(x), np.nan)
    r[w:] = x[w:] - x[:-w]
    return r


def _roll(v, w=600):
    """Sum of v over the last w seconds (this one included)."""
    cs = np.cumsum(np.r_[0.0, np.nan_to_num(v)])
    i = np.arange(len(v))
    return cs[i + 1] - cs[np.maximum(0, i + 1 - w)]


def _held(rows, t0, n):
    """Sample-and-hold of [(t, v)] rows (sorted) on the grid; nan before the first."""
    ts = t0 + np.arange(n, dtype=np.int64) * STEP
    if not rows:
        return np.full(n, np.nan)
    lt = np.array([r[0] for r in rows], dtype=np.int64)
    lv = np.array([np.nan if r[1] is None else r[1] for r in rows], dtype=float)
    idx = np.searchsorted(lt, ts, side='right') - 1
    return np.where(idx >= 0, lv[np.clip(idx, 0, None)], np.nan)


def build(pm_rows, bk_rows, outcome, sport, start_ms, three_way, book=None, full=False, t_end=None, causal=False):
    """book: Polymarket order-book rows (ts, outcome, bid_size, ask_size, bid_depth, ask_depth) or None.
    full: return the whole grid as a dict (ts, X, bid, ask, mid, pm_eps, evt, ok) for the wave models.
    t_end (ms): LIVE use (tools/maker): the grid runs to this moment with every line held at its last value, instead of
    stopping at the last recorded change. Give it rows up to t_end only; the last grid second then holds what a bot
    knew at t_end, the same numbers the full recording gives for that second (checked by tools/maker/parity.py).
    causal: bookmaker lines without hindsight (waves.book_lines); needed for that equality."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        return _build(pm_rows, bk_rows, outcome, sport, start_ms, three_way, book, full, t_end, causal)


def _build(pm_rows, bk_rows, outcome, sport, start_ms, three_way, book=None, full=False, t_end=None, causal=False):
    """(t (ms), X float32 [n, F], y [n, 2] (up, down), net [n, 4] (up at H, up trade, down at H, down trade), evt) for
    every second of the span where Polymarket and the bookmakers both exist; rows not tradable are dropped."""
    pm = W.pm_line(pm_rows, outcome)
    books = W.book_lines(bk_rows, outcome, causal)
    if not pm or not books:
        return None
    slp = W.superline(books)
    sl = [(t, p) for t, p, _ in slp]
    t0 = max(pm[0][0], sl[0][0])
    t1 = min(pm[-1][0], max(r[-1][0] for r in books.values())) if t_end is None else int(t_end)
    if t1 - t0 < 300_000:
        return None
    n = (t1 - t0) // STEP + 1
    gp = _held(pm, t0, n)
    gs = _held(sl, t0, n)
    pi = _held([(t, w) for t, _, w in slp], t0, n)
    # Raw Polymarket bid / ask for the outcome (the prices a trade gets), held.
    rb = [(r[0], r[3]) for r in pm_rows if r[2] == outcome]
    ra = [(r[0], r[4]) for r in pm_rows if r[2] == outcome]
    rb.sort(key=lambda r: r[0])
    ra.sort(key=lambda r: r[0])
    bid, ask = _held(rb, t0, n), _held(ra, t0, n)

    x, y = L.logit(gp), L.logit(gs)
    ld = L.lead_state(gp, gs)
    se, sU, sD = L.trends(gs)
    pe, pU, pD = L.trends(gp)

    F = {}
    F['x'] = x
    F['absx'] = np.abs(x)
    F['spread'] = (ask - bid) * 100
    # Seconds since Polymarket's mid last changed.
    q = np.zeros(n)
    last = 0
    for i in range(1, n):
        if not (gp[i] == gp[i - 1] or (math.isnan(gp[i]) and math.isnan(gp[i - 1]))):
            last = i
        q[i] = i - last
    F['pm_quiet'] = np.log1p(q)
    for w in WIN:
        F[f'pm_r{w}'] = _ret(x, w)
        F[f'sl_r{w}'] = _ret(y, w)
    st = ld['state']
    F['gap_e'] = ld['e']
    F['gap_z'] = np.clip(ld['z'], -10, 10)
    for name, code in (('lead_level', L.LEVEL), ('lead_up', L.AHEAD_UP), ('lead_down', L.AHEAD_DOWN), ('lead_pm', L.PM_AHEAD), ('lead_stale', L.STALE)):
        F[name] = (st == code).astype(float)
    F['lead_age'] = np.log1p(np.nan_to_num(ld['age']))
    F['lead_exp'] = np.nan_to_num(ld['exp']) * 100
    F['sl_U'], F['sl_D'], F['pm_U'], F['pm_D'] = sU / L.TREND['h'], sD / L.TREND['h'], pU / L.TREND['h'], pD / L.TREND['h']
    for key, eps in (('sl', se), ('pm', pe)):
        wv = np.zeros(n)
        age = np.zeros(n)
        for e in eps:   # known from the alarm on (causal), while it lasts
            a, b = e['alarm'], e['end']
            wv[a:b + 1] = e['dir']
            age[a:b + 1] = np.log1p(np.arange(b + 1 - a))
        F[f'{key}_wave'] = wv
        F[f'{key}_wave_age'] = age
    F['sl_pi'] = np.nan_to_num(pi)
    # Bookmaker families: the mean log-odds of a family's books present that second.
    fam = {}
    for b, line in books.items():
        f = W.family(b)
        g = L.logit(_held(line, t0, n))
        fam.setdefault(f, []).append(g)
    famx = {f: np.nanmean(np.vstack(v), axis=0) if len(v) > 1 else v[0] for f, v in fam.items()}
    with np.errstate(all='ignore'):
        for f in FAMS:
            g = famx.get(f)
            if g is None:
                F[f'{f}_gap'] = np.zeros(n)
                F[f'{f}_r10'] = np.zeros(n)
                F[f'{f}_on'] = np.zeros(n)
                continue
            F[f'{f}_gap'] = np.clip(g - x, -3, 3)
            F[f'{f}_r10'] = _ret(g, 10)
            F[f'{f}_on'] = np.isfinite(g).astype(float)
        allg = np.vstack(list(famx.values()))
        r5 = np.vstack([_ret(g, 5) for g in famx.values()])
        r30 = np.vstack([_ret(g, 30) for g in famx.values()])
        r10 = np.vstack([_ret(g, 10) for g in famx.values()])
        gaps = allg - x
        F['fam_n'] = np.isfinite(allg).sum(axis=0).astype(float)
        F['fam_up5'] = (r5 >= 0.04).sum(axis=0).astype(float)
        F['fam_dn5'] = (r5 <= -0.04).sum(axis=0).astype(float)
        F['fam_up30'] = (r30 >= 0.04).sum(axis=0).astype(float)
        F['fam_dn30'] = (r30 <= -0.04).sum(axis=0).astype(float)
        F['fam_gap_mean'] = np.nanmean(gaps, axis=0)
        F['fam_gap_max'] = np.nanmax(gaps, axis=0)
        F['fam_gap_min'] = np.nanmin(gaps, axis=0)
        F['fam_r10_max'] = np.nanmax(r10, axis=0)
        F['fam_r10_min'] = np.nanmin(r10, axis=0)
        r1 = np.vstack([_ret(g, 1) for g in famx.values()])
        evt = (np.nan_to_num(np.abs(r1)).sum(axis=0) > 0) | (np.nan_to_num(np.abs(_ret(x, 1))) > 0) | (np.nan_to_num(np.abs(_ret(y, 1))) > 0)
        # The market's regime over the last 10 minutes (games differ: liquidity, activity, how far apart the
        # bookmakers and Polymarket sit): Polymarket and bookmaker changes a minute, Polymarket's mean spread
        # (cents) and volatility (log-odds, root of summed squared 1 s steps), and the gap's own scale.
        px1 = np.nan_to_num(_ret(x, 1))
        span = np.minimum(np.arange(1, n + 1), 600)
        F['ctx_pm_rate'] = _roll(px1 != 0) / span * 60
        F['ctx_book_rate'] = _roll(np.nan_to_num(np.abs(r1)).astype(bool).sum(axis=0)) / span * 60
        sp_ = (ask - bid) * 100
        F['ctx_spread'] = _roll(np.where(np.isfinite(sp_), sp_, 0)) / np.maximum(_roll(np.isfinite(sp_)), 1)
        F['ctx_vol'] = np.sqrt(_roll(px1 ** 2))
        F['ctx_gap_scale'] = np.nan_to_num(ld['scale'], nan=0.05)
    # Polymarket's order book: imbalance at the best prices and within 2c (more resting buyers than sellers
    # = pressure up), total depth, and how the imbalance changed over 10 s. Zero with ob_on 0 where unknown.
    def held_col(j):
        return _held(sorted([(r[0], r[j]) for r in (book or []) if r[1] == outcome and r[j] is not None], key=lambda r: r[0]), t0, n)
    if book:
        bs, as_, bd, ad = held_col(2), held_col(3), held_col(4), held_col(5)
        with np.errstate(all='ignore'):
            imb = (bs - as_) / (bs + as_)
            dimb = (bd - ad) / (bd + ad)
        F['ob_on'] = np.isfinite(imb).astype(float)
        F['ob_imb'], F['ob_dimb'] = imb, dimb
        F['ob_logdepth'] = np.log1p(np.nan_to_num(bd) + np.nan_to_num(ad))
        F['ob_imb_r10'], F['ob_dimb_r10'] = _ret(imb, 10), _ret(dimb, 10)
    else:
        for k_ in ('ob_on', 'ob_imb', 'ob_dimb', 'ob_logdepth', 'ob_imb_r10', 'ob_dimb_r10'):
            F[k_] = np.zeros(n)
    sp = 'esports' if sport in ESPORTS else sport
    for s in SPORTS:
        F[f'sport_{s}'] = np.full(n, 1.0 if sp == s else 0.0)
    F['three_way'] = np.full(n, 1.0 if three_way else 0.0)
    ts = t0 + np.arange(n, dtype=np.int64) * STEP
    F['inplay'] = (ts >= (start_ms or 0)).astype(float) if start_ms else np.zeros(n)
    F['min_from_start'] = np.clip((ts - (start_ms or ts[0])) / 60000, -240, 300) / 60 if start_ms else np.zeros(n)
    X = np.column_stack([np.nan_to_num(F[k], nan=0.0, posinf=0.0, neginf=0.0) for k in NAMES]).astype(np.float32)

    # ---- labels: first passage of the executable net within HOR seconds, entering LAT s after the signal.
    lab = np.zeros((n, 2), dtype=np.int8)
    net = np.full((n, 4), np.nan, dtype=np.float32)
    hold = np.zeros((n, 2), dtype=np.int16)   # seconds the labelled trade lasts (to its take-profit, stop or H)
    ok = np.zeros(n, dtype=bool)
    idx = np.arange(n - LAT - HOR)
    if len(idx):
        e_i = idx + LAT
        tradable = np.isfinite(bid[e_i]) & np.isfinite(ask[e_i]) & (ask[e_i] - bid[e_i] <= MAX_SPREAD + 1e-9) & (bid[e_i] > 0) & (ask[e_i] < 1)
        win = np.lib.stride_tricks.sliding_window_view(np.arange(n), HOR)[e_i + 1 - 0]  # seconds e+1 .. e+HOR
        win = win[:, :HOR]
        for k, (buy, sell_px) in enumerate(((ask, bid), (1 - bid, 1 - ask))):
            cost = buy[e_i] + fee(buy[e_i])
            sp_ = sell_px[win]
            val = sp_ - fee(sp_)
            nt = val - cost[:, None]
            hit_tp = nt >= TP
            hit_st = sp_ <= (sell_px[e_i] - STOP)[:, None]
            first_tp = np.where(hit_tp.any(axis=1), hit_tp.argmax(axis=1), HOR + 1)
            first_st = np.where(hit_st.any(axis=1), hit_st.argmax(axis=1), HOR + 1)
            lab[idx, k] = ((first_tp < first_st) & (first_tp <= HOR)).astype(np.int8)
            net[idx, 2 * k] = nt[:, -1]
            # The trade's own net: at the take-profit, at the stop, else at the horizon.
            out = np.where(first_tp < first_st, nt[np.arange(len(idx)), np.minimum(first_tp, HOR - 1)],
                           np.where(first_st <= HOR, nt[np.arange(len(idx)), np.minimum(first_st, HOR - 1)], nt[:, -1]))
            net[idx, 2 * k + 1] = out
            hold[idx, k] = (np.minimum(np.minimum(first_tp, first_st), HOR - 1) + 1 + LAT).astype(np.int16)
        ok[idx] = tradable & np.isfinite(gp[idx]) & np.isfinite(gs[idx])
    keep = np.where(ok)[0]
    if full:
        return {'ts': ts, 'X': X, 'bid': bid, 'ask': ask, 'mid': gp, 'pm_eps': pe, 'sl_eps': se, 'sl': gs, 'evt': evt, 'ok': ok}
    return ts[keep], X[keep], lab[keep], net[keep], evt[keep], hold[keep]


def g_rows(G, outcome):
    """A Trade review game (GET /review/game/{id}) as the database rows build() reads."""
    pm_rows = [(r[0], r[0], o, r[1], r[2]) for o, rows in G['pm'].items() for r in rows]
    bk_rows = [(r[0], b, o, r[1]) for b, outs in G['books'].items() if b not in ('polymarket', 'kalshi') for o, rows in outs.items() for r in rows]
    pm_rows.sort(key=lambda r: r[0])
    bk_rows.sort(key=lambda r: r[0])
    return pm_rows, bk_rows
