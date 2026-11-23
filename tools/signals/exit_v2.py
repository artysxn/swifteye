"""Instant-exit model, round 2: bot-like entries, the entry's quality as input, and evidence before selling.

  python tools/signals/exit_v2.py build --db X --out DIR [--workers 28]
  python tools/signals/exit_v2.py test  --db X --out DIR [--first 2026-11-18 --last 2026-11-23]

Round 1 (exit_vs_hold.py) sold after a median 16 s and lost to the old bot's holding: it learned on every bookmaker
jump (bad entries, -5.45c held), so "sell fast" was right for them and wrong for the bot's filtered entries.

ENTRIES like the snipe bot's trigger: >= 2 bookmaker families moved >= 1 point in 5 s the same way, Polymarket's
spread <= 2c, the families sit >= 0.12 log-odds (about 3 points at 50%) from Polymarket in that direction, and
Polymarket has not followed in the last 3 s. Buy 1 s later; the plan: sell at +TP net, else at the give-up G.
POSITION inputs: time held, net now, best net so far, drawdown, entry spread, and the entry's quality: the gap to the
families at entry, how many families moved, the largest family move (all signed with the trade's direction).
DECISION: sell when the predicted delta = (plan's end - sell now) has stayed below -m for P seconds, and only
after a minimum hold M; m, M, P chosen on the validation games (the last 20% before the day), never on the test.
TEST: rolling walk-forward on every snipe trade (bot_trades), each valued as planned (its own exit within its
give-up, else the token's sell price at the give-up; no settlement luck), checked every 2 s."""
import argparse
import datetime as dt
import json
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import torch
from torch import nn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import features as F  # noqa: E402
import waves as W  # noqa: E402
from exit_vs_hold import Net, book_rows, fee, held, run  # noqa: E402

TP, G, CHECK, POS_GAP, L = 0.03, 900, 20, 300, 1
COL = {k: i for i, k in enumerate(F.NAMES)}
PSCALE = np.array([G, 5, 5, 5, 2, 0.2, 3, 0.2], dtype=np.float32)
GRID_M = (0.0, 1.0, 2.0, 3.0, 4.0, 6.0)
GRID_MIN = (0, 30, 60, 120)
GRID_P = (0, 20, 40)


def entry_quality(X, i, d):
    """(gap to the families in the trade's direction, families moved that way in 5 s, largest family move that way)."""
    gap = X[i, COL['fam_gap_max']] if d > 0 else -X[i, COL['fam_gap_min']]
    moved = X[i, COL['fam_up5']] if d > 0 else X[i, COL['fam_dn5']]
    jump = X[i, COL['fam_r10_max']] if d > 0 else -X[i, COL['fam_r10_min']]
    return float(gap), float(moved), float(jump)


def build_one(args):
    path, m, sport, start = args
    db = W.connect(path)
    out = []
    pm, bk = W.load(db, m)
    br = book_rows(db, m)
    for o in (['home', 'away'] if sport in W.THREE_WAY else ['home']):
        g = F.build(pm, bk, o, sport, start, sport in W.THREE_WAY, br, full=True)
        if g is None:
            continue
        X, n = g['X'], len(g['ts'])
        last = -10 ** 9
        for i in np.flatnonzero(g['evt'] & g['ok']):
            if i - last < POS_GAP or i + L + G >= n:
                continue
            up5, dn5 = X[i, COL['fam_up5']], X[i, COL['fam_dn5']]
            d = 1 if up5 >= 2 and up5 > dn5 else (-1 if dn5 >= 2 and dn5 > up5 else 0)
            if not d or X[i, COL['spread']] > 2.0:
                continue
            gap, moved, jump = entry_quality(X, i, d)
            if gap < 0.12 or X[i, COL['pm_r3']] * d > 0.02:
                continue
            e = i + L
            buy = g['ask'][e] if d > 0 else 1 - g['bid'][e]
            if not np.isfinite(buy):
                continue
            last = i
            cost = buy + fee(buy)
            sell = g['bid'] if d > 0 else 1 - g['ask']
            sp = sell[e + 1:e + 1 + G]
            v = (sp - fee(sp) - cost) * 100
            hit = np.flatnonzero(np.nan_to_num(v, nan=-99) >= TP * 100)
            end = hit[0] if len(hit) else G - 1
            cont = v[end]
            if not np.isfinite(cont):
                continue
            best = -1e9
            for k in range(CHECK, end + 1, CHECK):
                now = v[k - 1]
                if not np.isfinite(now):
                    continue
                best = max(best, np.nanmax(v[:k]))
                out.append((m, int(g['ts'][e + k]), int(g['ts'][e]), int(g['ts'][e + end + 1]), d, X[e + k],
                            np.array([k, now, best, best - now, (g['ask'][e] - g['bid'][e]) * 100, gap, moved, jump], dtype=np.float32), now, cont))
    return out


