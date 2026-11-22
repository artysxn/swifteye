"""FOLLOW THE LEADER: a buying and selling system built on a correlation score and a delay score (owner, 21 Nov 2026:
"write a new system for buying and selling in accordance with the correlation and delay systems. Completely
refactored. We have data to test if it works.").

Every bookmaker line in a game is a possible LEADER of the Polymarket market. For each pair the system keeps, from
the bookmaker's earlier jumps in this game (each known 90 s after it happened):
    delay        median seconds from the bookmaker's jump to Polymarket's own move being halfway done (+ = bookmaker first)
    first share  share of those jumps where the bookmaker was first by 3 s or more
    follow       share of the bookmaker's jumps that Polymarket also made
    response     how big Polymarket's move usually is against the bookmaker's (median share of the jump)
    correlation  likeness of the two lines over the last half hour at the best slide in time (0-100)

BUY when a leader jumps and all of this holds:
    1 the pair is known:        at least `n_min` earlier jumps, follow >= `follow`, correlation >= `corr`
    2 the leader leads:         delay >= `delay` seconds and first share >= `first`
    3 Polymarket has not gone:  its move over the last minute is under `pre` of the jump
    4 there is room:            usual response x jump, less what Polymarket did, the spread and both fees >= `edge`
    5 the book can be traded:   spread <= `spread`, price `lo`-`hi`, the offer has not run 2c by the time the order lands
SELL at the first of:
    a target   Polymarket made its usual response
    b time     it has not, within max(45 s, `tk` x delay + 30 s), never more than `tmax`
    c reason   the bookmaker took half the jump back
One position per game; `rest` seconds after a sale before the next buy in that game.

Data: lf_build.py (every bookmaker jump in every recorded game, Polymarket by its own clock, the next 5 minutes of
prices). $12 a trade, fee 5% x p x (1-p) each way, a sale fills 2 s after the decision and 1c under the bid.

usage: lf_system.py            the system, tuned on 17-19 Nov, judged on 20-21 Nov
       lf_system.py sample     same on the 40-game sample (for checking the code)
"""
import os, sys, pickle, time, statistics as st, collections, itertools

STAKE = 12.0
fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0
DEF = dict(n_min=3, follow=0.6, corr=70.0, delay=3.0, first=0.4, pre=0.25, edge=0.03, spread=0.04, lo=0.05, hi=0.90, min_jump=0.04,
           resp_lo=0.3, resp_hi=1.0, tk=3.0, tmax=300, tmin=45, reason=0.5, rest=60, family=True, target=True, use_scores=True)
# the old idea in the same engine: any jump of 8pt+ Polymarket has not followed, target = the whole jump, 5 minutes, no scores
OLD = dict(DEF, use_scores=False, min_jump=0.08, pre=0.05, resp_fixed=1.0, edge=0.04, tk=None, reason=None)


def load(sample=False):
    p = os.path.expanduser('~/swifteye/research/lf_cands%s.pickle' % ('_sample' if sample else ''))
    d = pickle.load(open(p, 'rb')); return d['cands'], d.get('path_s', 2)


def pair(c, P):
    """The scores the system uses for this jump: the bookmaker's own earlier jumps; its family's if it has too few."""
    s = c['sb']
    if s[0] < P['n_min'] and P['family']: s = c['sf']
    return s


def decide(c, P):
    """None = no buy; else (target bid, seconds to wait, expected gain)."""
    size = c['size']
    if size < P['min_jump']: return None
    sp = c['ask_d'] - c['bid_d']
    if sp > P['spread'] + 1e-9 or not (P['lo'] <= c['ask_e'] <= P['hi']): return None
    if c['ask_e'] - c['ask_d'] > 0.02 + 1e-9: return None                       # the offer ran away while the order was on its way
    done = max(c['m1'] - c['m60'], c['m1'] - c['m10'], 0.0)
    if done / size >= P['pre']: return None
    if P['use_scores']:
        n, paired, first, behind, delay, resp, follow = pair(c, P)
        if n < P['n_min'] or paired < 2 or delay is None: return None
        if follow < P['follow'] or delay < P['delay'] or first / paired < P['first']: return None
        if P['corr'] is not None and (c['corr'] is None or c['corr'] < P['corr']): return None
        r = min(max(resp, P['resp_lo']), P['resp_hi'])
        wait = min(max(P['tmin'], P['tk'] * max(delay, 0) + 30), P['tmax']) if P['tk'] is not None else P['tmax']
    else:
        r = P.get('resp_fixed', 1.0); wait = P['tmax']
    target_mid = min(c['m10'] + r * size, 0.985); tb = target_mid - sp / 2
    gain = (tb - 0.01) - c['ask_e'] - fee(c['ask_e']) - fee(tb)
    if gain < P['edge']: return None
    return tb, wait, gain


