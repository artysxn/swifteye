"""Can a model sell the moment a trade stops going to plan, and beat the old bot's holding?

  python tools/signals/exit_vs_hold.py build --db X --out DIR [--workers 30]
  python tools/signals/exit_vs_hold.py test  --db X --out DIR [--first 2026-11-18 --last 2026-11-23]

The old snipe bot holds a position until Polymarket catches up, else until its give-up (5 / 15 / 30 min): it found
that selling as soon as a wave looks failed did worse (a fast sale locks in the spread and both fees, and the price
often comes back). The question is whether a model can tell a real failure (sell now) from a dip (hold).

BUILD. Positions like the bot's: at a bookmaker jump (a family moved >= 1 point in 5 s while Polymarket had not; at
most one per POS_GAP s per game and outcome), buy 1 s later in the jump's direction (Yes at the ask, No at 1 - bid);
the old-bot-like plan: sell at +TP net (fees paid), else at the give-up G. Every CHECK s while held:
features, the position (time held, net now, best net so far, drawdown, entry spread), now = net if sold now,
cont = net the plan ends with. Continuation value: delta = cont - now (Longstaff-Schwartz; selling when the model
says delta < -m improves on the plan, one step of policy iteration).

TEST. Rolling walk-forward: for each day D, train on positions of games that kicked off before D (purged: the
position's plan must end before D), pick m on the last 20% of them, then take every snipe-bot trade of day D's
games (bot_trades): from its buy, every 2 s, sell at once if the model says delta < -m; else keep the bot's own
exit. Both priced on the recorded book (sell at the bid of the token, fees 5% p(1-p) each side)."""
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

TP, G, CHECK, POS_GAP, L = 0.03, 900, 20, 900, 1
PSCALE = np.array([G, 5, 5, 5, 2], dtype=np.float32)


def fee(p):
    return 0.05 * p * (1 - p)


def book_rows(db, m):
    try:
        return db.execute('SELECT ts_ms, outcome, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND bid_size IS NOT NULL ORDER BY ts_ms', (m,)).fetchall()
    except Exception:
        return None


def grid(db, m, sport, start, o):
    pm, bk = W.load(db, m)
    return F.build(pm, bk, o, sport, start, sport in W.THREE_WAY, book_rows(db, m), full=True)


def held(rows, times):
    """Sample-and-hold of sorted [(t, v)] at the given times (nan before the first or where v is None)."""
    if not rows:
        return np.full(len(times), np.nan)
    lt = np.array([x[0] for x in rows], dtype=np.int64)
    lv = np.array([np.nan if x[1] is None else x[1] for x in rows], dtype=float)
    k = np.searchsorted(lt, times, side='right') - 1
    return np.where(k >= 0, lv[np.clip(k, 0, None)], np.nan)


def path_net(g, e, d, cost, n_ahead):
    """Net per share (cents) if sold at each second e+1 .. e+n_ahead (nan where no price)."""
    sell = g['bid'] if d > 0 else 1 - g['ask']
    sp = sell[e + 1:e + 1 + n_ahead]
    return (sp - fee(sp) - cost) * 100