def cmd_build(a):
    gl = W.games(W.connect(a.db))
    print(f'{len(gl)} games', flush=True)
    rows, t0 = [], time.time()
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(build_one, [(a.db, m, s, st) for m, s, _, _, st in gl], chunksize=2)):
            rows += r
            if (k + 1) % 400 == 0:
                print(f'  {k + 1} games, {len(rows)} checks, {time.time() - t0:.0f} s', flush=True)
    starts = {m: st or 0 for m, _, _, _, st in gl}
    d = {'game': np.array([r[0] for r in rows], np.int64), 't': np.array([r[1] for r in rows], np.int64), 't_entry': np.array([r[2] for r in rows], np.int64),
         't_end': np.array([r[3] for r in rows], np.int64), 'dir': np.array([r[4] for r in rows], np.int8), 'X': np.stack([r[5] for r in rows]).astype(np.float32),
         'pos': np.stack([r[6] for r in rows]), 'now': np.array([r[7] for r in rows], np.float32), 'cont': np.array([r[8] for r in rows], np.float32)}
    d['start'] = np.array([starts[g] for g in d['game']], np.int64)
    np.savez_compressed(os.path.join(a.out, 'exit_v2.npz'), **d)
    npos = len(np.unique(np.stack([d['game'], d['t_entry'], d['dir']], 1), axis=0))
    print(f"{len(d['t'])} checks of {npos} bot-like positions from {len(np.unique(d['game']))} games; held to the plan they make {np.mean(d['cont'][np.unique(np.stack([d['game'], d['t_entry'], d['dir']], 1), axis=0, return_index=True)[1]]):+.2f}c; "
          f"holding beat selling at {100 * np.mean(d['cont'] > d['now']):.0f}% of checks; {time.time() - t0:.0f} s")


def inputs(Z, pos, d):
    dd = d.astype(np.float32)[:, None]
    return np.concatenate([Z, Z * dd, pos / PSCALE, dd], axis=1).astype(np.float32)


def prep(D, idx):
    """The validation positions in check order, prepared once (the rule search reuses them)."""
    held_s, cont, now = D['pos'][idx][:, 0], D['cont'][idx], D['now'][idx]
    g, te, dr = D['game'][idx], D['t_entry'][idx], D['dir'][idx]
    order = np.lexsort((held_s, dr, te, g))
    new_pos = np.r_[True, (g[order][1:] != g[order][:-1]) | (te[order][1:] != te[order][:-1]) | (dr[order][1:] != dr[order][:-1])]
    return {'order': order, 'new': new_pos, 'held': held_s[order], 'cont': cont[order], 'now': now[order]}


def policy_value(pred, ctx, m, M, P):
    """Mean per position: sell at the first check (time held >= M) where pred < -m has held for P seconds, else the plan."""
    pr = pred[ctx['order']]
    new, held_s, cont, now = ctx['new'], ctx['held'], ctx['cont'], ctx['now']
    vals, cur, below, done = [], 0.0, None, False
    for j in range(len(pr)):
        if new[j]:
            if j:
                vals.append(cur)
            cur, below, done = float(cont[j]), None, False
        if done:
            continue
        if pr[j] < -m:
            below = held_s[j] if below is None else below
            if held_s[j] >= M and held_s[j] - below >= P:
                cur, done = float(now[j]), True
        else:
            below = None
    if len(pr):
        vals.append(cur)
    return float(np.mean(vals)) if vals else -1e9