def play(c, tb, wait, P, path_s):
    """Walk the next minutes: (pnl, exit price, seconds held, why)."""
    e = c['ask_e']; n = STAKE / e; pb, pl = c['pb'], c['pl']; N = len(pb)
    half = c['base'] + (P['reason'] or 0) * c['size'] if P['reason'] is not None else None
    def out(k, why):
        kk = min(k + 1, N - 1); b = pb[kk] if pb[kk] != 255 else pb[k]
        if b == 255: return None
        x = max(b / 100.0 - 0.01, 0.0)
        return n * (x - e) - n * (fee(e) + fee(x)), x, kk * path_s, why
    last = min(int(wait // path_s), N - 2)
    for k in range(1, last + 1):
        b = pb[k]
        if b == 255: continue
        if P['target'] and b / 100.0 >= tb - 1e-9: return out(k, 'target')
        if half is not None and pl[k] != 255 and pl[k] / 100.0 <= half + 1e-9 - (0 if P['reason'] else 0):
            if pl[k] / 100.0 <= c['base'] + (1 - P['reason']) * c['size'] - 1e-9: return out(k, 'reason gone')
    return out(last, 'time')


def run(cands, P, path_s=2, t0=None, t1=None):
    busy = {}; T = []
    for c in cands:
        if (t0 and c['t'] < t0) or (t1 and c['t'] >= t1): continue
        if busy.get(c['m'], 0) > c['t']: continue
        d = decide(c, P)
        if d is None: continue
        r = play(c, d[0], d[1], P, path_s)
        if r is None: continue
        busy[c['m']] = c['t'] + (r[2] + P['rest']) * 1000
        T.append(dict(c=c, pnl=r[0], exit=r[1], held=r[2], why=r[3], gain=d[2]))
    return T


DAY = lambda t: time.strftime('%d', time.localtime(t / 1000))
def line(name, T, base=None):
    n = len(T); w = sum(1 for t in T if t['pnl'] > 0); tot = sum(t['pnl'] for t in T)
    dp = collections.defaultdict(float); dn = collections.Counter()
    for t in T: d = DAY(t['c']['t']); dp[d] += t['pnl']; dn[d] += 1
    why = collections.Counter(t['why'] for t in T)
    print('%-58s trades %4d | won %4d (%3.0f%%) | $%+9.2f | per trade $%+5.2f | %s | sold: %s' % (name, n, w, 100.0 * w / max(n, 1), tot, tot / max(n, 1), ' '.join('%s:%+.0f/%d' % (d, dp[d], dn[d]) for d in sorted(dn)), ' '.join('%s %d' % (k, v) for k, v in why.most_common())))
    return tot


def by(name, T, key):
    g = collections.defaultdict(list)
    for t in T: g[key(t)].append(t)
    print('   ' + name + ': ' + ' | '.join('%s %d trades %.0f%% $%+.2f' % (k, len(v), 100.0 * sum(1 for t in v if t['pnl'] > 0) / len(v), sum(t['pnl'] for t in v)) for k, v in sorted(g.items(), key=lambda x: -len(x[1]))[:9]))


if __name__ == '__main__':
    sample = len(sys.argv) > 1 and sys.argv[1] == 'sample'
    C, ps = load(sample)
    SPLIT = 1795129200000          # 20 Nov 00:00 Norway: before = tuning days, after = judging days
    days = collections.Counter(DAY(c['t']) for c in C)
    print('bookmaker jumps Polymarket had not yet made: %d in %d games | by day %s' % (len(C), len(set(c['m'] for c in C)), dict(sorted(days.items()))))
    print('\n=== THE OLD IDEA in this engine (8pt+ jump, Polymarket not moved, target = the whole jump, 5 min, no scores)')
    line('old idea, tuning days 17-19 Nov', run(C, OLD, ps, t1=SPLIT)); line('old idea, judging days 20-21 Nov', run(C, OLD, ps, t0=SPLIT))
    print('\n=== FOLLOW THE LEADER, first settings (written down before any result)')
    Ta = run(C, DEF, ps, t1=SPLIT); Tb = run(C, DEF, ps, t0=SPLIT)
    line('tuning days 17-19 Nov', Ta); line('judging days 20-21 Nov', Tb)
    by('by sport, all days', Ta + Tb, lambda t: t['c']['sport']); by('by bookmaker', Ta + Tb, lambda t: t['c']['book'])
    print('\n=== WHAT EACH PART DOES (tuning days only): one setting changed at a time')
    for name, ch in (('no scores at all (rules 3-5 only, response 1)', dict(use_scores=False, resp_fixed=1.0, tk=None)),
                     ('scores, but the leader need not lead (delay any)', dict(delay=-999, first=0.0)), ('leader ahead 0 s+', dict(delay=0.0, first=0.3)), ('leader ahead 5 s+', dict(delay=5.0)), ('leader ahead 10 s+', dict(delay=10.0)),
                     ('first share 60%+', dict(first=0.6)), ('correlation not asked', dict(corr=None)), ('correlation 85+', dict(corr=85.0)), ('correlation 95+', dict(corr=95.0)),
                     ('follow 80%+', dict(follow=0.8)), ('5+ earlier jumps', dict(n_min=5)), ('own bookmaker only, no family', dict(family=False)),
                     ('jump 6pt+', dict(min_jump=0.06)), ('jump 8pt+', dict(min_jump=0.08)), ('Polymarket moved under 10%', dict(pre=0.10)), ('room 5c+', dict(edge=0.05)), ('room 2c+', dict(edge=0.02)),
                     ('target = the whole jump', dict(resp_lo=1.0, resp_hi=1.0)), ('target = half the usual response', dict(resp_lo=0.15, resp_hi=0.5)),
                     ('wait a fixed 5 minutes', dict(tk=None)), ('wait 2 x delay + 30 s', dict(tk=2.0)), ('wait 6 x delay + 30 s', dict(tk=6.0)), ('do not sell when the leader takes it back', dict(reason=None)), ('spread 2c or less', dict(spread=0.02))):
        line(name, run(C, dict(DEF, **ch), ps, t1=SPLIT))
    # a small search on the tuning days, the best few then shown on the judging days
    print('\n=== SEARCH on the tuning days (243 settings), the five best with 30+ trades, then the SAME settings on the judging days')
    grid = dict(delay=[0.0, 3.0, 8.0], first=[0.3, 0.5], corr=[None, 80.0, 92.0], min_jump=[0.04, 0.06, 0.08], pre=[0.10, 0.25], tk=[2.0, 4.0, None])
    res = []
    keys = list(grid)
    for vals in itertools.product(*[grid[k] for k in keys]):
        P = dict(DEF, **dict(zip(keys, vals))); T = run(C, P, ps, t1=SPLIT)
        if len(T) >= 30: res.append((sum(t['pnl'] for t in T), len(T), P, dict(zip(keys, vals))))
    res.sort(key=lambda r: -r[0])
    print('settings tried with 30+ trades: %d | of these profitable on the tuning days: %d' % (len(res), sum(1 for r in res if r[0] > 0)))
    for tot, n, P, ch in res[:5]:
        print('  ' + ', '.join('%s=%s' % kv for kv in ch.items()))
        line('     tuning days', run(C, P, ps, t1=SPLIT)); line('     JUDGING days', run(C, P, ps, t0=SPLIT))
    if res:
        jt = [sum(t['pnl'] for t in run(C, r[2], ps, t0=SPLIT)) for r in res[:20]]
        print('the 20 best tuning settings on the judging days: %d profitable, median $%+.2f, best $%+.2f, worst $%+.2f' % (sum(1 for x in jt if x > 0), st.median(jt), max(jt), min(jt)))
