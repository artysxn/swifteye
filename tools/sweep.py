"""Multi-configuration backtest of the snipe rule set.

Data: sweep_build.py's dataset (every bookmaker move of 7+ points a loose rule set could take, 17 Nov 10:49 -
19 Nov 19:35 UTC, with the replay's fills and exits and Polymarket's own-clock quotes around each move).

Every configuration is replayed the way the live bot works: the rules decide, the game is locked, the order lands
when a real order lands, the buy must still be there by Polymarket's own clock, the sale follows the exit rule.
$12 a trade unless the stake is what is being varied. Fees 5% x p x (1-p) each way, 1c off every sale.

usage: sweep.py check | one | search [N] | money | all
"""
import os, sys, pickle, random, math, datetime as dt, statistics as st, json, time
from collections import Counter, defaultdict

D = pickle.load(open(os.path.expanduser('~/swifteye/research/sweep_%s.pickle' % os.environ.get('SWEEP_DATA', 'v16')), 'rb'))   # SWEEP_DATA=v18: 17 Nov - 22 Nov 01:00
ROWS, DELAYS = D['rows'], D['delays']
# Moves measured inside a gap of the Polymarket recording are not real (TRADING-TRIED.md, 21 Nov 2026: 37 of the 70
# trades of 17-20 Nov, +$103 of +$108). A set built before the recorder marked them gets the flags measured so far
# from tools/hole_check2.py; a move with no flag at all is kept, and the warning below says how many there are.
_H = json.load(open(os.path.expanduser('~/swifteye/research/hole_gap.json'))) if os.path.exists(os.path.expanduser('~/swifteye/research/hole_gap.json')) else {}
_L = json.load(open(os.path.expanduser('~/swifteye/research/feed_lag.json'))) if os.path.exists(os.path.expanduser('~/swifteye/research/feed_lag.json')) else {}
# hole_gap_full.flag = tools/hole_flag_all.py went through every tradable move: one it could not measure (under 3 other
# live games to compare with, and no feed-delay reading) is then treated as inside a gap, because it cannot be trusted.
_FULL = os.path.exists(os.path.expanduser('~/swifteye/research/hole_gap_full.flag'))
for c in ROWS:
    if c.get('blind') is None:
        k = f"{c['match_id']}|{c['outcome']}|{c['t0']}"; h = _H.get(k)
        c['blind'] = (h['gap'] > 2.0) if h else ((_L[k] >= 300) if _L.get(k) is not None else (True if _FULL and not c['nofill'] else None))
# The v18 blocks carry their own gap flag, which misses most of 18 Nov's gap buys (22 Nov 2026: 36 of 52 trades that day,
# +$69, sit in gaps by hole_gap.json / feed_lag.json). Add the stricter checks wherever they measured the move.
if os.environ.get('SWEEP_DATA', 'v16') != 'v16':
    for c in ROWS:
        k = f"{c['match_id']}|{c['outcome']}|{c['t0']}"; h = _H.get(k)
        strict = (h['gap'] > 2.0) if h else ((_L[k] >= 300) if _L.get(k) is not None else None)
        if strict: c['blind'] = True
_unk = sum(1 for c in ROWS if c['blind'] is None and not c['nofill'])
# Gap moves are dropped by default only once EVERY move carries the flag (a set rebuilt from v17 blocks): dropping some
# and not others would let unflagged gap moves take the freed places.
CLEAN_DEFAULT = _unk == 0
if _unk:
    print(f"!! sweep: {_unk} of {len(ROWS)} moves have no recording-gap flag. Results from this set are INFLATED by buys inside gaps "
          f"(17-18 Nov). Use tools/tells.py --clean or tells_final.py, or rebuild the set from the v17 replay blocks.", file=sys.stderr)
CATCH = [0.50, 0.75, 1.00]; GIVE = [60, 120, 300, 900, 1800]; CAPW = 0.02; MIN_FILL = 10.0
STOP_NAMES = {None: 'no early sale', 0: 'stop-loss at 25% of the move', 1: 'stop-loss at 50% of the move', 2: 'stop-loss at 100% of the move',
              3: 'sell when the bookmakers give back half', 4: 'stop-loss 50% + bookmakers give back'}
U = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
fee = lambda p: 0.05 * p * (1 - p)


