"""Real odds: pool Polymarket, Kalshi, the bookmakers and the superline into one probability, then bet where a price is
below it and hold to the result (owner, 24 Nov).

  python tools/signals/fair.py results --db X --signals DIR      official results from Polymarket (Gamma), cached
  python tools/signals/fair.py build   --db X --signals DIR      price snapshots of every game
  python tools/signals/fair.py test    --signals DIR

Why this is a different bet from the wave trades: it is held to the result, so it pays the spread and the fee ONCE
(Polymarket: about half a cent of spread + 5% p(1-p); nothing on the payout), and it does not need to be faster than
anyone: it needs a probability that is closer to the truth than the price.
SNAPSHOTS per game and side (home / away token): 120 / 60 / 30 / 10 / 3 min before kick-off and 10 / 20 / 30 / 45 / 60 /
90 min after, where Polymarket quotes it at 2-98c with a spread of 10c or less. At each: Polymarket bid / ask / mid;
the bookmakers' consensus (each book's margin-free probability, mean within a feed family, median over families),
how many families, how far apart they are; the superline (waves.superline); Kalshi's bid / ask for the same side
(its order book where recorded, else its ticker; its own home / away flipped; dropped when it is 25+ points from
Polymarket = a mapping doubt); how each moved in the last 10 / 60 min.
RESULT: Polymarket's own resolution (outcomePrices of the resolved moneyline markets), never the last quote.
MODELS (trained on games that kicked off at least 6 h before the test day, scored on that day's games):
  pool    a logarithmic opinion pool with learned weights: logit p = a + b logit(PM) + c (logit(books) - logit(PM))
          + d (logit(superline) - logit(PM)) + e (logit(Kalshi) - logit(PM)) + interactions with in-play, the price
          level, disagreement between books, spread, sport (b > 1 = extremizing; Satopaa et al. 2014);
  trees   Newton-boosted trees with the logistic loss on the same inputs;  blend = the mean of the two in log-odds.
SCORES against each source alone: log loss and Brier on the same snapshots (lower = closer to the truth), with the
difference to Polymarket's own price resampled by game.
BETS: per game the first snapshot where (model probability) - (ask + fee) >= a threshold; one bet a game, held to
the result; profit per $100 staked = 100 (result - cost) / cost. Next to the plain rules "bookmakers above the
price", "superline above the price", "Kalshi above the price", and the same bets placed on Kalshi (7% p(1-p))."""
import argparse
import bisect
import datetime as dt
import difflib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402

OFFS = (-120, -60, -30, -10, -3, 10, 20, 30, 45, 60, 90)
COLS = ['game', 'side', 'off', 't', 'start', 'y', 'pm_bid', 'pm_ask', 'cons', 'n_fam', 'fam_std', 'fam_min', 'fam_max', 'sl', 'k_bid', 'k_ask',
        'pm_chg10', 'pm_chg60', 'cons_chg60', 'k_chg60', 'three']
norm = lambda s: ' '.join(re.findall(r'[a-z0-9]+', (s or '').lower()))
sim = lambda a, b: difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
lg = lambda p: np.log(np.clip(p, 0.005, 0.995) / (1 - np.clip(p, 0.005, 0.995)))
pm_fee = lambda p: 0.05 * p * (1 - p)
k_fee = lambda p: 0.07 * p * (1 - p)
DAY = 86_400_000


