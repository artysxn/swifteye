"""Three other ways to use the bookmaker <-> Polymarket lead (python tools/signals/angles.py --db X --signals DIR).

Every study so far bought on Polymarket after a bookmaker moved, in-play, and lost to the ~4-6c round trip. Here:

A. PRE-MATCH LAG. Before kick-off prices drift over minutes. On a 1-min grid (up to 8 h before kick-off) the bookmaker
   CONSENSUS (median over feed families of each family's margin-free probability) vs Polymarket's mid. Signal: the
   consensus moved >= 2 pts in 15 min with >= 2 families moving >= 0.5 pt that way, Polymarket moved < 30% of it, and
   Polymarket sits >= 1.5 pts short of the consensus. Buy the token 1 min later at the ask; sell into the bid when it
   reaches the consensus level (taker), or rest the sale there (maker, filled when the bid goes 1c past), else at
   60 min (never later than 2 min before kick-off); or hold to 2 min before kick-off.
B. FADE POLYMARKET'S SOLO MOVES (in-play). Polymarket's mid jumps >= 0.12 log-odds within 10 s and NO bookmaker family
   moves that way (>= 0.05 log-odds, as received) from 20 s before to 10 s after: buy the other side 11 s after the
   jump, target = Polymarket's mid before the jump (full reversal), else sell at 5 min. Also: the same jumps WITH a
   bookmaker confirming, followed (bought the jump's way) 11 s after.
C. STALE BOOKMAKERS. Polymarket's jump (as B, upward on an outcome) vs each family's odds 1 s and 3 s later (as we
   receive them): still at the pre-jump price? The value of betting there = p x odds - 1 with p = Polymarket's mid
   60 s after the jump. Off-Polymarket (a bookmaker bet), no acceptance delay or limits modelled.
All in dollars per $100 order with Polymarket book depth (maker.trade), fees 5% p(1-p) on taker fills. CLEAN tests for
A and B: per day 19-23 Nov, a filter chosen on earlier days by total minus one standard error, one position per game."""
import argparse
import datetime as dt
import itertools
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402
from booklead import fee, held  # noqa: E402
from maker import TICK, fin, trade  # noqa: E402

FAM = {'unibet': 'kambi', 'paf': 'kambi', 'leovegas': 'kambi', 'linebet': '1x', 'melbet': '1x', 'betwinner': '1x', 'megapari': '1x', 'paripesa': '1x',
       '22bet': '1x', '22bit': '1x', '1xbit': '1x', 'fonbet': 'fonbet', 'pari': 'fonbet', 'vinn': 'altenar', 'betking': 'altenar'}
THREE = {'football', 'handball'}
DAY = 86_400_000
logit = lambda p: np.log(np.clip(p, 0.005, 0.995) / (1 - np.clip(p, 0.005, 0.995)))


def tok_series(ser, o, d, times):
    """Token (Yes of o if d > 0, else No) bid / ask / sizes / depths at `times`."""
    lt, lb, la, bs, as_, bd, ad = ser[o]
    yb, ya, ybs, yas, ybd, yad = (held(lt, v, times) for v in (lb, la, bs, as_, bd, ad))
    if d > 0:
        return yb, ya, ybs, ybd, yas, yad
    return 1 - ya, 1 - yb, yas, yad, ybs, ybd


def run_trade(ser, o, d, te, target, horizon_s, maker):
    """Buy at the ask at te, sell into the bid at the target (or rest the sale there), else at te + horizon."""
    times = te + 1000 * np.arange(0, horizon_s + 601)
    tb, ta, tbs, tbd, tas, tad = tok_series(ser, o, d, times)
    if not np.isfinite(ta[0]) or not 0.01 < ta[0] < 0.99:
        return None
    buy, s_in, cap_in = float(ta[0]), fin(tas[0]), max(fin(tad[0]), fin(tas[0]))
    seg = np.nan_to_num(tb[1:horizon_s + 1], nan=-1.0)
    hit = np.flatnonzero(seg >= (target + TICK - 1e-9 if maker else target - 1e-9))
    if len(hit):
        k = 1 + int(hit[0])
        if maker:
            sell, taker_out, s_out = float(target), False, np.inf
        else:
            sell, taker_out, s_out = float(tb[k]), True, fin(tbs[k])
        cap_out = max(fin(tbd[k]), fin(tbs[k]))
    else:
        k = horizon_s
        f = np.flatnonzero(np.isfinite(tb[k:]))
        if len(f):
            j = k + int(f[0])
            sell, s_out, cap_out = float(tb[j]), fin(tbs[j]), max(fin(tbd[j]), fin(tbs[j]))
        else:
            sell, s_out, cap_out = 0.0, np.inf, np.inf
        taker_out = True
    usd = trade(buy, True, s_in, cap_in, sell, taker_out, s_out, cap_out)[0]
    return usd, float(ta[0] - tb[0]) * 100, buy


