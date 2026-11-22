"""The Arbitrage Scout's 5%+ finds of the last 72 hours: what were they made of, and were the prices live?"""
import sqlite3, json, time, datetime as dt, statistics as st
from collections import Counter, defaultdict
c = sqlite3.connect('file:/Users/danielvinichenko/swifteye/oddsfeed.db?mode=ro', uri=True)
HM = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime('%d %b %H:%M:%S')
now = int(time.time() * 1000)
rows = c.execute("SELECT id, match_id, sport, home, away, market, line, legs, first_ms, last_ms, end_ms, end_reason, open_profit, peak_profit, censored FROM arb_history WHERE first_ms >= ? AND peak_profit >= 0.05 ORDER BY first_ms", (now - 72 * 3600_000,)).fetchall()
print(f"{len(rows)} records of 5%+ in {len({r[1] for r in rows})} games")
A = []
for id_, m, sport, home, away, market, line, legs, first, last, end, why, op, peak, cens in rows:
    L = json.loads(legs); start = (c.execute("SELECT start_ms FROM matches WHERE id=?", (m,)).fetchone() or [None])[0]
    info = []
    for l in L:
        b, o = l['book'], l['outcome']
        # the newest row this source had written for that side when the arbitrage first showed
        r = c.execute("SELECT ts_ms, kind, new_price FROM changes WHERE match_id=? AND +book=? AND market=? AND outcome=? AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1", (m, b, market, o if l.get('side') != 'no' else (l.get('against') or o), first)).fetchone()
        info.append(dict(book=b, odds=l['odds'], age=(first - l['updated_ms']) / 1000 if l.get('updated_ms') else None, last_kind=r[1] if r else None, last_row_age=(first - r[0]) / 1000 if r else None))
        time.sleep(0.003)
    A.append(dict(id=id_, m=m, sport=sport, game=f"{home} v {away}", market=market, first=first, dur=((end or last) - first) / 1000, why=why, peak=peak, cens=cens, legs=info,
                  live=bool(start and first >= start), books=tuple(sorted(i['book'] for i in info))))
ex = lambda b: b in ('polymarket', 'kalshi')
def kind(a):
    bs = a['books']
    if all(ex(b) for b in bs): return 'Polymarket vs Kalshi'
    if any(ex(b) for b in bs): return 'exchange vs bookmaker'
    return 'bookmaker vs bookmaker'
print("\nby what the legs are:")
for k, n in Counter(kind(a) for a in A).most_common(): print(f"  {k:24s} {n:4d} records, {len({a['m'] for a in A if kind(a) == k}):3d} games, average peak {st.mean(a['peak'] for a in A if kind(a) == k)*100:.0f}%")
print("\nended by a restart of the collector with zero length (not a market event):", sum(a['why'] == 'restart' for a in A))
B = [a for a in A if a['why'] != 'restart']
print(f"\nthe other {len(B)}: in play {sum(a['live'] for a in B)}, before the start {sum(not a['live'] for a in B)}")
stale = lambda a, lim: any(i['age'] is not None and i['age'] > lim for i in a['legs'])
susp = lambda a: any(i['last_kind'] in ('suspend', 'close') for i in a['legs'])
live = [a for a in B if a['live']]; pre = [a for a in B if not a['live']]
print(f"  in play ({len(live)}): a leg's price was already over 60 s old on {sum(stale(a, 60) for a in live)}, over 10 min old on {sum(stale(a, 600) for a in live)}; a leg's market was suspended or closed on {sum(susp(a) for a in live)}")
print(f"  before the start ({len(pre)}): a leg suspended or closed on {sum(susp(a) for a in pre)}; oldest leg over 1 h on {sum(stale(a, 3600) for a in pre)}")
fresh = [a for a in live if not stale(a, 60) and not susp(a)]
print(f"  in play with every leg under 60 s old and open: {len(fresh)} records in {len({a['m'] for a in fresh})} games; they lasted median {st.median(a['dur'] for a in fresh) if fresh else 0:.0f} s; by kind {dict(Counter(kind(a) for a in fresh))}")
print("\nwhich book supplied the OLD leg (in play, over 60 s old):", Counter(i['book'] for a in live for i in a['legs'] if i['age'] is not None and i['age'] > 60).most_common(8))
print("\nthe 12 biggest (not restarts):")
for a in sorted(B, key=lambda a: -a['peak'])[:12]:
    print(f"  {HM(a['first'])} {a['sport']:10s} {a['game'][:34]:34s} peak {a['peak']*100:5.1f}% {'live' if a['live'] else 'pre '} lasted {a['dur']:6.0f} s | " + ' + '.join(f"{i['book']} @{i['odds']:.2f} (price {i['age']:.0f} s old{', ' + i['last_kind'] if i['last_kind'] in ('suspend','close') else ''})" for i in a['legs']))
json.dump(A, open('arbs.json', 'w'))
