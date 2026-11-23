"""Wave to wave: who starts first, and which waves will be high (owner, 24 Nov).

  python tools/signals/waves2.py build --db X --signals DIR [--workers 22] [--limit N]
  python tools/signals/waves2.py test  --signals DIR

"Leading" measured the way the wave system means it: each superline wave is matched to Polymarket's wave of the same
direction (CUSUM trend waves of lead.py; onsets within 60 s, each wave used once). delay = Polymarket's onset minus
the superline's (+ = the superline started first). A wave nobody matched = the other line did not follow.
BUILD: one row per wave START (the second its alarm fires: the first moment a live bot knows the wave exists), for
superline waves and Polymarket waves, in play:
  X, H  the 99 inputs (mirrored to the wave's direction at training time) and mine.py's lead record;
  W     the game's WAVE-TO-WAVE record so far (only pairs already known): matched pairs, share the superline started
        first, median delay, share of superline waves Polymarket did not follow and the reverse; and this wave at its
        alarm: size and seconds since its onset, how far the other line has moved since that onset, whether the other
        line is already in a wave the same way, which line's wave it is;
  Y     dollars a $100 order makes buying the wave's direction 1 s later (fees, depth both sides, collapsed tokens):
        h60 / h300 / h600 = sold after 1 / 5 / 10 min; tp5 = +5c or 5 min; trail3 / trail5 = sold when the bid falls
        3c / 5c from its best since entry (10 min at most); wend = sold when the wave that triggered ends (10 min most);
  T     what the wave turned into: mfe5 (best sale within 5 min minus the price paid, cents), mv5 (mid move in 5 min),
        amp_rem (how much further the triggering line's wave went after the alarm, log-odds), amp_tot.
TEST (19-23 Nov, every choice on earlier days): 1 wave-to-wave facts and persistence; 2 does the record predict the
dollars of "superline wave started, Polymarket has not"; 3 HIGH vs LOW waves: boosted trees for "high" (mfe5 >= 6c)
and for dollars, picks with thresholds from earlier days, every exit on the same picks, by sport, and CS2-only /
tennis-only / football-only models."""
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
import labfeat as LF  # noqa: E402
import mine as MN  # noqa: E402
import wave_targets as WT  # noqa: E402
import waves as W  # noqa: E402
from booklead import fee, held  # noqa: E402

C = MN.C
LAB = ['h60', 'h300', 'h600', 'tp5', 'trail3', 'trail5', 'wend', 'h1200', 'h1800']
LABN = ['sell after 1 min', 'after 5 min', 'after 10 min', '+5c or 5 min', 'trail 3c', 'trail 5c', 'when the wave ends', 'after 20 min', 'after 30 min']
WN = ['w_n', 'w_slfirst', 'w_delay', 'w_unf_sl', 'w_unf_pm', 'amp_alarm', 'dur_alarm', 'other_moved', 'other_in_wave', 'is_pm_wave']
TG = ['mfe5', 'mv5', 'amp_rem', 'amp_tot']
DAY = 86_400_000
SPORTS = ('football', 'cs2', 'tennis', 'basketball', 'dota2')


def money(bid, ask, sz, rows, ends, d):
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
        return np.where(okb, m1 * sell + (nn - m1) * np.maximum(sell - 0.01, 0.0) - nn * fee(sell) - cost, np.nan)
    out = np.full((len(rows), len(LAB)), np.nan, np.float32)
    out[:, 0], out[:, 1], out[:, 2] = usd(e + 60), usd(e + 300), usd(e + 600)
    win = np.lib.stride_tricks.sliding_window_view(np.nan_to_num(tb, nan=-1.0), 600)[e + 1]      # tb[e+1 : e+601]
    hit = win[:, :300] >= (buy + 0.05)[:, None] - 1e-9
    out[:, 3] = usd(e + 1 + np.where(hit.any(1), hit.argmax(1), 299))
    rm = np.maximum.accumulate(win, axis=1)
    for j, s in ((4, 0.03), (5, 0.05)):
        st = win <= rm - s + 1e-9
        out[:, j] = usd(e + 1 + np.where(st.any(1), st.argmax(1), 599))
    out[:, 6] = usd(np.clip(ends + 1, e + 1, e + 600))
    out[:, 7], out[:, 8] = usd(e + 1200), usd(e + 1800)
    mid = (tb + ta) / 2
    j30 = nxt[e + 1800]
    sell30 = np.where((j30 < n) & (j30 - e - 1800 <= 600), np.nan_to_num(tb[np.minimum(j30, n - 1)]), 0.0)
    return out, np.where(okb, (win[:, :300].max(1) - buy) * 100, np.nan), (mid[e + 300] - mid[e]) * 100, np.where(okb, buy, np.nan), sell30


