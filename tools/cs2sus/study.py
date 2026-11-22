"""Step 2 of the suspicious-CS2 study: what the tracked wallets do, whether it keeps paying, and what their games look like.

Inputs: fetch_wallets.py's cache (~/swifteye/research/cs2sus), the year of CS2 match price paths kept by the older project
(~/aim4/server/data/insights/packs: start, format, teams, winner, price before the start, price path of team 0), and
Swifteye's cs2_activity table (Valve ranks, money traded, activity score; games since 4 Nov).

A position = one wallet on one side of one market. P/L = sells + shares still held x payout - buys. Positions that sold
more than they bought (shares from outside the trade list) and unresolved markets are left out.
usage: study.py [section ...]     sections: overall wallets forward timing converge teams games
"""
import os, sys, json, gzip, glob, math, sqlite3, datetime as dt, statistics as st
from collections import defaultdict, Counter

R = os.path.expanduser('~/swifteye/research/cs2sus')
NAMES = json.load(open(R + '/names.json')); M = json.load(open(R + '/markets.json'))
U = lambda ts: dt.datetime.fromtimestamp(ts, dt.UTC)
iso = lambda s: int(dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()) if s else None
fee = lambda p: 0.05 * p * (1 - p)

PACK = {}
for f, op in ((os.path.expanduser('~/aim4/server/data/insights/packs/comeback-pack.ndjson.gz'), gzip.open), (os.path.expanduser('~/aim4/server/data/insights/packs/comeback-recent.ndjson'), open)):
    with op(f, 'rt') as fh:
        for line in fh:
            try: d = json.loads(line)
            except Exception: continue
            if d.get('g') == 'cs2': PACK[d['cid']] = d
ACT = {}
try:
    db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
    for r in db.execute("SELECT slug, home, away, vrs_home, vrs_away, tier1, volume, bets, big_dog, big_fav, big_share, drift, score, start_ms FROM cs2_activity"):
        ACT[r[0]] = dict(home=r[1], away=r[2], vh=r[3], va=r[4], tier1=r[5], vol=r[6], bets=r[7], big_dog=r[8], big_fav=r[9], big_share=r[10], drift=r[11], score=r[12], start=r[13])
except Exception as e:
    print('cs2_activity not read:', e)

# ---- positions
POS = []
for f in glob.glob(R + '/wallets/*.json'):
    w = json.load(open(f)); a = w['addr']
    by = defaultdict(lambda: dict(b=[], s=[], r=0.0))
    for ts, kind, cid, oi, price, shares, usdc, slug in w['rows']:
        if not slug.startswith('cs2-') or not cid: continue
        k = (cid, oi)
        if kind == 'B': by[k]['b'].append((ts, price, shares, usdc))
        elif kind == 'S': by[k]['s'].append((ts, price, shares, usdc))
        elif kind == 'R': by[(cid, -1)]['r'] += usdc
    for (cid, oi), x in by.items():
        if oi < 0 or not x['b']: continue
        m = M.get(cid)
        if not m: continue
        x['b'].sort(); bought = sum(t[2] for t in x['b']); cost = sum(t[3] for t in x['b']); sold = sum(t[2] for t in x['s']); back = sum(t[3] for t in x['s'])
        if cost < 1 or sold > bought * 1.02: continue
        start = iso(m.get('start')); t1 = x['b'][0][0]
        p = dict(w=a, name=NAMES.get(a, a[:8]), cid=cid, oi=oi, t=t1, cost=cost, px=cost / bought, first_px=x['b'][0][1], shares=bought, sold=sold, back=back, type=m.get('type') or '?', event=m.get('event') or '',
                 start=start, pre=(start is not None and t1 < start), res=m.get('res'), team=(m.get('outs') or ['?', '?'])[oi] if oi < len(m.get('outs') or []) else '?', buys=x['b'], join=w.get('join'))
        if m.get('res') is not None:
            p['won'] = m['res'] == oi; p['pnl'] = back + (bought - sold) * (1.0 if p['won'] else 0.0) - cost
        POS.append(p)
