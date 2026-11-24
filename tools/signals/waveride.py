"""Riding a wave that has already started (owner, 24 Nov: "if a wave lasts for 15 seconds, and we detect it 1 second in,
there's still 14 seconds").

  python tools/signals/waveride.py build --db X --signals DIR [--workers 22] [--limit N]
  python tools/signals/waveride.py test  --signals DIR

Once a wave has begun its direction is known, so nothing has to be predicted. What is left to take?
WAVES (hindsight: we know it became a wave). For every Polymarket wave in play with a live recording, the wave's side
is bought at eight moments: 1 s BEFORE the first move, in the second of the first move, 1 / 2 / 3 / 5 / 10 s after it,
and 1 s after the wave is DETECTED (the CUSUM alarm: the first moment a bot knows it is a wave). Per moment: the price
paid, the spread, how much of the wave's whole move (mid before it -> mid at its top) the mid had already made, and
dollars on a $100 order (fees, depth both sides, live bids only) sold after 30 s / 1 / 2 / 5 min, when the wave ends,
and at its very top (not knowable).
FIRST TICKS (what a bot can really do 1 s in, when it cannot know yet whether it is a wave): every second Polymarket's
mid moves after being at rest (no move for 10 s, both CUSUMs at 0). Buy that way 1 s later. How many of those ticks
become a wave / a big wave, and what the trade makes, by the size of the tick.
SENSITIVITY (the same idea with a dial): a wave is "detected" when the CUSUM reaches 0.12 log-odds. Lower levels fire
earlier, when less of the move is made, but more of them are false starts. For every move that starts from rest: the
first second the CUSUM reaches 25 / 50 / 75 / 100% of that level, buy 1 s later.
A MODEL AT THE FIRST TICK: boosted trees on everything known in the second of the tick (the 99 inputs, the lead record,
the lab's and the direction study's inputs, all seen from the tick's side, plus the tick itself) for "this tick becomes
a BIG wave" and for the dollars of buying it; trained on the days before the day it is scored on (30-min gap).
A taker cannot be faster than 1 s: Polymarket holds marketable sports orders in a 1 s delay window (AGENTS.md 8h)."""
import argparse
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
import waves as W  # noqa: E402
import wavedir as WD  # noqa: E402
from booklead import held  # noqa: E402

C = MN.C
BIG = WD.BIG
ENT = ['1 s before the first move', 'in the second of the first move', '1 s after the first move', '2 s after', '3 s after', '5 s after', '10 s after', '1 s after it is DETECTED as a wave']
EOFF = (0, 1, 2, 3, 4, 6, 11)                    # buy index minus the wave's onset index (the first move is at onset + 1)
EXN = ['30 s', '1 min', '2 min', '5 min', 'wave ends', 'the top']
NE, NX = len(ENT), len(EXN)
WCOLS = 12
THS = (0.25, 0.5, 0.75, 1.0)                     # share of the alarm level (0.12 log-odds) the CUSUM has reached
TEX = ['30 s', '1 min', '2 min', '5 min', 'wave ends', '+5c / 5 min']
FNT = MN.NAMES + MN.HN + LF.QN + WD.DN + ['tick_logodds', 'tick_cents', 'spread_before', 'spread_after', 'price_before']
DAY = 86_400_000


