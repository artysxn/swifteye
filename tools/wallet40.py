"""What would $40 have turned into over the whole replay (17 Nov 05:00 - 22 Nov 01:00)? (owner, 22 Nov 2026)

The extended replay (SWEEP_DATA=v18, strict gap checks), today's rules, the bot's REAL-money sizing: stake = 25% of the
wallet, at most $12, half in a weak game (no earlier jump, or followed - lead under 0.2), only from free cash (money in
open trades is tied up), at least 5 shares; sales priced as real sales (at the bids); the replay's fills and fees.
Versions of the lagged rule's 'followed' (tools/lag_shift.py's rebuild):
  as replayed   the replay's own measure (arrival time; the late feed on 17-18 Nov)
  exact         Polymarket's own clock (the bot as it runs now, with the feed on time)
  +3 s / +5 s   own clock with 3 / 5 s slack
usage: SWEEP_DATA=v18 python3 wallet40.py      -> ~/swifteye/research/wallet40.txt
"""
import os, sys, collections, io, contextlib, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SWEEP_DATA', 'v18'); os.environ['SHIFTS'] = '0,3000,5000'; os.environ['FROM_MS'] = '0'
with contextlib.redirect_stdout(io.StringIO()):
    import lag_shift as LS                 # rebuilds 'followed' on each clock (about a minute)
S = LS.S


def wallet(rows, bank=40.0, pct=0.25, cap=12.0):
    p = dict(S.BASE, real_sale=True); ci = S.CATCH.index(p['catch']); gi = S.GIVE.index(p['give']); lock = p['lock'] * 1000
    recent, busy = {}, {}; openp = []; bal = bank; tr = []; low = peak = bank; dd = 0.0; skipped = 0
    for c in rows:
        if not S.gate(c, p, ci): continue
        m = c['match_id']; tf = c['tf']
        if lock and m in recent and tf - recent[m] < lock: continue
        recent[m] = tf
        if c['nofill']: continue
        x = c['exits'][ci][gi]
        if not x or (p['one_per_game'] and busy.get(m, 0) > tf): continue
        for o in [o for o in openp if o[0] <= tf]:
            bal += o[2]; openp.remove(o); low = min(low, bal); peak = max(peak, bal); dd = max(dd, peak - bal)
        cx = c['cx']; weak = cx['jumps'] == 0 or cx['follow'] - cx['lead'] < 0.2
        stake = min(pct * bal, cap) * (0.5 if weak else 1.0)
        stake = min(stake, bal - sum(o[1] for o in openp))
        if stake / max(c['_px'], 0.01) < 5: skipped += 1; continue        # under Polymarket's 5-share minimum
        S.MIN_FILL = stake * 5 / 6      # real money: the fill must be 5/6 of the stake (snipebot fill_too_small), not the paper bot's $10
        t = S.trade(c, x, dict(p, stake=stake), ci)
        if t is None: continue
        busy[m] = x[0]; openp.append((x[0], t[1], t[0])); tr.append((c['_day'], t[0], stake))
    for o in sorted(openp):
        bal += o[2]; low = min(low, bal); peak = max(peak, bal); dd = max(dd, peak - bal)
    return tr, bal, low, peak, dd, skipped


out = ['WHAT $40 TURNS INTO, 17 Nov 05:00 - 22 Nov 01:00 (the whole replay), today\'s rules, real-money sizing (25% of the wallet, max $12, half in weak games, free cash only), real-money sales\n']
for lab, R in (('as replayed (late feed 17-18 Nov)', S.ROWS), ('exact timing (the bot now)', LS.rows_with(0)), ('3 s slack', LS.rows_with(3000)), ('5 s slack', LS.rows_with(5000))):
    tr, bal, low, peak, dd, sk = wallet(R)
    by = collections.defaultdict(float); n = collections.Counter()
    for d, pnl, st_ in tr: by[d] += pnl; n[d] += 1
    run = 40.0; path = []
    for d in sorted(by): run += by[d]; path.append('%s Nov $%.2f' % (d, run))
    out.append('  %-36s $40 -> $%.2f (%+.0f%%) | %d trades, won %d | lowest $%.2f, highest $%.2f, worst drop $%.2f' % (lab, bal, 100 * (bal / 40 - 1), len(tr), sum(1 for _, p_, _ in tr if p_ > 0), low, peak, dd))
    out.append('      end of each day: ' + ' -> '.join(path))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/wallet40.txt'), 'w').write(txt + '\n')