def game(args):
    path, m = args
    db = W.connect(path)
    r = db.execute('SELECT sport, start_ms FROM matches WHERE id = ?', (m,)).fetchone()
    if not r or not r[1]:
        return {}
    sport, start = r
    three = sport in THREE
    pm = defaultdict(list)
    for x in db.execute('SELECT ts_ms, outcome, bid, ask, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)):
        pm[x[1]].append(x)
    ser = {}
    for o, rows in pm.items():
        if o not in ('home', 'away') or len(rows) < 20:
            continue
        arr = np.array([[x[0]] + [np.nan if v is None else v for v in x[2:]] for x in rows], dtype=float)
        ser[o] = (arr[:, 0].astype(np.int64),) + tuple(arr[:, k] for k in range(1, 7))
    if not ser:
        return {}
    # every bookmaker: margin-free probability of home / away after each change (nan while suspended / incomplete)
    books = defaultdict(lambda: {'t': [], 'home': [], 'away': [], 'oh': [], 'oa': []})
    state = defaultdict(dict)
    for ts, book, o, price in db.execute("SELECT ts_ms, book, outcome, new_price FROM changes WHERE match_id = ? AND book != 'polymarket' "
                                         "AND market IN ('1x2', 'x12', 'X12') ORDER BY ts_ms", (m,)):
        if o not in ('home', 'draw', 'away') or (o == 'draw' and not three):
            continue
        st = state[book]
        st[o] = price if price and price > 1 else None
        need = ('home', 'draw', 'away') if three else ('home', 'away')
        b = books[book]
        b['t'].append(ts)
        if all(st.get(k) for k in need):
            inv = {k: 1 / st[k] for k in need}
            s = sum(inv.values())
            b['home'].append(inv['home'] / s)
            b['away'].append(inv['away'] / s)
            b['oh'].append(st['home'])
            b['oa'].append(st['away'])
        else:
            b['home'].append(np.nan)
            b['away'].append(np.nan)
            b['oh'].append(np.nan)
            b['oa'].append(np.nan)
    B = {b: {k: np.array(v, np.int64 if k == 't' else float) for k, v in d.items()} for b, d in books.items() if len(d['t']) >= 3}
    fams = defaultdict(list)
    for b in B:
        fams[FAM.get(b, b)].append(b)

    def fam_at(o, times):
        """[families x times] margin-free probability of o (nan where none)."""
        out = np.full((len(fams), len(times)), np.nan)
        for i, (f, bl) in enumerate(fams.items()):
            vals = []
            for b in bl:
                k = np.searchsorted(B[b]['t'], times, side='right') - 1
                vals.append(np.where(k >= 0, B[b][o][np.clip(k, 0, None)], np.nan))
            out[i] = np.nanmean(np.array(vals), 0) if len(vals) > 1 else vals[0]
        return out
    res = {'A': [], 'Agrid': [], 'B': [], 'C': []}
    day = lambda t: dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime('%Y-%m-%d')
    # ------------------------------------------------------------ A: pre-match
    for o in ser:
        lt = ser[o][0]
        t0 = max(int(lt[0]), start - 8 * 3_600_000)
        t1 = start - 5 * 60_000
        if t1 - t0 < 30 * 60_000 or not fams:
            continue
        grid = np.arange(t0, t1, 60_000)
        lb, la = ser[o][1], ser[o][2]
        mid = (held(lt, lb, grid) + held(lt, la, grid)) / 2
        F = fam_at(o, grid)
        nf = np.sum(np.isfinite(F), 0)
        with np.errstate(all='ignore'):
            C = np.nanmedian(F, 0)
        # convergence on a 5-min sample: gap now vs PM's move over the next 60 min
        for i in range(0, len(grid) - 60, 5):
            if nf[i] >= 2 and np.isfinite(mid[i]) and np.isfinite(mid[i + 60]) and np.isfinite(C[i]):
                res['Agrid'].append((C[i] - mid[i], mid[i + 60] - mid[i], (la[0] - lb[0]) if False else np.nan))
        last = {}
        for i in range(15, len(grid) - 1):
            if nf[i] < 2 or not np.isfinite(C[i]) or not np.isfinite(C[i - 15]) or not np.isfinite(mid[i]) or not np.isfinite(mid[i - 15]):
                continue
            dC = C[i] - C[i - 15]
            if abs(dC) < 0.02:
                continue
            d = 1 if dC > 0 else -1
            moved = np.sum(d * (F[:, i] - F[:, i - 15]) >= 0.005)
            gap = d * (C[i] - mid[i])
            dM = d * (mid[i] - mid[i - 15])
            if moved < 2 or gap < 0.015 or dM >= 0.3 * abs(dC):
                continue
            if grid[i] - last.get(d, -10 ** 15) < 3_600_000:
                continue
            last[d] = grid[i]
            te = int(grid[i]) + 60_000
            hz = int(min(3600, (start - 120_000 - te) / 1000))
            if hz < 300:
                continue
            target = C[i] if d > 0 else 1 - C[i]
            rec = {'game': m, 't': te, 'day': day(te), 'sport': sport, 'dC': abs(dC) * 100, 'gap': gap * 100, 'moved': int(moved), 'mins_to_ko': (start - te) / 60_000}
            ok = True
            for lab, hzz, mk in (('taker', hz, False), ('maker', hz, True), ('hold', int((start - 120_000 - te) / 1000), False)):
                r_ = run_trade(ser, o, d, te, 2.0 if lab == 'hold' else target, hzz, mk)
                if r_ is None:
                    ok = False
                    break
                rec[lab], rec['spread'], rec['buy'] = r_
            if ok:
                ms = (held(lt, lb, np.array([te + 1000 * h for h in (900, 1800, 3600)])) + held(lt, la, np.array([te + 1000 * h for h in (900, 1800, 3600)]))) / 2
                m0 = (held(lt, lb, np.array([te]))[0] + held(lt, la, np.array([te]))[0]) / 2
                rec['g15'], rec['g30'], rec['g60'] = (d * (ms - m0) * 100).tolist()
                res['A'].append(rec)
    # ------------------------------------------------------------ B / C: in-play Polymarket jumps
    for o in ser:
        lt, lb, la = ser[o][0], ser[o][1], ser[o][2]
        t0, t1 = max(int(lt[0]), start), min(int(lt[-1]), start + 4 * 3_600_000)
        if t1 - t0 < 600_000:
            continue
        grid = np.arange(t0, t1, 1000)
        gb, ga = held(lt, lb, grid), held(lt, la, grid)
        mid, gsp = (gb + ga) / 2, ga - gb
        x = logit(mid)
        dx = np.full(len(x), np.nan)
        dx[10:] = x[10:] - x[:-10]
        last = {}
        for i in np.flatnonzero(np.abs(np.nan_to_num(dx)) >= 0.12):
            if i < 30 or i + 5 >= len(x) or abs(np.nan_to_num(dx[i - 1])) >= 0.12:
                continue
            d = 1 if dx[i] > 0 else -1
            # a real move, not the mid flickering as one side of the book empties: spread <= 4c before and at the jump,
            # and still at least half the jump 5 s later (known before the entry at +11 s)
            if not (gsp[i] <= 0.04 and gsp[i - 10] <= 0.04) or d * (x[i + 5] - x[i - 10]) < 0.5 * abs(dx[i]):
                continue
            t = int(grid[i])
            if t - last.get(d, -10 ** 15) < 60_000 or t + 400_000 > t1:
                continue
            last[d] = t
            times = np.array([t - 20_000, t + 10_000, t + 60_000, t + 1000, t + 3000, t - 10_000])
            F = fam_at(o, times) if fams else np.zeros((0, 6))
            conf10 = int(np.sum(d * (logit(F[:, 1]) - logit(F[:, 0])) >= 0.05)) if len(F) else 0
            conf60 = int(np.sum(d * (logit(F[:, 2]) - logit(F[:, 0])) >= 0.05)) if len(F) else 0
            pre_mid = mid[i - 10]
            rec = {'game': m, 't': t, 'day': day(t), 'sport': sport, 'jump': abs(dx[i]), 'size': abs(mid[i] - pre_mid) * 100, 'nfam': int(np.sum(np.isfinite(F[:, 0]))) if len(F) else 0,
                   'conf10': conf10, 'conf60': conf60}
            te = t + 11_000
            m_te = (held(lt, lb, np.array([te]))[0] + held(lt, la, np.array([te]))[0]) / 2
            mh = (held(lt, lb, te + 1000 * np.array([30, 60, 300])) + held(lt, la, te + 1000 * np.array([30, 60, 300]))) / 2
            rec['rev30'], rec['rev60'], rec['rev300'] = (-d * (mh - m_te) * 100).tolist()      # reversal = good for the fade
            tgt_fade = (1 - pre_mid) if d > 0 else pre_mid                                       # the fade token back at the pre-jump price
            for lab, dd, tgt, mk in (('fadeT', -d, tgt_fade, False), ('fadeM', -d, tgt_fade, True), ('follow', d, 2.0, False)):
                r_ = run_trade(ser, o, dd, te, tgt, 300, mk)
                rec[lab] = np.nan if r_ is None else r_[0]
                if lab == 'fadeT' and r_ is not None:
                    rec['spread'] = r_[1]
            res['B'].append(rec)
            # C: stale bookmakers on an upward Polymarket jump of o
            if d > 0 and len(F):
                p60 = (held(lt, lb, np.array([t + 60_000]))[0] + held(lt, la, np.array([t + 60_000]))[0]) / 2
                if not np.isfinite(p60):
                    continue
                key = 'oh' if o == 'home' else 'oa'
                for f, bl in fams.items():
                    for b in bl[:1]:
                        k = np.searchsorted(B[b]['t'], np.array([t - 10_000, t + 1000, t + 3000]), side='right') - 1
                        if np.any(k < 0):
                            continue
                        o_pre, o1, o3 = B[b][key][k]
                        nxt = np.searchsorted(B[b]['t'], t, side='right')
                        upd = next((int(B[b]['t'][j]) - t for j in range(nxt, min(nxt + 50, len(B[b]['t'])))
                                    if not np.isfinite(B[b][key][j]) or abs(B[b][key][j] - o_pre) > 1e-9), None)
                        res['C'].append({'fam': f, 'sport': sport, 'size': rec['size'], 'stale1': bool(np.isfinite(o1) and abs(o1 - o_pre) < 1e-9),
                                         'stale3': bool(np.isfinite(o3) and abs(o3 - o_pre) < 1e-9), 'ev1': (p60 * o1 - 1) * 100 if np.isfinite(o1) else np.nan,
                                         'ev3': (p60 * o3 - 1) * 100 if np.isfinite(o3) else np.nan, 'upd_ms': upd, 'day': rec['day']})
    return res


def cautious(v):
    v = np.asarray(v, float)
    return v.sum() - v.std() * np.sqrt(len(v)) if len(v) > 1 else -np.inf


def place(rows, hold_ms):
    rows = sorted(rows, key=lambda r: r['t'])
    busy, out = {}, []
    for r in rows:
        if r['t'] < busy.get(r['game'], -1):
            continue
        busy[r['game']] = r['t'] + hold_ms
        out.append(r)
    return out


def clean(rows, grid, val, hold_ms, label):
    """Per day 19-23 Nov: the filter (from `grid`: name -> predicate) and value key chosen on earlier days."""
    lines, taken = [], []
    for day in range(19, 24):
        dd = f'2026-11-{day:02d}'
        before = [r for r in rows if r['day'] < dd and r['t'] + hold_ms < int(dt.datetime(2026, 11, day, tzinfo=dt.timezone.utc).timestamp() * 1000)]
        today = [r for r in rows if r['day'] == dd]
        best = None
        for (fname, pred), vk in itertools.product(grid, val):
            tk = place([r for r in before if pred(r) and np.isfinite(r.get(vk, np.nan))], hold_ms)
            if len(tk) < 30:
                continue
            sc = cautious([r[vk] for r in tk])
            if sc > 0 and (best is None or sc > best[0]):
                best = (sc, fname, pred, vk)
        if best is None:
            lines.append(f'     {day} Nov: nothing made money before (cautious): no trades')
            continue
        _, fname, pred, vk = best
        tk = place([r for r in today if pred(r) and np.isfinite(r.get(vk, np.nan))], hold_ms)
        taken += [(r['game'], r[vk]) for r in tk]
        lines.append(f'     {day} Nov: {fname}, {vk}: {len(tk)} trades ${sum(r[vk] for r in tk):+.0f}')
    if taken:
        v = np.array([x[1] for x in taken])
        g = np.array([x[0] for x in taken])
        ug, inv = np.unique(g, return_inverse=True)
        gs = np.bincount(inv, v)
        rng = np.random.default_rng(4)
        boot = [gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(3000)]
        lines.append(f'     pooled {label}: {len(v)} trades ${v.sum():+,.0f} (90% {np.quantile(boot, 0.05):+,.0f}..{np.quantile(boot, 0.95):+,.0f}), ${v.mean():+.2f} per $100, win {np.mean(v > 0) * 100:.0f}%')
    else:
        lines.append(f'     pooled {label}: no trades')
    return lines


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=24)
    a = ap.parse_args()
    db = W.connect(a.db)
    games = [r[0] for r in db.execute("SELECT DISTINCT match_id FROM book_events WHERE book = 'polymarket'")]
    R = {'A': [], 'Agrid': [], 'B': [], 'C': []}
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(game, [(a.db, m) for m in games], chunksize=2)):
            for key in R:
                R[key] += r.get(key, [])
            if (k + 1) % 500 == 0:
                print(f'  {k + 1} of {len(games)} games: A {len(R["A"])}, B {len(R["B"])}, C {len(R["C"])}', flush=True)
    import pickle
    with open(os.path.join(a.signals, 'angles.pkl'), 'wb') as f:
        pickle.dump(R, f)
    report(R, a.signals)


