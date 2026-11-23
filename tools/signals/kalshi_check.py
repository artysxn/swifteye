"""Is the Polymarket -> Kalshi lag real money? (python tools/signals/kalshi_check.py --db X --signals DIR)

kalshi_lag.py with KALSHI_PRICES=book kept a positive slice: Kalshi's real order-book ask >= 5-10c below Polymarket's
mid right after a Polymarket jump, Kalshi spread <= 2c. Checks before believing it:
  - prices: Kalshi's order book rebuilt from orderbook_delta (book_bid / book_ask), never the ticker (it lags the book:
    median 1.5 s, 90th percentile 255 s after a 2c move);
  - execution delay: buy 0 / 1 / 2 s after the decision at the book's ask THEN (room is judged at the decision);
  - fills: the recorded sizes are measured at the ticker's prices, so they are only trusted where the ticker equals the
    book at that moment ("sizes known"); there the order takes at most the size at the best price plus what rests
    within 2c (1c worse), on both sides;
  - per day, per sport, and the dollars resting at the best ask.
Same events and exits as kalshi_lag.py (target = Polymarket's mid at the decision, else 5 min; Kalshi fee both fills)."""
import argparse
import datetime as dt
import os
import pickle
import sys
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402
from booklead import held  # noqa: E402
from kalshi_lag import fin, kfee, ktrade, logit, series, swapped  # noqa: E402