# ------------------------------------------------------------------ official results
def result_of(ev, home, away):
    try:
        req = urllib.request.Request(f'https://gamma-api.polymarket.com/events/{ev}', headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r:
            e = json.loads(r.read().decode())
    except Exception:
        return None
    out = {}
    for m in e.get('markets', []):
        if m.get('sportsMarketType') != 'moneyline' or not m.get('closed'):
            continue
        try:
            outs, pr = json.loads(m.get('outcomes') or '[]'), [float(x) for x in json.loads(m.get('outcomePrices') or '[]')]
        except Exception:
            continue
        if len(outs) != 2 or len(pr) != 2 or max(pr) < 0.99:       # not resolved, or a 50-50 (void)
            continue
        if [o.lower() for o in outs] == ['yes', 'no']:
            title = m.get('groupItemTitle') or m.get('question') or ''
            if 'draw' in title.lower():
                continue
            side = 'home' if sim(title, home) >= sim(title, away) else 'away'
            out[side] = int(pr[0] >= 0.99)
        else:
            hi = 0 if sim(outs[0], home) + sim(outs[1], away) >= sim(outs[1], home) + sim(outs[0], away) else 1
            out['home'], out['away'] = int(pr[hi] >= 0.99), int(pr[1 - hi] >= 0.99)
    return out or None


def cmd_results(a):
    db = sqlite3.connect('file:' + a.db.replace(chr(92), '/') + '?mode=ro', uri=True)
    path = os.path.join(a.signals, 'fair_results.json')
    res = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}
    todo = [r for r in db.execute("SELECT e.match_id, e.book_event_id, m.home, m.away FROM book_events e JOIN matches m ON m.id = e.match_id WHERE e.book = 'polymarket'") if str(r[0]) not in res]
    print(f'{len(todo)} games to look up ({len(res)} cached)', flush=True)
    with ThreadPoolExecutor(8) as ex:
        for k, (r, out) in enumerate(zip(todo, ex.map(lambda r: result_of(r[1], r[2], r[3]), todo))):
            res[str(r[0])] = out
            if (k + 1) % 500 == 0:
                print(f'  {k + 1}', flush=True)
                json.dump(res, open(path, 'w', encoding='utf-8'))
    json.dump(res, open(path, 'w', encoding='utf-8'))
    print(f'{sum(1 for v in res.values() if v)} of {len(res)} games have an official result')


# ------------------------------------------------------------------ snapshots
def at(times, vals, t, max_age):
    i = bisect.bisect_right(times, t) - 1
    return vals[i] if i >= 0 and t - times[i] <= max_age else None


def one(args):
    path, m, sport, start, home, away, res = args
    db = W.connect(path)
    pm, bk = W.load(db, m)
    three = sport in W.THREE_WAY
    kev = [x for x in db.execute("SELECT book_event_id, home, away FROM book_events WHERE book = 'kalshi' AND match_id = ?", (m,)) if 'GAME' in x[0]]
    kq = {}
    if kev:
        ev, eh, ea = kev[0]
        sw = (not three) and bool(eh) and sim(home, ea) + sim(away, eh) > sim(home, eh) + sim(away, ea)
        for ts, o, b, a_, bb, ba in db.execute('SELECT ts_ms, outcome, bid, ask, book_bid, book_ask FROM kalshi_quotes WHERE event = ? AND est = 0 ORDER BY ts_ms', (ev,)):
            if o not in ('home', 'away'):
                continue
            if sw:
                o = 'away' if o == 'home' else 'home'
            b, a_ = (bb, ba) if bb is not None and ba is not None else (b, a_)
            if b is not None and a_ is not None and 0 < b < a_ < 1:
                kq.setdefault(o, ([], []))
                kq[o][0].append(ts)
                kq[o][1].append((b, a_))
    rows = []
    for si, o in enumerate(('home', 'away')):
        if res.get(o) is None:
            continue
        q = [(r[0], r[3], r[4]) for r in pm if r[2] == o and r[3] is not None and r[4] is not None]
        if len(q) < 5:
            continue
        pt, pv = [x[0] for x in q], [(x[1], x[2]) for x in q]
        lines = W.book_lines(bk, o)
        fams = {}
        for b_, ser in lines.items():
            fams.setdefault(W.family(b_), []).append(([x[0] for x in ser], [x[1] for x in ser]))
        try:
            slp = W.superline(lines) if lines else []
        except Exception:
            slp = []
        st, sv = [x[0] for x in slp], [x[1] for x in slp]

        def cons_at(t, age):
            v = []
            for ser in fams.values():
                ps = [p for p in (at(tt, pp, t, age) for tt, pp in ser) if p is not None]
                if ps:
                    v.append(float(np.mean(ps)))
            return v
        for off in OFFS:
            t = start + off * 60_000
            live = off > 0
            qa = at(pt, pv, t, 120_000 if live else 1_800_000)
            if qa is None or not (0.02 < (qa[0] + qa[1]) / 2 < 0.98) or qa[1] - qa[0] > 0.10:
                continue
            age = 300_000 if live else 43_200_000
            v = cons_at(t, age)
            v60 = cons_at(t - 3_600_000, age)
            sl = at(st, sv, t, age) if st else None
            k = at(kq[o][0], kq[o][1], t, 300_000 if live else 3_600_000) if o in kq else None
            k60 = at(kq[o][0], kq[o][1], t - 3_600_000, 3_600_000) if o in kq else None
            mid = (qa[0] + qa[1]) / 2
            if k is not None and abs((k[0] + k[1]) / 2 - mid) > 0.25:
                k = None
            p10, p60 = at(pt, pv, t - 600_000, 1_800_000), at(pt, pv, t - 3_600_000, 3_600_000)
            rows.append([m, si, off, t, start, res[o], qa[0], qa[1], np.median(v) if v else np.nan, len(v), np.std(v) if len(v) > 1 else 0.0, min(v) if v else np.nan, max(v) if v else np.nan,
                         sl if sl is not None else np.nan, k[0] if k else np.nan, k[1] if k else np.nan,
                         mid - (p10[0] + p10[1]) / 2 if p10 else 0.0, mid - (p60[0] + p60[1]) / 2 if p60 else 0.0,
                         (np.median(v) - np.median(v60)) if v and v60 else 0.0, ((k[0] + k[1]) / 2 - (k60[0] + k60[1]) / 2) if k and k60 else 0.0, float(three)])
    return rows, sport


