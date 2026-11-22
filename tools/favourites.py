"""Are favourites underpriced on Polymarket? Follow-up to tools/calibration.py (21 Nov 2026).
From ~/swifteye/research/ev_snaps.json. The bet: buy the side priced inside a band at the ask 30 s after the check,
fee included, hold to the result. ONE bet a game (the first check where a side is in the band) unless said otherwise.
usage: favourites.py"""
import json, os, random, statistics as st, sqlite3, datetime as dt
from collections import defaultdict, Counter
R = json.load(open(os.path.expanduser('~/swifteye/research/ev_snaps.json')))
fee = lambda a: 0.05 * a * (1 - a)
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
lg = defaultdict(set)
for mid, l in db.execute("SELECT match_id, league FROM book_events WHERE league IS NOT NULL"): lg[mid].add(l.lower())
def tour(m):
    s = ' | '.join(lg.get(m, ()))
    if 'itf' in s: return 'ITF'
    if 'challenger' in s: return 'Challenger'
    if 'wta' in s: return 'WTA'
    if 'atp' in s: return 'ATP'
    if 'utr' in s: return 'UTR'
    return 'other'
for r in R:
    r['mid'] = (r['bid'] + r['ask']) / 2; r['sp'] = r['ask'] - r['bid']; r['pre'] = r['rel'] < 0
U = [r for r in R if r['sp'] <= 0.06 and r.get('ask30') is not None and 0.03 <= r['ask30'] <= 0.97 and r['age'] <= (1800 if r['pre'] else 120)]
random.seed(5)
def rng(B, n=1000):
    v = sorted(st.mean(random.choice(B) for _ in B) for _ in range(n)); return v[int(n * .05)], v[int(n * .95)]
ret = lambda r: (r['won'] - r['ask30'] - fee(r['ask30'])) / (r['ask30'] + fee(r['ask30']))
def bet(rows, lo, hi, phase=None, once=True):
    got = {}
    for r in sorted(rows, key=lambda r: r['t']):
        if phase is not None and r['pre'] != phase: continue
        if not lo <= r['ask30'] < hi: continue
        if once and r['m'] in got: continue
        got[(r['m'], r['t']) if not once else r['m']] = r
    return list(got.values())
def show(name, B):
    if len(B) < 25: print(f"  {name:50s} {len(B):4d} bets (too few)"); return
    x = [ret(r) for r in B]; lo, hi = rng(x)
    print(f"  {name:50s} {len(B):4d} bets | paid {st.mean(r['ask30'] for r in B) * 100:4.1f}c | won {st.mean(r['won'] for r in B) * 100:5.1f}% (needed {st.mean(r['ask30'] + fee(r['ask30']) for r in B) * 100:4.1f}%) | return {st.mean(x) * 100:+5.1f}% a $ ({lo * 100:+.0f} to {hi * 100:+.0f})")
for sp in ('tennis', 'cs2', 'basketball', 'football', 'hockey', 'dota2'):
    S = [r for r in U if r['sport'] == sp]
    print(f"\n== {sp.upper()}: {len({r['m'] for r in S})} games")
    for ph, nm in ((True, 'PRE-MATCH'), (False, 'IN PLAY (20-100 min after the start)')):
        print(f"  -- {nm}")
        for lo, hi in ((0.10, 0.25), (0.25, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.97)):
            show(f"side costing {lo * 100:.0f}-{hi * 100:.0f}c", bet(S, lo, hi, ph))
    if sp == 'tennis':
        print('  -- favourites 60-80c, one bet a game, any time, by level and by day')
        B = bet(S, 0.60, 0.80)
        show('all', B)
        for t in ('ATP', 'WTA', 'Challenger', 'ITF', 'UTR', 'other'): show(f"   {t}", [r for r in B if tour(r['m']) == t])
        for d in sorted({dt.datetime.fromtimestamp(r['t'] / 1000, dt.UTC).strftime('%d %b') for r in B}): show(f"   {d}", [r for r in B if dt.datetime.fromtimestamp(r['t'] / 1000, dt.UTC).strftime('%d %b') == d])
        show('   pre-match only', bet(S, 0.60, 0.80, True)); show('   in play only', bet(S, 0.60, 0.80, False))
        print('  -- the mirror: underdogs 20-40c, one bet a game'); show('all', bet(S, 0.20, 0.40))