def stat(v):
    v = np.asarray([x for x in v if np.isfinite(x)], float)
    return f'${v.mean():+6.2f} per $100 (n {len(v)}, win {np.mean(v > 0) * 100:.0f}%)' if len(v) else 'n 0'


def report(R, signals):
    out = ['Three other ways to use the lead (Mac data 17-23 Nov; dollars per $100 order with Polymarket book depth)', '']
    A = R['A']
    G = np.array(R['Agrid'], float) if R['Agrid'] else np.zeros((0, 3))
    out.append(f'A. PRE-MATCH. {len(G)} sampled moments (2+ families): Polymarket\'s move over the next 60 min against its gap to the consensus:')
    for lo, hi in ((-1, -0.04), (-0.04, -0.02), (-0.02, -0.005), (-0.005, 0.005), (0.005, 0.02), (0.02, 0.04), (0.04, 1)):
        mm = (G[:, 0] >= lo) & (G[:, 0] < hi)
        if mm.sum() >= 50:
            out.append(f'     gap {lo * 100:+5.1f}..{hi * 100:+5.1f} pt: {mm.sum():>7}  PM moved {G[mm, 1].mean() * 100:+.2f} pt (median gap {np.median(G[mm, 0]) * 100:+.1f})')
    out.append(f'   Signals (consensus moved >= 2 pt in 15 min, 2+ families, PM < 30% of it, gap >= 1.5 pt): {len(A)}')
    if A:
        for lab in ('taker', 'maker', 'hold'):
            out.append(f'     {lab:<6} ' + stat([r[lab] for r in A]))
        out.append(f'     gross mid move our way after 15 / 30 / 60 min: {np.nanmean([r["g15"] for r in A]):+.2f} / {np.nanmean([r["g30"] for r in A]):+.2f} / '
                   f'{np.nanmean([r["g60"] for r in A]):+.2f} pt; median spread {np.median([r["spread"] for r in A]):.1f}c; median gap {np.median([r["gap"] for r in A]):.1f} pt')
        for name, pred in (('gap >= 4', lambda r: r['gap'] >= 4), ('gap >= 6', lambda r: r['gap'] >= 6), ('dC >= 5', lambda r: r['dC'] >= 5),
                           ('spread <= 1c', lambda r: r['spread'] <= 1.01), ('3+ families moved', lambda r: r['moved'] >= 3), ('< 60 min to kick-off', lambda r: r['mins_to_ko'] < 60),
                           ('football', lambda r: r['sport'] == 'football'), ('tennis', lambda r: r['sport'] == 'tennis'), ('basketball', lambda r: r['sport'] == 'basketball'),
                           ('esports', lambda r: r['sport'] in ('cs2', 'dota2', 'valorant', 'lol'))):
            sub = [r for r in A if pred(r)]
            if len(sub) >= 30:
                out.append(f'     {name:<22} taker {stat([r["taker"] for r in sub])}; maker {stat([r["maker"] for r in sub])}; hold {stat([r["hold"] for r in sub])}')
        gridA = [(f'gap>={g} dC>={c} spread<={s}', (lambda g, c, s: lambda r: r['gap'] >= g and r['dC'] >= c and r['spread'] <= s)(g, c, s))
                 for g, c, s in itertools.product((1.5, 3, 5), (2, 3, 5), (1.01, 2.01, 99))]
        out.append('   CLEAN, day by day:')
        out += clean(A, gridA, ('taker', 'maker', 'hold'), 3_600_000, 'pre-match')
    out.append('')
    Bv = R['B']
    out.append(f'B. POLYMARKET JUMPS IN-PLAY (>= 0.12 log-odds in 10 s): {len(Bv)}')
    if Bv:
        for name, pred in (('no bookmaker moved by +10 s', lambda r: r['conf10'] == 0 and r['nfam'] >= 1), ('...and none by +60 s', lambda r: r['conf60'] == 0 and r['nfam'] >= 1),
                           ('a bookmaker confirmed by +10 s', lambda r: r['conf10'] >= 1), ('no bookmaker priced at all', lambda r: r['nfam'] == 0)):
            sub = [r for r in Bv if pred(r)]
            if not sub:
                continue
            out.append(f'   {name}: {len(sub)}; reversal after 30 s / 1 / 5 min {np.nanmean([r["rev30"] for r in sub]):+.2f} / {np.nanmean([r["rev60"] for r in sub]):+.2f} / '
                       f'{np.nanmean([r["rev300"] for r in sub]):+.2f} pt (median jump {np.median([r["size"] for r in sub]):.1f} pt)')
            out.append(f'      fade (sell into the bid) {stat([r["fadeT"] for r in sub])}; fade (limit sale) {stat([r["fadeM"] for r in sub])}; follow {stat([r["follow"] for r in sub])}')
        un = [r for r in Bv if r['conf10'] == 0 and r['nfam'] >= 1]
        for name, pred in (('jump >= 0.25', lambda r: r['jump'] >= 0.25), ('jump >= 0.5', lambda r: r['jump'] >= 0.5), ('spread <= 1c', lambda r: r.get('spread', 99) <= 1.01),
                           ('3+ families quoting', lambda r: r['nfam'] >= 3)):
            sub = [r for r in un if pred(r)]
            if len(sub) >= 30:
                out.append(f'      unconfirmed, {name:<18} fade {stat([r["fadeT"] for r in sub])}; limit {stat([r["fadeM"] for r in sub])}')
        gridB = [(f'unconfirmed jump>={j} spread<={s} fams>={n}', (lambda j, s, n: lambda r: r['conf10'] == 0 and r['nfam'] >= n and r['jump'] >= j and r.get('spread', 99) <= s)(j, s, n))
                 for j, s, n in itertools.product((0.12, 0.25, 0.5), (1.01, 2.01, 99), (1, 2, 3))]
        gridB += [(f'confirmed jump>={j} spread<={s}', (lambda j, s: lambda r: r['conf10'] >= 1 and r['jump'] >= j and r.get('spread', 99) <= s)(j, s))
                  for j, s in itertools.product((0.12, 0.25, 0.5), (1.01, 2.01, 99))]
        out.append('   CLEAN, day by day (fade or follow, whichever made money before):')
        out += clean(Bv, gridB, ('fadeT', 'fadeM', 'follow'), 300_000, 'in-play jumps')
    out.append('')
    Cv = R['C']
    out.append(f'C. STALE BOOKMAKERS after an upward Polymarket jump ({len(Cv)} family x jump pairs; value = PM mid 60 s later x the odds - 1):')
    if Cv:
        for f in sorted({r['fam'] for r in Cv}, key=lambda f: -sum(1 for r in Cv if r['fam'] == f))[:16]:
            sub = [r for r in Cv if r['fam'] == f]
            s1 = [r for r in sub if r['stale1']]
            s3 = [r for r in sub if r['stale3']]
            upd = [r['upd_ms'] for r in sub if r['upd_ms'] is not None]
            out.append(f'   {f:<12} {len(sub):>6}: still the old odds 1 s / 3 s after: {len(s1) / len(sub) * 100:3.0f}% / {len(s3) / len(sub) * 100:3.0f}%; '
                       f'value then ${np.nanmean([r["ev1"] for r in s1]) if s1 else float("nan"):+.2f} / ${np.nanmean([r["ev3"] for r in s3]) if s3 else float("nan"):+.2f} per $100 '
                       f'(> 0: {np.mean([r["ev3"] > 0 for r in s3]) * 100 if s3 else 0:.0f}%); changed after {np.median(upd) / 1000 if upd else float("nan"):.1f} s (median)')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(signals, 'angles.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
