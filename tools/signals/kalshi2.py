"""Polymarket leads Kalshi, tested harder (python tools/signals/kalshi2.py --db X --signals DIR --tag mac [--since MS] [--rule FILE]).

Same trade as kalshi_check.py (after an in-play Polymarket jump, buy the Kalshi contract while its ORDER-BOOK ask is
well below Polymarket's mid; Kalshi fee 7% p(1-p) both fills; $ per $100 order), plus what was missing:
  flow     the WHOLE Kalshi feed (all contracts pooled) has no silent stretch > 2 s from 5 s before the jump to the buy
           (tools/kalshi_snipe.py's check: in a hole the "old price" is only a price we stopped receiving);
  pm_late  Polymarket's own stamp vs our receive time at the jump (a late PM feed = the jump is older than it looks);
  conf     bookmaker families whose price had moved the jump's way (>= 0.05 log-odds, as received) by the decision;
  exits    tgt (Kalshi bid reaches Polymarket's mid at the decision, else 5 min), half (half that distance), tp5 (bid
           >= buy + 5c), h60 / h300 (sell into the bid after 1 / 5 min).
Without --rule: reports on this database and FREEZES two rules in <signals>/kalshi_rule.json: "plain" (the rule already
reported: decide 1 s after, ask >= 10c below, spread <= 2c, exit tgt) and "picked" (best of a small grid by total
minus one standard error over games, one position a game). With --rule: applies those frozen rules, unchanged, to
this database (data they have never seen)."""
import argparse
import datetime as dt
import itertools
import json
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
from kalshi_lag import fin, ktrade, logit, series, swapped  # noqa: E402

FAM = {'unibet': 'kambi', 'paf': 'kambi', 'leovegas': 'kambi', 'linebet': '1x', 'melbet': '1x', 'betwinner': '1x', 'megapari': '1x', 'paripesa': '1x',
       '22bet': '1x', '22bit': '1x', '1xbit': '1x', 'fonbet': 'fonbet', 'pari': 'fonbet', 'vinn': 'altenar', 'betking': 'altenar'}
EXITS = ('tgt', 'half', 'tp5', 'h60', 'h300')


