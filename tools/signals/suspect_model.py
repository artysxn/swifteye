"""Betting patterns in lower-tier CS2 games: do they say who wins? (owner, 24 Nov)

  python -I tools/signals/suspect_model.py fetch --db X --signals DIR     every trade of every game in cs2_activity (cached)
  python    tools/signals/suspect_model.py test  --signals DIR

suspect.rs scores a game by HOW MUCH unusual betting it sees (money, big underdog bets, big-bet share, drift), without
a direction. To learn whether the pattern predicts the result, the direction is rebuilt from Polymarket's own trade
history (Data API /v2/trades, taker side) for the match-winner market, using only trades up to 10 min before the
listed start:
  p_start     the home side's price at the cut (mean of the last 10 trades) = what the market says;
  drift       p_start minus the first trade's price, and over the last 6 h / 1 h (the side money moved to);
  net_all     dollars taken on the home side minus the away side, over all dollars (order flow);
  net_big     the same counting only wallets that put $500+ net on one side ("big money");
  top1 / top3 the share of all dollars from the biggest wallet / three wallets (one account driving the market);
  late        the share of dollars in the last hour, and their net direction;
  wallets, volume, and suspect.rs's own score / big_dog / big_fav for the game.
RESULT: Polymarket's resolution. TESTS (lower tier only): the price alone (is it calibrated?), then each pattern as a
rule ("back the side the big money is on", "back the drift", "fade the favourite in high-score games") with the win
rate needed at that price + 1c + fee, and a logistic model on all patterns trained on the days before each day."""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
GAMMA, DATA = 'https://gamma-api.polymarket.com', 'https://data-api.polymarket.com/v2'


def get(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_one(row):
    import fair as FR
    ev, home, away, start = row
    e = get(f'{GAMMA}/events/{ev}')
    if not e:
        return ev, None
    for m in e.get('markets', []):
        try:
            outs, pr, toks = json.loads(m.get('outcomes') or '[]'), [float(x) for x in json.loads(m.get('outcomePrices') or '[]')], json.loads(m.get('clobTokenIds') or '[]')
        except Exception:
            continue
        if m.get('sportsMarketType') != 'moneyline' or len(outs) != 2 or len(toks) != 2 or [o.lower() for o in outs] == ['yes', 'no']:
            continue
        hi = 0 if FR.sim(outs[0], home) + FR.sim(outs[1], away) >= FR.sim(outs[1], home) + FR.sim(outs[0], away) else 1
        res = None if (not m.get('closed') or max(pr) < 0.99) else int(pr[hi] >= 0.99)
        tr, cursor = [], None
        for _ in range(25):
            x = get(f'{DATA}/trades?condition={m["conditionId"]}&limit=1000' + (f'&cursor={cursor}' if cursor else ''))
            data = (x or {}).get('data') or []
            tr += [(int(z['timestamp']), 1 if z['token_id'] == toks[hi] else 0, 1 if z.get('side') == 'BUY' else 0, float(z['price']), float(z['size']), z.get('proxy_wallet', '')[-8:]) for z in data]
            pg = (x or {}).get('pagination') or {}
            cursor = pg.get('next_cursor') or pg.get('cursor')
            if not data or not cursor:
                break
        return ev, {'home_won': res, 'start': start, 'trades': tr}
    return ev, None


def cmd_fetch(a):
    db = sqlite3.connect('file:' + a.db.replace(chr(92), '/') + '?mode=ro', uri=True)
    path = os.path.join(a.signals, 'suspect_trades.json')
    cache = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}
    rows = [r for r in db.execute('SELECT event_id, home, away, start_ms FROM cs2_activity WHERE final = 1') if r[0] not in cache]
    print(f'{len(rows)} games to fetch ({len(cache)} cached)', flush=True)
    with ThreadPoolExecutor(6) as ex:
        for k, (ev, out) in enumerate(ex.map(fetch_one, rows)):
            cache[ev] = out
            if (k + 1) % 200 == 0:
                print(f'  {k + 1}', flush=True)
    json.dump(cache, open(path, 'w', encoding='utf-8'))
    meta = {r[0]: r[1:] for r in db.execute('SELECT event_id, tier1, score, big_dog, big_fav, big_share, volume, vrs_home, vrs_away FROM cs2_activity')}
    json.dump(meta, open(os.path.join(a.signals, 'suspect_meta.json'), 'w', encoding='utf-8'))
    ok = [v for v in cache.values() if v and v['home_won'] is not None and v['trades']]
    print(f'{len(ok)} games with a result and trades; {sum(len(v["trades"]) for v in ok)} trades')