def game(args):
    path, m = args
    db = W.connect(path)
    r = db.execute('SELECT sport, start_ms, home, away FROM matches WHERE id = ?', (m,)).fetchone()
    if not r or not r[1]:
        return []
    sport, start, mh, ma = r
    evs = [x for x in db.execute("SELECT book_event_id, home, away FROM book_events WHERE book = 'kalshi' AND match_id = ?", (m,)) if 'GAME' in x[0]]
    if not evs:
        return []
    pm = defaultdict(list)
    for x in db.execute('SELECT ts_ms, outcome, bid, ask FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)):
        pm[x[1]].append((x[0], x[2], x[3]))
    out = []
    ev, eh, ea = evs[0]
    sw = swapped(sport, mh, ma, eh, ea)
    kq = defaultdict(list)
    for x in db.execute('SELECT ts_ms, outcome, book_bid, book_ask, bid, ask, bid_size, ask_size, bid_depth, ask_depth FROM kalshi_quotes '
                        'WHERE event = ? AND est = 0 ORDER BY ts_ms', (ev,)):
        o = x[1]
        if sw and o in ('home', 'away'):
            o = 'away' if o == 'home' else 'home'
        kq[o].append((x[0],) + tuple(x[2:]))
    for o in ('home', 'away'):
        if len(pm.get(o, [])) < 50 or len(kq.get(o, [])) < 50:
            continue
        lt, lb, la = series(pm[o])
        kt, bb, ba, tkb, tka, kbs, kas, kbd, kad = series(kq[o])
        t0, t1 = max(int(lt[0]), int(kt[0]), start), min(int(lt[-1]), int(kt[-1]), start + 4 * 3_600_000)
        if t1 - t0 < 600_000:
            continue
        g60 = np.arange(t0, t1, 60_000)
        pmm = (held(lt, lb, g60) + held(lt, la, g60)) / 2
        km = (held(kt, bb, g60) + held(kt, ba, g60)) / 2
        okc = np.isfinite(pmm) & np.isfinite(km)
        if okc.sum() < 20 or np.corrcoef(pmm[okc], km[okc])[0, 1] < 0.8:
            continue
        grid = np.arange(t0, t1, 1000)
        gb, ga = held(lt, lb, grid), held(lt, la, grid)
        mid, gsp = (gb + ga) / 2, ga - gb
        x = logit(mid)
        dx = np.full(len(x), np.nan)
        dx[10:] = x[10:] - x[:-10]
        last = {}
        for i in np.flatnonzero(np.abs(np.nan_to_num(dx)) >= 0.12):
            if i < 30 or i + 5 >= len(x) or abs(np.nan_to_num(dx[i - 1])) >= 0.12 or not (gsp[i] <= 0.04 and gsp[i - 10] <= 0.04):
                continue
            d = 1 if dx[i] > 0 else -1
            t = int(grid[i])
            if t - last.get(d, -10 ** 15) < 60_000 or t + 1_000_000 > t1:
                continue
            last[d] = t
            persistent = d * (x[i + 5] - x[i - 10]) >= 0.5 * abs(dx[i])
            size = abs(mid[i] - mid[i - 10])
            for lag in (1, 5):
                if lag == 5 and not persistent:
                    continue
                td = t + lag * 1000
                pm_now = (held(lt, lb, np.array([td]))[0] + held(lt, la, np.array([td]))[0]) / 2
                times = td + 1000 * np.arange(0, 905)
                yb, ya, ytb, yta, ybs, yas, ybd, yad = (held(kt, v, times) for v in (bb, ba, tkb, tka, kbs, kas, kbd, kad))
                k_pre = (held(kt, bb, np.array([t - 10_000]))[0] + held(kt, ba, np.array([t - 10_000]))[0]) / 2
                if d > 0:   # Yes: buy at the ask (Yes asks), sell at the bid (Yes bids)
                    tb, ta, tbs, tbd, tas, tad, target = yb, ya, ybs, ybd, yas, yad, pm_now
                    eq_in, eq_out = np.abs(yta - ya) < 1e-9, np.abs(ytb - yb) < 1e-9
                else:       # No: buy at 1 - Yes bid (Yes bids), sell at 1 - Yes ask (Yes asks)
                    tb, ta, tbs, tbd, tas, tad, target = 1 - ya, 1 - yb, yas, yad, ybs, ybd, 1 - pm_now
                    eq_in, eq_out = np.abs(ytb - yb) < 1e-9, np.abs(yta - ya) < 1e-9
                if not (np.isfinite(ta[0]) and np.isfinite(tb[0]) and 0.01 < ta[0] < 0.99 and np.isfinite(k_pre) and np.isfinite(pm_now)):
                    continue
                kmid = (yb + ya) / 2
                room = (target - ta[0]) * 100
                if d * (kmid[0] - k_pre) / max(size, 1e-3) >= 0.3 or room < 3:
                    continue
                rec = {'game': m, 't': t, 'day': dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime('%Y-%m-%d'), 'sport': sport, 'lag': lag,
                       'size': float(size) * 100, 'kspread': float(ta[0] - tb[0]) * 100, 'kbuy': float(ta[0]), 'room': float(room),
                       'sizes_known': bool(eq_in[0]), 'usd_at_best': float(tas[0] * ta[0]) if eq_in[0] and np.isfinite(tas[0]) else np.nan}
                for ex in (0, 1, 2):
                    if not (np.isfinite(ta[ex]) and 0.01 < ta[ex] < 0.99):
                        rec[f'usd{ex}'] = np.nan
                        continue
                    buy = float(ta[ex])
                    s_in, cap_in = (fin(tas[ex]), max(fin(tad[ex]), fin(tas[ex]))) if eq_in[ex] else (np.inf, np.inf)
                    seg = np.nan_to_num(tb[ex + 1:ex + 301], nan=-1.0)
                    hit = np.flatnonzero(seg >= target - 1e-9)
                    if len(hit):
                        k = ex + 1 + int(hit[0])
                    else:
                        f = np.flatnonzero(np.isfinite(tb[ex + 300:]))
                        k = ex + 300 + int(f[0]) if len(f) else -1
                    if k < 0:
                        rec[f'usd{ex}'] = ktrade(buy, s_in, cap_in, 0.0, np.inf, np.inf)
                    else:
                        s_out, cap_out = (fin(tbs[k]), max(fin(tbd[k]), fin(tbs[k]))) if eq_out[k] else (np.inf, np.inf)
                        rec[f'usd{ex}'] = ktrade(buy, s_in, cap_in, float(tb[k]), s_out, cap_out)
                    rec[f'sizes_known{ex}'] = bool(eq_in[ex])
                out.append(rec)
    return out


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=24)
    a = ap.parse_args()
    db = W.connect(a.db)
    games = [r[0] for r in db.execute("SELECT DISTINCT match_id FROM book_events WHERE book = 'kalshi' AND match_id IN (SELECT match_id FROM book_events WHERE book = 'polymarket')")]
    R = []
    with Pool(a.workers) as pool:
        for r in pool.imap_unordered(game, [(a.db, m) for m in games], chunksize=2):
            R += r
    with open(os.path.join(a.signals, 'kalshi_check.pkl'), 'wb') as f:
        pickle.dump(R, f)
    out = [f'Polymarket jump -> Kalshi, ORDER-BOOK prices ({len(R)} decisions with Kalshi\'s ask >= 3c below Polymarket\'s mid), $ per $100 order', '']

    def line(name, rows, k):
        v = np.array([r[k] for r in rows], float)
        v = v[np.isfinite(v)]
        if not len(v):
            return f'     {name:<34} n 0'
        g = np.array([r['game'] for r in rows if np.isfinite(r[k])])
        ug, inv = np.unique(g, return_inverse=True)
        gs = np.bincount(inv, v)
        rng = np.random.default_rng(8)
        boot = [gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(3000)]
        return f'     {name:<34} {len(v):>5} trades {len(ug):>4} games  ${v.sum():+8,.0f} (90% {np.quantile(boot, 0.05):+,.0f}..{np.quantile(boot, 0.95):+,.0f})  ${v.mean():+6.2f} per $100  win {np.mean(v > 0) * 100:.0f}%'
    for lag in (1, 5):
        for rm in (5, 10):
            S = [r for r in R if r['lag'] == lag and r['room'] >= rm and r['kspread'] <= 2.01]
            if not S:
                continue
            out.append(f'Decision {lag} s after the jump, Kalshi ask >= {rm}c below PM, Kalshi spread <= 2c: {len(S)} decisions; sizes known in {np.mean([r["sizes_known"] for r in S]) * 100:.0f}%, '
                       f'$ resting at the best ask median ${np.nanmedian([r["usd_at_best"] for r in S]):,.0f}')
            for ex in (0, 1, 2):
                out.append(line(f'buy {ex} s after deciding', S, f'usd{ex}'))
            out.append(line('buy 1 s after, sizes known only', [r for r in S if r.get('sizes_known1')], 'usd1'))
            for day in sorted({r['day'] for r in S}):
                out.append(line(f'   {day} (buy 1 s after)', [r for r in S if r['day'] == day], 'usd1'))
            for sp in sorted({r['sport'] for r in S}, key=lambda s: -sum(1 for r in S if r['sport'] == s))[:6]:
                out.append(line(f'   {sp} (buy 1 s after)', [r for r in S if r['sport'] == sp], 'usd1'))
            out.append('')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'kalshi_check.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