def platform(b):
    if b in ('linebet', 'melbet', 'betwinner', 'megapari', 'paripesa', '22bet', '22bit', '1xbit'): return '1xbet'
    if b in ('unibet', 'leovegas', 'paf'): return 'kambi'
    if b in ('fonbet', 'pari', 'mostbet'): return 'fonbet'
    if b in ('vinn', 'betking'): return 'altenar'
    return b


for c in ROWS:
    cx = c['cx']; d = c['dir']
    c['_mv'] = c['bm'][0]
    c['_fol'] = max(max(c['fol']), c['fol_start'])
    c['_sp'] = max(c['sp'])
    c['_px'] = max(c['dec']) if c['dec'][0] > 0 else c['px'] + 0.01
    c['_gap'] = (cx['cons_now'] - cx['cons_move'] * d - c['pre_mid']) * d if c['pre_mid'] > 0 else None
    c['_emin'] = [min(c['edge'][0][i], c['edge'][1][i]) for i in range(3)]
    c['_books'] = min(c['books'])
    c['_hour'] = U(c['tf']).hour; c['_day'] = U(c['tf']).strftime('%d')
    c['_weak'] = platform(c['trigger']) in ('duel', '1xbet', 'olimp', 'betcity')
    s = (c.get('leagues') or '').lower() + ' ' + (c['home'] + ' ' + c['away']).lower()
    c['_usbb'] = c['sport'] == 'basketball' and (('nba' in s.replace('nbb', '') and 'wnba' not in s) or 'ncaa' in s or 'college' in s or '(stud)' in s or 'cbb' in s)
    # What Polymarket had really done 1.5 s after the move, by its own clock (None = not known).
    q = c['q_15']
    c['_tfol'] = ((q[0] + q[1]) / 2 - c['pre_mid']) * d / c['bm'][1] if q else None

# The rules the real bot runs today.
BASE = dict(
    min_move=0.08, max_move=None, fams=2, books=1, cons=0.03, max_age=None, rev=None, noise=None, pm_trend=None, book_trend=None,
    max_followed=0.05, max_spread=0.04, min_edge=0.04, cap=0.90, floor=None, lag=0.5, gap=0.10, lead=None, z=None, shock=None,
    # since 21 Nov 2026: moves 8+ pt, lagged 50%+, basketball outside the US in, Dota 2 only with a spread up to 2c,
    # no weak game (delay score under 0.2 or no earlier jump) at a price over 60c
    bb='nonus', skip=(), dota_spread=0.02, weak_cap=0.60, pause=True, weak=False, hours=None,
    catch=1.0, give=300, stop=None,
    lock=120, one_per_game=True, max_per_game=None, after_loss=False,
    delay=None, slip=0.02, stake=12.0, walk=True, legacy=False,
)


def gate(c, p, ci):
    cx = c['cx']
    if c['blind'] and p.get('clean', CLEAN_DEFAULT): return False        # inside a recording gap: not a real move
    if c['_mv'] < p['min_move']: return False
    if c['_fol'] > p['max_followed']: return False
    if cx['fam_moved'] < p['fams'] or cx['cons_move'] < p['cons']: return False
    if c['_sp'] > p['max_spread'] + 1e-9: return False
    if c['_emin'][ci] < p['min_edge']: return False
    if c['sport'] == 'basketball' and (p['bb'] == 'none' or (p['bb'] == 'nonus' and c['_usbb'])): return False
    if c['sport'] in p['skip']: return False
    if c['sport'] == 'dota2' and p.get('dota_spread') is not None and c['_sp'] > p['dota_spread'] + 1e-9: return False
    if p.get('weak_cap') is not None and c['_px'] - 0.01 > p['weak_cap'] + 1e-9 and (cx['jumps'] == 0 or cx['follow'] - cx['lead'] < 0.2): return False
    if p['pause'] and c['pause']: return False
    if c['_px'] > p['cap']: return False
    if p['lag'] is not None and (cx['jumps'] == 0 or cx['follow'] < p['lag']): return False
    if p['gap'] is not None and c['_gap'] is not None and c['_gap'] >= p['gap']: return False
    if c['_books'] < p['books']: return False
    if p['max_move'] is not None and c['_mv'] > p['max_move']: return False
    if p['floor'] is not None and c['_px'] < p['floor']: return False
    if p['max_age'] is not None and c['late_ms'] > p['max_age']: return False
    if p['lead'] is not None and cx['jumps'] > 0 and cx['lead'] > p['lead']: return False
    if p['z'] is not None and cx['z'] < p['z']: return False
    if p['shock'] is not None and cx['gap_shock'] < p['shock']: return False
    if p['pm_trend'] is not None and c['pm_tr'] < -p['pm_trend']: return False
    if p['book_trend'] is not None and c['b_tr10'] < -p['book_trend']: return False
    if p['rev'] is not None and c['b_tr5'] < p['rev'] * c['bm'][1]: return False
    if p['noise'] is not None and c['_mv'] < p['noise'] * c['b_rng']: return False
    if p['weak'] and c['_weak']: return False
    if p['hours'] is not None:
        a, b = p['hours']; h = c['_hour']
        if not (a <= h < b if a < b else (h >= a or h < b)): return False
    # Polymarket's true state: with the feed on time the bot sees this and does not try.
    if not p['legacy'] and c['_tfol'] is not None and c['_tfol'] > p['max_followed']: return False
    return True