def cmd_build(a):
    res = json.load(open(os.path.join(a.signals, 'fair_results.json'), encoding='utf-8'))
    db = W.connect(a.db)
    gl = [(a.db, m, sp, st, h, aw, res[str(m)]) for m, sp, h, aw, st in W.games(db) if res.get(str(m))]
    print(f'{len(gl)} games with an official result and a bookmaker', flush=True)
    rows, sports, t0 = [], [], time.time()
    with Pool(a.workers) as pool:
        for k, (r, sp) in enumerate(pool.imap_unordered(one, gl, chunksize=2)):
            rows += r
            sports += [sp] * len(r)
            if (k + 1) % 500 == 0:
                print(f'  {k + 1} of {len(gl)} games, {len(rows)} snapshots, {time.time() - t0:.0f} s', flush=True)
    A = np.array(rows, float)
    np.savez(os.path.join(a.signals, 'fair.npz'), A=A, sport=np.array(sports), cols=np.array(COLS))
    print(f'{len(A)} snapshots of {len(np.unique(A[:, 0]))} games; with Kalshi {int(np.isfinite(A[:, COLS.index("k_bid")]).sum())}; with the superline {int(np.isfinite(A[:, COLS.index("sl")]).sum())}')


# ------------------------------------------------------------------ models and bets
def fit_pool(X, y, l2=1.0):
    import torch
    Xt, yt = torch.tensor(X, dtype=torch.float64), torch.tensor(y, dtype=torch.float64)
    w = torch.zeros(X.shape[1], dtype=torch.float64, requires_grad=True)
    opt = torch.optim.LBFGS([w], lr=1.0, max_iter=300, line_search_fn='strong_wolfe')

    def closure():
        opt.zero_grad()
        loss = torch.nn.functional.binary_cross_entropy_with_logits(Xt @ w, yt) + l2 * (w[2:] ** 2).sum() / len(yt)
        loss.backward()
        return loss
    opt.step(closure)
    return w.detach().numpy()


def boot(v, g, n=3000, seed=1):
    v, g = np.asarray(v, float), np.asarray(g)
    ug, inv = np.unique(g, return_inverse=True)
    gs, gn = np.bincount(inv, v), np.bincount(inv)
    rng = np.random.default_rng(seed)
    k = rng.integers(0, len(ug), (n, len(ug)))
    return np.quantile(gs[k].sum(1) / gn[k].sum(1), [0.05, 0.95])


