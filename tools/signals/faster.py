"""What would a faster bookmaker feed be worth? (python tools/signals/faster.py --db X --signals DIR)

Some feeds stamp the moment the bookmaker published a price: Kambi (unibet; paf / leovegas are the same feed; 1 s
resolution, so the stamp + 0.5 s is used), SportyBet (ms) and Betcity (ms, but in UTC+3: 3 h are taken off). Our copy
arrives ~2.1 s (Kambi), ~2.2 s (SportyBet) and ~3.4 s (Betcity) later (median, 23 Nov). A faster feed is worth what a
bot could do at the publish time instead of at our receive time.

EVENTS: every 1X2 / winner jump of these books on a game Polymarket lists (home / away): the book's implied
probability moved >= 0.12 log-odds against its level 10 s earlier (suspensions bridged, so a goal counts), first row
over the line, one per game / outcome / direction per minute. DECISIONS at (a) publish + 0.25 s (an ideal feed),
(b) receive + 0.25 s, (c) receive + 1 s (today's bot). A decision is ELIGIBLE when Polymarket has not followed yet
(its mid moved < 0.02 log-odds the jump's way since 10 s before the publish time); a bot can only trade eligible ones.
TRADE: buy the token at the ask (taker), target = Polymarket's mid 1 s before the publish time moved by the book's jump
(PM follows fully); sell into the bid at the target (taker) or REST a sell there (maker: filled when the bid goes a
tick past it, no fee); else sell into the bid at 5 min. Dollars per $100 order with book depth (maker.trade);
collapsed tokens as booklead.py. All days 17-23 Nov, a fixed rule, no model: an estimate of the edge, not a tuned bot."""
import argparse
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402
from booklead import FILL_S, fee, held  # noqa: E402
from maker import TICK, fin, trade  # noqa: E402

BOOKS = {'unibet': 500, 'sportybet': 0, 'betcity': -10_800_000}     # ms added to the stamp to get the publish time
H = 300
JUMP = 0.12
FAM = {'unibet': 'kambi', 'paf': 'kambi', 'leovegas': 'kambi', 'linebet': '1x', 'melbet': '1x', 'betwinner': '1x', 'megapari': '1x', 'paripesa': '1x',
       '22bet': '1x', '22bit': '1x', '1xbit': '1x', 'fonbet': 'fonbet', 'pari': 'fonbet', 'vinn': 'altenar', 'betking': 'altenar'}
DEC = (('publish + 0.25 s (ideal feed)', 'src', 250), ('receive + 0.25 s', 'recv', 250), ('receive + 1 s (today)', 'recv', 1000))
logit = lambda p: np.log(p / (1 - p))