def trade(c, x, p, ci):
    """One trade: (pnl, spent, entry, exit) or None when the buy was not really there."""
    stake = p['stake']; px0 = c['px']
    walk = p['walk'] and c['edge'][1][ci] - CAPW >= p['min_edge'] - 1e-9
    if c['buy']:
        best, near = c['buy']; near = near if walk else best
        a = min(stake / px0, max(best, 0.0)); b = max(min((stake - a * px0) / (px0 + 0.02), max(near - best, 0.0)), 0.0)
        n = a + b; entry = (a * px0 + b * (px0 + 0.02)) / n if n > 0 else px0
        if n <= 0 or n * entry < MIN_FILL: return None
    else:
        n, entry = stake / px0, px0
    q = c['q_tf'] if p['delay'] is None else c['q_d'][DELAYS.index(p['delay'])]
    if not q: return None
    if p['legacy'] and (c['_tfol'] is None or c['_tfol'] > p['max_followed']): return None
    buy = q[1] if c['dir'] > 0 else 1 - q[0]
    if buy - entry > p['slip'] + 1e-9: return None
    entry = max(buy, entry)
    if x[2] is not None:
        best, near = x[2], x[3]; a = min(n, max(best, 0.0)); b = min(n - a, max(near - best, 0.0)); cc = n - a - b
        ex = max((a * x[1] + b * (x[1] - 0.02) + cc * (x[1] - 0.05)) / n, 0.0)
    else:
        ex = x[1]
    # The replay sells 1c under the bids, as the PAPER bot does (snipebot::monitor). A REAL sale is a limit order that fills
    # at the standing bids, about 1c better (22 Nov 2026, tools/replay_vs_live.py): real_sale=True prices it that way.
    if p.get('real_sale') and 0 < ex < 1: ex = min(1.0, ex + 0.01)
    pnl = n * (ex - entry) - n * (fee(entry) + (fee(ex) if 0 < ex < 1 else 0))
    return pnl, n * entry, entry, ex


def sim(p, rows=ROWS):
    ci = CATCH.index(p['catch']); gi = GIVE.index(p['give']); si = p['stop']; lock = p['lock'] * 1000
    out, recent, busy, cnt, lost = [], {}, {}, Counter(), set()
    for c in rows:
        if not gate(c, p, ci): continue
        m = c['match_id']; tf = c['tf']
        if lock and m in recent and tf - recent[m] < lock: continue
        recent[m] = tf
        if c['nofill']: continue
        x = c['stops'][si][ci][gi] if si is not None else c['exits'][ci][gi]
        if not x: continue
        if p['one_per_game'] and busy.get(m, 0) > tf: continue
        if p['max_per_game'] and cnt[m] >= p['max_per_game']: continue
        if p['after_loss'] and m in lost: continue
        if p['legacy']: busy[m] = x[0]
        t = trade(c, x, p, ci)
        if t is None: continue
        busy[m] = max(busy.get(m, 0), x[0]); cnt[m] += 1
        if t[0] < 0: lost.add(m)
        out.append(dict(c=c, pnl=t[0], spent=t[1], entry=t[2], exit=t[3], xts=x[0], caught=x[4]))
    return out