def game(args):
    path, m, since = args
    db = W.connect(path)
    r = db.execute('SELECT sport, start_ms, home, away FROM matches WHERE id = ?', (m,)).fetchone()
    if not r or not r[1]:
        return []
    sport, start, mh, ma = r
    evs = [x for x in db.execute("SELECT book_event_id, home, away FROM book_events WHERE book = 'kalshi' AND match_id = ?", (m,)) if 'GAME' in x[0]]
    if not evs:
        return []
    pm = defaultdict(list)
    for x in db.execute('SELECT ts_ms, outcome, bid, ask, server_ts_ms FROM pm_quotes WHERE match_id = ? AND ts_ms >= ? ORDER BY ts_ms', (m, since)):
        pm[x[1]].append((x[0], x[2], x[3], x[4]))
    ev, eh, ea = evs[0]
    sw = swapped(sport, mh, ma, eh, ea)
    kq = defaultdict(list)
    for x in db.execute('SELECT ts_ms, outcome, book_bid, book_ask, bid, ask, bid_size, ask_size, bid_depth, ask_depth FROM kalshi_quotes '
                        'WHERE event = ? AND est = 0 AND ts_ms >= ? ORDER BY ts_ms', (ev, since)):
        o = x[1]
        if sw and o in ('home', 'away'):
            o = 'away' if o == 'home' else 'home'
        kq[o].append((x[0],) + tuple(x[2:]))
    bk = defaultdict(list)
    for ts, book, o, price in db.execute("SELECT ts_ms, book, outcome, new_price FROM changes WHERE match_id = ? AND book NOT IN ('polymarket', 'kalshi') "
                                         "AND market IN ('1x2', 'x12', 'X12') AND outcome IN ('home', 'away') AND new_price > 1 AND ts_ms >= ? ORDER BY ts_ms", (m, since)):
        bk[(book, o)].append((ts, 1 / price))
    bk = {k: (np.array([x[0] for x in v], np.int64), np.array([x[1] for x in v])) for k, v in bk.items()}
    out = []
    for o in ('home', 'away'):
        if len(pm.get(o, [])) < 50 or len(kq.get(o, [])) < 50:
            continue
        lt, lb, la, lsv = series(pm[o])
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
            kp = np.searchsorted(lt, t, side='right') - 1
            pm_late = float(lt[kp] - lsv[kp]) if kp >= 0 and np.isfinite(lsv[kp]) else np.nan
            for lag in (1, 5):
                if lag == 5 and not persistent:
                    continue
                td = t + lag * 1000
                pm_now = (held(lt, lb, np.array([td]))[0] + held(lt, la, np.array([td]))[0]) / 2
                times = td + 1000 * np.arange(0, 905)
                yb, ya, ytb, yta, ybs, yas, ybd, yad = (held(kt, v, times) for v in (bb, ba, tkb, tka, kbs, kas, kbd, kad))
                k_pre = (held(kt, bb, np.array([t - 10_000]))[0] + held(kt, ba, np.array([t - 10_000]))[0]) / 2
                if d > 0:
                    tb, ta, tbs, tbd, tas, tad, target = yb, ya, ybs, ybd, yas, yad, pm_now
                    eq_in, eq_out = np.abs(yta - ya) < 1e-9, np.abs(ytb - yb) < 1e-9
                else:
                    tb, ta, tbs, tbd, tas, tad, target = 1 - ya, 1 - yb, yas, yad, ybs, ybd, 1 - pm_now
                    eq_in, eq_out = np.abs(ytb - yb) < 1e-9, np.abs(yta - ya) < 1e-9
                if not (np.isfinite(ta[0]) and np.isfinite(tb[0]) and 0.01 < ta[0] < 0.99 and np.isfinite(k_pre) and np.isfinite(pm_now) and np.isfinite(ta[1]) and 0.01 < ta[1] < 0.99):
                    continue
                kmid = (yb + ya) / 2
                room = (target - ta[0]) * 100
                if d * (kmid[0] - k_pre) / max(size, 1e-3) >= 0.3 or room < 3:
                    continue
                fams = set()
                for (book, o_), (tt, pp) in bk.items():
                    if o_ != o:
                        continue
                    k1, k0 = np.searchsorted(tt, td, side='right') - 1, np.searchsorted(tt, t - 20_000, side='right') - 1
                    if k1 >= 0 and k0 >= 0 and d * (logit(pp[k1]) - logit(pp[k0])) >= 0.05:
                        fams.add(FAM.get(book, book))
                kk = np.searchsorted(kt, td, side='right') - 1
                rec = {'event': ev, 'o': o, 'sw': sw, 'd': d, 'eh': eh, 'ea': ea, 'mh': mh, 'ma': ma, 'game': m, 't': t, 'buy_t': td + 1000, 'day': dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime('%Y-%m-%d'), 'sport': sport, 'lag': lag,
                       'size': float(size) * 100, 'kspread': float(ta[0] - tb[0]) * 100, 'kbuy': float(ta[1]), 'room': float(room), 'conf': len(fams),
                       'pm_late': pm_late, 'pm_spread': float(gsp[i]) * 100, 'k_age': float(td - kt[kk]) / 1000 if kk >= 0 else np.nan, 'sizes_known': bool(eq_in[1]),
                       'min_live': (t - start) / 60_000}
                buy = float(ta[1])
                s_in, cap_in = (fin(tas[1]), max(fin(tad[1]), fin(tas[1]))) if eq_in[1] else (np.inf, np.inf)

                def sell_at(k):
                    if k < 0:
                        return ktrade(buy, s_in, cap_in, 0.0, np.inf, np.inf)
                    s_out, cap_out = (fin(tbs[k]), max(fin(tbd[k]), fin(tbs[k]))) if eq_out[k] else (np.inf, np.inf)
                    return ktrade(buy, s_in, cap_in, float(tb[k]), s_out, cap_out)

                def first_bid(k):
                    f = np.flatnonzero(np.isfinite(tb[k:]))
                    return k + int(f[0]) if len(f) else -1
                seg = np.nan_to_num(tb[2:302], nan=-1.0)
                for name, level in (('tgt', target), ('half', buy + (target - buy) / 2), ('tp5', buy + 0.05)):
                    hit = np.flatnonzero(seg >= level - 1e-9)
                    rec[name] = sell_at(2 + int(hit[0]) if len(hit) else first_bid(301))
                rec['h60'], rec['h300'] = sell_at(first_bid(61)), sell_at(first_bid(301))
                out.append(rec)
    return out