POS.sort(key=lambda p: p['t'])
DONE = [p for p in POS if 'pnl' in p]


def line(name, P, extra=0.0):
    """ROI of a set of positions; `extra` = cents added to every entry price (what a copier pays more)."""
    if not P: return f"  {name:44s} {0:6d}"
    cost = sum(p['cost'] for p in P); pnl = sum(p['pnl'] - extra * p['shares'] for p in P)
    hold = [p for p in P if p['sold'] < 0.1 * p['shares']]
    wins = sum(p['won'] for p in P); exp = sum(p['px'] for p in P); var = sum(p['px'] * (1 - p['px']) for p in P)
    z = (wins - exp) / math.sqrt(var) if var > 0 else 0.0
    return (f"  {name:44s} {len(P):6d} {len({p['w'] for p in P}):4d} {cost:12,.0f} {pnl:+11,.0f} {pnl / cost * 100:+6.1f}% {wins * 100 / len(P):4.0f}% {exp * 100 / len(P):4.0f}% {z:+5.1f} {st.median(p['cost'] for p in P):7.0f}")


HEAD = f"  {'':44s} {'bets':>6s} {'accs':>4s} {'staked $':>12s} {'profit $':>11s} {'return':>7s} {'won':>5s} {'paid':>5s} {'z':>5s} {'median $':>8s}"


def overall():
    print(f"\nA. THE TRACKED WALLETS ON CS2: {len({p['w'] for p in POS})} wallets, {len(POS)} positions, {len(DONE)} with a result; first {U(POS[0]['t']):%d %b %Y}, last {U(POS[-1]['t']):%d %b %Y}")
    print(HEAD); print(line('all positions', DONE))
    for k, nm in (('moneyline', 'match winner'), ('child_moneyline', 'map winner')): print(line(nm, [p for p in DONE if p['type'] == k]))
    print(line('other markets (totals, handicaps ...)', [p for p in DONE if p['type'] not in ('moneyline', 'child_moneyline')]))
    print(line('bought before the start', [p for p in DONE if p['pre']])); print(line('bought after the start', [p for p in DONE if not p['pre']]))
    for a, b in ((0, .2), (.2, .35), (.35, .5), (.5, .65), (.65, .8), (.8, 1.01)): print(line(f"paid {int(a * 100)}-{int(min(b, 1) * 100)}c", [p for p in DONE if a <= p['px'] < b]))
    for a, b in ((0, 50), (50, 200), (200, 1000), (1000, 5000), (5000, 1e9)): print(line(f"position ${a:,.0f}-{b:,.0f}" if b < 1e9 else f"position ${a:,.0f}+", [p for p in DONE if a <= p['cost'] < b]))
    print(line('held to the end (sold under 10%)', [p for p in DONE if p['sold'] < 0.1 * p['shares']])); print(line('sold half or more before the end', [p for p in DONE if p['sold'] >= 0.5 * p['shares']]))
    print("  by month:")
    by = defaultdict(list)
    for p in DONE: by[U(p['t']).strftime('%Y-%m')].append(p)
    for k in sorted(by): print(line('   ' + k, by[k]))
    print("  last weeks:")
    by = defaultdict(list)
    for p in DONE:
        if p['t'] >= POS[-1]['t'] - 35 * 86400: by[U(p['t'] - (U(p['t']).weekday()) * 86400).strftime('week of %d %b')].append(p)
    for k in sorted(by, key=lambda k: min(p['t'] for p in by[k])): print(line('   ' + k, by[k]))