def build_one(args):
    path, m, sport, start = args
    db = W.connect(path)
    col = {k: i for i, k in enumerate(F.NAMES)}
    out = []
    for o in (['home', 'away'] if sport in W.THREE_WAY else ['home']):
        g = grid(db, m, sport, start, o)
        if g is None:
            continue
        X, n = g['X'], len(g['ts'])
        up5, dn5, pmr = X[:, col['fam_up5']], X[:, col['fam_dn5']], X[:, col['pm_r3']]
        last = -10 ** 9
        for i in np.flatnonzero(g['evt'] & g['ok']):
            if i - last < POS_GAP or i + L + G >= n:
                continue
            if up5[i] >= 1 and up5[i] > dn5[i] and pmr[i] <= 0:
                d = 1
            elif dn5[i] >= 1 and dn5[i] > up5[i] and pmr[i] >= 0:
                d = -1
            else:
                continue
            e = i + L
            buy = g['ask'][e] if d > 0 else 1 - g['bid'][e]
            if not np.isfinite(buy):
                continue
            last = i
            cost = buy + fee(buy)
            v = path_net(g, e, d, cost, G)
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
                            np.array([k, now, best, best - now, (g['ask'][e] - g['bid'][e]) * 100], dtype=np.float32), now, cont))
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
    d['names'] = np.array(F.NAMES)
    os.makedirs(a.out, exist_ok=True)
    np.savez_compressed(os.path.join(a.out, 'exit_hold.npz'), **d)
    npos = len(np.unique(np.stack([d['game'], d['t_entry'], d['dir']], 1), axis=0))
    print(f"{len(d['t'])} checks of {npos} positions from {len(np.unique(d['game']))} games; holding the plan beat selling at {100 * np.mean(d['cont'] > d['now']):.0f}% of checks; "
          f"mean plan end {np.mean(d['cont']):+.2f}c; {time.time() - t0:.0f} s")


class Net(nn.Module):
    def __init__(self, f, h=256):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(f, h), nn.GELU(), nn.Dropout(0.1), nn.Linear(h, h), nn.GELU(), nn.Dropout(0.1), nn.Linear(h, 1))

    def forward(self, x):
        return self.net(x).squeeze(1)


def inputs(Z, pos, d):
    dd = d.astype(np.float32)[:, None]
    return np.concatenate([Z, Z * dd, pos / PSCALE, dd], axis=1).astype(np.float32)


def train_cut(D, before, dev, epochs=8):
    """Exit model trained on positions whose plan ended before `before` (purged); m chosen on the last 20%."""
    keep = np.flatnonzero(D['t_end'] < before)
    gstart = {}
    for g, s in zip(D['game'][keep], D['start'][keep]):
        gstart[int(g)] = int(s)
    order = sorted(gstart, key=lambda g: gstart[g])
    val_g = set(order[int(0.8 * len(order)):])
    is_val = np.isin(D['game'][keep], list(val_g))
    val_start = min(gstart[g] for g in val_g)
    tr = keep[~is_val & (D['t_end'][keep] < val_start)]   # training plans end before validation starts
    va = keep[is_val]
    mu, sd = D['X'][tr].mean(0), D['X'][tr].std(0)
    sd[sd < 1e-6] = 1
    Z = lambda idx: np.clip((D['X'][idx] - mu) / sd, -10, 10).astype(np.float32)
    Xtr, ytr = inputs(Z(tr), D['pos'][tr], D['dir'][tr]), np.clip(D['cont'][tr] - D['now'][tr], -30, 30)
    torch.manual_seed(3)
    model = Net(Xtr.shape[1]).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    Xt, yt = torch.from_numpy(Xtr).to(dev), torch.from_numpy(ytr.astype(np.float32)).to(dev)
    for ep in range(epochs):
        model.train()
        perm = torch.randperm(len(Xt), device=dev)
        for i in range(0, len(Xt), 8192):
            b = perm[i:i + 8192]
            opt.zero_grad()
            nn.functional.huber_loss(model(Xt[b]), yt[b], delta=3.0).backward()
            opt.step()
    model.eval()
    pv = run(model, inputs(Z(va), D['pos'][va], D['dir'][va]), dev)
    # The margin: the policy (sell at the first check where pred < -m, else the plan) with the best mean on validation.
    key = np.stack([D['game'][va], D['t_entry'][va], D['dir'][va]], 1)
    best = (0.0, -1e9)
    for m in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0):
        vals = {}
        for j in np.argsort(D['pos'][va][:, 0], kind='stable'):
            k = tuple(key[j])
            if k in vals and vals[k][1]:
                continue
            if pv[j] < -m:
                vals[k] = (float(D['now'][va][j]), True)
            elif k not in vals:
                vals[k] = (float(D['cont'][va][j]), False)
        v = np.mean([x[0] for x in vals.values()]) if vals else -1e9
        if v > best[1]:
            best = (m, v)
    return model, mu, sd, best[0], len(tr), len(va)


