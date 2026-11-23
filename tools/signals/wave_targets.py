"""Targets for the wave models (the owner's three factors, 23 Nov 2026), from one (game, outcome) full grid
(features.build(..., full=True)). Prices in cents; a trade enters L = 1 s after the decision.

1. WHEN A WAVE COMES, HOW LONG IT LASTS. Polymarket's trend waves (lead.trends, CUSUM): up_soon / dn_soon = 1
   when a wave of that direction starts (its first move) within the next SOON = 30 s (a discrete-time hazard).
   Inside a wave (first move .. peak): rem_t = seconds left to the peak, rem_a = points left to the peak.
2. WHERE IT WILL GO. The move a trade could capture within H = 180 s: mfe_up = best Yes bid in (e, e+H] minus
   the Yes ask paid at e = i + L; mfe_dn the same for the No token (1 - ask against 1 - bid); t_up / t_dn =
   seconds from entry to that best price. Spread is inside; fees (fee_rt, both fills at the mid) are not.
   base_up / base_dn: what the plain policy makes (sell at +TP net, else at H), net of fees.
3. CASH OUT EARLY. exit_rows(): positions entered at sampled decision seconds, both directions, looked at every
   CHECK = 10 s while the plain policy still holds them: now = the net if sold now, cont = the net the plain
   policy ends with, delta = cont - now. A model of delta is a continuation value (Longstaff-Schwartz); selling
   when it says delta < 0 is one step of policy improvement over the plain policy."""
import numpy as np

L, H, SOON, CHECK, TP = 1, 180, 30, 10, 0.03
FEE = 0.05


def fee(p):
    return FEE * p * (1 - p)


def _first_tp(val, tp):
    """Index of the first value >= tp along axis 1, or -1."""
    hit = val >= tp
    return np.where(hit.any(axis=1), hit.argmax(axis=1), -1)


def wave_targets(g):
    bid, ask, mid, eps = g['bid'], g['ask'], g['mid'], g['pm_eps']
    n = len(mid)
    T = {k: np.full(n, np.nan, dtype=np.float32) for k in ('rem_t', 'rem_a', 'mfe_up', 't_up', 'mfe_dn', 't_dn', 'base_up', 'base_dn', 'hold_up', 'hold_dn', 'fee_rt')}
    T['up_soon'] = np.zeros(n, dtype=np.int8)
    T['dn_soon'] = np.zeros(n, dtype=np.int8)
    for e in eps:
        if e['on'] is None or e['peak'] is None:
            continue
        first = e['on'] + 1
        arr = T['up_soon'] if e['dir'] > 0 else T['dn_soon']
        arr[max(0, first - SOON):first] = 1
        for i in range(first, e['peak'] + 1):
            T['rem_t'][i] = e['peak'] - i
            if np.isfinite(mid[i]) and np.isfinite(mid[e['peak']]):
                T['rem_a'][i] = (mid[e['peak']] - mid[i]) * e['dir'] * 100
    T['fee_rt'][:] = 2 * fee(np.nan_to_num(mid, nan=0.5)) * 100
    m = n - L - H
    if m <= 0:
        return T
    ent = np.arange(m) + L
    w = np.lib.stride_tricks.sliding_window_view(np.arange(n), H)[ent + 1]       # seconds e+1 .. e+H
    for key, buy, sell in (('up', ask, bid), ('dn', 1 - bid, 1 - ask)):
        b = buy[ent]
        sp = sell[w]
        gain = (sp - b[:, None]) * 100
        with np.errstate(all='ignore'):
            ok = np.isfinite(gain).any(axis=1)
            mx = np.where(ok, np.nanmax(np.where(np.isfinite(gain), gain, -np.inf), axis=1), np.nan)
            am = np.where(ok, np.nanargmax(np.where(np.isfinite(gain), gain, -np.inf), axis=1), 0)
        T[f'mfe_{key}'][:m] = mx
        T[f't_{key}'][:m] = np.where(ok, am + 1, np.nan)
        val = sp - fee(sp) - (b + fee(b))[:, None]                                 # net if sold then
        j = _first_tp(np.nan_to_num(val, nan=-1), TP)
        last = val[:, -1]
        T[f'base_{key}'][:m] = np.where(j >= 0, val[np.arange(m), np.maximum(j, 0)], last) * 100
        T[f'hold_{key}'][:m] = np.where(j >= 0, j + 1, H) + L
    return T


def exit_rows(g, entries):
    """Rows for the cash-out model: (row index of the grid second looked at, direction +1/-1, entry index,
    position features [elapsed s, now c, best so far c, drawdown c, entry spread c], now c, cont c)."""
    bid, ask = g['bid'], g['ask']
    n = len(bid)
    out_t, out_dir, out_ent, out_pos, out_now, out_cont = [], [], [], [], [], []
    for i in entries:
        e = i + L
        if e + H >= n:
            continue
        for d, buy, sell in ((1, ask, bid), (-1, 1 - bid, 1 - ask)):
            b = buy[e]
            if not np.isfinite(b):
                continue
            cost = b + fee(b)
            sp = sell[e + 1:e + H + 1]
            val = sp - fee(sp) - cost
            hit = np.flatnonzero(np.nan_to_num(val, nan=-1) >= TP)
            stop = hit[0] if len(hit) else H - 1                                    # the plain policy's sale
            cont = val[stop]
            if not np.isfinite(cont):
                continue
            best = -np.inf
            for k in range(CHECK, stop + 1, CHECK):                                # still held at e + k
                v = val[k - 1]
                if not np.isfinite(v):
                    continue
                best = max(best, np.nanmax(val[:k]))
                out_t.append(e + k)
                out_dir.append(d)
                out_ent.append(i)
                out_pos.append((k, v * 100, best * 100, (best - v) * 100, (ask[e] - bid[e]) * 100))
                out_now.append(v * 100)
                out_cont.append(cont * 100)
    return (np.array(out_t, dtype=np.int64), np.array(out_dir, dtype=np.int8), np.array(out_ent, dtype=np.int64),
            np.array(out_pos, dtype=np.float32).reshape(-1, 5), np.array(out_now, dtype=np.float32), np.array(out_cont, dtype=np.float32))
