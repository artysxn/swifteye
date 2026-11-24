"""The reward markets where the minimum pair of orders would be paid the most RIGHT NOW (rule of 24 Nov, corrected in
the morning: see SWIFTEYE-NIGHT-LOG.md). Reads recorder 2's files (rec2.py must be running: no network here).
Others' score is the HIGH bound ((2 min + max) / 3 for a price of 10-90c); our prices are real cent prices, the pair
3c from the price (never past the reward limit). The list goes stale within HOURS: these are mostly brand-new markets
and other bots move in. Not verified by real orders; a filled order here can cost most of its stake.
usage: python pick.py [out.md] [min $ a day, default 3]"""
import json, os, sys, time, datetime as dt
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'candidates.md')
MIN = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
sys.argv = [sys.argv[0]]
import paperbot as PB
M, cond, end, created, T, B, C = PB.load()
k = len(T) - 1
age_min = (time.time() - T[k]) / 60
rate, bid, ask, n = PB.expected(k, T, B, C, end, 3.0, 300, 3)
mid, bb, ba, qb, qa, rb, ra = (B[k, :, j].astype(np.float64) for j in range(7))
oth = PB.others(mid, qb, qa)
order = [i for i in np.argsort(-np.nan_to_num(rate, nan=-1)) if np.isfinite(rate[i]) and rate[i] >= MIN]
L = [f'# Reward markets where the minimum pair is paid the most, {dt.datetime.fromtimestamp(int(T[k])).strftime("%d %b %Y %H:%M")} Norway time', '',
     f'Books read {age_min:.0f} minutes ago. **This list goes stale within hours** (most rows are markets created in the last day; other bots move in and the share drops from about a quarter of the pot to a few percent). '
     'Not verified by real orders. One filled order can cost most of its stake, and in an empty book the shares cannot be sold at a fair price.', '',
     'Rule: pot $5+ a day, minimum size 50 shares or less, 24-hour volume under $300, 3+ days to the end, price 5-95c, book under 20c wide, expected pot share $' + f'{MIN:g}+ a day. '
     'The pair: a buy of Yes and a buy of No, each 3c from the price (never past the reward limit).', '',
     '| Pot a day | Expected pot share a day | Market | Age | Ends | Price | Book | Buy Yes at | Buy No at | Shares each | Money tied up | Others\' score | 24h volume |', '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
for i in order[:80]:
    m = M[cond[i]]
    ag = (T[k] - created[i]) / 3600
    L.append(f'| ${C[k, i, 0]:.0f} | ${rate[i]:.2f} ({rate[i] / C[k, i, 0] * 100:.0f}%) | {m["q"][:70]} | {f"{ag:.0f} h" if ag < 48 else f"{ag / 24:.0f} d"} | {(m["end"] or "")[:10]} | {mid[i] * 100:.1f}c | {rb[i] * 100:.1f} / {ra[i] * 100:.1f} | {bid[i] * 100:.0f}c | {(1 - ask[i]) * 100:.0f}c | {n[i]:.0f} | ${n[i] * (bid[i] + 1 - ask[i]):.2f} | {oth[i]:.1f} | ${C[k, i, 3]:.0f} |')
tot = float(np.nansum([rate[i] for i in order]))
L += ['', f'{len(order)} markets pass out of {int(np.isfinite(C[k, :, 0]).sum())} watched; the minimum pair in all of them would hold about ${tot:,.0f} a day of pots at this moment (before any fill).']
open(OUT, 'w').write('\n'.join(L) + '\n')
ages = np.array([(T[k] - created[i]) / 3600 for i in order])
print(f'{len(order)} pass (expected ${MIN:g}+ a day); ${tot:,.0f} a day in all; created in the last 24 h: {(ages < 24).mean() * 100:.0f}%, in the last 6 h: {(ages < 6).mean() * 100:.0f}%; books {age_min:.0f} min old')
for i in order[:12]:
    print(f'  ${C[k, i, 0]:4.0f}/day -> ${rate[i]:5.2f} | {mid[i] * 100:5.1f}c book {rb[i] * 100:.0f}/{ra[i] * 100:.0f} | buy Yes {bid[i] * 100:.0f}c, buy No {(1 - ask[i]) * 100:.0f}c x{n[i]:.0f} | others {oth[i]:5.1f} | age {(T[k] - created[i]) / 3600:5.1f} h | {M[cond[i]]["q"][:56]}')