def run(model, X, dev, bs=65536):
    out = []
    with torch.no_grad():
        for i in range(0, len(X), bs):
            out.append(model(torch.from_numpy(X[i:i + bs]).to(dev)).cpu().numpy())
    return np.concatenate(out) if out else np.zeros(0)


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    dev = 'cuda' if torch.cuda.is_available() else 'cpu'
    D = dict(np.load(os.path.join(a.out, 'exit_hold.npz'), allow_pickle=False))
    db = W.connect(a.db)
    trades = db.execute("SELECT t.id, t.bot, t.real, t.match_id, t.data, m.sport, m.start_ms FROM bot_trades t JOIN matches m ON m.id = t.match_id WHERE t.bot != 'favs'").fetchall()
    day = dt.datetime.fromisoformat(a.first).replace(tzinfo=dt.timezone.utc)
    end = dt.datetime.fromisoformat(a.last).replace(tzinfo=dt.timezone.utc)
    res = []
    while day <= end:
        d0 = int(day.timestamp() * 1000)
        d1 = int((day + dt.timedelta(days=1)).timestamp() * 1000)
        todays = [t for t in trades if t[6] and d0 <= t[6] < d1]
        if not todays:
            day += dt.timedelta(days=1)
            continue
        model, mu, sd, m, ntr, nva = train_cut(D, d0, dev)
        print(f'== {day:%d %b}: exit model on {ntr} checks (validation {nva}), sells when it predicts holding loses over {m:.1f}c; {len(todays)} snipe trades', flush=True)
        cache = {}
        for tid, bot, real, mid, data, sport, start in todays:
            r = json.loads(data)
            o, buy_ms, dirn, entry = r.get('outcome'), r.get('ts'), (1 if (r.get('dir') or 1) > 0 else -1), r.get('entry')
            if not o or not buy_ms or entry is None:
                continue
            if (mid, o) not in cache:
                pm, bk = W.load(db, mid)
                gg = F.build(pm, bk, o, sport or '', start, (sport or '') in W.THREE_WAY, book_rows(db, mid), full=True)
                rb = sorted(((x[0], x[3]) for x in pm if x[2] == o), key=lambda r: r[0])
                ra = sorted(((x[0], x[4]) for x in pm if x[2] == o), key=lambda r: r[0])
                cache[(mid, o)] = (gg, rb, ra)
            g, rb, ra = cache[(mid, o)]
            if g is None:
                continue
            e = int(np.searchsorted(g['ts'], buy_ms, side='right') - 1)
            if e < 0 or buy_ms - g['ts'][e] > 5000:
                continue
            # The position's path on Polymarket's own prices, all the way to the bot's real exit: the feature grid
            # stops where the bookmakers stop quoting, Polymarket (and the bot's position) can go on long after.
            last_pm = rb[-1][0] if rb else int(g['ts'][-1])
            exit_ms = r.get('exit_ts') or last_pm
            n_ahead = int(max(1, (min(exit_ms, last_pm + 1000) - g['ts'][e]) // 1000))
            secs = g['ts'][e] + 1000 * np.arange(1, n_ahead + 1)
            hb, ha = held(rb, secs), held(ra, secs)
            sp = hb if dirn > 0 else 1 - ha
            cost = entry + fee(entry)
            v = (sp - fee(sp) - cost) * 100
            if not len(v) or not np.isfinite(v).any():
                continue
            # The bot's own result: what it really made a share (settlements included); else its exit on the book.
            if r.get('pnl') is not None and r.get('contracts'):
                bot_net = r['pnl'] / r['contracts'] * 100
            else:
                bot_net = v[-1] if np.isfinite(v[-1]) else v[np.isfinite(v)][-1]
            ks = np.arange(2, n_ahead + 1, 2)                                             # every 2 s while it held,
            ks = ks[(e + ks < len(g['ts'])) & np.isfinite(v[ks - 1])]                     # where the model has its inputs
            mod_net, sold_at = bot_net, None
            if len(ks):
                best = np.maximum.accumulate(np.nan_to_num(v, nan=-1e9))
                pos = np.stack([ks, v[ks - 1], best[ks - 1], best[ks - 1] - v[ks - 1], np.full(len(ks), (g['ask'][e] - g['bid'][e]) * 100)], 1).astype(np.float32)
                Zk = np.clip((g['X'][e + ks] - mu) / sd, -10, 10).astype(np.float32)
                pred = run(model, inputs(Zk, pos, np.full(len(ks), dirn, np.int8)), dev)
                fire = np.flatnonzero(pred < -m)
                if len(fire):
                    j = fire[0]
                    mod_net, sold_at = float(v[ks[j] - 1]), int(ks[j])
            res.append({'id': tid, 'bot': bot, 'real': bool(real), 'day': day.strftime('%d %b'), 'bot_net': float(bot_net), 'model_net': float(mod_net),
                        'sold_at_s': sold_at, 'held_s': n_ahead, 'bot_pnl_per_share': (r.get('pnl') or 0) / r['contracts'] * 100 if r.get('contracts') else None})
        day += dt.timedelta(days=1)
    b = np.array([x['bot_net'] for x in res])
    mo = np.array([x['model_net'] for x in res])
    early = [x for x in res if x['sold_at_s'] is not None]
    lines = [f"\nSnipe-bot trades judged day by day by an exit model trained only on earlier games: {len(res)} trades (per share, net of fees; the bot at what it really made, the model's early sales on the recorded book).",
             f"  the bot's own exits (hold until caught up or give-up)   mean {b.mean():+.2f}c  win {100 * np.mean(b > 0):.0f}%  total {b.sum():+.0f}c",
             f"  model: sell at once when it calls a failure, else the bot's exit   mean {mo.mean():+.2f}c  win {100 * np.mean(mo > 0):.0f}%  total {mo.sum():+.0f}c",
             f"  the model sold early in {len(early)} trades (median after {np.median([x['sold_at_s'] for x in early]) if early else float('nan'):.0f} s, the bot held a median {np.median([x['held_s'] for x in early]) if early else float('nan'):.0f} s): "
             f"on those the bot made {np.mean([x['bot_net'] for x in early]) if early else float('nan'):+.2f}c, the model {np.mean([x['model_net'] for x in early]) if early else float('nan'):+.2f}c"]
    if len(res) >= 10:
        diff = mo - b
        rng = np.random.default_rng(2)
        boot = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(5000)]
        lines.append(f"  model minus bot {diff.mean():+.2f}c a trade (90% range {np.quantile(boot, 0.05):+.2f}..{np.quantile(boot, 0.95):+.2f})")
    for day_ in sorted({x['day'] for x in res}):
        rs = [x for x in res if x['day'] == day_]
        lines.append(f"    {day_}: {len(rs)} trades, bot {np.mean([x['bot_net'] for x in rs]):+.2f}c, model {np.mean([x['model_net'] for x in rs]):+.2f}c, sold early {sum(x['sold_at_s'] is not None for x in rs)}")
    text = '\n'.join(lines)
    print(text)
    with open(os.path.join(a.out, 'exit_vs_hold.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    with open(os.path.join(a.out, 'exit_vs_hold.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, default=float)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build', 'test'])
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--workers', type=int, default=30)
    ap.add_argument('--first', default='2026-11-18')
    ap.add_argument('--last', default='2026-11-23')
    a = ap.parse_args()
    (cmd_build if a.cmd == 'build' else cmd_test)(a)


if __name__ == '__main__':
    main()