def train_cut(D, before, dev, epochs=8):
    keep = np.flatnonzero(D['t_end'] < before)
    gstart = {int(g): int(s) for g, s in zip(D['game'][keep], D['start'][keep])}
    order = sorted(gstart, key=lambda g: gstart[g])
    val_g = set(order[int(0.8 * len(order)):])
    is_val = np.isin(D['game'][keep], list(val_g))
    val_start = min(gstart[g] for g in val_g)
    tr = keep[~is_val & (D['t_end'][keep] < val_start)]
    va = keep[is_val]
    mu, sd = D['X'][tr].mean(0), D['X'][tr].std(0)
    sd[sd < 1e-6] = 1
    Z = lambda idx: np.clip((D['X'][idx] - mu) / sd, -10, 10).astype(np.float32)
    Xtr, ytr = inputs(Z(tr), D['pos'][tr], D['dir'][tr]), np.clip(D['cont'][tr] - D['now'][tr], -30, 30).astype(np.float32)
    torch.manual_seed(3)
    model = Net(Xtr.shape[1]).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    Xt, yt = torch.from_numpy(Xtr).to(dev), torch.from_numpy(ytr).to(dev)
    for ep in range(epochs):
        model.train()
        perm = torch.randperm(len(Xt), device=dev)
        for i in range(0, len(Xt), 4096):
            b = perm[i:i + 4096]
            opt.zero_grad()
            nn.functional.huber_loss(model(Xt[b]), yt[b], delta=3.0).backward()
            opt.step()
    model.eval()
    pv = run(model, inputs(Z(va), D['pos'][va], D['dir'][va]), dev)
    ctx = prep(D, va)
    plan = policy_value(np.full(len(va), 1e9), ctx, 0, 0, 0)   # never sells early: the plan itself
    best = (None, plan)
    for m in GRID_M:
        for M in GRID_MIN:
            for P in GRID_P:
                v = policy_value(pv, ctx, m, M, P)
                if v > best[1]:
                    best = ((m, M, P), v)
    return model, mu, sd, best[0], plan, best[1], len(tr), len(va)