def features(v):
    cut = v['start'] / 1000 - 600
    tr = sorted(t for t in v['trades'] if t[0] <= cut)
    if len(tr) < 15:
        return None
    ts = np.array([t[0] for t in tr], float)
    hp = np.array([t[3] if t[1] else 1 - t[3] for t in tr])                  # every print as the home side's price
    usd = np.array([t[3] * t[4] for t in tr])
    sign = np.array([1.0 if (t[1] == t[2]) else -1.0 for t in tr])          # buy home or sell away = backing home
    p_start = float(np.mean(hp[-10:]))
    if not 0.03 < p_start < 0.97:
        return None
    at = lambda sec: float(hp[max(0, np.searchsorted(ts, cut - sec) - 1)])
    vol = usd.sum()
    w = {}
    for t, s, u in zip(tr, sign, usd):
        w[t[5]] = w.get(t[5], 0.0) + s * u
    nets = np.array(sorted(w.values(), key=abs, reverse=True))
    late = ts >= cut - 3600
    return {'p': p_start, 'drift': p_start - float(hp[0]), 'drift6': p_start - at(6 * 3600), 'drift1': p_start - at(3600), 'net_all': float((sign * usd).sum() / vol),
            'net_big': float(nets[np.abs(nets) >= 500].sum() / vol), 'top1': float(abs(nets[0]) / vol), 'top1_dir': float(np.sign(nets[0])), 'top3': float(np.abs(nets[:3]).sum() / vol),
            'late_share': float(usd[late].sum() / vol), 'late_net': float((sign * usd)[late].sum() / max(usd[late].sum(), 1.0)), 'wallets': len(w), 'vol': float(vol), 'n': len(tr),
            'day': dt.datetime.fromtimestamp(v['start'] / 1000, dt.timezone.utc).strftime('%Y-%m-%d')}