def boot(T, n=300, seed=5):
    by = defaultdict(float)
    for t in T: by[t['c']['match_id']] += t['pnl']
    v = list(by.values())
    if len(v) < 5: return float('nan'), float('nan')
    rnd = random.Random(seed); s = sorted(sum(rnd.choice(v) for _ in v) for _ in range(n))
    return s[int(n * .05)], s[int(n * .95)]


def stats(T):
    if not T: return dict(n=0, tot=0.0, won=0, per=0.0, dd=0.0, worst=0.0, streak=0, days={'17': 0.0, '18': 0.0, '19': 0.0}, games=0, aw=0.0, al=0.0, big=0)
    tot = sum(t['pnl'] for t in T); sp = sum(t['spent'] for t in T)
    bal = peak = dd = 0.0; streak = cur = 0
    for t in sorted(T, key=lambda t: t['xts']):
        bal += t['pnl']; peak = max(peak, bal); dd = max(dd, peak - bal)
        cur = cur + 1 if t['pnl'] <= 0 else 0; streak = max(streak, cur)
    days = {'17': 0.0, '18': 0.0, '19': 0.0}
    for t in T: days[t['c']['_day']] = days.get(t['c']['_day'], 0.0) + t['pnl']
    w = [t['pnl'] for t in T if t['pnl'] > 0]; l = [t['pnl'] for t in T if t['pnl'] <= 0]
    return dict(n=len(T), games=len({t['c']['match_id'] for t in T}), won=len(w), tot=tot, per=tot / sp * 100, dd=dd, worst=min(t['pnl'] for t in T), streak=streak, days=days,
                aw=st.mean(w) if w else 0.0, al=st.mean(l) if l else 0.0, big=sum(t['pnl'] <= -5 for t in T))


HEAD = f"  {'setting':46s} {'trades':>6s} {'won':>5s} {'profit':>8s} {'per $':>6s} {'avg win':>7s} {'avg loss':>8s} {'worst':>7s} {'max drop':>8s} {'-$5+':>4s} {'row':>3s}  {'17 Nov':>7s} {'18 Nov':>7s} {'19 Nov':>7s}  90% range"


def row(name, T, rng=True, mark=''):
    s = stats(T)
    if not s['n']: return f"  {name:46s} {0:6d}"
    lo, hi = boot(T) if rng else (float('nan'), float('nan'))
    return (f"{mark or ' '} {name:46s} {s['n']:6d} {s['won'] * 100 / s['n']:4.0f}% {s['tot']:+8.2f} {s['per']:+5.1f}% {s['aw']:+7.2f} {s['al']:+8.2f} {s['worst']:+7.2f} {s['dd']:8.2f} {s['big']:4d} {s['streak']:3d}  "
            f"{s['days']['17']:+7.1f} {s['days']['18']:+7.1f} {s['days']['19']:+7.1f}  {lo:+5.0f} .. {hi:+5.0f}")


def table(title, variants, note=None):
    print(f"\n{title}"); print(HEAD)
    if note: print(f"  ({note})")
    for name, kw in variants:
        p = dict(BASE, **kw)
        print(row(name, sim(p), mark='>' if all(BASE[k] == v for k, v in kw.items()) else ''))


def check():
    """Must agree with offpeak_rules.py / other_sports.py: 54 trades, +$89.08 for today's rules (their order of checks)."""
    T = sim(BASE)
    print(f"{len(ROWS)} moves, {sum(1 for c in ROWS if not c['nofill'])} with a fill; span {U(ROWS[0]['tf']):%d %b %H:%M} - {U(ROWS[-1]['tf']):%d %b %H:%M} UTC")
    print(HEAD); print(row("today's rules", T, mark='>'))
    ages = sorted(c['late_ms'] for c in ROWS); tfd = sorted(c['tf'] - c['t0'] for c in ROWS)
    print(f"  age of the move when this bookmaker joined it (ms): median {ages[len(ages) // 2]}, 75% {ages[len(ages) * 3 // 4]}, 90% {ages[len(ages) * 9 // 10]}")
    print(f"  the order lands (replay) this long after the bookmaker move (ms): median {tfd[len(tfd) // 2]}, 10% {tfd[len(tfd) // 10]}, 90% {tfd[len(tfd) * 9 // 10]}")


