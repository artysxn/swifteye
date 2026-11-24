"""Extra causal inputs for the model lab (lab.py), from the market-microstructure literature.

  hk_*      Hawkes-style intensities: exponentially decayed sums of recent moves at half-lives 5 / 30 / 120 / 600 s, for
            Polymarket (size and signed), the superline (size and signed) and the count of bookmaker families that just
            moved up / down. Price moves cluster (self- and cross-excitation; Bacry, Mastromatteo & Muzy 2015): how fast
            they have been arriving is the natural input for "is a wave about to start".
  burst_*   the 5 s intensity over the 120 s one (a burst right now against the recent norm).
  ofi_*     order-flow imbalance of Polymarket's best bid / ask (Cont, Kukanov & Stoikov 2014): size added at or above
            the bid minus size added at or below the ask, over 10 / 60 s, per unit of average depth.
  since_*, n_*, last_*, mean_*   the game's own wave history so far: seconds since Polymarket's last wave ended and since
            the superline's last wave began, waves in the last 5 / 15 min, the last finished wave's size and length, and
            the average length and size of this game's finished waves (for "how long will this one last")."""
import numpy as np
from scipy.signal import lfilter

import features as F
import mine as MN

C = MN.C
HLS = (5, 30, 120, 600)
SER = ('pm_abs', 'pm_sgn', 'sl_abs', 'sl_sgn', 'bk_up', 'bk_dn')
QN = [f'hk_{s}_{h}' for s in SER for h in HLS] + ['burst_pm', 'burst_bk', 'ofi_10', 'ofi_60', 'since_pm_end', 'since_sl_alarm',
                                                   'n_pm_5m', 'n_sl_5m', 'n_pm_15m', 'n_sl_15m', 'last_pm_amp', 'last_pm_dur', 'mean_pm_dur', 'mean_pm_amp']
QC = {k: i for i, k in enumerate(QN)}


def ew(v, hl):
    a = 0.5 ** (1.0 / hl)
    return lfilter([1.0], [1.0, -a], v)


def extra(X, bid, ask, sz, pm_eps, sl_eps):
    n = len(X)
    q = {}
    x = X[:, C['x']].astype(float)
    dpm = np.nan_to_num(X[:, C['pm_r1']]).astype(float)
    dsl = np.nan_to_num(X[:, C['sl_r1']]).astype(float)
    up, dn = np.zeros(n), np.zeros(n)
    for f in F.FAMS:
        on = X[:, C[f + '_on']] > 0.5
        L = x + X[:, C[f + '_gap']]
        dL = np.zeros(n)
        dL[1:] = np.where(on[1:] & on[:-1], np.nan_to_num(L[1:] - L[:-1]), 0.0)
        up += dL > 0.01
        dn += dL < -0.01
    for name, v in zip(SER, (np.abs(dpm), dpm, np.abs(dsl), dsl, up, dn)):
        for h in HLS:
            q[f'hk_{name}_{h}'] = ew(v, h)
    q['burst_pm'] = q['hk_pm_abs_5'] / (q['hk_pm_abs_120'] + 1e-4)
    q['burst_bk'] = (q['hk_bk_up_5'] + q['hk_bk_dn_5']) / (q['hk_bk_up_120'] + q['hk_bk_dn_120'] + 1e-2)
    b, a = np.asarray(bid[:n], float), np.asarray(ask[:n], float)
    bs, as_ = np.nan_to_num(np.asarray(sz[0][:n], float)), np.nan_to_num(np.asarray(sz[1][:n], float))
    eb, ea = np.zeros(n), np.zeros(n)
    vb, va = np.isfinite(b[1:]) & np.isfinite(b[:-1]), np.isfinite(a[1:]) & np.isfinite(a[:-1])
    eb[1:] = np.where(vb, np.where(b[1:] > b[:-1], bs[1:], np.where(b[1:] == b[:-1], bs[1:] - bs[:-1], -bs[:-1])), 0.0)
    ea[1:] = np.where(va, np.where(a[1:] < a[:-1], as_[1:], np.where(a[1:] == a[:-1], as_[1:] - as_[:-1], -as_[:-1])), 0.0)
    ofi = (eb - ea) / (ew(bs + as_, 120) * (1 - 0.5 ** (1 / 120.0)) + 1.0)
    q['ofi_10'], q['ofi_60'] = ew(ofi, 10), ew(ofi, 60)
    idx = np.arange(n)

    def since(marks):
        last = np.full(n, -10 ** 9, np.int64)
        marks = marks[(marks >= 0) & (marks < n)]
        last[marks] = marks
        return np.log1p(np.minimum(idx - np.maximum.accumulate(last), 3600).astype(float))

    def lagged(c, w):
        return np.concatenate([np.zeros(w), c[:-w]]) if n > w else np.zeros(n)
    pm_al = np.array(sorted(e['alarm'] for e in pm_eps if e.get('alarm') is not None), np.int64)
    sl_al = np.array(sorted(e['alarm'] for e in sl_eps if e.get('alarm') is not None), np.int64)
    fin = sorted((e['end'], abs(e['amp']), e['peak'] - e['on']) for e in pm_eps if e.get('end') is not None and e.get('peak') is not None and e.get('on') is not None and not e.get('open'))
    q['since_pm_end'] = since(np.array([f_[0] for f_ in fin], np.int64))
    q['since_sl_alarm'] = since(sl_al)
    for name, al in (('pm', pm_al), ('sl', sl_al)):
        c = np.zeros(n)
        np.add.at(c, al[al < n], 1.0)
        c = np.cumsum(c)
        q[f'n_{name}_5m'], q[f'n_{name}_15m'] = c - lagged(c, 300), c - lagged(c, 900)
    for k in ('last_pm_amp', 'last_pm_dur', 'mean_pm_dur', 'mean_pm_amp'):
        q[k] = np.zeros(n)
    if fin:
        ends = np.array([f_[0] for f_ in fin], np.int64)
        amps, durs = np.array([f_[1] for f_ in fin], float), np.array([f_[2] for f_ in fin], float)
        k = np.searchsorted(ends, idx, side='left') - 1          # waves that had ended before this second
        has, kk = k >= 0, np.clip(k, 0, None)
        cnt = np.arange(1, len(durs) + 1)
        q['last_pm_amp'], q['last_pm_dur'] = np.where(has, amps[kk], 0.0), np.where(has, durs[kk], 0.0)
        q['mean_pm_dur'], q['mean_pm_amp'] = np.where(has, (np.cumsum(durs) / cnt)[kk], 0.0), np.where(has, (np.cumsum(amps) / cnt)[kk], 0.0)
    return np.nan_to_num(np.stack([q[k] for k in QN], 1)).astype(np.float32)


def mirror(Q):
    """The same inputs seen from the other side (for a trade against the outcome)."""
    M = Q.copy()
    for h in HLS:
        M[:, QC[f'hk_pm_sgn_{h}']], M[:, QC[f'hk_sl_sgn_{h}']] = -Q[:, QC[f'hk_pm_sgn_{h}']], -Q[:, QC[f'hk_sl_sgn_{h}']]
        M[:, QC[f'hk_bk_up_{h}']], M[:, QC[f'hk_bk_dn_{h}']] = Q[:, QC[f'hk_bk_dn_{h}']], Q[:, QC[f'hk_bk_up_{h}']]
    M[:, QC['ofi_10']], M[:, QC['ofi_60']] = -Q[:, QC['ofi_10']], -Q[:, QC['ofi_60']]
    return M
