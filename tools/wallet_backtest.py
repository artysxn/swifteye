"""What the REAL wallet would have done over everything recorded: the bot's rules in force, its real stake
(30% of the wallet x signal strength, at most $12), at most the wallet open at once, one position a game, the
safety stops. Moves and fills come from the in-app replay's saved blocks (order-book depth, 1c off every sale,
orders landing when a real order lands); hours those blocks do not cover yet come from live_emu.py.

usage: wallet_backtest.py [OUT.json]
"""
import sys, json, time, datetime as dt
from collections import defaultdict
import replay_eval as R
import live_emu as E

BANK, PCT, MAX_TRADE, LOSS, MIN_FILL = 40.0, 0.30, 12.0, 15.0, 10.0
DAY = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime('%d %b')
HM = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime('%d %b %H:%M')
fee1 = lambda p: 0.05 * p * (1 - p)

def opportunities():
    """Every move that passes the rules, in time order, with what a fill of a given stake would return."""
    C = R.load(); P = dict(R.BASE, hours=None); ci, gi = R.CATCH.index(1.0), R.GIVE.index(300)
    last = max(c['tf'] for c in C)
    out = []
    for c in C:
        if not R.passes(c, P): continue
        x = c['exits'][ci][gi]
        out.append(dict(src='replay', tf=c['tf'], m=c['match_id'], game=c['home'], sport=c['sport'], nofill=bool(c['nofill']) or not x, c=c, x=x,
                        edge=c['edge'][1][ci], close=x['ts'] if x else c['tf'], walk=c['edge'][1][ci] - R.CAP >= P['min_edge'] - 1e-9))
    block_end = (last // 14_400_000 + 1) * 14_400_000
    E.GIVE = 300_000
    T1 = int(time.time() * 1000) - 420_000
    trades, *_ = E.run(block_end, T1)
    for t in trades['F']:
        if 'entry' not in t or 'exit' not in t: continue
        out.append(dict(src='emu', tf=t['t0'] + 1750, m=t['m'], game=t['home'], sport=t['sport'], nofill=False, edge=t['edge'], close=t['tx'], entry=t['entry'], exit=t['exit']))
    out.sort(key=lambda o: o['tf'])
    return out, min(c['tf'] for c in C), block_end, T1

def result(o, stake, eb=0.0, es=0.0, fm=1.0):
    """(cost, profit, entry, exit) of buying `stake` dollars; eb / es = cents worse on the buy / the sale, fm = fee multiplier."""
    if o['src'] == 'replay':
        f = R.fill(o['c'], o['x'], stake, o['walk'])
        if not f: return None
        entry, ex = f['entry'], f['exit']; n = f['spent'] / entry
    else:
        entry, ex = o['entry'], max(o['exit'] - 0.01, 0.0) if 0.02 < o['exit'] < 0.98 else o['exit']; n = stake / entry
    entry2 = entry + eb; ex2 = max(ex - es, 0.0) if 0.02 < ex < 0.98 else ex
    pnl = n * (ex2 - entry2) - fm * n * (fee1(entry2) + (fee1(ex2) if 0.0 < ex2 < 1.0 else 0.0))
    return n * entry2, pnl, entry2, ex2

def run(O, stops=True, **kw):
    """One wallet. A safety stop switches real money off until the next UTC midnight, when a new run starts
    (as /realon does: a fresh $40 wallet for sizing; the money made or lost so far is kept in the total)."""
    total = 0.0; run_pnl = 0.0; run_tr = []; open_ = []; lock = {}; busy = {}; off_until = 0; day_pnl = defaultdict(float)
    T = []; skipped = defaultdict(int); stops_hit = []
    def settle(now):
        nonlocal total, run_pnl, off_until
        for p in sorted([p for p in open_ if p['close'] <= now], key=lambda p: p['close']):
            open_.remove(p); total += p['pnl']; run_pnl += p['pnl']; day_pnl[DAY(p['close'])] += p['pnl']; run_tr.append(p)
            if not stops or p['close'] < off_until: continue
            why = None
            if run_pnl <= -LOSS or day_pnl[DAY(p['close'])] < -LOSS: why = f"down ${-min(run_pnl, day_pnl[DAY(p['close'])]):.2f}, past the ${LOSS:.0f} limit"
            elif len(run_tr) >= 3 and all(t['pnl'] / t['cost'] < -0.05 for t in run_tr[-3:]): why = "3 losing trades in a row"
            elif len(run_tr) >= 10 and sum(t['pnl'] > 0 for t in run_tr) < 0.5 * len(run_tr): why = f"only {sum(t['pnl'] > 0 for t in run_tr)} of {len(run_tr)} won"
            if why:
                off_until = (p['close'] // 86_400_000 + 1) * 86_400_000
                stops_hit.append((p['close'], why)); run_pnl = 0.0; run_tr.clear()
    for o in O:
        settle(o['tf'])
        m = o['m']
        if o['tf'] < off_until: skipped['real money off after a safety stop'] += 1; continue
        if m in lock and o['tf'] - lock[m] < 120_000: continue
        lock[m] = o['tf']
        if o['nofill']: skipped['order would not have filled'] += 1; continue
        if busy.get(m, 0) > o['tf']: skipped['already in that game'] += 1; continue
        bal = BANK + run_pnl; open_cost = sum(p['cost'] for p in open_)
        k = min(max(o['edge'] / 0.04, 0.5), 3.0)
        stake = min(PCT * bal * k, MAX_TRADE, bal - open_cost, BANK - open_cost)
        if stake < MIN_FILL: skipped['no room in the wallet'] += 1; continue
        r = result(o, stake, **kw)
        if not r: skipped['too little on offer'] += 1; continue
        cost, pnl, entry, ex = r
        busy[m] = o['close']
        p = dict(t=o['tf'], close=o['close'], m=m, game=o['game'], sport=o['sport'], cost=cost, pnl=pnl, entry=entry, exit=ex, src=o['src'])
        open_.append(p); T.append(p)
    settle(10 ** 15)
    return T, dict(skipped), stops_hit

def summary(name, T, stops_hit=()):
    T = sorted(T, key=lambda p: p['close']); tot = sum(p['pnl'] for p in T); bal = peak = dd = 0.0; low = 0.0
    for p in T:
        bal += p['pnl']; peak = max(peak, bal); dd = max(dd, peak - bal); low = min(low, bal)
    w = sum(p['pnl'] > 0 for p in T)
    return (f"{name:46s} ends at ${BANK + tot:7.2f} ({tot:+7.2f}) | {len(T):3d} trades, {w / max(len(T), 1) * 100:3.0f}% won | lowest ${BANK + low:6.2f} | biggest fall ${dd:5.2f} | "
            f"worst trade {min((p['pnl'] for p in T), default=0):+6.2f} | safety stops {len(stops_hit)}")

if __name__ == '__main__':
    O, t_first, block_end, t_last = opportunities()
    print(f"recorded from {HM(t_first)} to {HM(t_last)} UTC ({(t_last - t_first) / 86400e3:.2f} days); replay blocks to {HM(block_end)}, the offline copy of the bot after that")
    print(f"moves that pass every rule: {len(O)} ({sum(o['src'] == 'emu' for o in O)} of them after the replay blocks end)\n")
    T, sk, st = run(O)
    print(summary("AS THE BOT RUNS NOW ($40, $12 a trade, stops on)", T, st))
    print("   not taken:", sk)
    for ts, why in st: print(f"   safety stop {HM(ts)} UTC: {why} -> real money off until midnight UTC")
    by = defaultdict(lambda: [0, 0, 0.0])
    for p in T: d = by[DAY(p['close'])]; d[0] += 1; d[1] += p['pnl'] > 0; d[2] += p['pnl']
    print("   by day (UTC):", ' | '.join(f"{k}: {v[0]} trades, {v[1]} won, {v[2]:+.2f}" for k, v in by.items()))
    hrs = defaultdict(lambda: [0, 0.0])
    for p in T:
        h = dt.datetime.fromtimestamp(p['t'] / 1000, dt.UTC).hour; b = '12-21 UTC' if 12 <= h < 21 else 'other hours'; hrs[b][0] += 1; hrs[b][1] += p['pnl']
    print("   by hours:", {k: (v[0], round(v[1], 2)) for k, v in hrs.items()})
    print()
    print(summary("same, safety stops ignored", *run(O, stops=False)[::2]))
    print("\nHOW MUCH THE RESULT DEPENDS ON FILLS (stops on)")
    for name, kw in (("every sale 1c worse than modelled", dict(es=0.01)), ("every buy 1c worse", dict(eb=0.01)), ("buy and sale both 1c worse", dict(eb=0.01, es=0.01)),
                     ("buy and sale both 2c worse", dict(eb=0.02, es=0.02)), ("fees as charged so far (70% of the formula)", dict(fm=0.7)), ("sales at the recorded bid (no 1c haircut)", dict(es=-0.01))):
        Tx, _, sx = run(O, **kw); print(summary("  " + name, Tx, sx))
    if len(sys.argv) > 1:
        bal = BANK; pts = [dict(t=t_first, bal=BANK)]
        for p in sorted(T, key=lambda p: p['close']):
            bal += p['pnl']; pts.append(dict(t=p['close'], bal=round(bal, 2), game=p['game'], pnl=round(p['pnl'], 2), entry=round(p['entry'], 3), exit=round(p['exit'], 3), cost=round(p['cost'], 2), sport=p['sport'], src=p['src']))
        json.dump(dict(points=pts, first=t_first, last=t_last, stops=st), open(sys.argv[1], 'w'))
