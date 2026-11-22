"""WHY does US basketball (NBA / college) lose for the snipe? (owner, 22 Nov 2026: "why do we block them?") Pattern check on the
v18 replay (17 Nov 05:00 - 22 Nov 01:00): moves that pass every rule except the basketball block, the lagged rule and the weak cap,
grouped by sport. How late Polymarket is (share of the jump already made 15 s later), how often it reaches the target within 15 min,
how it moved before (lead) and after (follow) earlier jumps in the same game, and the replay's $ with the bot's locks ($12, real sales)."""
import os, sys, io, contextlib, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SWEEP_DATA', 'v18')
with contextlib.redirect_stdout(io.StringIO()):
    import sweep as S
B = dict(S.BASE, real_sale=True, give=900); CI = S.CATCH.index(1.0); GI = S.GIVE.index(900)
OPEN = dict(B, bb='all', lag=None, weak_cap=None)
R = [c for c in S.ROWS if not c['blind'] and not c['nofill'] and S.gate(c, OPEN, CI)]
def grp(c):
    if c['sport'] == 'basketball': return 'US basketball' if c['_usbb'] else 'basketball elsewhere'
    return c['sport'] if c['sport'] in ('tennis', 'cs2') else 'other sports'
mid = lambda q: (q[0] + q[1]) / 2 if q else None
out = ['WHY US BASKETBALL LOSES - moves passing every rule except the basketball block, the lagged rule and the weak cap (v18 replay, 17-22 Nov)\n',
       '%-22s %6s %6s %10s %10s %10s %9s %9s %8s %8s %8s' % ('', 'moves', 'games', 'PM done', 'reached', 'earlier', 'PM led', 'PM foll.', 'trades', 'won', '$'),
       '%-22s %6s %6s %10s %10s %10s %9s %9s %8s %8s %8s' % ('', '', '', 'at 15 s', 'target', 'jumps/game', 'before', 'after', '', '', '')]
for g in ('US basketball', 'basketball elsewhere', 'tennis', 'cs2', 'other sports'):
    L = [c for c in R if grp(c) == g]
    if not L: continue
    done = [((mid(c['q_15']) - c['pre_mid']) * c['dir']) / c['_mv'] for c in L if c['q_15'] and c['pre_mid'] > 0 and c['_mv'] > 0]
    reach = [bool(c['exits'][CI][GI] and c['exits'][CI][GI][4]) for c in L if c['exits'][CI][GI]]
    J = [c['cx'] for c in L if c['cx']['jumps'] > 0]
    T = S.sim(OPEN, rows=L)
    out.append('%-22s %6d %6d %9.0f%% %9.0f%% %10.1f %8.0f%% %8.0f%% %8d %8d %+8.2f' % (g, len(L), len({c['match_id'] for c in L}), 100 * st.median(done) if done else 0,
        100.0 * sum(reach) / max(1, len(reach)), st.median([c['cx']['jumps'] for c in L]), 100 * st.median([x['lead'] for x in J]) if J else 0,
        100 * st.median([x['follow'] for x in J]) if J else 0, len(T), sum(t['pnl'] > 0 for t in T), sum(t['pnl'] for t in T)))
out.append('\n  PM done at 15 s = median share of the bookmakers\' jump Polymarket had made 15 s after it (higher = Polymarket is fast)')
out.append('  reached target = Polymarket reached the catch-up price within 15 min (a win for the bot)')
out.append('  PM led before / PM followed after = in EARLIER jumps of the same game, the median share Polymarket moved in the 40 s before / 60 s after')
txt = '\n'.join(out); print(txt); open(os.path.expanduser('~/swifteye/research/usbb_why.txt'), 'w').write(txt + '\n')