def one():
    table("1. HOW BIG THE MOVE MUST BE (now 10 points)", [(f"{int(v * 100)} points or more", dict(min_move=v)) for v in (0.07, 0.08, 0.09, 0.10, 0.11, 0.12, 0.14, 0.16, 0.20)])
    table("   ... and a ceiling on the move", [(('no ceiling' if v is None else f"at most {int(v * 100)} points"), dict(max_move=v)) for v in (None, 0.30, 0.20, 0.15)])
    table("2. HOW MANY BOOKMAKER GROUPS MUST MOVE (now 2)", [(f"{v} group{'s' if v > 1 else ''}", dict(fams=v)) for v in (1, 2, 3, 4, 5)])
    table("   ... how far the average of all bookmakers must move (now 3 points)", [(f"{v * 100:.0f} points", dict(cons=v)) for v in (0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.07)])
    table("   ... bookmakers that moved by the time we decide (now 1; groups above is the real test)", [(f"{v} bookmaker{'s' if v > 1 else ''}", dict(books=v)) for v in (1, 2, 3)])
    table("3. IN HOW MUCH TIME: how old the move may be when this bookmaker joins it (now any)",
          [(('any age' if v is None else f"at most {v / 1000:.0f} s old"), dict(max_age=v)) for v in (None, 60000, 20000, 10000, 5000, 2000, 1000)])
    table("   ... the move must not be a bounce (bookmaker's net move over 5 min at least this share of the jump; now off)",
          [(('off' if v is None else f"{int(v * 100)}% of the jump"), dict(rev=v)) for v in (None, 0.0, 0.25, 0.5, 0.75)])
    table("   ... the jump must stand out from the game's usual swings (jump at least k x the 10-min range; now off)",
          [(('off' if v is None else f"{v} x the range"), dict(noise=v)) for v in (None, 0.5, 0.75, 1.0)])
    table("4. WHEN WE BUY: if the order landed this long after the bookmaker move (now: when a real order lands, about 4 s)",
          [(('as the replay' if v is None else f"{v / 1000:g} s after"), dict(delay=v)) for v in (None, 1000, 1500, 2000, 3000, 4000, 5000, 7000, 10000, 15000, 30000)])
    table("   ... how far past the first price we still buy (now 2c)", [(f"{int(v * 100)}c", dict(slip=v)) for v in (0.0, 0.01, 0.02, 0.03, 0.05, 0.10)])
    table("5. HOW MUCH POLYMARKET MAY ALREADY HAVE MOVED (now 5% of the move)", [(f"{int(v * 100)}%", dict(max_followed=v)) for v in (0.0, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30)])
    table("6. SPREAD ALLOWED (now 4c)", [(f"{int(round(v * 100))}c", dict(max_spread=v)) for v in (0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10)])
    table("7. ROOM LEFT AFTER COSTS (now 4c)", [(f"{int(round(v * 100))}c", dict(min_edge=v)) for v in (0.0, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10)])
    table("8. PRICE WE BUY AT (now up to 90c)", [(f"up to {int(v * 100)}c", dict(cap=v)) for v in (0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.98)]
          + [(f"{int(v * 100)}c or more", dict(floor=v)) for v in (0.10, 0.20, 0.30)])
    table("9. SELLING WHEN IT WORKS: share of the bookmakers' move Polymarket must make up (now 100%)", [(f"{int(v * 100)}%", dict(catch=v)) for v in CATCH])
    table("10. SELLING WHEN IT DOES NOT: give up after (now 5 min)", [(f"{v} s", dict(give=v)) for v in GIVE])
    table("    ... early sale when losing (now none)", [(STOP_NAMES[v], dict(stop=v)) for v in (None, 0, 1, 2, 3, 4)])
    table("    ... exit and give-up together", [(f"sell at {int(cv * 100)}%, give up {g} s", dict(catch=cv, give=g)) for cv in CATCH for g in (120, 300, 900)])
    table("11. THE SAME GAME AGAIN: lock after a move was taken (now 2 min)", [(f"{v} s", dict(lock=v)) for v in (0, 30, 60, 120, 300, 600, 1800)])
    table("    ... positions and repeats in one game (now one open position, any number of trades)",
          [("one open position a game", {}), ("several open at once", dict(one_per_game=False)), ("several open, no lock", dict(one_per_game=False, lock=0)),
           ("at most 1 trade a game", dict(max_per_game=1)), ("at most 2 trades a game", dict(max_per_game=2)), ("at most 3 trades a game", dict(max_per_game=3)),
           ("no more trades in a game after a loss in it", dict(after_loss=True))])
    table("12. POLYMARKET MUST HAVE LAGGED EARLIER IN THIS GAME (now: followed 30%+ of earlier jumps)",
          [(('rule off' if v is None else f"followed {int(v * 100)}%+"), dict(lag=v)) for v in (None, 0.0, 0.2, 0.3, 0.5, 0.7, 1.0)])
    table("    ... the two must not already be apart before the move (now 10 points)", [(('rule off' if v is None else f"under {int(v * 100)} points apart"), dict(gap=v)) for v in (None, 0.20, 0.15, 0.10, 0.07, 0.05, 0.03)])
    table("13. OTHER SWITCHES", [("today's rules", {}), ("also right after a pause in play", dict(pause=False)), ("skip the weak feeds as trigger", dict(weak=True)),
                                ("basketball outside the US let in", dict(bb='nonus')), ("all basketball let in", dict(bb='all')),
                                ("no Dota 2", dict(skip=('dota2',))), ("tennis only", dict(skip=('cs2', 'dota2', 'football', 'valorant'))), ("CS2 only", dict(skip=('tennis', 'dota2', 'football', 'valorant'))),
                                ("13:00-22:00 Norway only", dict(hours=(12, 21))), ("outside 13:00-22:00 Norway only", dict(hours=(21, 12))),
                                ("Polymarket's own trend not against us (10 min, 2 pt)", dict(pm_trend=0.02)), ("bookmaker's 10-min trend not against us (3 pt)", dict(book_trend=0.03))])


