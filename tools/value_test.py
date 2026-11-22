"""The pre-match favourite bet, tested harder (owner, 21 Nov 2026: "Expand this test"; "not on games with a high
suspicion score"; "a lower amount than 1/4th of the wallet on favourites").

The bet (tools/value_bets.py): a favourite at 55-90c on Polymarket that the bookmakers' middle (3+ groups, margin
removed) rates 1+ point above the bet's cost (ask 30 s later + fee), game not started, bookmakers quiet for 10 min,
quote under 30 min old, spread <= 4c; one bet a game at the first check that qualifies; held to the result.

  A. UNSEEN games: settled after the bet was found (ev_unseen_games.json), never looked at before
  B. stress checks on every game: other thresholds, other ways to read the bookmakers, halves of the games, price bands
  C. the suspicion score (suspect.rs, CS2 only, 0-100): do high-score favourites fall more often?
  D. the stake: a share of the wallet per bet, the money tied up until the game ends
usage: value_test.py
"""
import json, os, random, sys, sqlite3, hashlib, statistics as st, datetime as dt
from collections import defaultdict, Counter
RES = os.path.expanduser('~/swifteye/research/')
R = json.load(open(RES + 'ev_snaps.json'))
UNSEEN = set(json.load(open(RES + 'ev_unseen_games.json'))) if os.path.exists(RES + 'ev_unseen_games.json') else set()
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
fee = lambda a: 0.05 * a * (1 - a)
NO = dt.timezone(dt.timedelta(hours=1))
random.seed(5)


def power(odds):
    pi = [1 / o for o in odds]
    if sum(pi) <= 1.0: return [p / sum(pi) for p in pi]
    lo, hi = 1.0, 6.0
    for _ in range(40):
        k = (lo + hi) / 2
        if sum(p ** k for p in pi) > 1: lo = k
        else: hi = k
    return [p ** k for p in pi]


grp = defaultdict(list)
for r in R: grp[(r['m'], r['t'])].append(r)
for rows in grp.values():
    full = len(rows) == rows[0]['k']
    fams = set.intersection(*[set(r['raw']) for r in rows]) if full else set()
    pw, pr = {}, {}
    for f in fams:
        od = [r['raw'][f] for r in rows]
        over = sum(1 / x for x in od)
        if 1.0 <= over <= 1.25:
            pw[f] = dict(zip([r['o'] for r in rows], power(od))); pr[f] = {r['o']: (1 / r['raw'][f]) / over for r in rows}
    for r in rows:
        v = sorted(x[r['o']] for x in pw.values()); u = sorted(x[r['o']] for x in pr.values())
        r['nb'] = len(v)
        r['mid_bk'] = st.median(v) if len(v) >= 3 else None           # the bookmakers' middle (margin removed, favourites keep less of it)
        r['mid_prop'] = st.median(u) if len(u) >= 3 else None          # the same with the margin split evenly
        r['low_bk'] = v[0] if len(v) >= 3 else None                    # the LOWEST bookmaker group
        r['pin'] = pw.get('pinnacle', {}).get(r['o'])
        r['day'] = dt.datetime.fromtimestamp(r['t'] / 1000, NO).strftime('%d')
        r['start'] = r['t'] - r['rel'] * 60_000
cost = lambda r: r['ask30'] + fee(r['ask30'])
roi = lambda x: sum(r['won'] - cost(r) for r in x) / sum(cost(r) for r in x) if x else float('nan')
half = lambda m: int(hashlib.md5(str(m).encode()).hexdigest()[:6], 16) % 2


def boot(b, n=1500):
    v = sorted(roi([random.choice(b) for _ in b]) for _ in range(n)); return v[int(n * .05)], v[int(n * .95)]


def first(rows):
    got = {}
    for r in sorted(rows, key=lambda r: r['t']): got.setdefault(r['m'], r)
    return list(got.values())


BASE = [r for r in R if r['rel'] < 0 and not r['live'] and r.get('ask30') is not None and r['mid_bk'] is not None]
OK = lambda r, sp=0.04, age=1800: r['ask'] - r['bid'] <= sp and r['age'] <= age


def bets(rows=None, thr=0.01, lo=0.55, hi=0.90, ref='mid_bk', sp=0.04, age=1800, nb=3):
    rows = BASE if rows is None else rows
    return first([r for r in rows if OK(r, sp, age) and lo <= r['ask30'] < hi and r[ref] is not None and r['nb'] >= nb and r[ref] - cost(r) >= thr])