def wallets():
    print("\nB. EACH WALLET (20+ CS2 positions with a result), by profit"); print(HEAD + '  before start  joined      first CS2 bet')
    by = defaultdict(list)
    for p in DONE: by[p['w']].append(p)
    rows = sorted(by.items(), key=lambda kv: -sum(p['pnl'] for p in kv[1]))
    for a, P in rows:
        if len(P) < 20: continue
        pre = sum(p['cost'] for p in P if p['pre']) / sum(p['cost'] for p in P)
        print(line(NAMES.get(a, a)[:22] + ' ' + a[:6], P) + f"  {pre * 100:5.0f}%       {U(P[0]['join']):%d %b %y}   {U(P[0]['t']):%d %b %y}" if P[0].get('join') else line(NAMES.get(a, a)[:22], P))
    small = [P for a, P in rows if len(P) < 20]
    print(f"  ({len(small)} wallets with under 20 positions: {sum(len(P) for P in small)} positions, {sum(p['pnl'] for P in small for p in P):+,.0f})")
    zs = []
    for a, P in rows:
        if len(P) >= 20:
            wins = sum(p['won'] for p in P); exp = sum(p['px'] for p in P); var = sum(p['px'] * (1 - p['px']) for p in P); zs.append((wins - exp) / math.sqrt(var))
    print(f"  wallets with 20+ positions: {len(zs)}; won clearly more often than the prices they paid imply (z 3+): {sum(z >= 3 for z in zs)}, z 2-3: {sum(2 <= z < 3 for z in zs)}, z under 0: {sum(z < 0 for z in zs)}")


def forward():
    print("\nC. DOES IT KEEP PAYING? Wallets judged only on positions opened before a date, then their LATER positions (their price, and a copier paying 2c and 4c more)")
    print(HEAD)
    for cut in ('2026-09-01', '2026-10-01', '2026-10-15', '2026-11-01', '2026-11-08'):
        c = iso(cut + 'T00:00:00Z'); before = defaultdict(list)
        for p in DONE:
            if p['t'] < c: before[p['w']].append(p)
        good = {a for a, P in before.items() if len(P) >= 10 and sum(p['pnl'] for p in P) > 0}
        bad = {a for a, P in before.items() if len(P) >= 10 and sum(p['pnl'] for p in P) <= 0}
        new = {p['w'] for p in DONE} - set(before)
        after = [p for p in DONE if p['t'] >= c]
        print(f"  from {cut}:")
        print(line(f"   in profit before ({len(good)} wallets)", [p for p in after if p['w'] in good]))
        print(line("      ... copier paying 2c more", [p for p in after if p['w'] in good], 0.02)); print(line("      ... copier paying 4c more", [p for p in after if p['w'] in good], 0.04))
        print(line(f"   losing before ({len(bad)} wallets)", [p for p in after if p['w'] in bad]))
        print(line(f"   not yet active / under 10 bets ({len(new)}+)", [p for p in after if p['w'] not in good and p['w'] not in bad]))


def timing():
    print("\nD. WHEN THEY BUY (match-winner and map markets; first buy of each position)"); print(HEAD)
    Pm = [p for p in DONE if p['start']]
    for a, b, nm in ((-1e9, -86400, 'more than a day before the start'), (-86400, -6 * 3600, '6-24 h before'), (-6 * 3600, -3600, '1-6 h before'), (-3600, -600, '10-60 min before'), (-600, 0, 'last 10 min before'),
                     (0, 900, 'first 15 min of play'), (900, 2700, '15-45 min in'), (2700, 5400, '45-90 min in'), (5400, 1e9, 'later than 90 min in')):
        print(line(nm, [p for p in Pm if a <= p['t'] - p['start'] < b]))
    print("  live buys on the match market, against the price before the start (path from the history pack):")
    live = []
    for p in DONE:
        d = PACK.get(p['cid'])
        if not d or p['pre'] or p['type'] != 'moneyline': continue
        pre = d['pre'] if p['oi'] == 0 else 1 - d['pre']; p['_chg'] = p['first_px'] - pre; live.append(p)
    for a, b, nm in ((-1, -0.25, 'bought 25+ points BELOW the pre-match price (team well behind)'), (-0.25, -0.10, '10-25 points below'), (-0.10, 0.10, 'within 10 points'), (0.10, 0.25, '10-25 points above'), (0.25, 1, '25+ points above (team well ahead)')):
        print(line(nm, [p for p in live if a <= p['_chg'] < b]))


