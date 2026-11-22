"""Extra signals for the winners-vs-losers study (tools/tells.py), read from the database for every candidate move
that has the systems_backtest signals (the loose rule set). Throttled; it waits while the bot's feed runs late.

Known AT THE BUY (tf):
  q_age      seconds since Polymarket's price last changed before the bookmaker's move (capped at 120)
  q_n60      Polymarket price changes in the 60 s before the move (how alive the market is)
  q_upd      Polymarket quote updates between the move and the buy (the market maker re-quoted and did not follow)
  sz_tf      $ on offer at the best price on the side we buy, at the buy;  sz_ratio = that against 1 s before the move
  imb        share of the top-of-book size that is on OUR side's bid (support behind us)
  drift10/60 Polymarket's own move in our direction over the 10 / 60 s before the move
  k_gap      Kalshi's price minus Polymarket's mid, in our direction, at the buy (None = Kalshi does not quote it)
  k_mv       Kalshi's move in our direction from 30 s before the move to the buy
  oth_buy    how much of the trigger's move the OTHER bookmaker families' middle had made by the buy
  n_fam60    bookmaker families that moved 2+ points our way in the 60 s up to the move
AFTER THE BUY (for the in-trade tells):
  bid{T}     our side's bid T seconds after the buy, minus the price paid
  trg{T}     share of its move the trigger bookmaker still holds T seconds after the buy
  oth{T}     share of the trigger's move the other families' middle has made T seconds after the buy
usage: tells_feats.py
"""
import sys, os, json, bisect, time, sqlite3, subprocess
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.argv = [sys.argv[0], 'lib']
import sweep as S
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
SRC = os.path.expanduser('~/swifteye/research/systems_feats.json')
OUT = os.path.expanduser('~/swifteye/research/tells_feats.json')
F = json.load(open(SRC))
G = json.load(open(OUT)) if os.path.exists(OUT) else {}
key = lambda c: f"{c['match_id']}|{c['outcome']}|{c['t0']}"
TS = (10, 20, 30, 45, 60, 90, 120, 180, 240)


def feed_ok():
    try:
        d = json.loads(subprocess.run(['curl', '-s', '-m', '8', 'http://127.0.0.1:8787/bots'], capture_output=True, text=True).stdout)
        return (d['snipe']['status'].get('pm_lag_ms') or 0) < 600
    except Exception:
        return True


def feats(c):
    m, o, t0, tf, d, mv, trig = c['match_id'], c['outcome'], c['t0'], c['tf'], c['dir'], c['_mv'], c['trigger']
    out = {}
    q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask, bid_size, ask_size FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? AND outcome=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY 1",
                   (m, t0 - 150_000, tf + 320_000, o)).fetchall()
    qt = [r[0] for r in q]
    def qat(t):
        i = bisect.bisect_right(qt, t); return q[i - 1] if i else None
    side = lambda r: (r[1], r[2], r[3], r[4]) if d > 0 else (1 - r[2], 1 - r[1], r[4], r[3])   # our bid, our ask, our bid size, our ask size
    mid = lambda r: (r[1] + r[2]) / 2
    i0 = bisect.bisect_right(qt, t0)
    last = None
    for j in range(i0 - 1, 0, -1):
        if (q[j][1], q[j][2]) != (q[j - 1][1], q[j - 1][2]): last = qt[j]; break
    out['q_age'] = min((t0 - last) / 1000, 120.0) if last is not None else (120.0 if i0 else None)
    out['q_n60'] = sum(1 for j in range(1, i0) if qt[j] >= t0 - 60_000 and (q[j][1], q[j][2]) != (q[j - 1][1], q[j - 1][2]))
    out['q_upd'] = bisect.bisect_right(qt, tf) - i0
    a, b = qat(tf), qat(t0 - 1000)
    if a:
        sa = side(a); out['sz_tf'] = (sa[3] or 0) * sa[1]
        out['imb'] = (sa[2] / (sa[2] + sa[3])) if (sa[2] and sa[3]) else None
        if b and side(b)[3]: out['sz_ratio'] = (sa[3] or 0) / side(b)[3]
    for T in (10, 60):
        x = qat(t0 - T * 1000)
        out[f'drift{T}'] = (mid(b) - mid(x)) * d if (b and x) else None
    for T in TS:
        x = qat(tf + T * 1000)
        out[f'bid{T}'] = side(x)[0] if x else None       # absolute; tells.py subtracts the entry
    rows = db.execute("SELECT ts_ms, book, new_price FROM changes WHERE match_id=? AND market='1x2' AND outcome=? AND ts_ms>=? AND ts_ms<=? AND new_price IS NOT NULL ORDER BY ts_ms",
                      (m, o, t0 - 1_200_000, tf + 250_000)).fetchall()
    books = {}
    for t, bk, p in rows:
        books.setdefault(bk, ([], [])); books[bk][0].append(t); books[bk][1].append(1 / max(p, 1.0001))
    def at(bk, t):
        if bk not in books: return None
        i = bisect.bisect_right(books[bk][0], t); return books[bk][1][i - 1] if i else None
    def others(t):
        fam = defaultdict(list)
        for bk in books:
            if bk in ('polymarket', 'kalshi') or S.platform(bk) == S.platform(trig): continue
            p = at(bk, t)
            if p is not None: fam[S.platform(bk)].append(p)
        ms = sorted(sum(x) / len(x) for x in fam.values()); return ms[len(ms) // 2] if ms else None
    k1, k0 = at('kalshi', tf), at('kalshi', t0 - 30_000)
    out['k_gap'] = (k1 - mid(a)) * d if (k1 is not None and a) else None
    out['k_mv'] = (k1 - k0) * d if (k1 is not None and k0 is not None) else None
    ob = others(t0 - 1000); new = at(trig, t0 + 1500)
    y = others(tf)
    out['oth_buy'] = d * (y - ob) / mv if (ob is not None and y is not None and mv > 0) else None
    nf = 0; seen = defaultdict(list)
    for bk in books:
        if bk in ('polymarket', 'kalshi'): continue
        p1, p0 = at(bk, t0 + 1500), at(bk, t0 - 60_000)
        if p1 is not None and p0 is not None: seen[S.platform(bk)].append((p1 - p0) * d)
    out['n_fam60'] = sum(1 for v in seen.values() if max(v) >= 0.02)
    if new is not None and mv > 0:
        old = new - d * mv
        for T in (10, 30, 60, 120, 180):
            x = at(trig, tf + T * 1000); out[f'trg{T}'] = None if x is None else d * (x - old) / mv
            y = others(tf + T * 1000)
            out[f'oth{T}'] = d * (y - ob) / mv if (ob is not None and y is not None) else None
    time.sleep(0.015 + (len(rows) + len(q)) / 60000)
    return out


if __name__ == '__main__':
    need = [c for c in S.ROWS if key(c) in F and key(c) not in G and not c['nofill'] and c['exits'][2][2]]
    print(f"{len(need)} candidate moves to read ({len(G)} cached)", flush=True)
    t_start = time.time()
    for i, c in enumerate(need):
        G[key(c)] = feats(c)
        if i % 300 == 299:
            json.dump(G, open(OUT, 'w')); print(f"  {i + 1}/{len(need)}, {time.time() - t_start:.0f} s", flush=True)
            while not feed_ok(): print('  bot feed late: waiting 20 s', flush=True); time.sleep(20)
    json.dump(G, open(OUT, 'w'))
    print(f"done: {len(G)} moves, {time.time() - t_start:.0f} s", flush=True)