def cmd_test(a):
    import torch
    import lab as LB
    import mine as MN
    sys.stdout.reconfigure(encoding='utf-8')
    Z = np.load(os.path.join(a.signals, 'fair.npz'))
    A, SP = Z['A'], Z['sport']
    c = {k: i for i, k in enumerate(COLS)}
    col = lambda k: A[:, c[k]]
    G, y, off, start = col('game').astype(np.int64), col('y'), col('off'), col('start')
    bid, ask = col('pm_bid'), col('pm_ask')
    mid = (bid + ask) / 2
    cons, sl, kmid = col('cons'), col('sl'), (col('k_bid') + col('k_ask')) / 2
    live = off > 0
    out = []
    say = lambda s: (out.append(s), print(s, flush=True))
    ll = lambda p, yy: -np.mean(yy * np.log(np.clip(p, 1e-4, 1)) + (1 - yy) * np.log(np.clip(1 - p, 1e-4, 1)))
    days = sorted({dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in start})
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    say(f'{len(A)} snapshots of {len(np.unique(G))} games with an official result ({days[0]} .. {days[-1]}); Polymarket\'s side won in {y.mean() * 100:.0f}% of them at an average price of {mid.mean() * 100:.0f}c')
    # ---- 1. each source alone, on the snapshots where all of them exist
    say('')
    say('1. WHO IS CLOSEST TO THE TRUTH? (log loss and Brier: lower is better; "no model" = always saying 50%)')
    for name, mm in (('before kick-off', ~live), ('in play', live)):
        m_all = mm & np.isfinite(cons) & np.isfinite(sl)
        m_k = m_all & np.isfinite(kmid)
        say(f'   {name}: {m_all.sum()} snapshots, {len(np.unique(G[m_all]))} games')
        for lab_, p in (('no model (50%)', np.full(len(y), 0.5)), ('Polymarket mid', mid), ('bookmakers\' consensus', cons), ('superline', sl), ('mean of Polymarket and bookmakers', (mid + cons) / 2)):
            say(f'      {lab_:<34} log loss {ll(p[m_all], y[m_all]):.4f}   Brier {np.mean((p[m_all] - y[m_all]) ** 2):.4f}')
        if m_k.sum() > 200:
            say(f'      where Kalshi is quoted too ({m_k.sum()} snapshots, {len(np.unique(G[m_k]))} games): Polymarket {ll(mid[m_k], y[m_k]):.4f}, Kalshi {ll(kmid[m_k], y[m_k]):.4f}, bookmakers {ll(cons[m_k], y[m_k]):.4f}, '
                f'mean of the three {ll(((mid + kmid + cons) / 3)[m_k], y[m_k]):.4f}')
    # ---- 2. when they disagree, who is right
    say('')
    say('2. WHEN TWO DISAGREE, WHO IS RIGHT? (10 min before kick-off; the side\'s real win rate next to each price)')
    pre = off == -10
    for name, other in (('bookmakers', cons), ('superline', sl), ('Kalshi', kmid)):
        mm = pre & np.isfinite(other)
        gap = (other - mid) * 100
        cells = []
        for lo, hi in ((-99, -4), (-4, -2), (-2, 2), (2, 4), (4, 99)):
            b = mm & (gap >= lo) & (gap < hi)
            if b.sum() >= 25:
                cells.append(f'{lo:+d}..{hi:+d} pt: {b.sum()} sides, won {y[b].mean() * 100:.0f}% (Polymarket said {mid[b].mean() * 100:.0f}%, {name} {other[b].mean() * 100:.0f}%)')
        say(f'   {name} minus Polymarket: ' + '; '.join(cells))
    # ---- 3. the pooled model, day by day
    sports = [s for s in ('football', 'tennis', 'basketball', 'cs2', 'dota2', 'hockey') if (SP == s).sum() > 200]
    hasc, hass, hask = np.isfinite(cons), np.isfinite(sl), np.isfinite(kmid)
    lp = lg(mid)
    gc, gs_, gk = np.where(hasc, lg(np.nan_to_num(cons, nan=0.5)) - lp, 0.0), np.where(hass, lg(np.nan_to_num(sl, nan=0.5)) - lp, 0.0), np.where(hask, lg(np.nan_to_num(kmid, nan=0.5)) - lp, 0.0)
    spread = (ask - bid) * 100
    F = [np.ones(len(y)), lp, gc, gs_, gk, hasc.astype(float), hask.astype(float), gc * live, gk * live, gs_ * live, gc * np.abs(lp), gc * col('fam_std') * 10, gc * np.minimum(col('n_fam'), 8) / 8,
         live.astype(float), lp * live, spread / 5, col('pm_chg10') * 10, col('pm_chg60') * 10, col('cons_chg60') * 10, col('k_chg60') * 10, off / 100.0, col('three')]
    F += [(SP == s).astype(float) for s in sports] + [(SP == s) * gc for s in sports]
    X = np.nan_to_num(np.stack(F, 1))
    FT = np.nan_to_num(np.stack([mid, bid, ask, np.nan_to_num(cons, nan=-1), np.nan_to_num(sl, nan=-1), np.nan_to_num(kmid, nan=-1), gc, gs_, gk, col('n_fam'), col('fam_std'), np.nan_to_num(col('fam_min'), nan=-1),
                                 np.nan_to_num(col('fam_max'), nan=-1), spread, col('pm_chg10'), col('pm_chg60'), col('cons_chg60'), col('k_chg60'), off, col('three')] + [(SP == s).astype(float) for s in sports], 1)).astype(np.float32)
    dev = MN.pick_device()
    P = {k: np.full(len(y), np.nan) for k in ('pool', 'trees', 'blend')}
    wts = []
    for dday in days[1:]:
        d0 = d_ms(dday)
        tr, te = np.flatnonzero(start + 6 * 3_600_000 < d0), np.flatnonzero((start >= d0) & (start < d0 + DAY))
        if len(np.unique(G[tr])) < 150 or not len(te):
            continue
        w = fit_pool(X[tr], y[tr])
        wts.append(w)
        P['pool'][te] = X[te] @ w
        ug = np.unique(G[tr])
        rng = np.random.default_rng(0)
        vg = set(rng.choice(ug, max(1, len(ug) // 5), replace=False).tolist())
        isv = np.array([g in vg for g in G[tr]])
        Xb = LB.binned(FT, tr)
        Xg, yg = torch.from_numpy(Xb).to(dev), torch.from_numpy(y.astype(np.float32)).to(dev)
        m = LB.GB(depth=3, lr=0.05, n_trees=300, min_leaf=60, lam=5.0, row_frac=0.7, col_frac=0.8, patience=40).fit(
            Xg[torch.from_numpy(tr[~isv]).to(dev)], yg[torch.from_numpy(tr[~isv]).to(dev)], Xg[torch.from_numpy(tr[isv]).to(dev)], yg[torch.from_numpy(tr[isv]).to(dev)])
        P['trees'][te] = m.predict(Xg[torch.from_numpy(te).to(dev)])
        P['blend'][te] = (P['pool'][te] + P['trees'][te]) / 2
    sg = lambda z: 1 / (1 + np.exp(-z))
    T = np.isfinite(P['pool'])
    say('')
    say(f'3. THE POOLED MODEL on days it had not seen ({T.sum()} snapshots, {len(np.unique(G[T]))} games)')
    if wts:
        w = np.mean(wts, 0)
        say(f'   what the pool learned (weights in log-odds): Polymarket x{w[1]:.2f} (over 1 = push further from 50%); bookmakers\' gap x{w[2]:+.2f} before kick-off, x{w[2] + w[7]:+.2f} in play; '
            f'superline\'s gap x{w[3]:+.2f} / x{w[3] + w[9]:+.2f}; Kalshi\'s gap x{w[4]:+.2f} / x{w[4] + w[8]:+.2f}')
    for name, mm in (('before kick-off', T & ~live), ('in play', T & live), ('all', T)):
        base = ll(mid[mm], y[mm])
        cells = [f'Polymarket {base:.4f}']
        for k in ('pool', 'trees', 'blend'):
            p = sg(P[k][mm])
            d = (-(y[mm] * np.log(np.clip(p, 1e-4, 1)) + (1 - y[mm]) * np.log(np.clip(1 - p, 1e-4, 1)))) - (-(y[mm] * np.log(np.clip(mid[mm], 1e-4, 1)) + (1 - y[mm]) * np.log(np.clip(1 - mid[mm], 1e-4, 1))))
            lo, hi = boot(d, G[mm])
            cells.append(f'{k} {ll(p, y[mm]):.4f} (vs Polymarket {d.mean():+.4f}, 90% {lo:+.4f}..{hi:+.4f})')
        say(f'   log loss {name:<16} ' + ' | '.join(cells))
    # ---- 4. bets held to the result
    say('')
    say('4. VALUE BETS HELD TO THE RESULT (one bet a game: the first snapshot where the edge is big enough; profit per $100 staked, fee paid)')
    cost = ask + pm_fee(ask)
    kcost = col('k_ask') + k_fee(np.nan_to_num(col('k_ask'), nan=0.5))
    pay = 100 * (y - cost) / cost
    kpay = 100 * (y - kcost) / kcost
    order = np.argsort(col('t'), kind='stable')

    def bets(edge, mask, payv):
        seen, idx = set(), []
        for i in order:
            if mask[i] and edge[i] >= 0 and G[i] not in seen:
                seen.add(G[i])
                idx.append(i)
        idx = np.array(idx, np.int64)
        if len(idx) < 20:
            return f'{len(idx)} bets: too few'
        v = payv[idx]
        lo, hi = boot(v, G[idx])
        c_ = (100 / (v[y[idx] > 0.5] + 100)).mean() if (y[idx] > 0.5).any() else np.nan
        need = np.mean(100.0 / (100.0 + np.where(y[idx] > 0.5, v, 100 * (1 - (cost if payv is pay else kcost)[idx]) / (cost if payv is pay else kcost)[idx])))
        return f'{len(idx):>4} bets, won {y[idx].mean() * 100:.0f}% (needed {need * 100:.0f}%), ${v.mean():+6.2f} per $100 (90% {lo:+.1f}..{hi:+.1f})'
    pb = sg(P['blend'])
    for name, mm in (('before kick-off only', T & ~live), ('in play only', T & live)):
        say(f'   {name}:')
        for th in (0.0, 0.02, 0.04, 0.06):
            say(f'      model {th * 100:.0f}+ pt above the cost   Polymarket: {bets(pb - cost - th, mm, pay)}')
        for lab_, src in (('bookmakers', cons), ('superline', sl), ('Kalshi', kmid)):
            for th in (0.02, 0.04):
                say(f'      {lab_} {th * 100:.0f}+ pt above the cost  Polymarket: {bets(np.nan_to_num(src, nan=-9) - cost - th, mm, pay)}')
        mk = mm & np.isfinite(kcost)
        for th in (0.0, 0.02, 0.04):
            say(f'      model {th * 100:.0f}+ pt above the cost   KALSHI:     {bets(pb - kcost - th, mk, kpay)}')
        say(f'      Polymarket mid {2}+ pt above Kalshi\'s cost, bet on KALSHI: {bets(mid - kcost - 0.02, mk, kpay)}')
    # the model's bets by kind (4+ points, before kick-off)
    say('   model 2+ pt above the cost, before kick-off, by sport and price:')
    mm0 = T & ~live
    for lab_, mm in [(s, mm0 & (SP == s)) for s in sports] + [('under 30c', mm0 & (ask < 0.3)), ('30-55c', mm0 & (ask >= 0.3) & (ask < 0.55)), ('55-90c', mm0 & (ask >= 0.55) & (ask < 0.9)), ('90c+', mm0 & (ask >= 0.9))]:
        say(f'      {lab_:<12} {bets(pb - cost - 0.02, mm, pay)}')
    with open(os.path.join(a.signals, 'fair.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['results', 'build', 'test'])
    ap.add_argument('--db')
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=22)
    a = ap.parse_args()
    {'results': cmd_results, 'build': cmd_build, 'test': cmd_test}[a.cmd](a)


if __name__ == '__main__':
    main()