def one(args):
    path, m, sport, start, grid = args[:5]
    lab = len(args) > 5 and args[5]       # lab.py: also the extra inputs (labfeat), the follow label and the per-second rows
    db = W.connect(path)
    pm, bk = W.load(db, m)
    try:
        book = db.execute('SELECT ts_ms, outcome, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND bid_size IS NOT NULL ORDER BY ts_ms', (m,)).fetchall()
    except Exception:
        book = None
    ev_out, facts = [], {'P': [], 'S': [], 'Q': []}
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
        x = X[:, C['x']].astype(float)
        slp = np.clip(np.asarray(g['sl'], float), 0.005, 0.995)
        sl = np.log(slp / (1 - slp))
        S = [e for e in g['sl_eps'] if live[e['alarm']]]
        Pw = [e for e in g['pm_eps'] if live[e['alarm']]]
        key = m * 4 + k
        lm = H[:, MN.HN.index('live_min')]
        # SURVIVORSHIP FIX (24 Nov): the feature grid ends when the bookmakers stop (the game is over). Dropping entries whose
        # sale falls after that keeps only games that went on, which for a cheap underdog means it had not lost yet. The
        # price path for the labels therefore runs on from Polymarket's own rows: past the end of the game when its last
        # quote is decided (bid 97c+, or no bid / ask 3c-: the token's final value), else only as far as it was recorded.
        pr_ = [(q[0], q[3], q[4]) for q in pm if q[2] == o]
        pt_x = np.array([q[0] for q in pr_], np.int64)
        pb_x = np.array([np.nan if q[1] is None else q[1] for q in pr_], float)
        pa_x = np.array([np.nan if q[2] is None else q[2] for q in pr_], float)
        ts_x = np.concatenate([ts, ts[-1] + 1000 * np.arange(1, 2401)])
        bid_x, ask_x = held(pt_x, pb_x, ts_x), held(pt_x, pa_x, ts_x)
        decided = (not np.isfinite(pb_x[-1])) or pb_x[-1] >= 0.97 or (np.isfinite(pa_x[-1]) and pa_x[-1] <= 0.03)
        n_lab = len(ts_x) if decided else min(len(ts_x), int(np.searchsorted(ts_x, pt_x[-1], side='right')))
        used, M = set(), []
        sK, sF = np.array([e['alarm'] + 60 for e in S], np.int64), np.zeros(len(S), bool)
        pK, pF = np.array([e['alarm'] + 60 for e in Pw], np.int64), np.zeros(len(Pw), bool)
        for i, s in enumerate(S):
            best = None
            for j, p in enumerate(Pw):
                dd = p['on'] - s['on']
                if j not in used and p['dir'] == s['dir'] and abs(dd) <= 60 and (best is None or abs(dd) < abs(best[1])):
                    best = (j, dd)
            if best is not None:
                j = best[0]
                used.add(j)
                kn = max(s['alarm'], Pw[j]['alarm'])
                sF[i], pF[j], sK[i], pK[j] = True, True, kn, kn
                M.append((kn, best[1]))
        M.sort()
        mk, md = np.array([a for a, _ in M], np.int64), np.array([b for _, b in M], float)
        so, po = np.argsort(sK), np.argsort(pK)
        sKs, sFs, pKs, pFs = sK[so], sF[so], pK[po], pF[po]
        facts['P'] += [(key, float(lm[min(a, n - 1)]), b) for a, b in M]
        facts['S'] += [(key, float(lm[e['alarm']]), bool(f)) for e, f in zip(S, sF)]
        facts['Q'] += [(key, float(lm[e['alarm']]), bool(f)) for e, f in zip(Pw, pF)]
        rows, Wf, meta = [], [], []
        oth_al = {(typ, d): np.array(sorted(e['alarm'] for e in (Pw if typ == 0 else S) if e['dir'] == d), np.int64) for typ in (0, 1) for d in (1, -1)}
        for typ, eps, L, O, other in ((0, S, sl, x, 'pm_wave'), (1, Pw, x, sl, 'sl_wave')):
            for e in eps:
                r, d = e['alarm'], e['dir']
                if r + 1802 >= n_lab or not g['ok'][r]:
                    continue
                a_ = oth_al[(typ, d)]
                i0 = int(np.searchsorted(a_, r, side='right'))
                fol = float(i0 < len(a_) and a_[i0] <= r + 60)       # the other line starts a wave the same way within 60 s
                nm = int(np.searchsorted(mk, r))
                ns, np_ = int(np.searchsorted(sKs, r)), int(np.searchsorted(pKs, r))
                Wf.append([np.log1p(nm), ((md[:nm] > 0).sum() + 1) / (nm + 2), float(np.median(np.clip(md[:nm], -60, 60))) if nm else 0.0,
                           1 - (sFs[:ns].sum() + 1) / (ns + 2), 1 - (pFs[:np_].sum() + 1) / (np_ + 2),
                           d * (L[r] - L[e['on']]), r - e['on'], d * (O[r] - O[e['on']]), float(X[r, C[other]] == d), typ])
                rows.append(r)
                meta.append((d, e['end'], d * (L[e['peak']] - L[r]), abs(e['amp']), fol))
        if grid:      # control (owner, 24 Nov: "buying at any time at random and holding 30 min might be profitable too"): a fixed clock, no waves, both directions
            rows, Wf, meta = [], [], []
            for r in range(int(np.argmax(live)) + 60, min(n, n_lab - 1802), grid):
                if g['ok'][r]:
                    for d in (1, -1):
                        rows.append(r)
                        Wf.append([0.0] * 9 + [2.0])
                        meta.append((d, r, 0.0, 0.0, 0.0))
        if not rows:
            continue
        rows, Wf, meta = np.array(rows, np.int64), np.nan_to_num(np.array(Wf, np.float32)), np.array(meta, float)
        sz = [np.full(n_lab, np.nan)] * 4
        bo = [r for r in (book or []) if r[1] == o]
        if bo:
            bt = np.array([r[0] for r in bo], np.int64)
            sz = [held(bt, np.array([np.nan if r[2 + j] is None else r[2 + j] for r in bo], float), ts_x)[:n_lab] for j in range(4)]
        bid, ask = bid_x[:n_lab], ask_x[:n_lab]
        Y = np.full((len(rows), len(LAB)), np.nan, np.float32)
        T_ = np.full((len(rows), 9), np.nan, np.float32)      # mfe5, mv5, amp_rem, amp_tot, price paid, sale price at 30 min, PM feed lateness (ms), PM quote age (s), other line followed
        T_[:, 8] = meta[:, 4]
        pq = [(r_[0], r_[1]) for r_ in pm if r_[2] == o]
        pt_ = np.array([q[0] for q in pq], np.int64)
        sv_ = np.array([np.nan if q[1] is None else q[1] for q in pq], float)
        kq = np.clip(np.searchsorted(pt_, ts[rows], side='right') - 1, 0, None)
        T_[:, 6], T_[:, 7] = pt_[kq] - sv_[kq], (ts[rows] - pt_[kq]) / 1000.0
        T_[:, 2], T_[:, 3] = meta[:, 2], meta[:, 3]
        for d in (1, -1):
            mm = meta[:, 0] == d
            if mm.any():
                Y[mm], T_[mm, 0], T_[mm, 1], T_[mm, 4], T_[mm, 5] = money(bid, ask, sz, rows[mm], meta[mm, 1].astype(np.int64), d)
        ev_out.append({'t': ts[rows], 'game': np.full(len(rows), m, np.int32), 'outcome': np.full(len(rows), k, np.int8), 'sport': np.array([sport] * len(rows)),
                       'd': meta[:, 0].astype(np.int8), 'X': X[rows].astype(np.float32), 'H': H[rows], 'W': Wf, 'Y': Y, 'TG': T_})
        if lab:
            Q = LF.extra(X, bid, ask, sz, g['pm_eps'], g['sl_eps'])
            ev_out[-1]['Q'] = Q[rows]
            Tt = WT.wave_targets(g)
            rs = np.flatnonzero(g['evt'] & g['ok'] & live)[::3]
            rs = rs[rs + 302 < n_lab]
            if len(rs):
                any120 = np.zeros(n, np.int8)
                for e in g['pm_eps']:
                    if e.get('on') is not None:
                        any120[max(0, e['on'] + 1 - 120):e['on'] + 1] = 1
                facts.setdefault('SEC', []).append({
                    't': ts[rs], 'game': np.full(len(rs), m, np.int32), 'sport': np.array([sport] * len(rs)), 'X': X[rs].astype(np.float32), 'H': H[rs], 'Q': Q[rs],
                    'up30': Tt['up_soon'][rs].astype(np.int8), 'dn30': Tt['dn_soon'][rs].astype(np.int8), 'any120': any120[rs],
                    'rem_t': Tt['rem_t'][rs].astype(np.float32), 'rem_a': Tt['rem_a'][rs].astype(np.float32),
                    'Y': np.stack([MN.labels(bid, ask, sz, rs, 1), MN.labels(bid, ask, sz, rs, -1)], 1).astype(np.float32)})
    return ev_out, facts