def place(rows):
    busy, out = {}, []
    for r in sorted(rows, key=lambda r: r['t']):
        if r['t'] < busy.get(r['game'], -1):
            continue
        busy[r['game']] = r['t'] + 300_000
        out.append(r)
    return out


def stats(rows, k):
    v = np.array([r[k] for r in rows], float)
    if not len(v):
        return None
    g = np.array([r['game'] for r in rows])
    ug, inv = np.unique(g, return_inverse=True)
    gs = np.bincount(inv, v)
    rng = np.random.default_rng(8)
    boot = np.array([gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(3000)])
    return {'n': len(v), 'games': len(ug), 'tot': float(v.sum()), 'lo': float(np.quantile(boot, 0.05)), 'hi': float(np.quantile(boot, 0.95)), 'mean': float(v.mean()),
            'med': float(np.median(v)), 'win': float(np.mean(v > 0) * 100), 'cautious': float(v.sum() - gs.std() * np.sqrt(len(ug))) if len(ug) > 1 else -1e9}


def line(name, rows, k='tgt'):
    s = stats(rows, k)
    if s is None:
        return f'   {name:<44} no trades'
    return (f'   {name:<44}{s["n"]:>5} trades {s["games"]:>4} games  ${s["tot"]:+8,.0f} (90% {s["lo"]:+,.0f}..{s["hi"]:+,.0f})  ${s["mean"]:+6.2f} per $100  '
            f'median ${s["med"]:+6.2f}  win {s["win"]:.0f}%')


