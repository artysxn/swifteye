"""Which way will the next wave go? (owner, 24 Nov: "there may be a positive gain to be made if we are able to predict
which way a wave will go with a model").

  python tools/signals/wavedir.py build --db X --signals DIR [--workers 22] [--limit N]
  python tools/signals/wavedir.py test  --signals DIR

A wave = a Polymarket CUSUM trend wave (lead.trends). Its FIRST MOVE is the second after its onset; a call made before
that second is made before the price has moved at all.
BUILD, one row per in-play decision second (every 3rd second any line changed, tradable):
  X, H, Q   the 99 inputs, the game's lead record, the lab's Hawkes / order-flow / wave-history inputs;
  D         new direction inputs: Polymarket's trade prints (pm_quotes.last: a new last price at or above the ask before
            it = a buyer took, at or below the bid = a seller; decayed sums over 5 / 30 / 120 s, prints a minute, last
            print against the mid, seconds since), and the direction of the last waves of Polymarket and the superline;
  tn, amp   seconds to the first move of the next up wave and of the next down wave, and that wave's size;
  Y         dollars a $100 order makes buying up / buying down 1 s later (fees, depth both sides, collapsed tokens,
            price path past the end of the game): sold after 30 / 60 / 120 / 300 s, +5c or 5 min, "follow" (after 30 s,
            the first second our side's CUSUM is back at 0; 5 min at most), and "peak" = sold at the very top of the
            wave that starts our way within 30 s (not knowable: the ceiling).
Also: every Polymarket wave in play (for wave-after-wave facts) and, where the bookmakers' live scores were recorded
(book_scores, 22-23 Nov), every score change two different books agree on, with Polymarket's price around it.
TEST (19-23 Nov, every model trained on the days before the day it is scored on, 10-min gap):
  1 how often a wave comes; 2 can a model tell one is coming; 3 GIVEN one comes within 30 s, which way: the model next
  to a coin and to simple rules; 4 the ceiling: what a perfect call would make; 5 the hit rate a call needs; 6 the
  model's own calls as trades, one position a game; 7 a last model that reads the inputs and every call and predicts
  dollars; 8 wave-after-wave facts; 9 the score feed: does it tell the direction before Polymarket has moved."""
import argparse
import datetime as dt
import difflib
import os
import re
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
import waves as W  # noqa: E402
from booklead import fee, held  # noqa: E402

C = MN.C
DAY = 86_400_000
SOON = 30
BIG = 0.45          # log-odds: a big wave (about 11 pt at 50c, 6 pt at 20c / 80c)
LAB = ['h30', 'h60', 'h120', 'h300', 'tp5', 'follow', 'peak']
LABN = ['sold after 30 s', 'after 1 min', 'after 2 min', 'after 5 min', '+5c or 5 min', 'when the wave ends', 'at the top of the wave (not knowable)']
DN = ['tf_sgn_5', 'tf_sgn_30', 'tf_sgn_120', 'tf_n_30', 'tf_n_120', 'tf_last_mid', 'tf_since', 'pm_prev1', 'pm_prev2', 'pm_run5', 'sl_prev1', 'sl_run5']
DNEG = [0, 1, 2, 5, 7, 8, 9, 10, 11]
OFFS = (-120, -60, -30, -10, -5, -2, 2, 5, 10, 30, 60, 120)
PAIR = re.compile(r'(\d+)\s*[-:]\s*(\d+)')


def mirror_d(D):
    M = D.copy()
    M[:, DNEG] = -D[:, DNEG]
    return M


def trader(bid, ask, sz, rows, d, fresh):
    """Buy direction d at the ask 1 s after `rows`. Returns usd(k) = dollars a $100 order makes sold into the bid at
    index k (shares capped by the depth within 2c at the buy and at the sale, 1c worse beyond the best level, 5% p(1-p)
    fee on both fills), the entry index, the token's bid series (-1 where it is not a price) and the price paid.
    `fresh` = the recording was alive that second (paths.py, the Mac's night of 23 Nov: a game's recorded book can
    freeze for minutes while it trades on). A sale happens at the first FRESH bid at or after k within 10 min; if the
    book was alive in that time and nobody bid, the token collapsed (0); if the recording was dead, the trade's value
    is unknown (nan), not 0 and not the frozen price."""
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
    okf = np.isfinite(tb) & fresh
    idx = np.where(okf, np.arange(n), n)
    nxt = np.minimum.accumulate(idx[::-1])[::-1]
    cf = np.concatenate([[0], np.cumsum(fresh)])

    def usd(k):
        j = nxt[k]
        good = (j < n) & (j - k <= 600)
        alive = cf[np.minimum(k + 601, n)] - cf[np.minimum(k, n)] > 0
        jj = np.minimum(j, n - 1)
        sell = np.where(good, np.nan_to_num(tb[jj]), 0.0)
        s_out = np.where(good, inf(tbs[jj]), np.inf)
        cap_out = np.where(good, np.maximum(inf(tbd[jj]), inf(tbs[jj])), np.inf)
        fb = fee(buy)
        nn = np.minimum(np.minimum(100.0 / (buy + fb), cap_in), cap_out)
        n1 = np.minimum(nn, s_in)
        cost = n1 * buy + (nn - n1) * (buy + 0.01) + nn * fb
        m1 = np.minimum(nn, s_out)
        return np.where(okb & (good | alive), m1 * sell + (nn - m1) * np.maximum(sell - 0.01, 0.0) - nn * fee(sell) - cost, np.nan)
    return usd, e, np.where(okf, tb, -1.0), np.where(okb, buy, np.nan)


def flow(prow, ts, mid):
    """Polymarket's trade prints of one token, from the `last` column: [n, 7] causal inputs (see DN)."""
    n = len(ts)
    out = np.zeros((n, 7), np.float32)
    if len(prow) < 2:
        return out
    t = np.array([r[0] for r in prow], np.int64)
    b, a, l = (np.array([np.nan if r[j] is None else r[j] for r in prow], float) for j in (1, 2, 3))
    k = np.flatnonzero(np.isfinite(l[1:]) & (l[1:] != l[:-1])) + 1                # a new last price = a print (the same price twice is not seen)
    # no early return when there is no print yet: "none so far" must read the same live as in a recording that has prints later
    with np.errstate(invalid='ignore'):
        sg = np.where(l[k] >= a[k - 1] - 1e-9, 1.0, np.where(l[k] <= b[k - 1] + 1e-9, -1.0, np.sign(np.nan_to_num(l[k] - l[k - 1]))))
    gi = np.searchsorted(ts, t[k], side='left')                                   # the first grid second that has seen it
    ok = gi < n
    s_, c_ = np.zeros(n), np.zeros(n)
    np.add.at(s_, gi[ok], sg[ok])
    np.add.at(c_, gi[ok], 1.0)
    lastp = held(t, l, ts)
    seen = np.full(n, -10 ** 9, np.int64)
    seen[gi[ok]] = gi[ok]
    since = np.minimum(np.arange(n) - np.maximum.accumulate(seen), 3600)
    with np.errstate(invalid='ignore'):
        cols = [LF.ew(s_, 5), LF.ew(s_, 30), LF.ew(s_, 120), LF.ew(c_, 30), LF.ew(c_, 120), np.clip((lastp - mid) * 100, -10, 10), np.log1p(since.astype(float))]
    return np.nan_to_num(np.stack(cols, 1)).astype(np.float32)