def cmd_build(a):
    db0 = W.connect(a.db)
    gl = [g for sp in a.sports.split(',') for g in W.games(db0, since_ms=a.since or None, sport=sp)] if a.sports else W.games(db0, since_ms=a.since or None)
    if a.limit:
        gl = gl[::max(1, len(gl) // a.limit)][:a.limit]
    print(f'{len(gl)} games', flush=True)
    parts, facts, t0 = [], {'P': [], 'S': [], 'Q': []}, time.time()
    with Pool(a.workers) as pool:
        for k, (r, f) in enumerate(pool.imap_unordered(one, [(a.db, m, sp, st, a.grid) for m, sp, _, _, st in gl], chunksize=2)):
            parts += r
            for key in facts:
                facts[key] += f[key]
            if (k + 1) % 500 == 0:
                print(f'  {k + 1} of {len(gl)} games, {sum(len(p["t"]) for p in parts)} wave starts, {time.time() - t0:.0f} s', flush=True)
    D = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    for key in facts:
        D['F' + key] = np.array(facts[key], float).reshape(-1, 3)
    np.savez(os.path.join(a.signals, a.name), **D)
    print(f'{len(D["t"])} wave starts ({int((D["W"][:, 9] == 0).sum())} superline, {int((D["W"][:, 9] == 1).sum())} Polymarket) from {len(np.unique(D["game"]))} games in {time.time() - t0:.0f} s; '
          f'{len(D["FP"])} matched pairs; every wave start: ' + ', '.join(f'{lab} ${np.nanmean(D["Y"][:, j]):+.2f}' for j, lab in enumerate(LAB)))


def auc(score, pos):
    r = np.argsort(np.argsort(score)) + 1
    n1, n0 = pos.sum(), (~pos).sum()
    return (r[pos].sum() - n1 * (n1 + 1) / 2) / max(n1 * n0, 1)


def cmd_test(a):
    import torch
    sys.stdout.reconfigure(encoding='utf-8')
    dev = 'cuda'
    D = np.load(os.path.join(a.signals, 'waves2.npz'))
    X, H, Wf, Y, TGT, T, G, SP, d = D['X'], D['H'], D['W'], D['Y'], D['TG'], D['t'], D['game'].astype(np.int64), D['sport'], D['d']
    Xd = X.copy()
    Xd[d < 0] = MN.mirror(X[d < 0])
    FE = np.concatenate([Xd, H, Wf], 1)
    FN = MN.NAMES + MN.HN + WN
    fc = {k: i for i, k in enumerate(FN)}
    okY = np.isfinite(Y).all(1) & np.isfinite(TGT[:, 0])
    out = []
    say = lambda s: (out.append(s), print(s, flush=True))
    rk = lambda v: np.argsort(np.argsort(v))
    sp_ = lambda p, q: np.corrcoef(rk(p), rk(q))[0, 1] if len(p) > 5 else float('nan')
    days = sorted({dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for x in T})
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    test_days = [x for x in days if x >= '2026-11-19']
    ndays = (T.max() - T.min()) / DAY
    # ---------------- 1: wave-to-wave facts
    FP, FS, FQ = D['FP'], D['FS'], D['FQ']
    dl = FP[:, 2]
    say(f'1. WAVE TO WAVE ({len(FS)} superline waves, {len(FQ)} Polymarket waves in play, {len(np.unique(G))} games, {ndays:.1f} days)')
    say(f'   Polymarket followed a superline wave (same direction, onsets within 60 s) in {FS[:, 2].mean() * 100:.0f}% of superline waves; the superline followed a Polymarket wave in {FQ[:, 2].mean() * 100:.0f}%')
    say(f'   in the {len(dl)} matched pairs: superline started first {np.mean(dl > 0) * 100:.0f}%, Polymarket first {np.mean(dl < 0) * 100:.0f}%, same second {np.mean(dl == 0) * 100:.0f}%; '
        f'median delay {np.median(dl):+.0f} s (+ = superline first); when the superline is first it is {np.median(dl[dl > 0]):.0f} s ahead (median), when Polymarket is first {np.median(-dl[dl < 0]):.0f} s')
    order = np.lexsort((FP[:, 1], FP[:, 0]))
    kk, ll, dd = FP[order, 0], FP[order, 1], FP[order, 2]
    cut = np.flatnonzero(np.r_[True, kk[1:] != kk[:-1], True])
    g_all, g1, g2, e3, l3 = [], [], [], [], []
    for i in range(len(cut) - 1):
        v = dd[cut[i]:cut[i + 1]] > 0
        if len(v) >= 6:
            h = len(v) // 2
            g_all.append(v.mean()), g1.append(v[:h].mean()), g2.append(v[h:].mean()), e3.append(v[:3].sum()), l3.append(v[3:].mean())
    g_all, g1, g2, e3, l3 = (np.array(v) for v in (g_all, g1, g2, e3, l3))
    say(f'   per game ({len(g_all)} games with 6+ matched pairs): share of pairs the superline started first: median {np.median(g_all) * 100:.0f}%, top quarter of games {np.quantile(g_all, 0.75) * 100:.0f}%+, '
        f'top tenth {np.quantile(g_all, 0.9) * 100:.0f}%+; first in more than half the pairs in {np.mean(g_all > 0.5) * 100:.0f}% of games')
    say(f'   does a game keep its order? first half of its pairs vs second half: rank correlation {sp_(g1, g2):+.2f}. Superline first in 0-1 of the first 3 pairs ({np.mean(e3 <= 1) * 100:.0f}% of games) -> '
        f'first in {l3[e3 <= 1].mean() * 100:.0f}% of the later pairs; first in 2-3 of the first 3 -> {l3[e3 >= 2].mean() * 100:.0f}%')
    # ---------------- 2: the record and the dollars
    live = FE[:, fc['live_min']]
    say('')
    say('2. DOES THE WAVE-TO-WAVE RECORD PREDICT DOLLARS? ($ per $100, sold after 5 min / 10 min; win = +5c-or-5-min won)')
    base = okY & (Wf[:, 9] == 0) & (Wf[:, 8] == 0) & (Wf[:, 7] < 0.02)
    row = lambda name, mm: say(f'   {name:<62} {mm.sum():>6}  ${Y[mm, 1].mean():+6.2f} / ${Y[mm, 2].mean():+6.2f}  win {np.mean(Y[mm, 3] > 0) * 100:.0f}%  high waves {np.mean(TGT[mm, 0] >= 6) * 100:.0f}%') if mm.sum() >= 30 else None
    row('a superline wave starts, Polymarket has not moved', base)
    row('   first 15 min live', base & (live < 15))
    row('   after 15 min live', base & (live >= 15))
    rec = base & (Wf[:, 0] >= np.log1p(3)) & (live >= 15)
    for lo, hi in ((0, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 1.01)):
        row(f'   record so far (3+ pairs): superline first in {lo * 100:.0f}-{min(hi, 1) * 100:.0f}% of pairs', rec & (Wf[:, 1] >= lo) & (Wf[:, 1] < hi))
    for lo, hi in ((0, 0.3), (0.3, 0.6), (0.6, 1.01)):
        row(f'   Polymarket ignored {lo * 100:.0f}-{min(hi, 1) * 100:.0f}% of the superline\'s waves so far', rec & (Wf[:, 3] >= lo) & (Wf[:, 3] < hi))
    pmw = okY & (Wf[:, 9] == 1)
    row('a Polymarket wave starts, the superline was already in that wave', pmw & (Wf[:, 8] == 1))
    row('a Polymarket wave starts, the superline is not in a wave', pmw & (Wf[:, 8] == 0))
    row('a superline wave starts, Polymarket is already in that wave', okY & (Wf[:, 9] == 0) & (Wf[:, 8] == 1))
    # ---------------- 3: high vs low waves
    say('')
    hi_w = TGT[:, 0] >= 6
    say(f'3. HIGH vs LOW WAVES. High = within 5 min the token could be sold 6c+ above the price paid: {hi_w[okY].mean() * 100:.0f}% of wave starts. '
        f'Knowing them in advance: high ${Y[okY & hi_w, 1].mean():+.2f} / low ${Y[okY & ~hi_w, 1].mean():+.2f} per $100 sold after 5 min; best single exit for the high ones: ' +
        ', '.join(f'{LABN[j]} ${Y[okY & hi_w, j].mean():+.2f}' for j in (3, 4, 5, 6)))
    first = d_ms(test_days[0])
    rng = np.random.default_rng(0)
    b0 = np.flatnonzero(T < first)
    samp = rng.choice(b0, min(200_000, len(b0)), replace=False)
    Xb = np.zeros(FE.shape, np.uint8)
    for j in range(FE.shape[1]):
        e = np.unique(np.quantile(FE[samp, j], np.linspace(0, 1, 65)[1:-1]))
        Xb[:, j] = np.searchsorted(e, FE[:, j], side='right')
    Xg = torch.from_numpy(Xb).to(dev)
    YT = np.concatenate([np.clip(np.nan_to_num(Y), -40, 40), np.nan_to_num(hi_w.astype(np.float32))[:, None] * 40], 1).astype(np.float32)     # last column: "high wave"
    Yc = torch.from_numpy(YT).to(dev)
    tg = lambda v: torch.from_numpy(v).to(dev)
    JH = len(LAB)

    def fit(idx, j):
        idx = idx[np.argsort(T[idx], kind='stable')]
        cut_ = int(0.8 * len(idx))
        va, tr = idx[cut_:], idx[:cut_]
        tr = tr[T[tr] + 2_700_000 < T[va].min()]      # labels look 30 min (+ up to 10) ahead: 45-min gap
        m = MN.Trees(depth=5, n_trees=300, min_leaf=100, lam=50.0, row_frac=0.5).fit(Xg[tg(tr)], Yc[tg(tr), j], Xg[tg(va)], Yc[tg(va), j])
        return m, m.predict(Xg[tg(va)]).cpu().numpy()
    models = {'dollars after 5 min': 1, 'dollars after 10 min': 2, 'dollars after 30 min': 8, '"high wave"': JH}
    picks = {(mn, q): [] for mn in models for q in (0.9, 0.97)}
    sport_picks = {s: [] for s in SPORTS}
    aucs, prec, lines = [], np.zeros((2, 2)), []
    for dday in test_days:
        d0 = d_ms(dday)
        tr = np.flatnonzero(okY & (T + 2_700_000 < d0))
        today = np.flatnonzero(okY & (T >= d0) & (T < d0 + DAY))
        if len(tr) < 10_000 or len(today) < 500:
            continue
        for mn, j in models.items():
            m, pv = fit(tr, j)
            pt = m.predict(Xg[tg(today)]).cpu().numpy()
            for q in (0.9, 0.97):
                picks[(mn, q)] += list(MN.place(T, G, today[pt >= np.quantile(pv, q)], hold=1_801_000 if j == 8 else 601_000))
            if j == JH:
                aucs.append((auc(pt, hi_w[today]), len(today)))
                for b, q in enumerate((0.9, 0.97)):
                    sel = pt >= np.quantile(pv, q)
                    prec[b] += (hi_w[today][sel].sum(), sel.sum())
        for s in SPORTS:
            trs, tds = tr[SP[tr] == s], today[SP[today] == s]
            if len(trs) >= 4000 and len(tds) >= 50:
                m, pv = fit(trs, 8)
                sport_picks[s] += list(MN.place(T, G, tds[m.predict(Xg[tg(tds)]).cpu().numpy() >= np.quantile(pv, 0.9)], hold=1_801_000))
        lines.append(f'{dday[8:]} Nov {len(today)} wave starts')
    say('   ' + '; '.join(lines))
    if aucs:
        say(f'   can it tell a high wave at its start? AUC {np.average([x[0] for x in aucs], weights=[x[1] for x in aucs]):.2f} (0.5 = guessing); of the waves it calls high, top 10%: '
            f'{prec[0, 0] / max(prec[0, 1], 1) * 100:.0f}% were high, top 3%: {prec[1, 0] / max(prec[1, 1], 1) * 100:.0f}% (all waves: {hi_w[okY].mean() * 100:.0f}%)')
    say('   picks (threshold from earlier days, one position a game), every exit on the same picks: $ per $100 / share won')
    for (mn, q), pk in picks.items():
        pk = np.array(pk, np.int64)
        if len(pk):
            say(f'      model for {mn}, top {round((1 - q) * 100)}%: {len(pk)} trades ({len(pk) / len(lines):.0f} a day) | ' + ' | '.join(f'{LABN[j]} ${Y[pk, j].mean():+.2f} / {np.mean(Y[pk, j] > 0) * 100:.0f}%' for j in range(len(LAB))))
    main_pk = np.array(picks[('dollars after 30 min', 0.97)], np.int64)
    if len(main_pk):
        for j in (8, 7, 2):
            say(MN.fmt(f'   model for dollars after 30 min, top 3%, {LABN[j]}', MN.gstats(Y[main_pk, j], G[main_pk])))
        dd_ = np.array([dt.datetime.fromtimestamp(T[i] / 1000, dt.timezone.utc).strftime('%d') for i in main_pk])
        say('      by day (after 30 min): ' + '; '.join(f'{x} Nov {np.sum(dd_ == x)} ${Y[main_pk[dd_ == x], 8].mean():+.2f}' for x in sorted(set(dd_))))
        say('      by sport (after 30 min): ' + '; '.join(f'{s} {np.sum(SP[main_pk] == s)} ${Y[main_pk[SP[main_pk] == s], 8].mean():+.2f} (win {np.mean(Y[main_pk[SP[main_pk] == s], 8] > 0) * 100:.0f}%)'
                                                        for s in SPORTS if np.sum(SP[main_pk] == s) >= 15))
        y8, buy_, s30, late, age = Y[main_pk, 8], TGT[main_pk, 4], TGT[main_pk, 5], TGT[main_pk, 6], TGT[main_pk, 7]
        dec = (s30 >= 0.97) | (s30 <= 0.03)
        say(f'      CHECK game decided at the sale (token at 97c+ or 3c-): {dec.mean() * 100:.0f}% of trades, they make ${y8[dec].mean() if dec.any() else 0:+.2f} per $100; all the others:')
        say(MN.fmt('         not decided at the sale', MN.gstats(y8[~dec], G[main_pk][~dec])))
        say(f'      CHECK price paid: quartiles {np.nanquantile(buy_, 0.25) * 100:.0f}c / {np.nanquantile(buy_, 0.5) * 100:.0f}c / {np.nanquantile(buy_, 0.75) * 100:.0f}c; ' + '; '.join(
            f'{int(lo * 100)}-{int(hi * 100)}c: {int(((buy_ >= lo) & (buy_ < hi)).sum())} ${y8[(buy_ >= lo) & (buy_ < hi)].mean():+.2f} (win {np.mean(y8[(buy_ >= lo) & (buy_ < hi)] > 0) * 100:.0f}%)' for lo, hi in ((0, 0.1), (0.1, 0.3), (0.3, 0.7), (0.7, 1.01)) if ((buy_ >= lo) & (buy_ < hi)).sum() >= 10))
        ont = late <= 500
        say(f'      CHECK Polymarket feed at the entry: stamped under 0.5 s late {ont.sum()} trades ${y8[ont].mean():+.2f}; later than that {(~ont).sum()} ${y8[~ont].mean() if (~ont).any() else 0:+.2f}; '
            f'last quote under 10 s old {(age <= 10).sum()} ${y8[age <= 10].mean():+.2f}; older {(age > 10).sum()} ${y8[age > 10].mean() if (age > 10).any() else 0:+.2f}')
        say('      no model, every wave start held 30 min: ' + '; '.join(f'{s} ${Y[okY & (SP == s), 8].mean():+.2f}' for s in SPORTS))
        np.savez(os.path.join(a.signals, 'waves2_picks.npz'), game=G[main_pk], t=T[main_pk], d=d[main_pk], outcome=D['outcome'][main_pk], buy=TGT[main_pk, 4], usd=Y[main_pk, 8], sport=SP[main_pk])
        v = np.sort(Y[main_pk, 8])[::-1]
        say(f'      median trade ${np.median(v):+.2f}; the 20 best trades make ${v[:20].sum():+,.0f} of ${v.sum():+,.0f}; which line\'s wave: superline {np.mean(Wf[main_pk, 9] == 0) * 100:.0f}%, Polymarket {np.mean(Wf[main_pk, 9] == 1) * 100:.0f}%')
    say('   one model per sport (trained on that sport\'s waves only; top 10%, one position a game, sold after 30 min / 10 min):')
    for s in SPORTS:
        pk = np.array(sport_picks[s], np.int64)
        ng = len(np.unique(G[SP == s]))
        if len(pk):
            st = MN.gstats(Y[pk, 8], G[pk])
            say(f'      {s:<11} {ng} games, {np.sum(SP == s) / ndays:.0f} wave starts a day -> {st["n"]} trades ({st["n"] / len(lines):.0f} a day) ${st["tot"]:+,.0f} (90% {st["lo"]:+,.0f}..{st["hi"]:+,.0f}), '
                f'${st["mean"]:+.2f} per $100, win {st["win"]:.0f}%; sold after 10 min ${Y[pk, 2].mean():+.2f}, win {np.mean(Y[pk, 2] > 0) * 100:.0f}%')
        else:
            say(f'      {s:<11} {ng} games, {np.sum(SP == s) / ndays:.0f} wave starts a day -> too few to train or trade')
    with open(os.path.join(a.signals, 'waves2.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'test'])
    ap.add_argument('--db')
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=22)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--since', type=int, default=0, help='only games that kicked off at or after this time (ms)')
    ap.add_argument('--sports', default='', help='comma list, e.g. tennis,cs2')
    ap.add_argument('--name', default='waves2.npz')
    ap.add_argument('--grid', type=int, default=0, help='control: one entry every N seconds of play instead of wave starts')
    a = ap.parse_args()
    (cmd_build if a.cmd == 'build' else cmd_test)(a)


if __name__ == '__main__':
    main()