def game(args):
    path, m = args
    db = W.connect(path)
    pm = defaultdict(list)
    for r in db.execute('SELECT ts_ms, outcome, bid, ask, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)):
        pm[r[1]].append(r)
    if not pm:
        return []
    ser = {}
    for o, rows in pm.items():
        arr = np.array([[x[0]] + [np.nan if v is None else v for v in x[2:]] for x in rows], dtype=float)
        ser[o] = (arr[:, 0].astype(np.int64),) + tuple(arr[:, k] for k in range(1, 7))
    out = []
    sport = (db.execute('SELECT sport FROM matches WHERE id = ?', (m,)).fetchone() or ('?',))[0]
    allb = defaultdict(list)
    for ts_, book_, o_, pr_ in db.execute("SELECT ts_ms, book, outcome, new_price FROM changes WHERE match_id = ? AND book != 'polymarket' AND market IN ('1x2', 'x12', 'X12') "
                                          "AND outcome IN ('home', 'away') AND new_price > 1 ORDER BY ts_ms", (m,)):
        allb[(book_, o_)].append((ts_, 1 / pr_))
    allb = {k: (np.array([x[0] for x in v], np.int64), np.array([x[1] for x in v])) for k, v in allb.items()}

    def confirm(o, d, ref_t, td, fam0):
        """Other bookmaker families (not the jumping one) whose price had moved >= 0.05 log-odds the same way, as received by td."""
        fams = set()
        for (book_, o_), (tt, pp) in allb.items():
            if o_ != o or FAM.get(book_, book_) == fam0:
                continue
            k1, k0 = np.searchsorted(tt, td, side='right') - 1, np.searchsorted(tt, ref_t, side='right') - 1
            if k1 >= 0 and k0 >= 0 and d * (logit(np.clip(pp[k1], 0.01, 0.99)) - logit(np.clip(pp[k0], 0.01, 0.99))) >= 0.05:
                fams.add(FAM.get(book_, book_))
        return len(fams)
    q = ('SELECT ts_ms, book_ts_ms, book, outcome, new_price, kind FROM changes WHERE match_id = ? AND book IN (%s) AND market IN (\'1x2\', \'x12\', \'X12\') '
         'AND outcome IN (\'home\', \'away\') ORDER BY ts_ms' % ','.join('?' * len(BOOKS)))
    bk = defaultdict(list)
    for ts, bts, book, o, price, kind in db.execute(q, (m, *BOOKS)):
        bk[(book, o)].append((ts, bts, price, kind))
    for (book, o), rows in bk.items():
        if o not in ser:
            continue
        lt, lb, la, bs, as_, bd, ad = ser[o]
        tp = [(ts, bts, 1 / price, kind) for ts, bts, price, kind in rows if price and price > 1 and kind in ('open', 'move', 'resume')]
        if len(tp) < 3:
            continue
        T = np.array([x[0] for x in tp], np.int64)
        P = np.array([x[2] for x in tp])
        last = {}
        for i in range(1, len(tp)):
            ts, bts, p, kind = tp[i]
            if bts is None or kind == 'open' or not 0.02 < p < 0.98:   # an open after earlier rows = a recording gap
                continue
            k = np.searchsorted(T, ts - 10_000, side='right') - 1
            if k < 0:
                continue
            p_ref = P[k]
            jump = logit(p) - logit(np.clip(p_ref, 0.02, 0.98))
            if abs(jump) < JUMP:
                continue
            d = 1 if jump > 0 else -1
            if ts - last.get(d, -10 ** 15) < 60_000:
                continue
            last[d] = ts
            src = bts + BOOKS[book]
            if not 0 <= ts - src <= 15_000:
                continue
            pre = held(lt, (lb + la) / 2, np.array([src - 1000, src - 10_000]))
            if not np.all(np.isfinite(pre)) or not 0.01 < pre[0] < 0.99:
                continue
            target_yes = np.clip(pre[0] + (p - p_ref), 0.01, 0.99)
            ev = {'game': m, 't': int(ts), 'sport': sport, 'dir': d, 'book': book, 'jump': float(abs(jump)), 'delay': int(ts - src), 'size': float(abs(p - p_ref)) * 100, 'dec': []}
            for lab, base, add in DEC:
                td = (src if base == 'src' else ts) + add
                secs = td + 1000 * np.arange(0, H + FILL_S + 1)
                yb, ya, ybs, yas, ybd, yad = (held(lt, v, secs) for v in (lb, la, bs, as_, bd, ad))
                if d > 0:
                    tb, ta, tbs, tbd, tas, tad, Tt = yb, ya, ybs, ybd, yas, yad, target_yes
                else:
                    tb, ta, tbs, tbd, tas, tad, Tt = 1 - ya, 1 - yb, yas, yad, ybs, ybd, 1 - target_yes
                mid0 = (yb[0] + ya[0]) / 2
                if not np.isfinite(mid0) or not np.isfinite(ta[0]) or not 0 < ta[0] < 1:
                    ev['dec'].append(None)
                    continue
                followed = d * (logit(np.clip(mid0, 0.01, 0.99)) - logit(np.clip((pre[1]), 0.01, 0.99)))
                eligible = followed < 0.02
                buy, s_in, cap_in = float(ta[0]), fin(tas[0]), max(fin(tad[0]), fin(tas[0]))
                idx = np.where(np.isfinite(tb), np.arange(len(tb)), len(tb))
                nxt = np.minimum.accumulate(idx[::-1])[::-1]
                fol = [d * ((yb[h] + ya[h]) / 2 - mid0) / max(abs(p - p_ref), 1e-3) for h in (10, 30, 60)]   # share of the book's jump PM made
                res = {'spread': float(ta[0] - tb[0]) * 100, 'price': buy, 'confirm': confirm(o, d, src - 10_000, td, FAM.get(book, book)),
                       'f10': float(fol[0]), 'f30': float(fol[1]), 'f60': float(fol[2]),
                       'eligible': bool(eligible), 'gross': float(d * (((yb[H] + ya[H]) / 2) - mid0) * 100) if np.isfinite(yb[H] + ya[H]) else np.nan,
                       'cost': float(((ta[0] - tb[0]) / 2 + (np.nan_to_num(ta[H] - tb[H], nan=0.02)) / 2 + fee(buy) * 2) * 100)}
                for eout in 'TM':
                    seg = np.nan_to_num(tb[1:H + 1], nan=-1.0)
                    hit = np.flatnonzero(seg >= (Tt - 1e-9 if eout == 'T' else Tt + TICK - 1e-9))
                    if len(hit):
                        kx = 1 + int(hit[0])
                        if eout == 'T':
                            sell, taker_out, s_out, cap_out = float(tb[kx]), True, fin(tbs[kx]), max(fin(tbd[kx]), fin(tbs[kx]))
                        else:
                            sell, taker_out, s_out, cap_out = float(Tt), False, np.inf, max(fin(tbd[kx]), fin(tbs[kx]))
                    else:
                        jj = nxt[H]
                        if jj < len(tb) and jj - H <= FILL_S:
                            sell, s_out, cap_out = float(tb[jj]), fin(tbs[jj]), max(fin(tbd[jj]), fin(tbs[jj]))
                        else:
                            sell, s_out, cap_out = 0.0, np.inf, np.inf
                        taker_out = True
                    res[f'usd_{eout}'] = trade(buy, True, s_in, cap_in, sell, taker_out, s_out, cap_out)[0]
                ev['dec'].append(res)
            out.append(ev)
    return out


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=24)
    a = ap.parse_args()
    db = W.connect(a.db)
    games = [r[0] for r in db.execute('SELECT DISTINCT match_id FROM book_events WHERE book IN (%s)' % ','.join('?' * len(BOOKS)), tuple(BOOKS))]
    pmg = {r[0] for r in db.execute("SELECT DISTINCT match_id FROM book_events WHERE book = 'polymarket'")}
    games = [g for g in games if g in pmg]
    print(f'{len(games)} games with Polymarket and a time-stamped bookmaker', flush=True)
    evs = []
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(game, [(a.db, m) for m in games], chunksize=2)):
            evs += r
            if (k + 1) % 500 == 0:
                print(f'  {k + 1} games, {len(evs)} jumps', flush=True)
    out = [f'{len(evs)} bookmaker jumps (>= {JUMP} log-odds) on {len(games)} Polymarket games, 17-23 Nov. Dollars per $100 order, book depth.', '']
    for book in list(BOOKS) + ['all']:
        E = [e for e in evs if book == 'all' or e['book'] == book]
        if not E:
            continue
        dl = np.array([e['delay'] for e in E])
        out.append(f'{book}: {len(E)} jumps, our feed {np.median(dl) / 1000:.1f} s behind the publish time (median)')
        for lo, hi in ((0.12, 0.3), (0.3, 0.7), (0.7, 99)):
            sel = [e for e in E if lo <= e['jump'] < hi]
            if len(sel) < 20:
                continue
            out.append(f'   jump {lo}-{hi if hi < 99 else "up"} log-odds ({len(sel)} jumps, median {np.median([e["size"] for e in sel]):.1f} pt):')
            for k, (lab, _, _) in enumerate(DEC):
                rs = [e['dec'][k] for e in sel if e['dec'][k] is not None]
                el = [r for r in rs if r['eligible']]
                if not el:
                    out.append(f'      {lab:<30} nothing eligible')
                    continue
                g = np.array([r['gross'] for r in el])
                c = np.array([r['cost'] for r in el])
                uT = np.array([r['usd_T'] for r in el])
                uM = np.array([r['usd_M'] for r in el])
                out.append(f'      {lab:<30} PM not moved yet {len(el) / max(len(rs), 1) * 100:3.0f}%  move left {np.nanmean(g):+5.2f}c  cost {c.mean():4.2f}c  '
                           f'per $100: sell into the bid ${uT.mean():+6.2f} (total ${uT.sum():+7.0f})  limit sell ${uM.mean():+6.2f} (total ${uM.sum():+7.0f})')
        out.append('')
    text = '\n'.join(out)
    print(text)
    dump = {k: np.array([e[k] for e in evs]) for k in ('game', 't', 'sport', 'dir', 'book', 'jump', 'delay', 'size')}
    for k in range(len(DEC)):
        dump[f'ok{k}'] = np.array([e['dec'][k] is not None for e in evs])
        for f_ in ('eligible', 'gross', 'cost', 'usd_T', 'usd_M', 'spread', 'price', 'confirm', 'f10', 'f30', 'f60'):
            dump[f'{f_}{k}'] = np.array([e['dec'][k][f_] if e['dec'][k] is not None else np.nan for e in evs], float)
    np.savez_compressed(os.path.join(a.signals, 'faster_events.npz'), **dump)
    with open(os.path.join(a.signals, 'faster.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