def show(name, x, extra=''):
    if len(x) < 6: print(f"  {name:58s} {len(x):4d} bets: too few"); return
    l, h = boot(x)
    print(f"  {name:58s} {len(x):4d} bets | {100 * st.mean(r['won'] for r in x):3.0f}% won, needed {100 * st.mean(cost(r) for r in x):3.0f}% | {100 * roi(x):+5.1f}% a $ ({100 * l:+.0f} to {100 * h:+.0f}){extra}")


# ---------------------------------------------------------------- suspicion score (CS2 only)
_ev = {}
def score(m):
    if m not in _ev:
        e = db.execute("SELECT event_id FROM pm_quotes WHERE match_id=? LIMIT 1", (m,)).fetchone()
        s = db.execute("SELECT score, tier1 FROM cs2_activity WHERE event_id=?", (e[0],)).fetchone() if e else None
        _ev[m] = (s[0], s[1]) if s else (None, None)
    return _ev[m]


if __name__ == '__main__':
    seen = [r for r in BASE if r['m'] not in UNSEEN]; new = [r for r in BASE if r['m'] in UNSEEN]
    print(f"pre-match rows: {len(BASE)} in {len({r['m'] for r in BASE})} games; {len({r['m'] for r in new})} of the games were settled AFTER the bet was found (unseen)")

    print("\nA. UNSEEN GAMES (settled 21 Nov 00:30-05:15, never looked at when the bet was chosen)")
    show('the bet, unseen games', bets(new))
    show('the bet, the games it was found on', bets(seen))
    show('every favourite 55-90c, unseen games (no signal)', first([r for r in new if OK(r) and 0.55 <= r['ask30'] < 0.90]))
    for r in sorted(bets(new), key=lambda r: r['t']):
        print(f"       {dt.datetime.fromtimestamp(r['t'] / 1000, NO).strftime('%d %H:%M')} {r['sport']:10s} match {r['m']:6d} paid {100 * r['ask30']:4.1f}c, bookmakers' middle {100 * r['mid_bk']:4.1f}%, {r['nb']} groups, {r['rel']:+5d} min: {'WON' if r['won'] else 'lost'}")

    print("\nB. STRESS CHECKS, every game (the bet as chosen is marked >)")
    print("  how far above the cost the bookmakers must be")
    for thr in (0.0, 0.005, 0.01, 0.015, 0.02, 0.03):
        show(('> ' if thr == 0.01 else '  ') + f"{100 * thr:.1f} pt or more", bets(thr=thr))
    print("  how the bookmakers are read")
    show('> middle of the groups (margin removed, power method)', bets())
    show('  middle of the groups, margin split evenly', bets(ref='mid_prop'))
    show('  the LOWEST bookmaker group must be 1 pt above', bets(ref='low_bk'))
    show('  Pinnacle alone', bets(ref='pin'))
    for nb in (4, 5, 6): show(f'  middle, {nb}+ groups quoting', bets(nb=nb))
    print("  the market")
    show('  spread 2c or less', bets(sp=0.02)); show('  quote under 2 min old', bets(age=120))
    for lo, hi in ((0.55, 0.65), (0.65, 0.75), (0.75, 0.90), (0.50, 0.55), (0.90, 0.97)): show(f'  price {100 * lo:.0f}-{100 * hi:.0f}c', bets(lo=lo, hi=hi))
    print("  halves of the games (split by game number), days, sports")
    x = bets()
    for h in (0, 1): show(f'  half {h + 1}', [r for r in x if half(r['m']) == h])
    for d in sorted({r['day'] for r in x}): show(f'  {d} Nov', [r for r in x if r['day'] == d])
    for s in ('tennis', 'cs2', 'dota2', 'basketball', 'football'): show(f'  {s}', [r for r in x if r['sport'] == s])
    print("  is it the signal or just favourites? the same games' favourites WITHOUT the signal, and a shuffle test")
    nos = first([r for r in BASE if OK(r) and 0.55 <= r['ask30'] < 0.90 and r['mid_bk'] - cost(r) < 0.0])
    show('  favourites the bookmakers rate BELOW the cost', nos)
    allf = first([r for r in BASE if OK(r) and 0.55 <= r['ask30'] < 0.90])
    obs = roi(x); k = len(x); cnt = 0; N = 4000
    for _ in range(N):
        if roi(random.sample(allf, k)) >= obs: cnt += 1
    print(f"  {k} favourites picked at random from the {len(allf)} do as well as the bet in {100 * cnt / N:.1f}% of {N} draws")

    print("\nC. THE SUSPICION SCORE (CS2 only; 80+ is the app's alert level)")
    cs = first([r for r in BASE if r['sport'] == 'cs2' and OK(r, 0.06) and 0.50 <= r['ask30'] < 0.95])
    for r in cs: r['score'], r['tier1'] = score(r['m'])
    have = [r for r in cs if r['score'] is not None]
    print(f"  CS2 pre-match favourites (50-95c, one per game): {len(cs)}, with a score: {len(have)}")
    for lab, fn in (('score 80+', lambda s: s >= 80), ('score 60-80', lambda s: 60 <= s < 80), ('score under 60', lambda s: s < 60)):
        show(f'  favourite bought, {lab}', [r for r in have if fn(r['score'])])
    xb = [r for r in x if r['sport'] == 'cs2']
    for r in xb: r['score'], r['tier1'] = score(r['m'])
    show('  the bet in CS2, score 80+', [r for r in xb if r['score'] is not None and r['score'] >= 80])
    show('  the bet in CS2, score under 80 or none', [r for r in xb if r['score'] is None or r['score'] < 80])
    keep = [r for r in x if not (r['sport'] == 'cs2' and r.get('score') is not None and r['score'] >= 80)]
    show('THE BET WITHOUT HIGH-SCORE GAMES', keep)

    print("\nD. THE STAKE: $40 wallet, a share of the wallet on each bet, money tied up until 3 h after the listed start")
    seq = sorted(keep, key=lambda r: r['t'])
    span = (seq[-1]['t'] - seq[0]['t']) / 86_400_000
    print(f"  {len(seq)} bets over {span:.1f} days ({len(seq) / span:.0f} a day); price paid on average {100 * st.mean(r['ask30'] for r in seq):.0f}c; a lost bet loses the whole stake")
    for pct in (0.03, 0.05, 0.08, 0.10, 0.15, 0.25):
        def run(order):
            w = 40.0; openb = []; low = w; peak = w; dd = 0.0; skipped = 0; most = 0.0
            for r in order:
                done = [o for o in openb if o[0] <= r['t']]
                for o in done: w += o[1]
                openb = [o for o in openb if o[0] > r['t']]
                tied = sum(o[2] for o in openb); stake = pct * w
                if stake < 1.0 or tied + stake > 0.9 * w: skipped += 1; continue
                n = stake / cost(r); pnl = n * r['won'] - stake
                openb.append((r['start'] + 3 * 3_600_000, pnl, stake)); most = max(most, (tied + stake) / w)
                # mark the wallet as if settled now, for the drop
            for o in openb: w += o[1]
            return w, skipped, most
        w, sk, most = run(seq)
        sims = []
        for _ in range(1500):
            ww = 40.0
            for r in (random.choice(seq) for _ in seq):
                stake = pct * ww; ww += stake / cost(r) * r['won'] - stake
            sims.append(ww)
        sims.sort()
        print(f"  {100 * pct:4.0f}% of the wallet a bet: $40 -> ${w:6.2f} in order ({sk} skipped, at most {100 * most:.0f}% of the wallet tied up) | if the same bets came in another order or mix: 1 in 10 below ${sims[150]:.0f}, middle ${sims[750]:.0f}, 1 in 10 above ${sims[1350]:.0f}; ended under $40 in {100 * sum(s < 40 for s in sims) / 1500:.0f}%")
    p = st.mean(r['won'] for r in seq); c = st.mean(cost(r) for r in seq); b = (1 - c) / c
    for nm, pp in (('as measured', p), ('if the true win rate is halfway between measured and needed', (p + c) / 2), ('if the edge is a quarter of what was measured', c + (p - c) / 4)):
        k = (pp * b - (1 - pp)) / b
        print(f"  the stake that grows the wallet fastest {nm} ({100 * pp:.0f}% won at {100 * c:.0f}c): {100 * k:.0f}% of the wallet; half of that: {100 * k / 2:.0f}%")