def one(args):
    path, m, sport, start = args
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
    all_ts = np.array([r[0] for r in pmr], np.int64)
    wv, tk, sw, tkf = [], [], [], []
    for k, o in enumerate(['home', 'away'] if sport in W.THREE_WAY else ['home']):
        g = F.build(pm, bk, o, sport, start, sport in W.THREE_WAY, book, full=True)
        if g is None:
            continue
        ts, X = np.asarray(g['ts'], np.int64), g['X']
        n = len(ts)
        live = X[:, C['inplay']] > 0.5
        if live.sum() < 1200 or abs((ts[1] - ts[0]) / 1000.0 - 1.0) > 1e-6:
            continue
        key = m * 4 + k
        pr_ = [(q[0], q[3], q[4]) for q in pmr if q[2] == o]
        pr4 = [(q[0], q[3], q[4], q[5]) for q in pmr if q[2] == o]
        srv = np.array([q[0] if q[1] is None else q[1] for q in pmr if q[2] == o], np.int64)
        pt_x = np.array([q[0] for q in pr_], np.int64)
        pb_x = np.array([np.nan if q[1] is None else q[1] for q in pr_], float)
        pa_x = np.array([np.nan if q[2] is None else q[2] for q in pr_], float)
        ts_x = np.concatenate([ts, ts[-1] + 1000 * np.arange(1, 2401)])
        bid_x, ask_x = held(pt_x, pb_x, ts_x), held(pt_x, pa_x, ts_x)
        decided = (not np.isfinite(pb_x[-1])) or pb_x[-1] >= 0.97 or (np.isfinite(pa_x[-1]) and pa_x[-1] <= 0.03)
        n_lab = len(ts_x) if decided else min(len(ts_x), int(np.searchsorted(ts_x, pt_x[-1], side='right')))
        ja = np.searchsorted(all_ts, ts_x, side='right') - 1
        age = np.where(ja >= 0, (ts_x - all_ts[np.clip(ja, 0, None)]) / 1000.0, 1e6)[:n_lab]
        fresh = (age <= 30) | (decided & (ts_x[:n_lab] > pt_x[-1]))
        kq = np.clip(np.searchsorted(pt_x, ts, side='right') - 1, 0, None)
        late = (pt_x[kq] - srv[kq]).astype(np.float32)
        sz = [np.full(n_lab, np.nan)] * 4
        bo = [r for r in (book or []) if r[1] == o]
        if bo:
            bt = np.array([r[0] for r in bo], np.int64)
            sz = [held(bt, np.array([np.nan if r[2 + j] is None else r[2 + j] for r in bo], float), ts_x)[:n_lab] for j in range(4)]
        bid, ask = bid_x[:n_lab], ask_x[:n_lab]
        mid = (bid + ask) / 2
        spr = (ask - bid) * 100
        rest = (X[:, C['pm_U']] == 0) & (X[:, C['pm_D']] == 0) & (X[:, C['pm_r10']] == 0)
        ok_live = (age[:n] <= 15) & (late <= 1000)
        cus = {1: np.concatenate([X[:, C['pm_U']], np.zeros(max(n_lab - n, 0), np.float32)])[:n_lab], -1: np.concatenate([X[:, C['pm_D']], np.zeros(max(n_lab - n, 0), np.float32)])[:n_lab]}
        # ---------------- waves, bought at eight moments
        first_of = {}
        for e in g['pm_eps']:
            on, al, pk, en, d = e.get('on'), e['alarm'], e['peak'], e['end'], e['dir']
            if on is None:
                continue
            first_of[(on + 1, d)] = abs(e['amp']) if np.isfinite(e['amp']) else 0.0
            B = np.array([on + o_ for o_ in EOFF] + [al + 1], np.int64)
            if on < 15 or not live[al] or B.max() + 302 >= n_lab or not (ok_live[on] and ok_live[min(al, n - 1)]):
                continue
            tm = mid if d > 0 else 1 - mid
            total = tm[pk] - tm[on]
            usd, e_, tb, buy = WD.trader(bid, ask, sz, B - 1, d, fresh)
            ys = np.full((NE, NX), np.nan, np.float32)
            for j, h_ in enumerate((30, 60, 120, 300)):
                ys[:, j] = usd(e_ + h_)
            ys[:, 4] = usd(np.clip(en + 1, e_ + 1, e_ + 300))
            ys[:, 5] = np.where(pk > e_, usd(np.clip(pk, e_ + 1, e_ + 300)), np.nan)
            done = (tm[B] - tm[on]) / total if (np.isfinite(total) and total > 0) else np.full(NE, np.nan)
            st_ = np.nan_to_num(np.diff(tm[on:pk + 1]))
            jb = int(np.argmax(st_)) if len(st_) else 0
            wv.append(np.concatenate([[key, ts[on + 1], d, abs(e['amp']) if np.isfinite(e['amp']) else 0.0, tm[on], float(rest[on]), al - on - 1, pk - on - 1, en - on - 1, total * 100,
                                       (st_[jb] / total) if (len(st_) and np.isfinite(total) and total > 0) else np.nan, jb],
                                      buy, spr[B], done, ys.ravel()]).astype(np.float64))
        # ---------------- first ticks from rest (what can be done live)
        s_ = np.arange(1, n)
        gp = np.asarray(g['mid'], float)[:n]                                       # the line the CUSUM runs on (nan while the spread is over 6c)
        with np.errstate(invalid='ignore', divide='ignore'):
            x = np.log(gp / (1 - gp))
            mv = gp[s_] - gp[s_ - 1]
        cand = s_[rest[s_ - 1] & live[s_] & g['ok'][s_] & ok_live[s_] & np.isfinite(mv) & (mv != 0) & (s_ + 303 < n_lab) & (s_ > 15)]
        if len(cand):
            Hh = MN.lead_record(X, live, 1.0)
            Q = LF.extra(X, bid, ask, sz, g['pm_eps'], g['sl_eps'])
            p1, p2, pr5 = WD.wave_hist(g['pm_eps'], n)
            s1, _, sr5 = WD.wave_hist(g['sl_eps'], n)
            Dx = np.concatenate([WD.flow(pr4, ts, mid[:n]), np.stack([p1, p2, pr5, s1, sr5], 1).astype(np.float32)], 1)
            for d in (1, -1):
                r = cand[np.sign(gp[cand] - gp[cand - 1]) == d]
                if not len(r):
                    continue
                usd, e_, tb, buy = WD.trader(bid, ask, sz, r, d, fresh)
                ys = np.full((len(r), 6), np.nan, np.float32)
                for j, h_ in enumerate((30, 60, 120, 300)):
                    ys[:, j] = usd(e_ + h_)
                flat = np.lib.stride_tricks.sliding_window_view(cus[d] == 0, 300)[e_ + 1][:, 29:]
                ys[:, 4] = usd(e_ + 30 + np.where(flat.any(1), flat.argmax(1), 270))
                win = np.lib.stride_tricks.sliding_window_view(tb, 300)[e_ + 1]
                hit = win >= (buy + 0.05)[:, None] - 1e-9
                ys[:, 5] = usd(e_ + 1 + np.where(hit.any(1), hit.argmax(1), 299))
                amp_ = np.array([first_of.get((int(v), d), -1.0) for v in r])
                tm = mid if d > 0 else 1 - mid
                tk.append(np.column_stack([np.full(len(r), key), ts[r], np.full(len(r), d), np.abs(x[r] - x[r - 1]), np.abs(gp[r] - gp[r - 1]) * 100, amp_, tm[r - 1], spr[r - 1], spr[r], buy, ys]).astype(np.float64))
                Xr, Qr, Dr = X[r], Q[r], Dx[r]
                if d < 0:
                    Xr, Qr, Dr = MN.mirror(Xr), LF.mirror(Qr), WD.mirror_d(Dr)
                tkf.append(np.nan_to_num(np.concatenate([Xr, Hh[r], Qr, Dr, np.column_stack([np.abs(x[r] - x[r - 1]), np.abs(gp[r] - gp[r - 1]) * 100, spr[r - 1], spr[r], tm[r - 1]])], 1)).astype(np.float32))
        # ---------------- sensitivity: the first second the CUSUM reaches a share of the alarm level, moves that start from rest
        for d in (1, -1):
            u = np.asarray(X[:, C['pm_U']] if d > 0 else X[:, C['pm_D']], float)
            st = np.flatnonzero((u[1:] > 0) & (u[:-1] == 0)) + 1
            st = st[(st > 15) & rest[st - 1] & live[st] & ok_live[st]]
            if not len(st):
                continue
            zero = np.flatnonzero(u == 0)
            tm = mid if d > 0 else 1 - mid
            rows, meta = [], []
            for s0 in st:
                kz = int(np.searchsorted(zero, s0, side='right'))
                t_end = int(zero[kz]) if kz < len(zero) else n
                seg = u[s0:t_end]
                mx = float(seg.max())
                amp_ = first_of.get((int(s0), d), -1.0)
                for ti, th in enumerate(THS):
                    hit = np.flatnonzero(seg >= th)
                    if not len(hit):
                        break
                    j = int(s0 + hit[0])
                    if j + 303 >= n_lab or not g['ok'][j] or not ok_live[j]:
                        continue
                    rows.append(j)
                    meta.append((key, ts[j], d, ti, j - s0, mx, amp_, (tm[j] - tm[s0 - 1]) * 100, tm[s0 - 1], spr[j]))
            if rows:
                rows = np.array(rows, np.int64)
                usd, e_, tb, buy = WD.trader(bid, ask, sz, rows, d, fresh)
                ys = np.full((len(rows), 6), np.nan, np.float32)
                for j, h_ in enumerate((30, 60, 120, 300)):
                    ys[:, j] = usd(e_ + h_)
                flat = np.lib.stride_tricks.sliding_window_view(cus[d] == 0, 300)[e_ + 1][:, 29:]
                ys[:, 4] = usd(e_ + 30 + np.where(flat.any(1), flat.argmax(1), 270))
                win = np.lib.stride_tricks.sliding_window_view(tb, 300)[e_ + 1]
                hit = win >= (buy + 0.05)[:, None] - 1e-9
                ys[:, 5] = usd(e_ + 1 + np.where(hit.any(1), hit.argmax(1), 299))
                sw.append(np.column_stack([np.array(meta, float), buy, ys]))
    return sport, wv, tk, sw, tkf


