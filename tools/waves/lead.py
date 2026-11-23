"""Lead state (is the superline ahead of Polymarket?) and up/down trend waves, on a 1-second grid.

Both are causal: the value at second i uses data up to i only, so the same numbers can drive a bot.
The browser port is in src/web/waves.js (lead, trends); tools/waves/test_parity.mjs keeps them equal.

LEAD STATE. x = logit(Polymarket mid), y = logit(superline). Both are late, noisy views of one price, so
their gap is an error-correction term: e = y - x - b, where b is a slow level offset (one market a little
more optimistic than the other: not a lead). b is a robust EWMA (residual clipped at CLIP scale units, so a
wave does not drag it); a gap open longer than STALE_S stops being a lead and is absorbed fast. The scale s is
the EWMA of |e| in calm seconds (as a normal sigma: E|e| * sqrt(pi/2)), floored. z = e / s.
A gap opens at |z| >= Z_ON and closes at |z| < Z_OFF (hysteresis). Who opened it: since the last calm second
(anchor xa, ya), the superline contributed cy = (y - ya) sgn(e) and Polymarket cx = -(x - xa) sgn(e); cy >= cx
means the superline moved and Polymarket has not caught up (AHEAD, up or down), else Polymarket moved first
(PM_AHEAD). Expected Polymarket catch-up within h seconds: PASS * (1 - exp(-KAPPA h)) * e, fitted by
lead_fit.py on every recorded game (ordinary least squares of Polymarket's later change on e, per state).

TREND WAVES. Two-sided CUSUM (Page's test) on 1-second log-odds increments d: U = max(0, U + d - k),
D = max(0, D - d - k). An up wave is detected when U >= h; its onset is the last second U left 0, it lasts
until U is back at 0 (or the down side fires), and its size is the extreme level reached minus the level at
onset. One rule catches a jump (one big d) and a climb (many small d above k per second). k and h are the
smallest drift and move that count: k = 0.002 log-odds/s (about 3 points a minute at 50%), h = 0.12 (the
wave size of waves.py)."""
import math

import numpy as np

LEVEL, AHEAD_UP, AHEAD_DOWN, PM_AHEAD, STALE, UNKNOWN = 0, 1, 2, 3, 4, 9
STATE_NAME = {LEVEL: 'level', AHEAD_UP: 'superline ahead, up', AHEAD_DOWN: 'superline ahead, down', PM_AHEAD: 'Polymarket ahead',
              STALE: 'stale gap', UNKNOWN: 'no price'}

LEAD = {
    'step': 1000,          # grid, ms
    'bias_half': 600.0,    # s, half-life of the level offset b
    'stale_half': 30.0,    # s, half-life of b while a gap is stale
    'scale_half': 300.0,   # s, half-life of the gap scale s
    'scale_floor': 0.03,   # log-odds
    'scale_init': 0.06,
    'clip': 1.5,           # b's residual is clipped at this many s
    'z_on': 2.5,
    'z_off': 1.0,
    'stale_s': 180,        # a gap open this long is a disagreement, not a lead
    'kappa': 0.0305,       # 1/s, Polymarket's catch-up speed (lead_fit.py over 1149 games, 23 Nov 2026)
    'pass': 0.17,          # share of a superline-led gap Polymarket closes in the end (same fit)
    'horizon': 30,         # s, the expected catch-up shown
}
TREND = {'k': 0.002, 'h': 0.12}


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def grid(line, t0, t1, step=1000):
    """Sample-and-hold of a [(t, p or None)] line at t0, t0 + step, ... <= t1; nan before the first point
    and while the line is None (suspended / spread too wide)."""
    ts = np.arange(t0, t1 + 1, step, dtype=np.int64)
    if not line:
        return ts, np.full(len(ts), np.nan)
    lt = np.array([r[0] for r in line], dtype=np.int64)
    lp = np.array([np.nan if r[1] is None else r[1] for r in line], dtype=float)
    idx = np.searchsorted(lt, ts, side='right') - 1
    out = np.where(idx >= 0, lp[np.clip(idx, 0, None)], np.nan)
    return ts, out


