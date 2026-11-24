"""Does the live bot see what the backtest saw? And does the backtest use anything from the future?

  python tools/maker/parity.py --db X [--games 12] [--per 6]

For recorded games: the backtest's way (the whole recording at once, then the row of a second) against the live way
(only the rows up to that second, feat.at). Every one of the model's 173 inputs is compared at random in-play
seconds. An input that differs is one the whole-recording build fills with knowledge of LATER data: such an input
would make the backtest look better than a live bot can be. Also prints how long one live decision takes."""
import argparse
import os
import sqlite3
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feat as FT  # noqa: E402


def load(db, m):
    pm = db.execute('SELECT ts_ms, server_ts_ms, outcome, bid, ask, last, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)).fetchall()
    bk = []
    for table in ('changes', 'changes_research'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            bk += db.execute(f"SELECT ts_ms, book, outcome, new_price FROM {table} WHERE match_id = ? AND market = '1x2' AND book NOT IN ('polymarket', 'kalshi') ORDER BY ts_ms", (m,)).fetchall()
    return pm, bk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--games', type=int, default=12)
    ap.add_argument('--per', type=int, default=6)
    a = ap.parse_args()
    db = sqlite3.connect('file:' + a.db + '?mode=ro', uri=True, timeout=30)
    rows = db.execute("""SELECT m.id, m.sport, m.start_ms FROM matches m WHERE m.sport IN ('tennis','basketball','cs2','hockey','valorant','dota2','amfootball') AND m.start_ms IS NOT NULL
                         AND m.id IN (SELECT match_id FROM book_events WHERE book = 'polymarket') ORDER BY m.start_ms""").fetchall()
    rng = np.random.default_rng(5)
    pick = [rows[i] for i in rng.choice(len(rows), min(a.games * 4, len(rows)), replace=False)]
    worst = np.zeros(len(FT.FN) - 6)
    nbad = np.zeros(len(FT.FN) - 6)
    checks, done, times, ns = 0, 0, [], []
    for m, sport, start in pick:
        if done >= a.games:
            break
        pm, bk = load(db, m)
        if len(pm) < 500 or not bk:
            continue
        full = FT.grid(pm, bk, sport, start)
        if full is None:
            continue
        live = np.flatnonzero(full['X'][:, FT.C['inplay']] > 0.5)
        if len(live) < 1500:
            continue
        done += 1
        for r in rng.choice(live[600:], a.per, replace=False):
            T = int(full['ts'][r])
            t0 = time.time()
            d = FT.at(pm, bk, sport, start, T)
            times.append(time.time() - t0)
            ns.append(r)
            if d is None:
                continue
            assert d['grid_ts'] == T, (d['grid_ts'], T)
            for name, a_, b_ in (('buy', full['FE'][r], d['row_buy'][:-6]), ('sell', full['FEm'][r], d['row_sell'][:-6])):
                diff = np.abs(a_.astype(np.float64) - b_.astype(np.float64))
                tol = 1e-4 + 1e-4 * np.abs(a_)
                worst = np.maximum(worst, diff)
                nbad += diff > tol
            checks += 1
        print(f'  game {m} ({sport}): {len(pm):,} Polymarket rows, {len(bk):,} bookmaker rows, grid {len(full["ts"]):,} s', flush=True)
    print(f'\n{checks} moments in {done} games; each compared for a buy and a sale ({checks * 2} rows of {len(worst)} inputs)')
    bad = np.flatnonzero(nbad > 0)
    if not len(bad):
        print('EVERY input is identical: the live bot sees exactly what the backtest saw, and no input of the backtest holds later data.')
    else:
        print(f'{len(bad)} inputs differ (the whole-recording build knows something a live bot does not):')
        for j in bad[np.argsort(-nbad[bad])]:
            print(f'   {FT.FN[j]:<18} differs in {int(nbad[j])} of {checks * 2} rows, largest gap {worst[j]:.4f}')
    print(f'one live decision (features of the whole game so far): median {np.median(times):.2f} s, slowest {max(times):.2f} s; grid length median {int(np.median(ns)):,} s')


if __name__ == '__main__':
    main()