def converge():
    print("\nE. SEVERAL TRACKED WALLETS ON THE SAME SIDE: buy when the k-th wallet arrives (at its price + 2c), hold to the end")
    print(f"  {'':52s} {'signals':>7s} {'won':>5s} {'paid':>5s} {'return':>7s} {'return +4c':>10s} | before 1 Nov | from 1 Nov")
    side = defaultdict(list)
    for p in DONE:
        if p['type'] in ('moneyline', 'child_moneyline') and p['start']: side[(p['cid'], p['oi'])].append(p)
    cutn = iso('2026-11-01T00:00:00Z')

    def roi(S, extra):
        if not S: return float('nan')
        return sum(((1.0 if w else 0.0) - (px + extra) - fee(px + extra)) / (px + extra) for px, w, t in S) / len(S) * 100
    for label, flt in (('any time', lambda p: True), ('before the start only', lambda p: p['pre']), ('match winner, before the start', lambda p: p['pre'] and p['type'] == 'moneyline'),
                       ('before the start, each position $100+', lambda p: p['pre'] and p['cost'] >= 100), ('after the start only', lambda p: not p['pre'])):
        for k in (1, 2, 3, 4, 6):
            S = []
            for key, P in side.items():
                Q = sorted([p for p in P if flt(p)], key=lambda p: p['t'])
                if len(Q) >= k and Q[k - 1]['first_px'] < 0.95: S.append((Q[k - 1]['first_px'], Q[k - 1]['won'], Q[k - 1]['t']))
            if len(S) < 10: continue
            a = [s for s in S if s[2] < cutn]; b = [s for s in S if s[2] >= cutn]
            print(f"  {label + ', wallet no. ' + str(k):52s} {len(S):7d} {sum(s[1] for s in S) * 100 / len(S):4.0f}% {sum(s[0] for s in S) * 100 / len(S):4.0f}% {roi(S, 0.02):+6.1f}% {roi(S, 0.04):+9.1f}% | {len(a):4d} {roi(a, 0.02):+6.1f}% | {len(b):4d} {roi(b, 0.02):+6.1f}%")


def teams():
    print("\nF. THE TEAMS THEY BACK (match and map markets), by money"); print(HEAD + '  games')
    by = defaultdict(list)
    for p in DONE:
        if p['type'] in ('moneyline', 'child_moneyline'): by[p['team']].append(p)
    for t, P in sorted(by.items(), key=lambda kv: -sum(p['cost'] for p in kv[1]))[:22]:
        print(line(t[:40], P) + f"  {len({p['event'] for p in P}):5d}")
    print("  does a team's history with these wallets carry over? Positions grouped by how many EARLIER games the wallets had backed that team in:")
    seen = defaultdict(set); grp = defaultdict(list)
    for p in sorted(DONE, key=lambda p: p['t']):
        if p['type'] not in ('moneyline', 'child_moneyline'): continue
        n = len(seen[p['team']] - {p['event']})
        grp['first time' if n == 0 else '1-2 earlier games' if n <= 2 else '3-9 earlier games' if n <= 9 else '10-29 earlier games' if n <= 29 else '30+ earlier games'].append(p)
        seen[p['team']].add(p['event'])
    for k in ('first time', '1-2 earlier games', '3-9 earlier games', '10-29 earlier games', '30+ earlier games'): print(line(k, grp[k]))