def lead_state(pm, sl, P=LEAD):
    """pm, sl: probabilities on one grid (nan = none). Returns dict of arrays: state, e, z, bias, scale, age (s
    the gap has been open), share (cy / (cy + cx), the superline's part in opening it) and exp (expected
    Polymarket catch-up within P['horizon'] s, in probability)."""
    n = len(pm)
    x, y = logit(pm), logit(sl)
    a_b = 1 - 0.5 ** (1 / P['bias_half'])
    a_st = 1 - 0.5 ** (1 / P['stale_half'])
    a_s = 1 - 0.5 ** (1 / P['scale_half'])
    state = np.full(n, UNKNOWN, dtype=np.int8)
    E, Z, B, S, AGE, SH, EXP = (np.full(n, np.nan) for _ in range(7))
    b = s = None
    open_ = False
    xa = ya = 0.0
    age = 0
    beta = P['pass'] * (1 - math.exp(-P['kappa'] * P['horizon']))
    for i in range(n):
        xi, yi = x[i], y[i]
        if math.isnan(xi) or math.isnan(yi):
            continue
        r = yi - xi
        if b is None:
            b, s, xa, ya = r, P['scale_init'], xi, yi
        e = r - b
        z = e / s
        if not open_:
            if abs(z) >= P['z_on']:
                open_, age = True, 0
            else:
                xa, ya = xi, yi
        elif abs(z) < P['z_off']:
            open_ = False
            xa, ya = xi, yi
        if open_:
            age += 1
            sg = 1.0 if e > 0 else -1.0
            cy, cx = (yi - ya) * sg, -(xi - xa) * sg
            tot = max(cy, 0.0) + max(cx, 0.0)
            SH[i] = max(cy, 0.0) / tot if tot > 0 else 0.5
            if age > P['stale_s']:
                st = STALE
            elif cy >= cx:
                st = AHEAD_UP if e > 0 else AHEAD_DOWN
            else:
                st = PM_AHEAD
        else:
            st = LEVEL
        state[i], E[i], Z[i], B[i], S[i], AGE[i] = st, e, z, b, s, age if open_ else 0
        if st in (AHEAD_UP, AHEAD_DOWN):
            EXP[i] = 1 / (1 + math.exp(-(xi + beta * e))) - 1 / (1 + math.exp(-xi))
        # Updates for the next second (causal): b robust and slow, fast while stale; s from calm seconds only.
        if st == STALE:
            b += a_st * e
        else:
            b += a_b * max(-P['clip'] * s, min(P['clip'] * s, e))
        if not open_:
            s = max(P['scale_floor'], s + a_s * (abs(e) * 1.2533 - s))
    return {'state': state, 'e': E, 'z': Z, 'bias': B, 'scale': S, 'age': AGE, 'share': SH, 'exp': EXP}


def trends(p, P=TREND):
    """Up/down trend waves of one series on the grid (index units). Returns (episodes, U, D): episodes are dicts
    dir (+1/-1), on (index of the level before the move), alarm (index it was detected), peak (index of the
    extreme level), end (index the CUSUM was back at 0, or the other side fired), amp (log-odds, signed),
    p_on, p_peak; U and D are the two CUSUM statistics per second."""
    x = logit(p)
    n = len(x)
    k, h = P['k'], P['h']
    U = np.zeros(n)
    D = np.zeros(n)
    u = d = 0.0
    u_on = d_on = 0
    cur = None
    eps = []
    last = None
    for i in range(n):
        xi = x[i]
        if math.isnan(xi):
            U[i], D[i] = u, d
            continue
        if last is None:
            last = xi
            u_on = d_on = i
            continue
        dx = xi - last
        last = xi
        if u == 0:
            u_on = i - 1
        if d == 0:
            d_on = i - 1
        u = max(0.0, u + dx - k)
        d = max(0.0, d - dx - k)
        U[i], D[i] = u, d
        if cur is not None:
            if (cur['dir'] > 0 and xi > x[cur['peak']]) or (cur['dir'] < 0 and xi < x[cur['peak']]):
                cur['peak'] = i
            ended = (u == 0) if cur['dir'] > 0 else (d == 0)
            flipped = (d >= h) if cur['dir'] > 0 else (u >= h)
            if ended or flipped:
                cur['end'] = i
                eps.append(cur)
                cur = None
        if cur is None:
            if u >= h:
                cur = {'dir': 1, 'on': u_on, 'alarm': i, 'peak': i, 'end': None}
                d, D[i] = 0.0, 0.0
            elif d >= h:
                cur = {'dir': -1, 'on': d_on, 'alarm': i, 'peak': i, 'end': None}
                u, U[i] = 0.0, 0.0
    if cur is not None:
        cur['end'] = n - 1
        eps.append(cur)
    for c in eps:
        c['amp'] = float(x[c['peak']] - x[c['on']]) if not math.isnan(x[c['on']]) else float('nan')
        c['p_on'], c['p_peak'] = float(p[c['on']]), float(p[c['peak']])
    return eps, U, D


def lead_all(pm_line, sl_line, P=LEAD, T=TREND):
    """Everything the page shows from two lines ([(t, p)]): the grid from where both exist to where the later
    one ends, the lead state on it, and both lines' trend waves (indexes into the grid). Same as waves.js lead()."""
    if not pm_line or not sl_line:
        return None
    t0 = max(pm_line[0][0], sl_line[0][0])
    t1 = max(pm_line[-1][0], sl_line[-1][0])
    if t1 <= t0:
        return None
    ts, gp = grid(pm_line, t0, t1, P['step'])
    _, gs = grid(sl_line, t0, t1, P['step'])
    r = lead_state(gp, gs, P)
    se, _, _ = trends(gs, T)
    pe, _, _ = trends(gp, T)
    return {'t0': int(t0), 'step': P['step'], 'n': len(ts), **r, 'sl_trends': se, 'pm_trends': pe}


def pair_trends(sl_eps, pm_eps, before=60, after=180):
    """Each superline trend with the Polymarket trend of the same direction whose onset is closest to it,
    within [-before, +after] s: lead = Polymarket's onset - the superline's (+ = superline first), and
    detection lead = the same for the alarm times."""
    out = []
    for s in sl_eps:
        best = None
        for q in pm_eps:
            if q['dir'] != s['dir']:
                continue
            dl = q['on'] - s['on']
            if -before <= dl <= after and (best is None or abs(dl) < abs(best['on'] - s['on'])):
                best = q
        out.append({'sl': s, 'pm': best, 'lead': None if best is None else best['on'] - s['on'],
                    'alarm_lead': None if best is None else best['alarm'] - s['alarm']})
    return out
