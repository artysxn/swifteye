"""$40 wallets (real-money sizing, as tools/wallet40.py) for the single changes that held up out of sample (tools/rule_single_loo.py)."""
import os, sys, io, contextlib, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SWEEP_DATA', 'v18'); os.environ['SHIFTS'] = '0,3000,5000'; os.environ['FROM_MS'] = '0'
with contextlib.redirect_stdout(io.StringIO()):
    import lag_shift as LS
S = LS.S
V = {'exact': LS.rows_with(0), '+3 s': LS.rows_with(3000), '+5 s': LS.rows_with(5000)}
def wallet(rows, kw):
    p = dict(S.BASE, real_sale=True, **kw); ci = S.CATCH.index(p['catch']); gi = S.GIVE.index(p['give'])
    bal = 40.0; openp = []; recent, busy = {}, {}; n = w = 0; low = peak = 40.0; dd = 0.0; by = collections.defaultdict(float); worst = 0.0
    for c in rows:
        if not S.gate(c, p, ci): continue
        m = c['match_id']; tf = c['tf']
        if p['lock'] and m in recent and tf - recent[m] < p['lock'] * 1000: continue
        recent[m] = tf
        if c['nofill']: continue
        x = c['exits'][ci][gi]
        if not x or (p['one_per_game'] and busy.get(m, 0) > tf): continue
        for o in [o for o in openp if o[0] <= tf]:
            bal += o[2]; openp.remove(o); low = min(low, bal); peak = max(peak, bal); dd = max(dd, peak - bal)
        cx = c['cx']; weak = cx['jumps'] == 0 or cx['follow'] - cx['lead'] < 0.2
        stake = min(min(0.25 * bal, 12.0) * (0.5 if weak else 1.0), bal - sum(o[1] for o in openp))
        if stake / max(c['_px'], 0.01) < 5: continue
        S.MIN_FILL = stake * 5 / 6      # real money: the fill must be 5/6 of the stake (snipebot fill_too_small), not the paper bot's $10
        t = S.trade(c, x, dict(p, stake=stake), ci)
        if t is None: continue
        busy[m] = x[0]; openp.append((x[0], t[1], t[0])); n += 1; w += t[0] > 0; by[c['_day']] += t[0]; worst = min(worst, t[0])
    for o in sorted(openp):
        bal += o[2]; low = min(low, bal); peak = max(peak, bal); dd = max(dd, peak - bal)
    return bal, n, w, dd, worst, by
out = ['$40 WALLETS, 17 Nov 05:00 - 22 Nov 01:00, real-money sizing and sales']
for v, kw, lab in (('exact', {}, "today's rules (the bot now)"), ('exact', dict(give=900), 'give up at 15 min'), ('exact', dict(give=1800), 'give up at 30 min'),
                   ('+3 s', {}, '3 s slack'), ('+3 s', dict(give=900), '3 s slack + give up at 15 min'), ('+3 s', dict(give=1800), '3 s slack + give up at 30 min'),
                   ('+5 s', dict(give=900), '5 s slack + give up at 15 min'), ('+5 s', dict(give=1800), '5 s slack + give up at 30 min')):
    bal, n, w, dd, worst, by = wallet(V[v], kw)
    run = 40.0; path = []
    for d in sorted(by): run += by[d]; path.append('%.0f' % run)
    out.append('  %-34s $40 -> $%6.2f | %2d trades, won %2d (%3.0f%%) | worst drop $%5.2f | worst single trade %+.2f $ | end of day %s' % (lab, bal, n, w, 100.0 * w / max(1, n), dd, worst, ' / '.join(path)))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/wallet_best.txt'), 'w').write(txt + '\n')