def cmd_test(a):
    sys.stdout.reconfigure(encoding='utf-8')
    cache = json.load(open(os.path.join(a.signals, 'suspect_trades.json'), encoding='utf-8'))
    meta = json.load(open(os.path.join(a.signals, 'suspect_meta.json'), encoding='utf-8'))
    R = []
    for ev, v in cache.items():
        if not v or v['home_won'] is None or meta.get(ev) is None or meta[ev][0]:
            continue                                                         # lower tier only, resolved
        f = features(v)
        if f:
            f.update(y=v['home_won'], score=meta[ev][1] or 0.0, big_dog=meta[ev][2] or 0, big_fav=meta[ev][3] or 0, big_share=meta[ev][4] or 0.0)
            R.append(f)
    out = []
    say = lambda s: (out.append(s), print(s, flush=True))
    g = lambda k: np.array([r[k] for r in R], float)
    y, p = g('y'), g('p')
    n = len(R)
    days = sorted({r['day'] for r in R})
    rng = np.random.default_rng(1)
    ll = lambda q, yy: -np.mean(yy * np.log(np.clip(q, 1e-4, 1)) + (1 - yy) * np.log(np.clip(1 - q, 1e-4, 1)))
    say(f'{n} lower-tier CS2 games with a result and 15+ trades before the start ({days[0]} .. {days[-1]}); median ${np.median(g("vol")):,.0f} traded before the start by {np.median(g("wallets")):.0f} wallets')
    say('')
    say('1. IS THE PRICE RIGHT? (the home side\'s price 10 min before the listed start vs how often it won)')
    for lo, hi in ((0, .3), (.3, .45), (.45, .55), (.55, .7), (.7, 1)):
        m = (p >= lo) & (p < hi)
        if m.sum() >= 20:
            say(f'   priced {int(lo * 100)}-{int(hi * 100)}c: {m.sum():>4} games, priced {p[m].mean() * 100:.0f}%, won {y[m].mean() * 100:.0f}%')
    say(f'   log loss of the price {ll(p, y):.4f} (no model, always 50%: 0.6931)')

    def rule(name, side, mask):
        """Back `side` (+1 home, -1 away) where mask: win rate, the rate needed at price + 1c + fee, profit per $100, range."""
        idx = np.flatnonzero(mask & (side != 0))
        if len(idx) < 25:
            return say(f'   {name:<58} {len(idx)} games: too few')
        price = np.where(side[idx] > 0, p[idx], 1 - p[idx]) + 0.01
        cost = price + 0.05 * price * (1 - price)
        won = np.where(side[idx] > 0, y[idx], 1 - y[idx])
        pay = 100 * (won - cost) / cost
        bt = [pay[rng.integers(0, len(pay), len(pay))].mean() for _ in range(4000)]
        say(f'   {name:<58} {len(idx):>4} bets at {price.mean() * 100:.0f}c: won {won.mean() * 100:.0f}% (needed {cost.mean() * 100:.0f}%), ${pay.mean():+6.2f} per $100 (90% {np.quantile(bt, .05):+.1f}..{np.quantile(bt, .95):+.1f})')
    fav = np.where(p >= 0.5, 1.0, -1.0)
    say('')
    say('2. EACH PATTERN AS A BET (held to the result; one bet a game)')
    allm = np.ones(n, bool)
    rule('no pattern: always the favourite', fav, allm)
    rule('no pattern: always the underdog', -fav, allm)
    for thr in (0.2, 0.4):
        rule(f'big money ($500+ wallets) net {int(thr * 100)}%+ of volume on a side', np.sign(g('net_big')), np.abs(g('net_big')) >= thr)
    rule('big money on the UNDERDOG (20%+ of volume)', np.sign(g('net_big')), (np.abs(g('net_big')) >= 0.2) & (np.sign(g('net_big')) == -fav))
    rule('big money on the FAVOURITE (20%+ of volume)', np.sign(g('net_big')), (np.abs(g('net_big')) >= 0.2) & (np.sign(g('net_big')) == fav))
    for thr in (0.05, 0.10):
        rule(f'price drifted {int(thr * 100)}+ pt to a side since the first trade', np.sign(g('drift')), np.abs(g('drift')) >= thr)
    rule('price drifted 5+ pt in the last 6 hours', np.sign(g('drift6')), np.abs(g('drift6')) >= 0.05)
    rule('price drifted 3+ pt in the last hour', np.sign(g('drift1')), np.abs(g('drift1')) >= 0.03)
    rule('FADE a 10+ pt drift (back the other side)', -np.sign(g('drift')), np.abs(g('drift')) >= 0.10)
    rule('one wallet is 40%+ of the money: back its side', g('top1_dir'), g('top1') >= 0.4)
    rule('one wallet is 40%+ of the money: back the other side', -g('top1_dir'), g('top1') >= 0.4)
    rule('late money (last hour) 30%+ one way', np.sign(g('late_net')), (np.abs(g('late_net')) >= 0.3) & (g('late_share') >= 0.2))
    for lo in (60, 80):
        rule(f'suspect.rs score {lo}+: the favourite', fav, g('score') >= lo)
        rule(f'suspect.rs score {lo}+: the underdog', -fav, g('score') >= lo)
    rule('games with big underdog bets (big_dog 1+): the underdog', -fav, g('big_dog') >= 1)
    rule('games with 3+ big underdog bets: the underdog', -fav, g('big_dog') >= 3)
    # ---- 3. a model on all patterns, trained on the days before each day
    import fair as FR
    lp = FR.lg(p)
    X = np.stack([np.ones(n), lp, g('net_big'), g('net_all'), g('drift') * 5, g('drift6') * 5, g('drift1') * 10, g('top1') * g('top1_dir'), g('late_net') * g('late_share'),
                  g('score') / 100 * np.sign(lp), g('big_dog') / 5 * -np.sign(lp), g('big_share') * np.sign(lp), np.log10(g('vol')) / 5 * np.sign(lp)], 1)
    names = ['', 'price (log-odds)', 'big money', 'all order flow', 'drift', 'drift 6 h', 'drift 1 h', 'top wallet\'s side', 'late money', 'score x favourite', 'big underdog bets x underdog', 'big-bet share x favourite', 'volume x favourite']
    dd = np.array([r['day'] for r in R])
    pm, ws = np.full(n, np.nan), []
    for d in days:
        tr, te = np.flatnonzero(dd < d), np.flatnonzero(dd == d)
        if len(tr) < 150:
            continue
        w = FR.fit_pool(X[tr], y[tr], l2=5.0)
        ws.append(w)
        pm[te] = 1 / (1 + np.exp(-(X[te] @ w)))
    T = np.isfinite(pm)
    say('')
    say(f'3. A MODEL ON ALL PATTERNS (logistic, trained on the days before each day; scored on {T.sum()} games from {sorted(dd[T].tolist())[0] if T.any() else "-"} on)')
    if T.sum() > 50:
        d_ = -(y[T] * np.log(np.clip(pm[T], 1e-4, 1)) + (1 - y[T]) * np.log(np.clip(1 - pm[T], 1e-4, 1))) + (y[T] * np.log(np.clip(p[T], 1e-4, 1)) + (1 - y[T]) * np.log(np.clip(1 - p[T], 1e-4, 1)))
        bt = [d_[rng.integers(0, len(d_), len(d_))].mean() for _ in range(4000)]
        say(f'   log loss: the price alone {ll(p[T], y[T]):.4f} | the model {ll(pm[T], y[T]):.4f} (difference {d_.mean():+.4f}, 90% {np.quantile(bt, .05):+.4f}..{np.quantile(bt, .95):+.4f}; below 0 = the patterns add information)')
        w = np.mean(ws, 0)
        say('   weights: ' + ', '.join(f'{nm} {w[i]:+.2f}' for i, nm in enumerate(names) if nm))
        edge = pm - p
        for thr in (0.03, 0.06, 0.10):
            rule(f'the model {int(thr * 100)}+ pt away from the price: back its side', np.sign(edge), T & (np.abs(np.nan_to_num(edge)) >= thr))
    with open(os.path.join(a.signals, 'suspect_model.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['fetch', 'test'])
    ap.add_argument('--db')
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    (cmd_fetch if a.cmd == 'fetch' else cmd_test)(a)


if __name__ == '__main__':
    main()