def wave_hist(eps, n):
    """Direction of the last wave known at each second (from its alarm), of the one before, and the net of the last 5."""
    al = sorted((e['alarm'], e['dir']) for e in eps)
    if not al:
        return np.zeros(n), np.zeros(n), np.zeros(n)
    a, d = np.array([x[0] for x in al], np.int64), np.array([x[1] for x in al], float)
    k = np.searchsorted(a, np.arange(n), side='right') - 1
    cs = np.cumsum(d)
    kk = np.clip(k, 0, None)
    return np.where(k >= 0, d[kk], 0.0), np.where(k >= 1, d[np.clip(k - 1, 0, None)], 0.0), np.where(k >= 0, cs[kk] - np.where(k >= 5, cs[np.clip(k - 5, 0, None)], 0.0), 0.0)


def next_wave(eps, d, rs):
    """Per row: seconds to the first move of the next wave of direction d (9999 = none), its size (log-odds), its peak index."""
    w = sorted((e['on'] + 1, e['peak'], abs(e['amp']) if np.isfinite(e['amp']) else 0.0) for e in eps if e['dir'] == d and e.get('on') is not None)
    if not w:
        return np.full(len(rs), 9999, np.int32), np.zeros(len(rs), np.float32), rs.copy()
    first, peak, amp = np.array([x[0] for x in w], np.int64), np.array([x[1] for x in w], np.int64), np.array([x[2] for x in w], np.float32)
    k = np.searchsorted(first, rs, side='right')
    has, kk = k < len(first), np.minimum(k, len(first) - 1)
    return np.where(has, np.minimum(first[kk] - rs, 9999), 9999).astype(np.int32), np.where(has, amp[kk], 0.0).astype(np.float32), np.where(has, peak[kk], rs)


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def swapped(mh, ma, eh, ea):
    if not eh or not ea:
        return False
    sim = lambda p, q: difflib.SequenceMatcher(None, norm(p), norm(q)).ratio()
    return sim(mh, ea) + sim(ma, eh) > sim(mh, eh) + sim(ma, ea) + 0.2


def pairs(s, sw):
    if not s:
        return ()
    return tuple((int(q), int(p)) if sw else (int(p), int(q)) for p, q in PAIR.findall(s))


def who(old, new):
    """+1 (home) / -1 (away) when exactly one pair changed and in it exactly one side went up; else 0."""
    if len(old) != len(new) or not old:
        return 0
    ch = [(o, v) for o, v in zip(old, new) if o != v]
    if len(ch) != 1:
        return 0
    (oa, ob), (na, nb) = ch[0]
    return 1 if (na > oa and nb == ob) else (-1 if (nb > ob and na == oa) else 0)


def score_events(reports):
    """reports: (ts, book, main pairs, detail pairs or None) in time order. A new state counts from the moment a SECOND
    book shows it (one book alone flickers). Returns (ts confirmed, ts first shown, side, level 0 main / 1 detail)."""
    ev = []
    for level in (0, 1):
        acc, old, seen = None, set(), {}
        for ts_, bk_, mp, dp in reports:
            if level == 1 and dp is None:
                continue
            key = mp if level == 0 else (mp, dp)
            if not (key if level == 0 else dp) or key == acc or key in old:
                continue
            s = seen.setdefault(key, [ts_, set()])
            s[1].add(bk_)
            if len(s[1]) < 2:
                continue
            if acc is not None:
                side = who(acc, key) if level == 0 else (who(acc[1], key[1]) if acc[0] == key[0] else 0)
                if side:
                    ev.append((ts_, s[0], side, level))
                old.add(acc)
            acc = key
    return ev