def rule_pred(rule):
    return lambda r: (r['lag'] == rule['lag'] and r['room'] >= rule['room'] and r['kspread'] <= rule['kspread'] and r['conf'] >= rule['conf']
                      and r['flow'] and r['min_live'] >= rule.get('min_live', 0))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--tag', default='mac')
    ap.add_argument('--since', type=int, default=0)
    ap.add_argument('--rule')
    ap.add_argument('--workers', type=int, default=24)
    a = ap.parse_args()
    db = W.connect(a.db)
    first = db.execute('SELECT MIN(id) FROM kalshi_quotes WHERE book_ask IS NOT NULL AND est = 0').fetchone()[0]
    t_book = db.execute('SELECT ts_ms FROM kalshi_quotes WHERE id = ?', (first,)).fetchone()[0]
    since = max(a.since, t_book)
    pool_t = np.array([r[0] for r in db.execute('SELECT ts_ms FROM kalshi_quotes WHERE id >= ? AND est = 0 AND book_ask IS NOT NULL', (first,))], np.int64)
    pool_t = np.sort(pool_t[pool_t >= since])
    gaps = np.diff(pool_t)
    f = lambda x: dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).strftime('%d %b %H:%M')
    games = [r[0] for r in db.execute("SELECT DISTINCT e.match_id FROM book_events e JOIN matches m ON m.id = e.match_id WHERE e.book = 'kalshi' AND m.start_ms >= ? "
                                      "AND e.match_id IN (SELECT match_id FROM book_events WHERE book = 'polymarket')", (since - 5 * 3_600_000,))]
    print(f'[{a.tag}] Kalshi order book recorded {f(pool_t[0])} -> {f(pool_t[-1])} UTC; feed silent > 2 s for {gaps[gaps > 2000].sum() / 3.6e6:.1f} h of '
          f'{(pool_t[-1] - pool_t[0]) / 3.6e6:.1f} h; {len(games)} games on both venues', flush=True)
    R = []
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(game, [(a.db, m, since) for m in games], chunksize=2)):
            R += r
            if (k + 1) % 100 == 0:
                print(f'  {k + 1} of {len(games)} games, {len(R)} decisions', flush=True)

    def flowing(a_, b_):
        i, j = np.searchsorted(pool_t, a_ - 2000), np.searchsorted(pool_t, b_ + 2000, side='right')
        seg = pool_t[i:j]
        k = np.searchsorted(pool_t, a_, side='right') - 1
        return bool(len(seg) >= 2 and np.all(np.diff(seg) <= 2000) and k >= 0 and a_ - pool_t[k] <= 2000 and seg[-1] >= b_ - 2000)
    for r in R:
        r['flow'] = flowing(r['t'] - 5000, r['buy_t'] + 300)
    with open(os.path.join(a.signals, f'kalshi2_{a.tag}.pkl'), 'wb') as fh:
        pickle.dump(R, fh)
    out = [f'[{a.tag}] Polymarket jump -> buy Kalshi; {len(R)} decisions with Kalshi\'s order-book ask >= 3c below Polymarket; $ per $100 order, Kalshi fees paid', '']
    if a.rule:
        rules = json.load(open(a.rule, encoding='utf-8'))
        out.append('FROZEN RULES from the other recording, applied unchanged (one position a game):')
        for name, rule in rules.items():
            tk = place([r for r in R if rule_pred(rule)(r)])
            out.append(line(f'{name}: {rule}', tk, rule['exit']).replace('   ', '   ', 1))
            for day in sorted({r['day'] for r in tk}):
                out.append(line(f'      {day}', [r for r in tk if r['day'] == day], rule['exit']))
            for sp in sorted({r['sport'] for r in tk}, key=lambda s: -sum(1 for r in tk if r['sport'] == s))[:5]:
                out.append(line(f'      {sp}', [r for r in tk if r['sport'] == sp], rule['exit']))
        out.append('')
    base = [r for r in R if r['lag'] == 1 and r['kspread'] <= 2.01]
    out.append('Decide 1 s after the jump, Kalshi spread <= 2c, exit tgt (every decision, overlaps not removed):')
    for rm in (5, 10):
        S = [r for r in base if r['room'] >= rm]
        out.append(line(f'ask >= {rm}c below: feed flowing', [r for r in S if r['flow']]))
        out.append(line(f'ask >= {rm}c below: feed had a hole', [r for r in S if not r['flow']]))
    F10 = [r for r in base if r['room'] >= 10 and r['flow']]
    out.append('Within "ask >= 10c below, feed flowing":')
    for name, pred in (('bookmakers moved the same way (conf >= 1)', lambda r: r['conf'] >= 1), ('no bookmaker moved (conf = 0)', lambda r: r['conf'] == 0),
                       ('Polymarket feed on time (< 0.5 s late)', lambda r: r['pm_late'] <= 500), ('Polymarket feed late', lambda r: r['pm_late'] > 500),
                       ('Polymarket spread <= 2c', lambda r: r['pm_spread'] <= 2.01), ('sizes known', lambda r: r['sizes_known']),
                       ('this contract quoted in the last 10 s', lambda r: r['k_age'] <= 10), ('this contract quiet > 10 s', lambda r: r['k_age'] > 10),
                       ('after the first 15 min live', lambda r: r['min_live'] >= 15), ('first 15 min live', lambda r: r['min_live'] < 15),
                       ('ask 10-20c below', lambda r: r['room'] < 20), ('ask 20c+ below', lambda r: r['room'] >= 20),
                       ('Kalshi price 20-80c', lambda r: 0.2 <= r['kbuy'] <= 0.8), ('football', lambda r: r['sport'] == 'football'), ('basketball', lambda r: r['sport'] == 'basketball'),
                       ('cs2', lambda r: r['sport'] == 'cs2'), ('hockey', lambda r: r['sport'] == 'hockey')):
        out.append(line('   ' + name, [r for r in F10 if pred(r)]))
    out.append('Exits on the same decisions:')
    for ex in EXITS:
        out.append(line('   ' + ex, F10, ex))
    out.append('By day:')
    for day in sorted({r['day'] for r in F10}):
        out.append(line('   ' + day, [r for r in F10 if r['day'] == day]))
    if not a.rule:
        best = None
        for lag, rm, ks, cf, ex in itertools.product((1, 5), (5, 8, 10, 15), (1.01, 2.01), (0, 1), EXITS):
            rule = {'lag': lag, 'room': rm, 'kspread': ks, 'conf': cf, 'exit': ex}
            tk = place([r for r in R if rule_pred(rule)(r)])
            s = stats(tk, ex)
            if s and s['n'] >= 60 and (best is None or s['cautious'] > best[0]):
                best = (s['cautious'], rule, s)
        rules = {'plain': {'lag': 1, 'room': 10, 'kspread': 2.01, 'conf': 0, 'exit': 'tgt'}}
        if best:
            rules['picked'] = best[1]
        out.append('')
        out.append('Rules frozen for the test on the other recording (one position a game):')
        for name, rule in rules.items():
            out.append(line(f'{name}: {rule}', place([r for r in R if rule_pred(rule)(r)]), rule['exit']))
        with open(os.path.join(a.signals, 'kalshi_rule.json'), 'w', encoding='utf-8') as fh:
            json.dump(rules, fh)
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, f'kalshi2_{a.tag}.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


if __name__ == '__main__':
    main()