SPACE = dict(
    min_move=[0.07, 0.08, 0.09, 0.10, 0.11, 0.12, 0.14], fams=[1, 2, 3], cons=[0.0, 0.02, 0.03, 0.04, 0.05], max_followed=[0.0, 0.05, 0.10, 0.15, 0.20],
    max_spread=[0.02, 0.03, 0.04, 0.05, 0.06], min_edge=[0.02, 0.04, 0.05, 0.06, 0.08], cap=[0.80, 0.90, 0.95], lag=[None, 0.2, 0.3, 0.5, 0.7], gap=[None, 0.05, 0.10, 0.15],
    pause=[True, False], bb=['none', 'nonus', 'all'], skip=[(), ('dota2',)], catch=[0.75, 1.0], give=[120, 300, 900], stop=[None, 3], lock=[30, 120, 300],
    after_loss=[False, True], max_age=[None, 60000, 10000], weak=[False, True],
)


def summary(T):
    by_game = defaultdict(float); by_day = defaultdict(float)
    for t in T: by_game[t['c']['match_id']] += t['pnl']; by_day[t['c']['_day']] += t['pnl']
    s = stats(T)
    return dict(n=s['n'], won=s['won'], tot=s['tot'], per=s['per'], dd=s['dd'], worst=s['worst'], big=s['big'], game=dict(by_game), day=dict(by_day), ngame=Counter(t['c']['match_id'] for t in T))