def cmd_build(a):
    gl = W.games(W.connect(a.db))
    if a.limit:
        gl = gl[::max(1, len(gl) // a.limit)][:a.limit]
    print(f'{len(gl)} games', flush=True)
    wv, tk, ws, tks, sw, sws, tkf, t0 = [], [], [], [], [], [], [], time.time()
    with Pool(a.workers) as pool:
        for k, (sp, w_, t_, s_, f_) in enumerate(pool.imap_unordered(one, [(a.db, m, sp, st) for m, sp, _, _, st in gl], chunksize=2)):
            wv += w_
            ws += [sp] * len(w_)
            for part in t_:
                tk.append(part)
                tks += [sp] * len(part)
            for part in s_:
                sw.append(part)
                sws += [sp] * len(part)
            tkf += f_
            if (k + 1) % 500 == 0:
                print(f'  {k + 1} of {len(gl)} games, {len(wv)} waves, {sum(len(p) for p in tk)} first ticks, {time.time() - t0:.0f} s', flush=True)
    np.savez(os.path.join(a.signals, 'waveride.npz'), WV=np.array(wv), WS=np.array(ws), TK=np.concatenate(tk), TS=np.array(tks), SW=np.concatenate(sw), SS=np.array(sws), TKF=np.concatenate(tkf))
    print(f'{len(wv)} waves, {sum(len(p) for p in tk)} first ticks from rest, {time.time() - t0:.0f} s')


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    Z = np.load(os.path.join(a.signals, 'waveride.npz'))
    WV, WS, TK, TS = Z['WV'], Z['WS'], Z['TK'], Z['TS']
    G, d, amp, p0, rest, t_al, t_pk, t_en, tot = WV[:, 0] // 4, WV[:, 2], WV[:, 3], WV[:, 4], WV[:, 5] > 0.5, WV[:, 6], WV[:, 7], WV[:, 8], WV[:, 9]
    jshare, jwhen = WV[:, 10], WV[:, 11]
    o = WCOLS
    buy, spr, done = WV[:, o:o + NE], WV[:, o + NE:o + 2 * NE], WV[:, o + 2 * NE:o + 3 * NE]
    Y = WV[:, o + 3 * NE:].reshape(-1, NE, NX)
    big = amp >= BIG
    q3 = lambda v: '/'.join(f'{x:.0f}' for x in np.nanquantile(v, [0.25, 0.5, 0.75]))
    say(f'RIDING A WAVE THAT HAS ALREADY STARTED: {len(WV):,} Polymarket waves in play with a live recording, {len(np.unique(G)):,} games, 17-23 Nov; {rest.mean() * 100:.0f}% start from rest, {big.mean() * 100:.0f}% are big ({BIG}+ log-odds).')
    say('')
    say('1. HOW LONG A WAVE LASTS (seconds from its first move; quartiles 25% / median / 75%) and how big it is')
    for name, mm in (('all waves', np.ones(len(WV), bool)), ('waves that start from rest', rest), (f'big waves ({BIG}+)', big), ('big waves from rest', big & rest)):
        say(f'   {name:<28}{mm.sum():>8,}  to detection {q3(t_al[mm])} s | to its top {q3(t_pk[mm])} s | to its end {q3(t_en[mm])} s | whole move {np.nanmedian(tot[mm]):.1f}c (median), price before {np.nanmedian(p0[mm]) * 100:.0f}c')
        say(f'   {"":<36}its biggest single second carries {np.nanmedian(jshare[mm]) * 100:.0f}% of the whole move (median; {np.nanmean(jshare[mm] >= 0.5) * 100:.0f}% of waves have half or more in one second); '
            f'that second comes {q3(jwhen[mm])} s after the first move; it IS the first move in {np.mean(jwhen[mm] == 0) * 100:.0f}% of waves')
    say('')
    say('2. HOW MUCH OF THE MOVE IS ALREADY MADE when we could buy (share of the whole move, mid before the wave -> mid at its top; median / mean) and the spread then')
    for name, mm in (('all waves', np.ones(len(WV), bool)), ('waves from rest', rest), ('big waves from rest', big & rest)):
        say(f'   {name}:')
        for j in range(NE):
            say(f'      bought {ENT[j]:<40} move made {np.nanmedian(done[mm, j]) * 100:4.0f}% / {np.nanmean(np.clip(done[mm, j], -1, 2)) * 100:4.0f}%   spread {np.nanmedian(spr[mm, j]):.1f}c (mean {np.nanmean(spr[mm, j]):.1f}c)   '
                f'price paid above the mid before the wave {np.nanmedian((buy[mm, j] - p0[mm]) * 100):+.1f}c of {np.nanmedian(tot[mm]):.1f}c')
    say('')
    say('3. WHAT THE WAVE\'S SIDE MAKES, bought at each moment ($ per $100, fees + depth; mean over waves). The top is not knowable; blank = the top had passed.')
    for name, mm in (('ALL WAVES (direction known, size not)', np.ones(len(WV), bool)), ('WAVES FROM REST', rest), ('BIG WAVES, hindsight (we cannot know it will be big)', big), ('BIG WAVES FROM REST, hindsight', big & rest)):
        say(f'   {name}: {mm.sum():,} waves')
        say('      ' + ' ' * 42 + ' | '.join(f'{x:>9}' for x in EXN) + ' |  top still ahead')
        for j in range(NE):
            okj = np.isfinite(Y[mm, j, :5]).all(1)
            say(f'      {ENT[j]:<42}' + ' | '.join(f'{np.nanmean(Y[mm, j, e_][okj]):>+9.2f}' for e_ in range(5)) + f' | {np.nanmean(Y[mm, j, 5]):>+9.2f} | {np.mean(np.isfinite(Y[mm, j, 5])) * 100:5.0f}%')
    say('   the one rule here a bot can run as it is (every wave, bought 1 s after it is detected), one position a game:')
    j = NE - 1
    okj = np.isfinite(Y[:, j, :5]).all(1)
    idx = MN.place(WV[:, 1].astype(np.int64), G.astype(np.int64), np.flatnonzero(okj), hold=301_000)
    for e_ in (1, 2, 3, 4):
        say(MN.fmt(f'   every wave at detection, sold {EXN[e_]}', MN.gstats(Y[idx, j, e_], G[idx])))
    idx = MN.place(WV[:, 1].astype(np.int64), G.astype(np.int64), np.flatnonzero(okj & rest), hold=301_000)
    for e_ in (2, 3):
        say(MN.fmt(f'   waves from rest at detection, sold {EXN[e_]}', MN.gstats(Y[idx, j, e_], G[idx])))
    say('')
    # ---------------- first ticks
    tg, tt, td, tl, tc, ta, tp, s0, s1, tb_ = TK[:, 0] // 4, TK[:, 1].astype(np.int64), TK[:, 2], TK[:, 3], TK[:, 4], TK[:, 5], TK[:, 6], TK[:, 7], TK[:, 8], TK[:, 9]
    TY = TK[:, 10:]
    okt = np.isfinite(TY).all(1)
    wave, bigw = ta >= 0, ta >= BIG
    say(f'4. WHAT A BOT CAN REALLY DO 1 s IN: every time Polymarket\'s mid moves after being at rest ({len(TK):,} first ticks, {len(np.unique(tg)):,} games), buy that way 1 s later.')
    say(f'   {wave.mean() * 100:.0f}% of those ticks are the first move of a wave, {bigw.mean() * 100:.1f}% of a big wave. Median tick {np.median(tc):.1f}c; spread before {np.nanmedian(s0):.1f}c, after {np.nanmedian(s1):.1f}c.')
    say('      ' + ' ' * 50 + ' | '.join(f'{x:>11}' for x in TEX) + ' |  became a wave / a big wave')
    rowt = lambda name, mm: say(f'      {name:<50}' + ' | '.join(f'{np.nanmean(TY[mm & okt, e_]):>+11.2f}' for e_ in range(6)) + f' | {wave[mm].mean() * 100:4.0f}% / {bigw[mm].mean() * 100:4.1f}%   ({mm.sum():,})') if mm.sum() >= 50 else None
    allm = np.ones(len(TK), bool)
    rowt('every first tick', allm)
    for name, mm in (('tick under 0.03 log-odds (under ~1 pt at 50c)', tl < 0.03), ('tick 0.03-0.06', (tl >= 0.03) & (tl < 0.06)), ('tick 0.06-0.12', (tl >= 0.06) & (tl < 0.12)), ('tick 0.12-0.30 (a wave at once)', (tl >= 0.12) & (tl < 0.30)), ('tick 0.30+ (a jump)', tl >= 0.30)):
        rowt(name, mm)
    for name, mm in (('price before 20-80c', (tp >= 0.2) & (tp <= 0.8)), ('spread after the tick 1c or less', s1 <= 1.01), ('spread after the tick 2c+', s1 >= 1.99)):
        rowt(name, mm)
    for sp in ('football', 'tennis', 'cs2', 'basketball', 'dota2', 'hockey'):
        rowt(sp, TS == sp)
    say('   and if we somehow knew (hindsight) what the tick would become:')
    rowt('ticks that became a wave', wave)
    rowt('ticks that became a BIG wave', bigw)
    rowt('ticks that did not become a wave', ~wave)
    say('   as trades, one position a game:')
    for name, mm in (('every first tick from rest', allm), ('first tick 0.06+ log-odds', tl >= 0.06), ('first tick 0.12+ (a wave at once)', tl >= 0.12)):
        idx = MN.place(tt, tg.astype(np.int64), np.flatnonzero(mm & okt), hold=301_000)
        for e_ in (2, 3):
            say(MN.fmt(f'   {name}, sold {TEX[e_]}', MN.gstats(TY[idx, e_], tg[idx])))
    say('')
    SW, SS = Z['SW'], Z['SS']
    sg, st_, sth, sdt, smx, samp, smade, sp0, sspr = SW[:, 0] // 4, SW[:, 1].astype(np.int64), SW[:, 3].astype(int), SW[:, 4], SW[:, 5], SW[:, 6], SW[:, 7], SW[:, 8], SW[:, 9]
    SY = SW[:, 11:]
    oks = np.isfinite(SY).all(1)
    say(f'5. THE DIAL: how early to call it a wave. Moves that start from rest; buy 1 s after the CUSUM first reaches a share of the alarm level (0.12 log-odds). $ per $100.')
    say('      ' + ' ' * 34 + ' | '.join(f'{x:>11}' for x in TEX) + ' |  calls   s after 1st move   move made so far   became a wave / big   spread')
    for ti, th in enumerate(THS):
        mm = sth == ti
        if mm.sum() < 50:
            continue
        say(f'      reaches {th * 100:3.0f}% ({th * 0.12:.2f} log-odds)       ' + ' | '.join(f'{np.nanmean(SY[mm & oks, e_]):>+11.2f}' for e_ in range(6)) +
            f' | {mm.sum():>6,}   {q3(sdt[mm]):>12}   {np.nanmedian(smade[mm]):>8.1f}c          {np.mean(smx[mm] >= 1) * 100:4.0f}% / {np.mean(samp[mm] >= BIG) * 100:4.1f}%      {np.nanmedian(sspr[mm]):.1f}c')
    say('   the same calls split by what they became (hindsight), sold after 5 min: ' + '; '.join(
        f'{THS[ti] * 100:.0f}%: big wave {np.nanmean(SY[(sth == ti) & oks & (samp >= BIG), 3]):+.2f}, small wave {np.nanmean(SY[(sth == ti) & oks & (smx >= 1) & (samp < BIG), 3]):+.2f}, no wave {np.nanmean(SY[(sth == ti) & oks & (smx < 1), 3]) if ((sth == ti) & oks & (smx < 1)).any() else float("nan"):+.2f}'
        for ti in range(len(THS))))
    say('   as trades, one position a game:')
    for ti, th in enumerate(THS):
        idx = MN.place(st_, sg.astype(np.int64), np.flatnonzero((sth == ti) & oks), hold=301_000)
        for e_ in (2, 3):
            say(MN.fmt(f'   CUSUM at {th * 100:.0f}% of the alarm level, sold {TEX[e_]}', MN.gstats(SY[idx, e_], sg[idx])))
    for sp in ('football', 'tennis', 'cs2', 'basketball'):
        idx = MN.place(st_, sg.astype(np.int64), np.flatnonzero((sth == 1) & oks & (SS == sp)), hold=301_000)
        if len(idx) >= 30:
            say(MN.fmt(f'   {sp}: CUSUM at 50%, sold 5 min', MN.gstats(SY[idx, 3], sg[idx])))
    with open(os.path.join(a.signals, 'waveride.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    # ---------------- 6: a model at the first tick
    import datetime as dt
    import torch
    import lab as LB
    say('')
    dev = MN.pick_device()
    tg_ = lambda v: torch.from_numpy(np.asarray(v)).to(dev)
    d_ms = lambda s_: int(dt.datetime.fromisoformat(s_).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    TKF = Z['TKF']
    days = sorted({dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in tt})
    Xb = tg_(LB.binned(TKF, np.flatnonzero(tt < d_ms(days[2]))))
    ybig, ywave = bigw.astype(np.float32), wave.astype(np.float32)
    yus = np.clip(np.nan_to_num(TY), -40, 40).astype(np.float32)
    PB, PU = np.full(len(TK), np.nan), np.full(len(TK), np.nan)
    thr = {}
    imp = np.zeros(TKF.shape[1])
    for dday in days[1:]:
        d0 = d_ms(dday)
        tr_all = np.flatnonzero(okt & (tt + 1_800_000 < d0))
        today = np.flatnonzero(okt & (tt >= d0) & (tt < d0 + DAY))
        if len(tr_all) < 4000 or not len(today):
            continue
        tr_all = tr_all[np.argsort(tt[tr_all], kind='stable')]
        cut = int(0.8 * len(tr_all))
        va, tr = tr_all[cut:], tr_all[:cut]
        tr = tr[tt[tr] + 1_800_000 < tt[va].min()]
        mb = LB.GB(min_leaf=50, row_frac=0.7, lam=20.0).fit(Xb[tg_(tr)], tg_(ybig)[tg_(tr)], Xb[tg_(va)], tg_(ybig)[tg_(va)])
        mu = LB.GB(loss='l2', min_leaf=100, lam=50.0, row_frac=0.7).fit(Xb[tg_(tr)], tg_(yus[:, 3])[tg_(tr)], Xb[tg_(va)], tg_(yus[:, 3])[tg_(va)])
        PB[today], PU[today] = mb.predict(Xb[tg_(today)]), mu.predict(Xb[tg_(today)])
        pvb, pvu = mb.predict(Xb[tg_(va)]), mu.predict(Xb[tg_(va)])
        thr[dday] = {q: (np.quantile(pvb, q), np.quantile(pvu, q)) for q in (0.9, 0.97, 0.99)}
        for f_, _, _ in mb.trees:
            for lvl in f_:
                np.add.at(imp, lvl.cpu().numpy().ravel(), 1.0)
    sc_days = [x for x in days if x >= '2026-11-19' and x in thr]
    rowday = np.array([dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in tt])
    scored = np.isin(rowday, sc_days) & np.isfinite(PB)
    say(f'6. A MODEL AT THE FIRST TICK: will this tick become a BIG wave? ({scored.sum():,} first ticks on {", ".join(x[8:] for x in sc_days)} Nov, model trained on the days before each)')
    say(f'   AUC {LB.auc(PB[scored], bigw[scored]):.3f} (0.50 = no model). A big wave follows {bigw[scored].mean() * 100:.1f}% of first ticks. To break even on the 5-minute sale the picks need a big wave after about '
        f'{-np.nanmean(TY[scored & ~bigw, 3]) / (np.nanmean(TY[scored & bigw, 3]) - np.nanmean(TY[scored & ~bigw, 3])) * 100:.0f}% of them (big ${np.nanmean(TY[scored & bigw, 3]):+.2f}, not big ${np.nanmean(TY[scored & ~bigw, 3]):+.2f}).')
    for q in (0.9, 0.97, 0.99):
        for nm, P_, col in (('"big wave" model', PB, 0), ('dollar model', PU, 1)):
            sel = np.zeros(len(TK), bool)
            for x in sc_days:
                rr = np.flatnonzero(scored & (rowday == x))
                sel[rr[P_[rr] >= thr[x][q][col]]] = True
            if sel.sum() < 30:
                continue
            say(f'   {nm}, its top {round((1 - q) * 100)}%: {sel.sum():,} ticks; became a wave {wave[sel].mean() * 100:.0f}%, a BIG wave {bigw[sel].mean() * 100:.0f}%; every pick $ per $100: ' + ' | '.join(f'{TEX[e_]} {np.nanmean(TY[sel, e_]):+.2f}' for e_ in (1, 2, 3, 4)))
            idx = MN.place(tt, tg.astype(np.int64), np.flatnonzero(sel), hold=301_000)
            for e_ in (2, 3):
                say(MN.fmt(f'      one position a game, sold {TEX[e_]}', MN.gstats(TY[idx, e_], tg[idx])))
    say('   what the "big wave" model uses most (share of its splits): ' + ', '.join(f'{FNT[i]} {imp[i] / max(imp.sum(), 1) * 100:.0f}%' for i in np.argsort(-imp)[:12]))
    with open(os.path.join(a.signals, 'waveride.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'test'])
    ap.add_argument('--db')
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=22)
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    (cmd_build if a.cmd == 'build' else cmd_test)(a)


if __name__ == '__main__':
    main()