def one(args):
    path, m, sport, start, mh, ma = args
    db = W.connect(path)
    pmr = db.execute('SELECT ts_ms, server_ts_ms, outcome, bid, ask, last FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)).fetchall()
    pm = [r[:5] for r in pmr]
    bk = []
    for table in ('changes', 'changes_research'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            bk += db.execute(f"SELECT ts_ms, book, outcome, new_price FROM {table} WHERE match_id = ? AND market = '1x2' AND book NOT IN ('polymarket', 'kalshi') ORDER BY ts_ms", (m,)).fetchall()
    try:
        book = db.execute('SELECT ts_ms, outcome, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND bid_size IS NOT NULL ORDER BY ts_ms', (m,)).fetchall()
    except Exception:
        book = None
    sec, wv, sc = [], [], []
    all_ts = np.array([r[0] for r in pmr], np.int64)
    for k, o in enumerate(['home', 'away'] if sport in W.THREE_WAY else ['home']):
        g = F.build(pm, bk, o, sport, start, sport in W.THREE_WAY, book, full=True)
        if g is None:
            continue
        ts, X = np.asarray(g['ts'], np.int64), g['X']
        n = len(ts)
        live = X[:, C['inplay']] > 0.5
        if live.sum() < 1200 or abs((ts[1] - ts[0]) / 1000.0 - 1.0) > 1e-6:
            continue
        H = MN.lead_record(X, live, 1.0)
        key = m * 4 + k
        # the price path for the labels runs on from Polymarket's own rows (see waves2.py: survivorship)
        pr_ = [(q[0], q[3], q[4], q[5]) for q in pmr if q[2] == o]
        srv = np.array([q[0] if q[1] is None else q[1] for q in pmr if q[2] == o], np.int64)
        pt_x = np.array([q[0] for q in pr_], np.int64)
        pb_x = np.array([np.nan if q[1] is None else q[1] for q in pr_], float)
        pa_x = np.array([np.nan if q[2] is None else q[2] for q in pr_], float)
        ts_x = np.concatenate([ts, ts[-1] + 1000 * np.arange(1, 2401)])
        bid_x, ask_x = held(pt_x, pb_x, ts_x), held(pt_x, pa_x, ts_x)
        decided = (not np.isfinite(pb_x[-1])) or pb_x[-1] >= 0.97 or (np.isfinite(pa_x[-1]) and pa_x[-1] <= 0.03)
        n_lab = len(ts_x) if decided else min(len(ts_x), int(np.searchsorted(ts_x, pt_x[-1], side='right')))
        ja = np.searchsorted(all_ts, ts_x, side='right') - 1                      # HOLES: seconds since the game's newest Polymarket row of any outcome
        age = np.where(ja >= 0, (ts_x - all_ts[np.clip(ja, 0, None)]) / 1000.0, 1e6)[:n_lab]
        fresh = (age <= 30) | (decided & (ts_x[:n_lab] > pt_x[-1]))                # after a decided game's last row its last quote is its value
        kq = np.clip(np.searchsorted(pt_x, ts, side='right') - 1, 0, None)
        late = (pt_x[kq] - srv[kq]).astype(np.float32)
        sz = [np.full(n_lab, np.nan)] * 4
        bo = [r for r in (book or []) if r[1] == o]
        if bo:
            bt = np.array([r[0] for r in bo], np.int64)
            sz = [held(bt, np.array([np.nan if r[2 + j] is None else r[2 + j] for r in bo], float), ts_x)[:n_lab] for j in range(4)]
        bid, ask = bid_x[:n_lab], ask_x[:n_lab]
        mid = (bid + ask) / 2
        Q = LF.extra(X, bid, ask, sz, g['pm_eps'], g['sl_eps'])
        p1, p2, pr5 = wave_hist(g['pm_eps'], n)
        s1, _, sr5 = wave_hist(g['sl_eps'], n)
        Dx = np.concatenate([flow(pr_, ts, mid[:n]), np.stack([p1, p2, pr5, s1, sr5], 1).astype(np.float32)], 1)
        cus = {1: np.concatenate([X[:, C['pm_U']], np.zeros(max(n_lab - n, 0), np.float32)])[:n_lab], -1: np.concatenate([X[:, C['pm_D']], np.zeros(max(n_lab - n, 0), np.float32)])[:n_lab]}
        rs = np.flatnonzero(g['evt'] & g['ok'] & live)[::3]
        rs = rs[rs + 302 < n_lab]
        if len(rs):
            tn, amp, Y = np.zeros((len(rs), 2), np.int32), np.zeros((len(rs), 2), np.float32), np.full((len(rs), 2, len(LAB)), np.nan, np.float32)
            buyp = np.full((len(rs), 2), np.nan, np.float32)
            for j, d in enumerate((1, -1)):
                tn[:, j], amp[:, j], peak = next_wave(g['pm_eps'], d, rs)
                usd, e, tb, buy = trader(bid, ask, sz, rs, d, fresh)
                buyp[:, j] = buy
                Y[:, j, 0], Y[:, j, 1], Y[:, j, 2], Y[:, j, 3] = usd(e + 30), usd(e + 60), usd(e + 120), usd(e + 300)
                win = np.lib.stride_tricks.sliding_window_view(tb, 300)[e + 1]                               # tb[e+1 : e+301], fresh bids only
                hit = win >= (buy + 0.05)[:, None] - 1e-9
                Y[:, j, 4] = usd(e + 1 + np.where(hit.any(1), hit.argmax(1), 299))
                flat = np.lib.stride_tricks.sliding_window_view(cus[d] == 0, 300)[e + 1][:, 29:]              # from 30 s on: our CUSUM back at 0
                Y[:, j, 5] = usd(e + 30 + np.where(flat.any(1), flat.argmax(1), 270))
                soon = tn[:, j] <= SOON
                Y[:, j, 6] = np.where(soon, usd(np.clip(peak, e + 1, e + 300)), np.nan)
            sec.append({'t': ts[rs], 'game': np.full(len(rs), m, np.int32), 'outcome': np.full(len(rs), k, np.int8), 'sport': np.array([sport] * len(rs)),
                        'X': X[rs].astype(np.float32), 'H': H[rs], 'Q': Q[rs], 'D': Dx[rs], 'tn': tn, 'amp': amp, 'Y': Y, 'buy': buyp, 'age': age[rs].astype(np.float32), 'late': late[rs]})
        pe = sorted((e for e in g['pm_eps'] if e.get('on') is not None and live[e['alarm']]), key=lambda e: e['on'])
        for i, e in enumerate(pe):
            pv = pe[i - 1] if i else None
            wv.append((key, float(ts[e['on']]), e['dir'], abs(e['amp']) if np.isfinite(e['amp']) else 0.0, e['alarm'] - e['on'], pv['dir'] if pv else 0, (e['on'] - pv['end']) if pv else -1,
                       float(X[e['on'], C['sl_wave']]), float(X[e['on'], C['gap_e']]), float(X[e['on'], C['lead_up']] - X[e['on'], C['lead_down']]), float(Dx[e['on'], 1]), float(max(age[e['on']], age[e['alarm']]))))
        if k == 0:      # the score feed (home token; an away score = the home token falls)
            reports = []
            for bname, eid, eh, ea in db.execute('SELECT book, book_event_id, home, away FROM book_events WHERE match_id = ?', (m,)).fetchall():
                if bname in ('polymarket', 'kalshi'):
                    continue
                sw = sport != 'football' and swapped(mh, ma, eh, ea)
                for t_, s_, det in db.execute('SELECT ts_ms, score, detail FROM book_scores WHERE book = ? AND book_event_id = ? ORDER BY ts_ms', (bname, eid)):
                    reports.append((t_, bname, pairs(s_, sw), pairs(det, sw) if det else None))
            if reports:
                reports.sort(key=lambda r: r[0])
                al = {d: np.array(sorted(e['alarm'] for e in g['pm_eps'] if e['dir'] == d), np.int64) for d in (1, -1)}
                fm = {d: np.array(sorted(e['on'] + 1 for e in g['pm_eps'] if e['dir'] == d and e.get('on') is not None), np.int64) for d in (1, -1)}
                for t_c, t_f, side, level in score_events(reports):
                    i = int(np.searchsorted(ts, t_c, side='left'))
                    if i < 125 or i + 302 >= n_lab or i >= n or not live[i] or age[i] > 15:
                        continue
                    mv = [side * (mid[i + o_] - mid[i]) * 100 if fresh[i + o_] else np.nan for o_ in OFFS]
                    near = lambda arr: float(arr[np.argmin(np.abs(arr - i))] - i) if len(arr) and np.min(np.abs(arr - i)) <= 120 else np.nan
                    usd, e, tb, buy = trader(bid, ask, sz, np.array([i]), side, fresh)
                    win = tb[e[0] + 1:e[0] + 301]
                    hit = np.flatnonzero(win >= buy[0] + 0.05 - 1e-9)
                    ys = [float(usd(e + h_)[0]) for h_ in (30, 60, 120, 300)] + [float(usd(e + 1 + (hit[0] if len(hit) else 299))[0])]
                    sc.append((sport, [key, t_c, side, level, (t_c - t_f) / 1000.0, mid[i], (ask[i] - bid[i]) * 100] + mv + [near(al[side]), near(fm[side])] + ys))
    return sec, wv, sc


def cmd_build(a):
    gl = W.games(W.connect(a.db))
    if a.limit:
        gl = gl[::max(1, len(gl) // a.limit)][:a.limit]
    print(f'{len(gl)} games', flush=True)
    sec, wv, sc, t0 = [], [], [], time.time()
    with Pool(a.workers) as pool:
        for k, (s_, w_, c_) in enumerate(pool.imap_unordered(one, [(a.db, m, sp, st, h, aw) for m, sp, h, aw, st in gl], chunksize=2)):
            sec += s_
            wv += w_
            sc += c_
            if (k + 1) % 500 == 0:
                print(f'  {k + 1} of {len(gl)} games, {sum(len(p["t"]) for p in sec)} rows, {len(sc)} score changes, {time.time() - t0:.0f} s', flush=True)
    S = {key: np.concatenate([p[key] for p in sec]) for key in sec[0]}
    S['WV'] = np.array(wv, float).reshape(-1, 12)
    S['SC'] = np.array([x[1] for x in sc], float).reshape(-1, 7 + len(OFFS) + 2 + 5)
    S['SC_sport'] = np.array([x[0] for x in sc])
    np.savez(os.path.join(a.signals, a.name), **S)
    print(f'{len(S["t"])} decision seconds from {len(np.unique(S["game"]))} games, {len(S["WV"])} Polymarket waves, {len(S["SC"])} score changes, {time.time() - t0:.0f} s')


def cmd_test(a):
    import torch
    import lab as LB
    sys.stdout.reconfigure(encoding='utf-8')
    dev = MN.pick_device()
    tg = lambda v: torch.from_numpy(v).to(dev)
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    sig = lambda v: 1 / (1 + np.exp(-v))
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    S0 = np.load(os.path.join(a.signals, a.name))
    live_rows = (S0['age'] <= 15) & (S0['late'] <= 1000)          # the Mac's rule: decide only where the recording is alive and on time
    n_all = len(live_rows)
    S = {k: (S0[k][live_rows] if k not in ('WV', 'SC', 'SC_sport') else S0[k]) for k in S0.files}
    X, Hh, Q, D = S['X'], S['H'], S['Q'], S['D']
    FS = np.concatenate([X, Hh, Q, D], 1)
    FN = MN.NAMES + MN.HN + LF.QN + DN
    fc = {k: i for i, k in enumerate(FN)}
    Ts, G, SP, tn, amp, Y = S['t'], S['game'].astype(np.int64), S['sport'], S['tn'], S['amp'], S['Y']
    N = len(Ts)
    up, dn = tn[:, 0] <= SOON, tn[:, 1] <= SOON
    anyw, one30 = up | dn, up ^ dn
    up120, dn120 = tn[:, 0] <= 120, tn[:, 1] <= 120
    one120 = up120 ^ dn120
    bigup, bigdn = up & (amp[:, 0] >= BIG), dn & (amp[:, 1] >= BIG)
    rest = (FS[:, fc['pm_U']] == 0) & (FS[:, fc['pm_D']] == 0) & (FS[:, fc['pm_r10']] == 0)      # Polymarket has not moved: a call here is made before any move
    days = sorted({dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in Ts[::2000]})
    dayof = np.array([dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in (Ts // DAY * DAY)[np.unique(Ts // DAY, return_index=True)[1]]])
    daykey = dict(zip(np.unique(Ts // DAY).tolist(), dayof.tolist()))
    rowday = np.array([daykey[v] for v in (Ts // DAY).tolist()])
    first = d_ms(days[1])
    Xb = tg(LB.binned(FS, np.flatnonzero(Ts < first)))
    P = np.full((N, 8), np.nan, np.float32)             # out-of-sample logits: any, up, down, direction (30 s), direction (120 s), big up, big down, direction at rest
    TH = {}

    def split(idx, gap=600_000):
        idx = idx[np.argsort(Ts[idx], kind='stable')]
        cut = int(0.8 * len(idx))
        va, tr = idx[cut:], idx[:cut]
        return tr[Ts[tr] + gap < Ts[va].min()], va
    t00 = time.time()
    imp = np.zeros(len(FN))
    for dday in days[1:]:
        d0 = d_ms(dday)
        tr_all, today = np.flatnonzero(Ts + 600_000 < d0), np.flatnonzero((Ts >= d0) & (Ts < d0 + DAY))
        if len(tr_all) < 50_000 or not len(today):
            continue
        tr, va = split(tr_all)
        tdg = tg(today)
        PV = np.zeros((len(va), 8), np.float32)
        for j, y in ((0, anyw), (1, up), (2, dn), (5, bigup), (6, bigdn)):
            yt = tg(y.astype(np.float32))
            m = LB.GB().fit(Xb[tg(tr)], yt[tg(tr)], Xb[tg(va)], yt[tg(va)])
            P[today, j], PV[:, j] = m.predict(Xb[tdg]), m.predict(Xb[tg(va)])
        for j, mask, y in ((3, one30, up), (4, one120, up120), (7, one30 & rest, up)):
            trd, vad = tr[mask[tr]], va[mask[va]]
            if len(trd) < 2000 or len(vad) < 200:
                continue
            yt = tg(y.astype(np.float32))
            m = (LB.GB(min_leaf=50, row_frac=0.7, lam=20.0) if j == 7 else LB.GB(min_leaf=100, row_frac=0.5)).fit(Xb[tg(trd)], yt[tg(trd)], Xb[tg(vad)], yt[tg(vad)])
            P[today, j], PV[:, j] = m.predict(Xb[tdg]), m.predict(Xb[tg(va)])
            if j == 7:
                for f_, _, _ in m.trees:
                    for lvl in f_:
                        np.add.at(imp, lvl.cpu().numpy().ravel(), 1.0)
        sA = sig(PV[:, 1]) - sig(PV[:, 2])                                   # rule A: up-wave chance minus down-wave chance
        sB = sig(PV[:, 0]) * (2 * sig(PV[:, 3]) - 1)                         # rule B: chance of a wave x how sure of its direction
        sC = sig(PV[:, 5]) - sig(PV[:, 6])                                   # rule C: big-up-wave chance minus big-down-wave chance
        TH[dday] = {(r, q): (np.quantile(s_, 1 - q), np.quantile(-s_, 1 - q)) for r, s_ in (('A', sA), ('B', sB), ('C', sC)) for q in (0.01, 0.003, 0.001)}
        rv = rest[va]
        if rv.sum() >= 2000:
            sD = (sig(PV[:, 0]) * (2 * sig(PV[:, 7]) - 1))[rv]                 # rule D: only while Polymarket is at rest
            sE = (sig(PV[:, 5]) - sig(PV[:, 6]))[rv]                           # rule E: big waves, only at rest
            TH[dday].update({(r, q): (np.quantile(s_, 1 - q), np.quantile(-s_, 1 - q)) for r, s_ in (('D', sD), ('E', sE)) for q in (0.01, 0.003, 0.001)})
        print(f'  {dday}: trained on {len(tr)} rows ({time.time() - t00:.0f} s)', flush=True)
    sc_days = [x for x in days if x >= '2026-11-19' and x in TH]
    scored = np.isin(rowday, sc_days) & np.isfinite(P[:, 0])
    say(f'WHICH WAY WILL THE NEXT WAVE GO? {N:,} in-play decision seconds with a live recording ({n_all - N:,} = {(n_all - N) / n_all * 100:.1f}% dropped: Polymarket rows older than 15 s or over 1 s late), {len(np.unique(G))} games; scored on {", ".join(x[8:] for x in sc_days)} Nov ({scored.sum():,} seconds), '
        f'every model trained on the days before the day it is scored on.')
    say('')
    say(f'1. HOW OFTEN: the first move of an up wave comes within 30 s after {up[scored].mean() * 100:.1f}% of decision seconds, of a down wave {dn[scored].mean() * 100:.1f}%, of either {anyw[scored].mean() * 100:.1f}%, '
        f'of both {(up & dn)[scored].mean() * 100:.2f}%.')
    wavg = lambda f_: np.average([f_(np.flatnonzero(scored & (rowday == x))) for x in sc_days], weights=[(scored & (rowday == x)).sum() for x in sc_days])
    say(f'2. IS A WAVE COMING (either way, within 30 s)? no model 0.50 | model {wavg(lambda r: LB.auc(P[r, 0], anyw[r])):.3f}. An up wave: {wavg(lambda r: LB.auc(P[r, 1], up[r])):.3f}; a down wave: {wavg(lambda r: LB.auc(P[r, 2], dn[r])):.3f}.')
    top = np.zeros(N, bool)
    for x in sc_days:
        r = np.flatnonzero(scored & (rowday == x))
        top[r[P[r, 0] >= np.quantile(P[r, 0], 0.99)]] = True
    say(f'   in the model\'s top 1% of seconds a wave came {anyw[top].mean() * 100:.0f}% of the time (all seconds: {anyw[scored].mean() * 100:.1f}%).')
    tb_ = np.zeros(N, bool)
    for x in sc_days:
        r = np.flatnonzero(scored & (rowday == x))
        tb_[r[P[r, 5] >= np.quantile(P[r, 5], 0.99)]] = True
    say(f'   A BIG wave ({BIG} log-odds or more, about 11 pt at 50c): up {wavg(lambda r: LB.auc(P[r, 5], bigup[r])):.3f}, down {wavg(lambda r: LB.auc(P[r, 6], bigdn[r])):.3f}; a big up wave follows {bigup[scored].mean() * 100:.1f}% of seconds, '
        f'{bigup[tb_].mean() * 100:.0f}% of the model\'s top 1% (a big DOWN wave follows {bigdn[tb_].mean() * 100:.0f}% of those).')
    # ---------------- 3: direction given a wave
    say('')
    R = np.flatnonzero(scored & one30)
    yb = up[R]
    xr = FS[R]
    dl = P[R, 3]
    ca = np.abs(dl)
    rR = rest[R]
    lead_t = np.where(yb, tn[R, 0], tn[R, 1])
    ampR = np.where(yb, amp[R, 0], amp[R, 1])
    rules = (('Polymarket\'s move over the last 60 s', 'pm_r60', None), ('the superline is above / below Polymarket (gap)', 'gap_e', None), ('the superline is "ahead" (lead state)', 'lead_up', 'lead_down'),
             ('the superline is in a wave now', 'sl_wave', None), ('the superline\'s move over the last 10 s', 'sl_r10', None), ('the superline\'s move over the last 60 s', 'sl_r60', None),
             ('the superline\'s last wave', 'sl_prev1', None), ('bookmaker families that moved in 5 s (up minus down)', 'fam_up5', 'fam_dn5'), ('families that moved in 30 s (up minus down)', 'fam_up30', 'fam_dn30'),
             ('all bookmakers\' mean gap to Polymarket', 'fam_gap_mean', None), ('order book: more size bid than offered (best level)', 'ob_imb', None), ('order book within 2c', 'ob_dimb', None),
             ('order-flow imbalance, 10 s', 'ofi_10', None), ('order-flow imbalance, 60 s', 'ofi_60', None), ('trade prints: buyers minus sellers, 30 s', 'tf_sgn_30', None), ('trade prints, 120 s', 'tf_sgn_120', None),
             ('the last print above / below the mid', 'tf_last_mid', None), ('same way as Polymarket\'s last wave', 'pm_prev1', None), ('Polymarket\'s net of its last 5 waves', 'pm_run5', None))
    say(f'3. GIVEN A WAVE STARTS WITHIN 30 s, WHICH WAY? ({len(R):,} seconds before the waves of {len(np.unique(G[R]))} games; {yb.mean() * 100:.0f}% of them were up; a coin = 50%)')
    say(f'   3a. EVERY such second: the direction model is right {np.mean((dl > 0) == yb) * 100:.1f}% (AUC {LB.auc(dl, yb):.3f}); its surest 10%: {np.mean(((dl > 0) == yb)[ca >= np.quantile(ca, 0.9)]) * 100:.1f}%.')
    cu = xr[:, fc['pm_U']] - xr[:, fc['pm_D']]
    hc = np.abs(cu) > 1e-9
    say(f'       BUT that is mostly one mechanical fact: {(~rR).mean() * 100:.0f}% of these seconds have Polymarket already moving, and a wave the SAME way cannot "start" while one is running, so the next start is the '
        f'pull-back. "Against the side Polymarket is moving now" alone is right {np.mean((cu[hc] < 0) == yb[hc]) * 100:.1f}%; "against its last wave" {np.mean((xr[:, fc["pm_prev1"]] < 0) == yb)[()] * 100:.1f}%.')
    say('       the model while Polymarket is moving, by the size of the wave that came: ' + '; '.join(f'{nm} {np.mean(((dl > 0) == yb)[~rR & mm]) * 100:.1f}%' for nm, mm in (
        ('under 0.25 log-odds', ampR < 0.25), ('0.25-0.45', (ampR >= 0.25) & (ampR < BIG)), (f'big ({BIG}+)', ampR >= BIG), ('1.0+', ampR >= 1.0))))
    Rr = R[rR]
    yr_, d7, d3 = up[Rr], P[Rr, 7], P[Rr, 3]
    okr = np.isfinite(d7)
    say(f'   3b. POLYMARKET AT REST (no move for 10 s, both its CUSUMs at 0: the call is made BEFORE any move): {len(Rr):,} seconds, {len(np.unique(G[Rr]))} games; {yr_.mean() * 100:.0f}% were up')
    say(f'       the general direction model {np.mean((d3 > 0) == yr_) * 100:.1f}% right (AUC {LB.auc(d3, yr_):.3f}) | a model trained ONLY on such seconds {np.mean((d7[okr] > 0) == yr_[okr]) * 100:.1f}% (AUC {LB.auc(d7[okr], yr_[okr]):.3f})')
    c7 = np.abs(d7)
    for q, name in ((0.5, 'surer half'), (0.9, 'surest 10%'), (0.97, 'surest 3%')):
        sel = okr & (c7 >= np.nanquantile(c7[okr], q))
        say(f'          its {name}: {np.mean((d7[sel] > 0) == yr_[sel]) * 100:.1f}% right ({sel.sum():,} seconds, {len(np.unique(G[Rr][sel]))} games)')
    xq = FS[Rr]
    aq = np.where(yr_, amp[Rr, 0], amp[Rr, 1])
    lq = np.where(yr_, tn[Rr, 0], tn[Rr, 1])
    say(f'       by the size of the wave that came: ' + '; '.join(f'{nm} {np.mean(((d7 > 0) == yr_)[okr & mm]) * 100:.1f}% ({(okr & mm).sum():,})' for nm, mm in (
        ('under 0.25 log-odds', aq < 0.25), ('0.25-0.45', (aq >= 0.25) & (aq < BIG)), (f'big ({BIG}+)', aq >= BIG), ('1.0+', aq >= 1.0))))
    say('       by how long before the first move: ' + '; '.join(f'{nm} {np.mean(((d7 > 0) == yr_)[okr & mm]) * 100:.1f}% ({(okr & mm).sum():,})' for nm, mm in (
        ('1-5 s', lq <= 5), ('6-15 s', (lq >= 6) & (lq <= 15)), ('16-30 s', lq >= 16))))
    say('       by sport: ' + '; '.join(f'{sp_} {np.mean(((d7 > 0) == yr_)[okr & (SP[Rr] == sp_)]) * 100:.1f}% ({(okr & (SP[Rr] == sp_)).sum():,})' for sp_ in ('football', 'tennis', 'cs2', 'basketball', 'dota2', 'hockey') if (okr & (SP[Rr] == sp_)).sum() >= 200))
    say('       simple rules at rest (how often the rule has a view; right when it has one; right when the wave that came was BIG):')
    for name, k1, k2 in rules:
        v = xq[:, fc[k1]] - (xq[:, fc[k2]] if k2 else 0)
        has = np.abs(v) > 1e-9
        hb = has & (aq >= BIG)
        if has.sum() >= 200:
            say(f'          {name:<56} view {has.mean() * 100:5.1f}%  right {np.mean((v[has] > 0) == yr_[has]) * 100:5.1f}%  big waves {("%5.1f%%" % (np.mean((v[hb] > 0) == yr_[hb]) * 100)) if hb.sum() >= 100 else "  n/a"} ({hb.sum():,})')
    say('       what the at-rest model uses most (share of its splits): ' + ', '.join(f'{FN[i]} {imp[i] / max(imp.sum(), 1) * 100:.0f}%' for i in np.argsort(-imp)[:12]))
    R2 = np.flatnonzero(scored & one120 & np.isfinite(P[:, 4]))
    r2 = rest[R2]
    say(f'   3c. A LONGER LOOK, a wave within 2 min ({len(R2):,} seconds): model {np.mean((P[R2, 4] > 0) == up120[R2]) * 100:.1f}% right (AUC {LB.auc(P[R2, 4], up120[R2]):.3f}); at rest only {np.mean(((P[R2, 4] > 0) == up120[R2])[r2]) * 100:.1f}% ({r2.sum():,} seconds)')
    # ---------------- 4: the ceiling
    say('')
    say('4. THE CEILING: what a call makes when it is RIGHT, WRONG, or NO WAVE comes ($ per $100, fees and book depth; all scored seconds, no model)')
    okY = np.isfinite(Y[:, :, :6]).all((1, 2))
    Yr = np.where(up[:, None], Y[:, 0], Y[:, 1])                    # bought the wave's side
    Yw = np.where(up[:, None], Y[:, 1], Y[:, 0])                    # bought against it
    base = scored & okY
    Rw = base & one30
    Rn = base & ~anyw
    ampw = np.where(up, amp[:, 0], amp[:, 1])
    say('      ' + ' ' * 46 + ' | '.join(f'{x:>14}' for x in ('30 s', '1 min', '2 min', '5 min', '+5c / 5 min', 'wave ends', 'the top')))
    row = lambda name, v: say(f'      {name:<46}' + ' | '.join(f'{np.nanmean(v[:, j]):>+14.2f}' for j in range(7)))
    row('right way (bought the side the wave took)', Yr[Rw])
    row('wrong way', np.concatenate([Yw[Rw][:, :6], np.full((Rw.sum(), 1), np.nan)], 1))
    row('no wave came (either side)', np.concatenate([Y[Rn][:, :, :6].mean(1), np.full((Rn.sum(), 1), np.nan)], 1))
    lt = np.where(up, tn[:, 0], tn[:, 1])
    for name, mm in (('right way, 1 s before the first move', lt <= 1), ('right way, 2-5 s before', (lt >= 2) & (lt <= 5)), ('right way, 6-15 s before', (lt >= 6) & (lt <= 15)), ('right way, 16-30 s before', lt >= 16)):
        row(name, Yr[Rw & mm])
    qa = np.quantile(ampw[Rw], [0.5, 0.8, 0.95])
    for name, mm in ((f'right way, smaller half of waves (under {qa[0]:.2f} log-odds)', ampw < qa[0]), (f'right way, biggest 20% of waves ({qa[1]:.2f}+)', ampw >= qa[1]), (f'right way, biggest 5% ({qa[2]:.2f}+)', ampw >= qa[2])):
        row(name, Yr[Rw & mm])
    pb = np.where(up, S['buy'][:, 0], S['buy'][:, 1])
    for name, mm in (('right way, price paid 20-80c', (pb >= 0.2) & (pb <= 0.8)), ('right way, price paid under 20c or over 80c', (pb < 0.2) | (pb > 0.8)), ('right way, Polymarket AT REST', rest),
                     (f'right way, at rest, big waves ({BIG}+)', rest & (ampw >= BIG)), ('right way, Polymarket already moving', ~rest), (f'right way, already moving, big waves ({BIG}+)', ~rest & (ampw >= BIG))):
        row(name, Yr[Rw & mm])
    row('wrong way, Polymarket at rest', np.concatenate([Yw[Rw & rest][:, :6], np.full(((Rw & rest).sum(), 1), np.nan)], 1))
    row('no wave came, Polymarket at rest', np.concatenate([Y[Rn & rest][:, :, :6].mean(1), np.full(((Rn & rest).sum(), 1), np.nan)], 1))
    say(f'      waves from rest: {(Rw & rest).sum():,} seconds; {np.mean(ampw[Rw & rest] >= BIG) * 100:.0f}% of them big ({BIG}+); from a moving market {np.mean(ampw[Rw & ~rest] >= BIG) * 100:.0f}% big')
    say(f'      the price paid at those seconds: median {np.nanmedian(S["buy"][Rw]) * 100:.0f}c; spread median {np.median(FS[Rw, fc["spread"]]):.1f}c; a wave\'s size: median {qa[0]:.2f} log-odds (about {qa[0] * 25:.0f} pt at 50c)')
    # ---------------- 5: what a call needs
    say('')
    say('5. THE HIT RATE A CALL NEEDS. If a wave does come and the call is right with chance a: a x right + (1 - a) x wrong = 0.')
    for j in (1, 2, 3, 5, 6):
        Wn = np.nanmean(Yr[Rw][:, j])
        Ls = -np.nanmean(Yw[Rw][:, min(j, 5)])
        Ln = -np.nanmean(Y[Rn][:, :, min(j, 5)])
        if Wn > 0:
            a0 = Ls / (Wn + Ls)
            s_ = f'needs {a0 * 100:.0f}% right if a wave ALWAYS comes'
            for pw in (0.5, 0.25):
                need = (Ln * (1 - pw) / pw + Ls) / (Wn + Ls)
                s_ += f'; {"over 100" if need > 1 else "%.0f" % (need * 100)}% if one comes {pw * 100:.0f}% of the time'
        else:
            s_ = 'loses even when every call is right'
        say(f'      {LABN[j]:<40} right ${Wn:+.2f}, wrong ${-Ls:+.2f}, no wave ${-Ln:+.2f}: {s_}')
    # ---------------- 6: the model's calls as trades
    say('')
    say('6. THE MODEL\'S OWN CALLS AS TRADES (threshold from the 20% of earlier data held back, one position a game, $100 orders)')
    sA = sig(P[:, 1]) - sig(P[:, 2])
    sB = sig(P[:, 0]) * (2 * sig(P[:, 3]) - 1)
    sC = sig(P[:, 5]) - sig(P[:, 6])
    sD = np.where(rest, sig(P[:, 0]) * (2 * sig(P[:, 7]) - 1), np.nan)
    sE = np.where(rest, sC, np.nan)
    for r_, s_, rname in (('A', sA, 'up-wave chance minus down-wave chance'), ('B', sB, 'chance of a wave x how sure of the direction'), ('C', sC, 'BIG up-wave chance minus BIG down-wave chance'),
                          ('D', sD, 'at rest only: chance of a wave x the at-rest direction model'), ('E', sE, 'at rest only: BIG up-wave chance minus BIG down-wave chance')):
        for q in (0.01, 0.003, 0.001):
            rows, dirs = [], []
            for x in sc_days:
                if (r_, q) not in TH[x]:
                    continue
                tu, td = TH[x][(r_, q)]
                rr = np.flatnonzero(scored & okY & (rowday == x) & np.isfinite(s_))
                for d_, sel in ((0, s_[rr] >= tu), (1, -s_[rr] >= td)):
                    rows.append(rr[sel])
                    dirs.append(np.full(sel.sum(), d_))
            if not rows:
                continue
            rows, dirs = np.concatenate(rows), np.concatenate(dirs)
            if len(rows) < 30:
                continue
            o = np.argsort(Ts[rows], kind='stable')
            rows, dirs = rows[o], dirs[o]
            right = np.where(dirs == 0, up[rows], dn[rows])
            wrong = np.where(dirs == 0, dn[rows], up[rows]) & ~right
            none = ~right & ~wrong
            yp = Y[rows, dirs]
            say(f'   rule {r_} ({rname}), top {q * 100:g}% of seconds each way: {len(rows):,} calls. A wave their way came after {right.mean() * 100:.0f}%, the other way {wrong.mean() * 100:.0f}%, none within 30 s {none.mean() * 100:.0f}%.')
            say('        every call, $ per $100: ' + ' | '.join(f'{LABN[j]} {np.nanmean(yp[:, j]):+.2f}' for j in range(6)))
            for j in (2, 5):
                Wn, Ls = np.nanmean(yp[right, j]), -np.nanmean(yp[~right, j])
                say(f'        {LABN[j]}: right ${Wn:+.2f}, not right ${-Ls:+.2f}: needs {("%.0f%%" % (Ls / (Wn + Ls) * 100)) if Wn > 0 else "more than always"} right, is right {right.mean() * 100:.0f}%')
            bigr = right & (np.where(dirs == 0, amp[rows, 0], amp[rows, 1]) >= BIG)
            if bigr.sum() >= 10 and (~bigr).sum() >= 10:
                for j in (2, 3):
                    Wn, Ls = np.nanmean(yp[bigr, j]), -np.nanmean(yp[~bigr, j])
                    say(f'        a BIG wave their way came after {bigr.mean() * 100:.0f}%; {LABN[j]}: then ${Wn:+.2f}, otherwise ${-Ls:+.2f}: needs a big wave {("%.0f%%" % (Ls / (Wn + Ls) * 100)) if Wn > 0 else "more than always"} of the time')
            busy, keep = {}, []
            for i_, (r, d_) in enumerate(zip(rows, dirs)):
                if Ts[r] >= busy.get(int(G[r]), -1):
                    busy[int(G[r])] = int(Ts[r]) + 301_000
                    keep.append(i_)
            keep = np.array(keep, np.int64)
            for j in (1, 2, 3, 5):
                say(MN.fmt(f'     one position a game, {LABN[j]}', MN.gstats(yp[keep, j], G[rows[keep]])))
    with open(os.path.join(a.signals, 'wavedir.txt'), 'w', encoding='utf-8') as fh:      # safe on disk before the slow last steps
        fh.write('\n'.join(out) + '\n')
    # ---------------- 7: one model for dollars that reads every call
    say('')
    say('7. ONE MODEL FOR DOLLARS that reads the inputs AND every call above (both sides of every second; top picks, one position a game)')
    have = np.isfinite(P[:, :7]).all(1) & okY
    Pz = np.nan_to_num(P)
    Fm = np.concatenate([MN.mirror(X), Hh, LF.mirror(Q), mirror_d(D)], 1)
    calls = lambda d_: np.stack([Pz[:, 1] if d_ > 0 else Pz[:, 2], Pz[:, 2] if d_ > 0 else Pz[:, 1], Pz[:, 0], d_ * Pz[:, 3], d_ * Pz[:, 4], Pz[:, 5] if d_ > 0 else Pz[:, 6], Pz[:, 6] if d_ > 0 else Pz[:, 5], d_ * Pz[:, 7]], 1)
    FC = np.concatenate([np.concatenate([FS, calls(1)], 1), np.concatenate([Fm, calls(-1)], 1)])
    del Fm
    Tc, Gc, Hc, Dc = np.concatenate([Ts, Ts]), np.concatenate([G, G]), np.concatenate([have, have]), np.concatenate([rowday, rowday])
    Yc = np.concatenate([Y[:, 0, :6], Y[:, 1, :6]])
    base_c = np.flatnonzero(Hc & (Tc < d_ms(days[min(3, len(days) - 1)])))
    if len(base_c) < 5000:
        base_c = np.flatnonzero(Hc)
    Xc = tg(LB.binned(np.nan_to_num(FC), base_c))
    Yg = tg(np.clip(np.nan_to_num(Yc), -40, 40).astype(np.float32))
    nF = FS.shape[1]
    res = {(v, j, q): [] for v in ('with the calls', 'without them') for j in (1, 3) for q in (0.003, 0.001)}
    for dday in [x for x in sc_days if x >= '2026-11-20']:
        d0 = d_ms(dday)
        tr_all, today = np.flatnonzero(Hc & (Tc + 600_000 < d0)), np.flatnonzero(Hc & (Tc >= d0) & (Tc < d0 + DAY))
        if len(tr_all) < 100_000 or not len(today):
            continue
        idx = tr_all[np.argsort(Tc[tr_all], kind='stable')]
        cut = int(0.8 * len(idx))
        va, tr = idx[cut:], idx[:cut]
        tr = tr[Tc[tr] + 600_000 < Tc[va].min()]
        for vname, cols in (('with the calls', None), ('without them', torch.arange(nF, device=dev))):
            for j in (1, 3):
                m = LB.GB(loss='l2', min_leaf=300, lam=100.0, n_trees=200, lr=0.1, row_frac=0.15).fit(Xc[tg(tr)], Yg[tg(tr), j], Xc[tg(va)], Yg[tg(va), j], cols=cols)
                pt, pv = m.predict(Xc[tg(today)]), m.predict(Xc[tg(va)])
                for q in (0.003, 0.001):
                    tk = MN.place(Tc, Gc, today[pt >= np.quantile(pv, 1 - q)], hold=301_000)
                    res[(vname, j, q)] += [(Yc[i, j], Gc[i]) for i in tk]
        print(f'  {dday}: dollar models trained ({time.time() - t00:.0f} s)', flush=True)
    for (vname, j, q), rows in res.items():
        if rows:
            say(MN.fmt(f'{vname}, top {q * 100:g}%, {LABN[j]}', MN.gstats([x[0] for x in rows], [x[1] for x in rows])))
    del Xc, Yg, FC
    # ---------------- 8: wave after wave
    say('')
    WV = S['WV']
    WV = WV[WV[:, 11] <= 15]
    wd, wa, wp, wgap, wsl, wge, wld, wtf = WV[:, 2], WV[:, 3], WV[:, 5], WV[:, 6], WV[:, 7], WV[:, 8], WV[:, 9], WV[:, 10]
    hp = wp != 0
    say(f'8. WAVE AFTER WAVE ({len(WV):,} Polymarket waves in play, all 7 days, no model): {np.mean(wd > 0) * 100:.0f}% are up.')
    say(f'   the next wave goes the SAME way as the one before it {np.mean(wd[hp] == wp[hp]) * 100:.1f}% of the time; when it starts within 30 s of the last one ending {np.mean((wd == wp)[hp & (wgap <= 30)]) * 100:.1f}% ({(hp & (wgap <= 30)).sum():,}), '
        f'30 s - 5 min later {np.mean((wd == wp)[hp & (wgap > 30) & (wgap <= 300)]) * 100:.1f}%, later than that {np.mean((wd == wp)[hp & (wgap > 300)]) * 100:.1f}%')
    for name, v in (('the superline was in a wave when it began', wsl), ('the superline was "ahead" when it began', wld), ('the gap to the superline when it began', wge), ('trade prints over the 30 s before it began', wtf)):
        has = np.abs(v) > 1e-9
        say(f'   {name:<50} in {has.mean() * 100:4.1f}% of waves; the wave went that way {np.mean((v[has] > 0) == (wd[has] > 0)) * 100:.1f}%')
    # ---------------- 9: the score feed
    say('')
    SC, SS = S['SC'], S['SC_sport']
    say(f'9. THE BOOKMAKERS\' LIVE SCORES (recorded 22 Nov 05:40 - 23 Nov 12:52 UTC only): {len(SC):,} score changes that two books agreed on, in {len(np.unique(SC[:, 0] // 4)) if len(SC) else 0} Polymarket games.')
    if len(SC):
        o0 = 7
        col = {o_: o0 + i for i, o_ in enumerate(OFFS)}
        ia, ifm, iy = o0 + len(OFFS), o0 + len(OFFS) + 1, o0 + len(OFFS) + 2
        say('   Polymarket\'s price of the side that scored, cents, around the second WE saw the score (median / mean); then: was Polymarket\'s wave that way already detected; buying that side then, $ per $100')
        for sp in ('football', 'cs2', 'tennis', 'basketball', 'dota2', 'hockey', 'volleyball', 'lol', 'valorant'):
            for lv, lname in ((0, 'main score'), (1, 'detail (rounds / games / points)')):
                mm = (SS == sp) & (SC[:, 3] == lv)
                if mm.sum() < 40:
                    continue
                v = SC[mm]
                md = lambda c_: f'{np.nanmedian(v[:, c_]):+.1f} / {np.nanmean(v[:, c_]):+.1f}'
                before = -v[:, col[-60]]
                say(f'   {sp}, {lname}: {mm.sum():,} changes in {len(np.unique(v[:, 0] // 4))} games; a second book confirmed {np.median(v[:, 4]):.1f} s after the first showed it')
                say(f'        moved in the 60 s BEFORE we saw it {np.nanmedian(before):+.1f} / {np.nanmean(before):+.1f}c, in the last 10 s before {np.nanmedian(-v[:, col[-10]]):+.1f} / {np.nanmean(-v[:, col[-10]]):+.1f}c; '
                    f'AFTER: +10 s {md(col[10])}c, +60 s {md(col[60])}c, +120 s {md(col[120])}c')
                al = v[:, ia]
                say(f'        Polymarket\'s wave that way: detected before we saw the score {np.mean(al < 0) * 100:.0f}%, within 60 s after {np.mean((al >= 0) & (al <= 60)) * 100:.0f}%, none near {np.mean(~np.isfinite(al) | (al > 60)) * 100:.0f}%')
                okb = np.isfinite(v[:, iy:iy + 5]).all(1)
                if okb.sum() >= 30:
                    for j, nm in ((1, 'after 1 min'), (3, 'after 5 min'), (4, '+5c or 5 min')):
                        say(MN.fmt(f'     buy the scorer when we see it, sold {nm}', MN.gstats(v[okb, iy + j], v[okb, 0] // 4)))
                    fresh = okb & (np.nan_to_num(-v[:, col[-60]]) < 1.0) & ~(al < 0)
                    if fresh.sum() >= 30:
                        say(MN.fmt('     only where Polymarket had NOT moved yet, 5 min', MN.gstats(v[fresh, iy + 3], v[fresh, 0] // 4)))
    with open(os.path.join(a.signals, 'wavedir.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'test'])
    ap.add_argument('--db')
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=22)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--name', default='wavedir.npz')
    a = ap.parse_args()
    (cmd_build if a.cmd == 'build' else cmd_test)(a)


if __name__ == '__main__':
    main()