def search(n=3000, seed=11):
    rnd = random.Random(seed); t0 = time.time()
    cfgs = [dict()] + [{k: rnd.choice(v) for k, v in SPACE.items()} for _ in range(n)]
    R = [(kw, summary(sim(dict(BASE, **kw)))) for kw in cfgs]
    base = R[0][1]; rest = [r for r in R[1:] if r[1]['n'] >= 15]
    print(f"\nCOMBINED SEARCH: {n} random mixes of {len(SPACE)} settings ({time.time() - t0:.0f} s); {len(rest)} of them made 15+ trades")
    tots = sorted(r[1]['tot'] for r in rest)
    q = lambda f: tots[int(len(tots) * f)]
    print(f"  profit across the mixes: worst {tots[0]:+.0f}, 10% {q(.1):+.0f}, median {q(.5):+.0f}, 90% {q(.9):+.0f}, best {tots[-1]:+.0f}")
    rank = sum(t > base['tot'] for t in tots)
    ra = lambda s: s['tot'] / max(s['dd'], 5.0)
    rank2 = sum(ra(r[1]) > ra(base) for r in rest)
    print(f"  today's rules: {base['n']} trades, {base['tot']:+.2f}, max drop {base['dd']:.2f}; {rank} of {len(rest)} mixes made more profit ({rank * 100 // len(rest)}%), {rank2} had a better profit-to-drop ratio ({rank2 * 100 // len(rest)}%)")

    def show(title, key, k=8):
        print(f"\n  {title}")
        for kw, s in sorted(rest, key=key, reverse=True)[:k]:
            diff = ', '.join(f"{a}={b}" for a, b in kw.items() if BASE[a] != b)
            print(f"    {s['n']:3d} trades {s['won'] * 100 // s['n']:3d}% won {s['tot']:+8.2f} ({s['per']:+5.1f}%/$) drop {s['dd']:5.1f} worst {s['worst']:+6.1f} | 17: {s['day'].get('17', 0):+6.1f} 18: {s['day'].get('18', 0):+6.1f} 19: {s['day'].get('19', 0):+6.1f} | {diff}")
    show("MOST PROFIT (all three days; chosen with hindsight)", lambda r: r[1]['tot'])
    show("BEST PROFIT FOR THE RISK (profit / max drop)", lambda r: ra(r[1]))
    show("BEST ON THE WORST DAY (19 Nov), with 40+ trades", lambda r: r[1]['day'].get('19', 0) if r[1]['n'] >= 40 else -1e9)

    print("\n  WHAT THE BEST 10% (by profit) HAVE IN COMMON: share of each value among them, against its share among all mixes")
    top = sorted(rest, key=lambda r: r[1]['tot'], reverse=True)[:max(len(rest) // 10, 1)]
    for k, vals in SPACE.items():
        cell = []
        for v in vals:
            a = sum(r[0][k] == v for r in top) / len(top); b = sum(r[0][k] == v for r in rest) / len(rest)
            cell.append(f"{v}: {a * 100:.0f}% (all {b * 100:.0f}%)")
        strong = max(abs(sum(r[0][k] == v for r in top) / len(top) - sum(r[0][k] == v for r in rest) / len(rest)) for v in vals)
        print(f"   {'**' if strong >= 0.15 else '  '} {k:13s} " + ' | '.join(cell))

    print("\n  OUT OF SAMPLE: pick the best mix on part of the data, then see what it made on the rest")
    for test in ('17', '18', '19'):
        tr = lambda s: sum(v for d, v in s['day'].items() if d != test)
        cand = [r for r in rest if sum(1 for _ in r[1]['game']) >= 10]
        best = max(cand, key=lambda r: tr(r[1]))
        top20 = sorted(cand, key=lambda r: tr(r[1]), reverse=True)[:20]
        print(f"    test day {test} Nov: today's rules made {base['day'].get(test, 0):+7.2f} | the mix that was best on the other two days made {best[1]['day'].get(test, 0):+7.2f} "
              f"| the 20 best on the other days made {st.mean(r[1]['day'].get(test, 0) for r in top20):+7.2f} on average ({sum(r[1]['day'].get(test, 0) > base['day'].get(test, 0) for r in top20)} of 20 beat today's rules)")
    games = sorted({g for r in R for g in r[1]['game']}); rnd2 = random.Random(3); wins = 0; diffs = []; K = 40
    for _ in range(K):
        A = set(rnd2.sample(games, len(games) // 2))
        ina = lambda s: sum(v for g, v in s['game'].items() if g in A); inb = lambda s: sum(v for g, v in s['game'].items() if g not in A)
        best = max(rest, key=lambda r: ina(r[1]))
        d = inb(best[1]) - inb(base); diffs.append(d); wins += d > 0
    diffs.sort()
    print(f"    random halves of the games, {K} times: the mix that was best on one half beat today's rules on the other half {wins} times of {K}; "
          f"difference on the unseen half: median {diffs[K // 2]:+.1f}, from {diffs[K // 10]:+.1f} to {diffs[K * 9 // 10]:+.1f}")
    json.dump([dict(cfg={k: (list(v) if isinstance(v, tuple) else v) for k, v in kw.items()}, **{k: s[k] for k in ('n', 'won', 'tot', 'per', 'dd', 'worst', 'big', 'day')}) for kw, s in R],
              open(os.path.expanduser('~/swifteye/research/sweep_search.json'), 'w'))


def wallet(p, bank=40.0, pct=0.30, cap=12.0, edge_mult=True):
    """The real wallet's sizing: stake = pct x balance x signal strength, at most `cap`, at most the free cash; under $10 no trade."""
    ci = CATCH.index(p['catch']); gi = GIVE.index(p['give']); si = p['stop']; lock = p['lock'] * 1000
    recent, busy = {}, {}; openp = []; bal = bank; out = []; nocash = 0; low = bank; peak = bank; dd = 0.0
    for c in ROWS:
        if not gate(c, p, ci): continue
        m = c['match_id']; tf = c['tf']
        if lock and m in recent and tf - recent[m] < lock: continue
        recent[m] = tf
        if c['nofill']: continue
        x = c['stops'][si][ci][gi] if si is not None else c['exits'][ci][gi]
        if not x or (p['one_per_game'] and busy.get(m, 0) > tf): continue
        for o in [o for o in openp if o[0] <= tf]:
            bal += o[2]; openp.remove(o); low = min(low, bal); peak = max(peak, bal); dd = max(dd, peak - bal)
        open_cost = sum(o[1] for o in openp)
        k = min(max(c['edge'][1][ci] / 0.04, 0.5), 3.0) if edge_mult else 1.0
        stake = min(pct * bal * k, cap, bal - open_cost)
        if stake < MIN_FILL: nocash += 1; continue
        t = trade(c, x, dict(p, stake=stake), ci)
        if t is None: continue
        busy[m] = x[0]; openp.append((x[0], t[1], t[0])); out.append(dict(c=c, pnl=t[0], spent=t[1], entry=t[2], exit=t[3], xts=x[0], caught=x[4]))
    for o in sorted(openp):
        bal += o[2]; low = min(low, bal); peak = max(peak, bal); dd = max(dd, peak - bal)
    return out, bal, low, dd, nocash


def money():
    print("\nHOW MUCH PER TRADE: the same trades at a fixed stake (fills limited by what was on offer; a bigger order pays more and sells for less)")
    print(f"  {'stake':>8s} {'trades':>6s} {'won':>5s} {'profit':>9s} {'per $':>7s} {'avg bought':>10s} {'worst':>8s} {'max drop':>9s}")
    for s in (12, 20, 30, 50, 100, 200, 500, 1000):
        T = sim(dict(BASE, stake=float(s))); x = stats(T)
        print(f"  {'$' + str(s):>8s} {x['n']:6d} {x['won'] * 100 // max(x['n'], 1):4d}% {x['tot']:+9.2f} {x['per']:+6.1f}% {sum(t['spent'] for t in T) / max(len(T), 1):10.2f} {x['worst']:+8.2f} {x['dd']:9.2f}")
    print("\nTHE WALLET: stake = share of the balance x signal strength, capped; under $10 no trade; nothing more open than the wallet holds (safety stops not applied)")
    print(f"  {'wallet':34s} {'trades':>6s} {'won':>5s} {'end':>9s} {'return':>7s} {'lowest':>8s} {'max drop':>9s} {'no cash':>7s}")
    for name, kw in [("$40, 30% x signal, at most $12 (now)", dict()), ("$40, 30%, at most $20", dict(cap=20.0)), ("$40, 30%, at most $40", dict(cap=40.0)), ("$40, 50%, at most $20", dict(pct=0.5, cap=20.0)),
                     ("$40, flat $12 (no signal sizing)", dict(pct=1.0, edge_mult=False)), ("$100, 30%, at most $12", dict(bank=100.0)), ("$100, 30%, at most $30", dict(bank=100.0, cap=30.0)),
                     ("$200, 30%, at most $60", dict(bank=200.0, cap=60.0)), ("$500, 30%, at most $150", dict(bank=500.0, cap=150.0)), ("$1,000, 30%, at most $300", dict(bank=1000.0, cap=300.0)),
                     ("$5,000, 30%, at most $1,500", dict(bank=5000.0, cap=1500.0))]:
        T, bal, low, dd, nocash = wallet(BASE, **kw); b0 = kw.get('bank', 40.0)
        print(f"  {name:34s} {len(T):6d} {sum(t['pnl'] > 0 for t in T) * 100 // max(len(T), 1):4d}% {bal:9.2f} {(bal / b0 - 1) * 100:+6.0f}% {low:8.2f} {dd:9.2f} {nocash:7d}")


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'check'
    if cmd in ('check', 'all'): check()
    if cmd in ('one', 'all'): one()
    if cmd in ('search', 'all'): search(int(sys.argv[2]) if len(sys.argv) > 2 else 3000)
    if cmd in ('money', 'all'): money()