def games():
    print("\nG. THEIR GAMES AGAINST NORMAL GAMES (match markets in the history pack, lower-tier CS2, same months)")
    backed = defaultdict(lambda: defaultdict(lambda: [0.0, set()]))
    for p in POS:
        if p['type'] == 'moneyline' and p['cid'] in PACK and p['pre']:        # money placed BEFORE the start only
            x = backed[p['cid']][p['oi']]; x[0] += p['cost']; x[1].add(p['w'])
    t_first = min(PACK[c]['s'] for c in backed) if backed else 0

    def feats(d, side):
        """For `side` (0/1): price before the start, lowest and highest price during play, won?"""
        pr = [x / 1000 for x in d['p'][1::2]]
        if side == 1: pr = [1 - x for x in pr]
        pre = d['pre'] if side == 0 else 1 - d['pre']
        mid = pr[:-2] if len(pr) > 4 else pr
        return pre, (min(mid) if mid else pre), (max(mid) if mid else pre), d['w'] == side
    sus, norm = [], []
    for cid, d in PACK.items():
        if d['s'] < t_first or not d.get('p') or d.get('w') not in (0, 1): continue
        b = backed.get(cid)
        if b:
            oi = max(b, key=lambda k: b[k][0]); money, ws = b[oi]
            other = b[1 - oi][0] if (1 - oi) in b else 0.0
            if money >= 300 and len(ws) >= 2 and money >= 3 * other: sus.append((d, oi, money, len(ws)))
            continue
        if d.get('low'): norm.append(d)
    print(f"  games where 2+ tracked wallets put $300+ on one side BEFORE the start (3x the other side): {len(sus)}; lower-tier games they did not touch: {len(norm)}")
    print(f"  {'':46s} {'games':>6s} {'won':>5s} {'priced':>6s} | of the wins: fell 15+ pts below the start price / traded at 30c or less | of the losses: rose 15+ pts above / traded at 70c+")

    def show(name, F):
        if not F: return
        W = [f for f in F if f[3]]; L = [f for f in F if not f[3]]
        print(f"  {name:46s} {len(F):6d} {len(W) * 100 / len(F):4.0f}% {sum(f[0] for f in F) * 100 / len(F):5.0f}% | {sum(f[1] <= f[0] - 0.15 for f in W) * 100 / max(len(W), 1):6.0f}% {sum(f[1] <= 0.30 for f in W) * 100 / max(len(W), 1):6.0f}%"
              f"   | {sum(f[2] >= f[0] + 0.15 for f in L) * 100 / max(len(L), 1):6.0f}% {sum(f[2] >= 0.70 for f in L) * 100 / max(len(L), 1):6.0f}%")
    S = [feats(d, oi) for d, oi, m, n in sus]
    for a, b in ((0, .35), (.35, .5), (.5, .65), (.65, .8), (.8, 1.01)):
        show(f"their side, priced {int(a * 100)}-{int(min(b, 1) * 100)}c before the start", [f for f in S if a <= f[0] < b])
        N = [feats(d, s) for d in norm for s in (0, 1)]
        show("   normal games, same price band", [f for f in N if a <= f[0] < b])
    show("their side, all", S)
    big = [feats(d, oi) for d, oi, m, n in sus if m >= 2000 or n >= 4]
    show("their side, $2,000+ or 4+ wallets", big)
    json.dump([dict(cid=d['cid'], teams=d['n'], side=oi, money=m, wallets=n, start=d['s'], pre=d['pre'], w=d['w']) for d, oi, m, n in sus], open(R + '/sus_games.json', 'w'))