def planned_value(db, mid, d):
    """A trade as its plan meant it to end (no settlement): its own exit within its give-up, else the token's sell
    price at the give-up moment, fees paid. Cents per share, or None."""
    buy, gu = d['ts'], (d.get('give_up_s') or 1800) * 1000
    if d.get('pnl') is None or not d.get('contracts') or d.get('entry') is None:
        return None
    if d.get('exit_ts') is not None and d['exit_ts'] <= buy + gu + 60_000:
        return d['pnl'] / d['contracts'] * 100
    up = (d.get('dir') or 1) > 0
    q = db.execute('SELECT bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1', (mid, d['outcome'], buy + gu)).fetchone()
    px = None if not q else (q[0] if up else (None if q[1] is None else 1 - q[1]))
    if px is None:
        return None
    e = d['entry']
    return (px - fee(px) - (e + fee(e))) * 100


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    D = dict(np.load(os.path.join(a.out, 'exit_v2.npz'), allow_pickle=False))
    db = W.connect(a.db)
    trades = db.execute("SELECT t.id, t.bot, t.real, t.match_id, t.data, m.sport, m.start_ms FROM bot_trades t JOIN matches m ON m.id = t.match_id WHERE t.bot != 'favs'").fetchall()
    day = dt.datetime.fromisoformat(a.first).replace(tzinfo=dt.timezone.utc)
    end = dt.datetime.fromisoformat(a.last).replace(tzinfo=dt.timezone.utc)
    res, cache = [], {}
    while day <= end:
        d0, d1 = int(day.timestamp() * 1000), int((day + dt.timedelta(days=1)).timestamp() * 1000)
        todays = [t for t in trades if t[6] and d0 <= t[6] < d1]
        if not todays:
            day += dt.timedelta(days=1)
            continue
        model, mu, sd, rule, plan_v, rule_v, ntr, nva = train_cut(D, d0, dev)
        if rule is None:
            print(f'== {day:%d %b}: on {ntr} checks no early-sale rule beat the plan on validation ({plan_v:+.2f}c): the model holds like the bot; {len(todays)} trades', flush=True)
        else:
            print(f'== {day:%d %b}: on {ntr} checks; validation picks: sell when holding is predicted to lose over {rule[0]:.0f}c for {rule[2]} s, '
                  f'after at least {rule[1]} s ({rule_v:+.2f}c vs the plan {plan_v:+.2f}c); {len(todays)} trades', flush=True)
        for tid, bot, real, mid, data, sport, start in todays:
            r = json.loads(data)
            o, buy_ms, dirn, entry = r.get('outcome'), r.get('ts'), (1 if (r.get('dir') or 1) > 0 else -1), r.get('entry')
            bot_v = planned_value(db, mid, r)
            if not o or not buy_ms or entry is None or bot_v is None:
                continue
            if (mid, o) not in cache:
                pm, bk = W.load(db, mid)
                g = F.build(pm, bk, o, sport or '', start, (sport or '') in W.THREE_WAY, book_rows(db, mid), full=True)
                rb = sorted(((x[0], x[3]) for x in pm if x[2] == o), key=lambda q: q[0])
                ra = sorted(((x[0], x[4]) for x in pm if x[2] == o), key=lambda q: q[0])
                cache[(mid, o)] = (g, rb, ra)
            g, rb, ra = cache[(mid, o)]
            sold_at, mod_v = None, bot_v
            if g is not None and rule is not None:
                e = int(np.searchsorted(g['ts'], buy_ms, side='right') - 1)
                if 0 <= e and buy_ms - g['ts'][e] <= 5000:
                    gu = (r.get('give_up_s') or 1800)
                    horizon = min(gu, ((r.get('exit_ts') or (buy_ms + gu * 1000)) - buy_ms) // 1000)
                    secs = g['ts'][e] + 1000 * np.arange(1, horizon + 1)
                    hb, ha = held(rb, secs), held(ra, secs)
                    sp = hb if dirn > 0 else 1 - ha
                    v = (sp - fee(sp) - (entry + fee(entry))) * 100
                    ks = np.arange(2, horizon + 1, 2)
                    ks = ks[(e + ks < len(g['ts'])) & np.isfinite(v[ks - 1])]
                    if len(ks):
                        gap, moved, jump = entry_quality(g['X'], e - L if e >= L else e, dirn)
                        best = np.maximum.accumulate(np.nan_to_num(v, nan=-1e9))
                        pos = np.stack([ks, v[ks - 1], best[ks - 1], best[ks - 1] - v[ks - 1], np.full(len(ks), (g['ask'][e] - g['bid'][e]) * 100),
                                        np.full(len(ks), gap), np.full(len(ks), moved), np.full(len(ks), jump)], 1).astype(np.float32)
                        Zk = np.clip((g['X'][e + ks] - mu) / sd, -10, 10).astype(np.float32)
                        pred = run(model, inputs(Zk, pos, np.full(len(ks), dirn, np.int8)), dev)
                        m_, M_, P_ = rule
                        below = None
                        for j, k in enumerate(ks):
                            if pred[j] < -m_:
                                below = k if below is None else below
                                if k >= M_ and k - below >= P_:
                                    sold_at, mod_v = int(k), float(v[k - 1])
                                    break
                            else:
                                below = None
            res.append({'id': tid, 'name': bot, 'real': bool(real), 'day': day.strftime('%d %b'), 'bot': bot_v, 'model': mod_v, 'sold_at_s': sold_at})
        day += dt.timedelta(days=1)
    b = np.array([x['bot'] for x in res])
    mo = np.array([x['model'] for x in res])
    early = [x for x in res if x['sold_at_s'] is not None]
    diff = mo - b
    rng = np.random.default_rng(2)
    boot = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(5000)]
    lines = [f'\nSnipe-bot trades, each valued as planned (no settlement), judged day by day by exit model v2 trained only on earlier games: {len(res)} trades.',
             f'  the bot (sell when caught up, else at its give-up)   mean {b.mean():+.2f}c a share  win {100 * np.mean(b > 0):.0f}%',
             f'  v2: sell when it calls a failure (with its patience), else the bot\'s exit   mean {mo.mean():+.2f}c  win {100 * np.mean(mo > 0):.0f}%',
             f'  v2 minus the bot {diff.mean():+.2f}c a trade (90% range {np.quantile(boot, 0.05):+.2f}..{np.quantile(boot, 0.95):+.2f})',
             f"  sold early in {len(early)} trades (median after {np.median([x['sold_at_s'] for x in early]) if early else float('nan'):.0f} s): there the bot made "
             f"{np.mean([x['bot'] for x in early]) if early else float('nan'):+.2f}c, v2 {np.mean([x['model'] for x in early]) if early else float('nan'):+.2f}c"]
    for dd in sorted({x['day'] for x in res}):
        rs = [x for x in res if x['day'] == dd]
        lines.append(f"    {dd}: {len(rs)} trades, bot {np.mean([x['bot'] for x in rs]):+.2f}c, v2 {np.mean([x['model'] for x in rs]):+.2f}c, sold early {sum(x['sold_at_s'] is not None for x in rs)}")
    text = '\n'.join(lines)
    print(text)
    with open(os.path.join(a.out, 'exit_v2.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    with open(os.path.join(a.out, 'exit_v2.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, default=float)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'test'])
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--workers', type=int, default=28)
    ap.add_argument('--first', default='2026-11-18')
    ap.add_argument('--last', default='2026-11-23')
    a = ap.parse_args()
    (cmd_build if a.cmd == 'build' else cmd_test)(a)


if __name__ == '__main__':
    main()
