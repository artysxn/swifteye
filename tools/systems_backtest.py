"""Backtest of every 'system' found on 20-21 Nov 2026, as rules on top of the bot's rules and of looser rules.
Backtest data: tools/sweep.py (17-19 Nov, Polymarket by its own clock, $12 a trade), with these signals computed for
EVERY candidate move from the database (so a rule changes which trades are taken, locks and one-a-game included):

  first     the trigger bookmaker moved before the other families (they had made under half of its move)   [lead_rule.py]
  follows   the bot's own per-game score: follow - lead >= 0.2 (Polymarket moves after the bookmakers here)
  verdict   the ahead/behind detector against the first bookmaker family to move, last 30 min             [ahead_behind.py]
  stands    D seconds after the move the trigger still holds half of it (a blip is gone by then); the buy is made
            D seconds after the move at the price really there
  hold      exits: catch-ups are sold as now, give-ups are held to the result                              [hold_test.py]

usage: systems_backtest.py            (first run reads the database for about 10 min, throttled; then cached)
"""
import sys, os, json, bisect, time, random, sqlite3, hashlib
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.argv = [sys.argv[0], 'lib']
import ahead_behind as AB
sys.argv = sys.argv[:1]
import sweep as S
db = AB.db
CACHE = os.path.expanduser('~/swifteye/research/systems_feats.json')
F = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
key = lambda c: f"{c['match_id']}|{c['outcome']}|{c['t0']}"
BASE = dict(S.BASE)
LOOSE = dict(S.BASE, min_move=0.07, cons=0.0, fams=1, lag=None, gap=None, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False)
ci = S.CATCH.index(S.BASE['catch'])
_hist = {}