def speed():
    print("\nH. WHAT THE PRICE DOES AFTER THEY BUY (live buys on match markets with a recorded price path; cents, in their favour = +)")
    import bisect
    rows = []
    for p in DONE:
        d = PACK.get(p['cid'])
        if not d or p['pre'] or p['type'] != 'moneyline' or not d.get('p'): continue
        T = d['p'][0::2]; V = [x / 1000 if p['oi'] == 0 else 1 - x / 1000 for x in d['p'][1::2]]
        for ts, px, sh, usd in p['buys'][:1]:
            t = ts - d['s']
            def at(dt):
                i = bisect.bisect_right(T, t + dt) - 1
                return V[i] if i >= 0 and T[i] >= t - 600 else None
            a0, a1, a5, a15 = at(0), at(60), at(300), at(900)
            if a0 is None or abs(a0 - px) > 0.08: continue          # the path must agree with their price at that moment
            rows.append((usd, px, a1, a5, a15, p['won'], p['w']))
    if not rows: print('  no rows'); return
    tot = sum(r[0] for r in rows)
    for i, nm in ((2, '1 min later'), (3, '5 min later'), (4, '15 min later')):
        X = [(r[0], r[i] - r[1]) for r in rows if r[i] is not None]
        print(f"  {nm:14s} {len(X):5d} buys: price {sum(u * d for u, d in X) / sum(u for u, d in X) * 100:+5.1f}c (money-weighted), median {st.median(d for u, d in X) * 100:+5.1f}c, moved their way in {sum(d > 0.005 for u, d in X) * 100 // len(X)}%, against in {sum(d < -0.005 for u, d in X) * 100 // len(X)}%")
    print(f"  ({len(rows)} first buys, ${tot:,.0f}; a copier who is 1-5 minutes late pays about that much more)")


def maps():
    print("\nI. MAP BY MAP: what they do inside one series (BO3 events where they traded map markets)")
    ev = defaultdict(list)
    for p in DONE:
        if p['type'] in ('moneyline', 'child_moneyline'): ev[p['event']].append(p)
    def mapno(p):
        t = (M.get(p['cid']) or {}).get('title') or ''
        for k in (1, 2, 3, 4, 5):
            if f'Map {k}' in t or f'Game {k}' in t: return k
        return 0
    both = split = same = 0; S = defaultdict(list)
    for e, P in ev.items():
        bym = defaultdict(lambda: defaultdict(float))
        for p in P: bym[mapno(p)][p['team']] += p['cost']
        maps_ = {k: max(v, key=v.get) for k, v in bym.items() if k > 0 and max(v.values()) >= 200}
        if len(maps_) < 2: continue
        both += 1
        teams_ = set(maps_.values())
        if len(teams_) > 1: split += 1
        else: same += 1
        for k, t in maps_.items():
            for p in P:
                if mapno(p) == k and p['team'] == t: S[('split' if len(teams_) > 1 else 'same', k)].append(p)
    print(f"  series with $200+ on a side in two or more maps: {both}; same team in every map: {same}; DIFFERENT teams in different maps: {split}")
    print(HEAD)
    for k in sorted(S): print(line(f"{'different teams by map' if k[0] == 'split' else 'same team every map'}, map {k[1]}", S[k]))
    print("  by map number, all map positions:")
    for k in (1, 2, 3):
        X = [p for p in DONE if p['type'] == 'child_moneyline' and mapno(p) == k]
        print(line(f"   map {k}", X)); print(line(f"      bought before that map could have started (pre-match)", [p for p in X if p['pre']]))
    # the match market after map 1: do they buy the team that just lost a map?
    print("  match-winner buys during play, by the price path so far (history pack): side had been 20+ points higher earlier in the game (it is coming back from a lead lost) or 20+ lower (it has come back)")
    import bisect
    lost, gained, flat = [], [], []
    for p in DONE:
        d = PACK.get(p['cid'])
        if not d or p['pre'] or p['type'] != 'moneyline' or not d.get('p'): continue
        T = d['p'][0::2]; V = [x / 1000 if p['oi'] == 0 else 1 - x / 1000 for x in d['p'][1::2]]
        i = bisect.bisect_right(T, p['t'] - d['s'])
        hist = V[:i]
        if len(hist) < 3: continue
        if max(hist) - p['first_px'] >= 0.20: lost.append(p)
        elif p['first_px'] - min(hist) >= 0.20: gained.append(p)
        else: flat.append(p)
    print(line('   bought a side that had been 20+ pts higher', lost)); print(line('   bought a side that had been 20+ pts lower', gained)); print(line('   neither', flat))


if __name__ == '__main__':
    S = sys.argv[1:] or ['overall', 'wallets', 'forward', 'timing', 'converge', 'teams', 'games', 'speed', 'maps']
    for s in S: globals()[s]()