def feats(c):
    m, o, t0, trig, d, mv = c['match_id'], c['outcome'], c['t0'], c['trigger'], c['dir'], c['_mv']
    books = {}
    rows = db.execute("SELECT ts_ms, book, new_price FROM changes WHERE match_id=? AND market='1x2' AND outcome=? AND ts_ms>=? AND ts_ms<=? AND new_price IS NOT NULL AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (m, o, t0 - 1_200_000, t0 + 61_000)).fetchall()
    for t, b, p in rows:
        books.setdefault(b, ([], [])); books[b][0].append(t); books[b][1].append(1 / max(p, 1.0001))
    def at(b, t):
        if b not in books: return None
        i = bisect.bisect_right(books[b][0], t); return books[b][1][i - 1] if i else None
    def others(t):
        fam = defaultdict(list)
        for b in books:
            if AB.fam(b) == AB.fam(trig): continue
            p = at(b, t)
            if p is not None: fam[AB.fam(b)].append(p)
        ms = sorted(sum(x) / len(x) for x in fam.values()); return ms[len(ms) // 2] if ms else None
    new, ob = at(trig, t0 + 1500), others(t0 - 1000)
    out = dict(toward=None, held5=None, held10=None, oth5=None, oth10=None)
    if new is not None and mv > 0:
        old = new - d * mv
        if ob is not None: out['toward'] = d * (ob - old) / mv
        for D in (5, 10):
            x = at(trig, t0 + D * 1000); out[f'held{D}'] = None if x is None else bool(d * (x - old) >= 0.5 * mv)
            y = others(t0 + D * 1000)
            if ob is not None and y is not None: out[f'oth{D}'] = d * (y - ob) / mv
    k = (m, o, t0 // 60_000)
    if k not in _hist:
        sm = AB.summary(AB.events(AB.history(m, o, t0), AB.WIN - 5)); v = AB.verdict(sm)
        _hist[k] = ('B' if v.startswith('BEHIND') else 'A' if v.startswith('AHEAD') else 'L' if v == 'level' else 'F', sm['n'])
    out['v'], out['ev'] = _hist[k]
    time.sleep(0.02 + len(rows) / 40000)
    return out


need = [c for c in S.ROWS if key(c) not in F and (S.gate(c, LOOSE, ci) or S.gate(c, dict(BASE, lag=None), ci) or S.gate(c, dict(BASE, lag=None, min_move=0.08), ci))]
if need:
    t_start = time.time()
    print(f"computing signals for {len(need)} candidate moves ...", flush=True)
    for i, c in enumerate(need):
        F[key(c)] = feats(c)
        if i % 400 == 399:
            json.dump(F, open(CACHE, 'w')); print(f"  {i + 1}/{len(need)}, {time.time() - t_start:.0f} s", flush=True)
    json.dump(F, open(CACHE, 'w'))
    print(f"done in {time.time() - t_start:.0f} s", flush=True)
for c in S.ROWS:
    f = F.get(key(c))
    if f is not None: f['fl'] = (c['cx']['follow'] - c['cx']['lead']) if c['cx']['jumps'] > 0 else None

# ---------------------------------------------------------------- rules
first = lambda f: f['toward'] is None or f['toward'] < 0.5
follows = lambda f: f['fl'] is not None and f['fl'] >= 0.2
notahead = lambda f: f['v'] != 'A'
behind = lambda f: f['v'] == 'B'
AND = lambda *fs: (lambda f: all(g(f) for g in fs))
W = {g[0]: (g[3], g[4]) for g in json.load(open(os.path.join(HERE, 'strategy_tests', 'games.json')))}
orig_gate = S.gate


def hold_pnl(t):
    c = t['c']; m = c['match_id']
    if m not in W or len(W[m][0]) != 2: return None
    outs, win = W[m]; side = c['outcome'] if c['dir'] > 0 else next(o for o in outs if o != c['outcome'])
    n = t['spent'] / t['entry']
    return n * ((1.0 if side == win else 0.0) - t['entry']) - n * S.fee(t['entry'])


def run(p, extra=None, hold=False):
    S.gate = (lambda c, pp, cc: orig_gate(c, pp, cc) and key(c) in F and extra(F[key(c)])) if extra else orig_gate
    try: T = S.sim(p)
    finally: S.gate = orig_gate
    if hold:
        for t in T:
            if not t['caught']:
                h = hold_pnl(t)
                if h is not None: t['pnl'] = h
    return T


half = lambda m: int(hashlib.md5(str(m).encode()).hexdigest()[:6], 16) % 2
random.seed(11)


def stats(T, base=None):
    n = len(T)
    if not n: return None
    T = sorted(T, key=lambda t: t['c']['tf'])
    cum = peak = dd = 0.0
    for t in T:
        cum += t['pnl']; peak = max(peak, cum); dd = max(dd, peak - cum)
    by = defaultdict(float)
    for t in T: by[t['c']['match_id']] += t['pnl']
    bb = defaultdict(float)
    for t in (base or []): bb[t['c']['match_id']] += t['pnl']
    games = list(set(by) | set(bb)); tot = []; dif = []
    for _ in range(1500):
        s = [random.choice(games) for _ in games]
        tot.append(sum(by.get(g, 0) for g in s)); dif.append(sum(by.get(g, 0) - bb.get(g, 0) for g in s))
    tot.sort(); dif.sort()
    day = defaultdict(float)
    for t in T: day[t['c']['_day']] += t['pnl']
    return dict(n=n, games=len(by), won=sum(t['pnl'] > 0 for t in T) / n, p=sum(t['pnl'] for t in T), dd=dd, worst=min(t['pnl'] for t in T), lo=tot[75], hi=tot[1425],
                day=[day.get(d, 0) for d in ('17', '18', '19', '20')], h=[sum(t['pnl'] for t in T if half(t['c']['match_id']) == k) for k in (0, 1)],
                better=sum(x > 0 for x in dif) / len(dif) if base is not None else None, dlo=dif[75], dhi=dif[1425])


def show(name, T, base=None):
    s = stats(T, base)
    if not s: print(f"  {name:50s} no trades"); return
    extra = '' if base is None else f" | vs baseline {s['p'] - sum(t['pnl'] for t in base):+7.2f} ({s['dlo']:+.0f} to {s['dhi']:+.0f}), better in {s['better'] * 100:3.0f}%"
    print(f"  {name:50s} {s['n']:4d} {s['won'] * 100:4.0f}% {s['p']:+9.2f} {s['p'] / s['n']:+6.2f} | drop {s['dd']:6.2f} worst {s['worst']:+6.2f} | range {s['lo']:+5.0f} to {s['hi']:+5.0f} | days {' '.join(format(x, '+6.1f') for x in s['day'])} | halves {s['h'][0]:+6.1f} {s['h'][1]:+6.1f}{extra}")


HDR = f"  {'':50s} {'n':>4s} {'won':>5s} {'profit':>9s} {'each':>6s} | max drop, worst trade | 90% range of profit | by day 17 / 18 / 19 / 20 Nov | two halves of the games | against its baseline"
print("\n== ON TOP OF THE BOT'S RULES (feed on time)"); print(HDR)
B0 = run(BASE); show('the bot as it is', B0)
for name, p, ex, hold in (
        ('+ trigger moved first', BASE, first, False), ('+ follows (follow - lead 0.2+)', BASE, follows, False), ('+ not AHEAD (detector)', BASE, notahead, False), ('+ BEHIND only (detector)', BASE, behind, False),
        ('+ first and not AHEAD', BASE, AND(first, notahead), False), ('+ first, follows and not AHEAD', BASE, AND(first, follows, notahead), False),
        ('give-ups held instead of sold', BASE, None, True), ('+ not AHEAD, give-ups held', BASE, notahead, True), ('+ first and not AHEAD, give-ups held', BASE, AND(first, notahead), True),
        ('wait 5 s, trigger must still stand', dict(BASE, delay=5000), lambda f: f['held5'] is True, False), ('wait 10 s, trigger must still stand', dict(BASE, delay=10000), lambda f: f['held10'] is True, False),
        ('wait 5 s, stands, others made 25%+ of it', dict(BASE, delay=5000), lambda f: f['held5'] is True and f['oth5'] is not None and f['oth5'] >= 0.25, False),
        ('wait 5 s only (no extra rule)', dict(BASE, delay=5000), None, False)):
    show(name, run(p, ex, hold), B0)
print("\n== THE BOT'S OWN DELAY RULE ('Polymarket lagged earlier in this game') REPLACED"); print(HDR)
N0 = run(dict(BASE, lag=None)); show('no delay rule at all', N0, B0)
for name, ex in (('replaced by: not AHEAD (detector)', notahead), ('replaced by: BEHIND only (detector)', behind), ('replaced by: follows (follow - lead 0.2+)', follows), ('replaced by: trigger moved first', first),
                 ('replaced by: first and not AHEAD', AND(first, notahead))):
    show(name, run(dict(BASE, lag=None), ex), B0)
print("\n== MINIMUM MOVE 8 POINTS (the earlier candidate), with the systems"); print(HDR)
M0 = run(dict(BASE, min_move=0.08)); show('move 8+ points', M0, B0)
for name, ex in (('move 8+, not AHEAD', notahead), ('move 8+, first', first), ('move 8+, first and not AHEAD', AND(first, notahead))):
    show(name, run(dict(BASE, min_move=0.08), ex), B0)
print("\n== ON TOP OF THE LOOSER RULES (does a system rescue a wider net?)"); print(HDR)
L0 = run(LOOSE); show('looser rules', L0)
for name, ex, hold in (('+ trigger moved first', first, False), ('+ follows (follow - lead 0.2+)', follows, False), ('+ not AHEAD (detector)', notahead, False), ('+ BEHIND only (detector)', behind, False),
                       ('+ first and follows', AND(first, follows), False), ('+ first, follows and not AHEAD', AND(first, follows, notahead), False), ('+ first, follows, not AHEAD, give-ups held', AND(first, follows, notahead), True),
                       ('give-ups held instead of sold', None, True)):
    show(name, run(LOOSE, ex, hold), L0)
show('wait 5 s, trigger must still stand', run(dict(LOOSE, delay=5000), lambda f: f['held5'] is True), L0)
show('wait 5 s, stands, others made 25%+', run(dict(LOOSE, delay=5000), lambda f: f['held5'] is True and f['oth5'] is not None and f['oth5'] >= 0.25), L0)

# ---------------------------------------------------------------- the live real trades, same rules
A = {r['id']: r for r in json.load(open('/private/tmp/claude-501/-Users-danielvinichenko--claude-spares-spare-gVuQbw/cd21609b-6e94-423f-8770-e4a80f6afcbe/scratchpad/audit.json'))} if os.path.exists('/private/tmp/claude-501/-Users-danielvinichenko--claude-spares-spare-gVuQbw/cd21609b-6e94-423f-8770-e4a80f6afcbe/scratchpad/audit.json') else {}
V = {t['id']: t['d'] for t in json.load(open(os.path.expanduser('~/swifteye/research/ahead_behind.json'))).get('LIVE real trades since 20 Nov', [])}
L = []
for i, data in db.execute("SELECT id, data FROM bot_trades WHERE real = 1 AND ts_ms >= 1795129200000 AND closed_ms IS NOT NULL ORDER BY ts_ms"):
    v = json.loads(data); a = A.get(i, {})
    tw = (a['others_before'] - a['trig_old']) / a['move'] if a.get('move') else None
    vd = AB.verdict(V[i]) if i in V else 'too few events'
    r = db.execute("SELECT bid FROM pm_quotes WHERE match_id = ? AND outcome = ? AND bid IS NOT NULL ORDER BY ts_ms DESC LIMIT 1", (v['match_id'], v['outcome'])).fetchone()
    held = None
    if r and (r[0] >= 0.97 or r[0] <= 0.03): held = v['contracts'] * ((1.0 if (r[0] >= 0.97) == (v['dir'] > 0) else 0.0) - v['entry']) - v['fee_in']
    caught = v['reason'] == 'caught up'
    L.append(dict(id=i, pnl=v['pnl'], mixed=v['pnl'] if caught or held is None else held, f=dict(toward=tw, fl=v['cx']['follow'] - v['cx']['lead'], v='B' if vd.startswith('BEHIND') else 'A' if vd.startswith('AHEAD') else 'L' if vd == 'level' else 'F')))
print(f"\n== THE {len(L)} LIVE REAL TRADES since 20 Nov under the same rules (the rules only REMOVE trades here)")
print(f"  {'':44s} {'n':>3s} {'won':>4s} {'sold as now':>12s} {'give-ups held':>14s}")
for name, ex in (('all', None), ('trigger moved first', first), ('follows (follow - lead 0.2+)', follows), ('not AHEAD (detector)', notahead), ('BEHIND only (detector)', behind), ('first and not AHEAD', AND(first, notahead)),
                 ('first, follows and not AHEAD', AND(first, follows, notahead)), ('follows and not AHEAD', AND(follows, notahead))):
    X = [t for t in L if ex is None or ex(t['f'])]
    print(f"  {name:44s} {len(X):3d} {sum(t['pnl'] > 0.005 for t in X):4d} {sum(t['pnl'] for t in X):+12.2f} {sum(t['mixed'] for t in X):+14.2f}")
