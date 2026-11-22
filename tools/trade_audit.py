"""Audit each bot trade against the data its graph is drawn from: was the buy made before Polymarket
caught up, was the bookmaker move real, and did the sale happen where the plan says.

For every trade it reads only that game's rows (pm_quotes and changes by match_id + time, both indexed),
so it is safe to run while the bot trades.

Per trade, on the BOUGHT side's price scale (Yes as is; No = 1 - the other side):
  trigger   the bookmaker that moved: old -> new price, how long the old price had stood, where the other
            bookmaker families stood before and 60 s after, whether the trigger went back
  buy       Polymarket's ask 10 s before the move, the ask the bot saw, the ask on Polymarket's own clock
            at the buy, the fill; how late the feed was
  hold      the sale target, when the bid first reached it, the highest bid, the sale and the bid then
  after     the highest bid in the 5 min after the sale
usage: trade_audit.py [hours=24] [--all-real] [--json out.json]
"""
import sqlite3, json, os, sys, time, bisect, statistics as st, datetime as dt

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].replace('.', '').isdigit() else 24
ALL_REAL = '--all-real' in sys.argv
OUT = sys.argv[sys.argv.index('--json') + 1] if '--json' in sys.argv else None
DB = os.path.expanduser('~/swifteye/oddsfeed.db')
db = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
NOW = int(time.time() * 1000)
no = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone(dt.timedelta(hours=1))).strftime('%d %H:%M:%S')
c100 = lambda x: '   - ' if x is None else f"{x * 100:5.1f}"


def platform(b):
    if b in ('linebet', 'melbet', 'betwinner', 'megapari', 'paripesa', '22bet', '22bit', '1xbit', '1xbet'): return '1xbet'
    if b in ('unibet', 'leovegas', 'paf'): return 'kambi'
    if b in ('fonbet', 'pari', 'mostbet'): return 'fonbet'
    if b in ('vinn', 'betking'): return 'altenar'
    return b


def at(times, vals, t, max_age=None):
    i = bisect.bisect_right(times, t)
    if i == 0 or (max_age and t - times[i - 1] > max_age): return None
    return vals[i - 1]


def audit(row):
    tid, bot, real, ts, cl, data = row
    v = json.loads(data)
    m, o, d = v['match_id'], v['outcome'], v.get('dir', 1.0)
    yes = d > 0
    mv_ms = v.get('move_ms') or ts
    end = cl or NOW
    side = (lambda b, a: (b, a)) if yes else (lambda b, a: (None if a is None else 1 - a, None if b is None else 1 - b))
    q = db.execute("SELECT ts_ms, COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms",
                   (m, o, ts - 900_000, end + 600_000)).fetchall()
    rt = [r[0] for r in q]; rq = [side(r[2], r[3]) for r in q]              # as received (what the bot saw)
    sq = sorted(((r[1], side(r[2], r[3])) for r in q), key=lambda x: x[0])   # on Polymarket's own clock
    stt = [x[0] for x in sq]; sv = [x[1] for x in sq]
    seen = lambda t: at(rt, rq, t) or (None, None)
    true = lambda t: at(stt, sv, t) or (None, None)
    # how late the feed was at the buy: smallest receive-minus-stamp among this game's last quotes
    i = bisect.bisect_right(rt, ts)
    late = min((q[k][0] - q[k][1] for k in range(max(0, i - 8), i)), default=None)
    # bookmakers, bought side
    books = {}
    for t, b, p in db.execute("SELECT ts_ms, book, new_price FROM changes WHERE match_id = ? AND market = '1x2' AND outcome = ? AND ts_ms >= ? AND ts_ms <= ? AND new_price IS NOT NULL AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms",
                              (m, o, ts - 2_400_000, end + 600_000)):
        imp = 1 / max(p, 1.0001)
        books.setdefault(b, ([], []))
        books[b][0].append(t); books[b][1].append(imp if yes else 1 - imp)
    trig = v.get('trigger', '')
    bk = lambda b, t: at(books[b][0], books[b][1], t, 1_200_000) if b in books else None

    def others(t, skip_fam=None):
        fam = {}
        for b in books:
            f = platform(b)
            if f == skip_fam: continue
            p = bk(b, t)
            if p is not None: fam.setdefault(f, []).append(p)
        means = sorted(sum(x) / len(x) for x in fam.values())
        return (means[len(means) // 2] if means else None), len(means)

    tf = platform(trig)
    trig_old, trig_new = bk(trig, mv_ms - 1), bk(trig, mv_ms + 1500)
    prev = None
    if trig in books:
        j = bisect.bisect_left(books[trig][0], mv_ms - 1)
        # when the old price was set: walk back over repeats of the same value
        while j > 0 and trig_old is not None and abs(books[trig][1][j - 1] - trig_old) < 1e-9: j -= 1
        prev = books[trig][0][j] if j < len(books[trig][0]) else None
    stood = (mv_ms - prev) / 1000 if prev and prev < mv_ms else None
    oth_b, n_oth = others(mv_ms - 1000, tf)
    oth_a5, _ = others(mv_ms + 5_000, tf)
    oth_a60, _ = others(mv_ms + 60_000, tf)
    oth_end, _ = others(ts + 300_000, tf)
    trig_60, trig_300 = bk(trig, mv_ms + 60_000), bk(trig, ts + 300_000)
    move = (trig_new - trig_old) if trig_old is not None and trig_new is not None else None

    pre = seen(mv_ms - 10_000); pre_true = true(mv_ms - 10_000)
    s_buy = seen(ts); t_buy = true(ts)
    entry, exit_ = v.get('entry'), v.get('exit')
    pre_side = v['pre_mid'] if yes else 1 - v['pre_mid']
    target = min(1.0, max(0.0, pre_side + v.get('catch', 1.0) * v.get('move', 0) - v.get('spread', 0) / 2))
    # the hold, as received
    hit = hit_true = None; hi = (None, None); lo = None
    for t, (b, a) in zip(rt, rq):
        if t < ts or t > end or b is None: continue
        if hi[0] is None or b > hi[0]: hi = (b, t)
        if lo is None or b < lo: lo = b
        if hit is None and b >= target - 1e-9: hit = t
    for t, (b, a) in zip(stt, sv):
        if t >= ts and t <= end and b is not None and b >= target - 1e-9: hit_true = t; break
    s_exit = seen(end)
    after_hi = (None, None)
    for t, (b, a) in zip(rt, rq):
        if t <= end or t > end + 300_000 or b is None: continue
        if after_hi[0] is None or b > after_hi[0]: after_hi = (b, t)
    # when did Polymarket (own clock) first trade half the trigger's move above the old price?
    half = pre_side + 0.5 * v.get('move', 0)
    pm_half = None
    for t, (b, a) in zip(stt, sv):
        if t < mv_ms - 300_000 or b is None or a is None: continue
        if (b + a) / 2 >= half: pm_half = t; break

    r = dict(id=tid, bot=bot, real=bool(real), ts=ts, closed=cl, home=v.get('home'), away=v.get('away'), sport=v.get('sport'), side=('Yes ' if yes else 'No ') + o, trig=trig,
             reason=v.get('reason'), pnl=v.get('pnl'), cost=v.get('cost'), entry=entry, exit=exit_, target=target, pre_side=pre_side, bot_move=v.get('move'),
             trig_old=trig_old, trig_new=trig_new, move=move, stood_s=stood, others_before=oth_b, others_5s=oth_a5, others_60s=oth_a60, others_end=oth_end, n_others=n_oth,
             trig_60s=trig_60, trig_300s=trig_300, pre_ask=pre[1], pre_bid=pre[0], pre_ask_true=pre_true[1], seen_ask=s_buy[1], seen_bid=s_buy[0], true_ask=t_buy[1], true_bid=t_buy[0],
             late_ms=late, react_s=(ts - mv_ms) / 1000, hit_s=None if hit is None else (hit - ts) / 1000, hit_true_s=None if hit_true is None else (hit_true - ts) / 1000,
             hi_bid=hi[0], hi_s=None if hi[1] is None else (hi[1] - ts) / 1000, lo_bid=lo, held_s=(end - ts) / 1000, exit_bid=s_exit[0],
             after_hi=after_hi[0], after_hi_s=None if after_hi[1] is None else (after_hi[1] - end) / 1000, pm_half_s=None if pm_half is None else (pm_half - mv_ms) / 1000,
             cons_now=v.get('cx', {}).get('cons_now'), gap_shock=v.get('cx', {}).get('gap_shock'), fams=v.get('cx', {}).get('fams'), fam_moved=v.get('cx', {}).get('fam_moved'), n_pm=len(q))
    # ---- verdicts
    notes = []
    mvv = v.get('move', 0) or 0
    # trigger
    if move is None: r['v_trig'] = 'no bookmaker rows'
    else:
        if oth_b is not None and abs(trig_new - oth_b) <= 0.03 and abs(trig_old - oth_b) >= 0.6 * abs(move): r['v_trig'] = 'STALE LINE: trigger caught up with the others'
        elif oth_b is not None and oth_a60 is not None and (oth_a60 - oth_b) >= 0.5 * move: r['v_trig'] = 'real move: others followed'
        elif oth_b is not None and oth_a60 is not None and (oth_a60 - oth_b) >= 0.2 * move: r['v_trig'] = 'partly followed by the others'
        else: r['v_trig'] = 'only the trigger moved'
        if trig_300 is not None and (trig_new - trig_300) >= 0.5 * move: notes.append('trigger went back within the hold')
    # buy
    if entry is not None and pre[1] is not None and entry > pre[1] + 0.025: r['v_buy'] = f"PAID UP: {c100(entry - pre[1]).strip()} pt above the price before the move"
    elif t_buy[1] is not None and entry is not None and t_buy[1] > entry + 0.025 and not real: r['v_buy'] = 'PRICE GONE on Polymarket\'s clock (paper fill a real order would miss)'
    else: r['v_buy'] = 'bought before Polymarket moved'
    if pm_half is not None and pm_half < mv_ms - 5000: notes.append(f"Polymarket was half-way there {abs(r['pm_half_s']):.0f} s BEFORE the trigger moved")
    # sale
    why = (v.get('reason') or '')
    if cl is None: r['v_sale'] = 'open'
    elif why == 'caught up':
        r['v_sale'] = 'sold on the catch-up' + ('' if hit is None or (end - hit) < 8000 else f" ({(end - hit) / 1000:.0f} s after the bid first reached the target)")
        if exit_ is not None and exit_ < target - 0.02: notes.append(f"sold {c100(target - exit_).strip()} pt under the target")
    elif why == 'gave up':
        r['v_sale'] = 'gave up on time' if abs((end - ts) / 1000 - v.get('give_up_s', 300)) < 25 else f"gave up after {(end - ts) / 1000:.0f} s"
        if hit is not None: r['v_sale'] = f"MISSED CATCH-UP: bid reached the target {(hit - ts) / 1000:.0f} s after the buy, sold at give-up"
        if exit_ is not None and s_exit[0] is not None and exit_ < s_exit[0] - 0.025: notes.append(f"sold {c100(s_exit[0] - exit_).strip()} pt under the bid shown")
        if after_hi[0] is not None and after_hi[0] >= target - 1e-9: notes.append(f"reached the target {r['after_hi_s']:.0f} s after the sale")
    else: r['v_sale'] = why
    r['notes'] = notes
    return r


if ALL_REAL:
    rows = db.execute("SELECT id, bot, real, ts_ms, closed_ms, data FROM bot_trades WHERE real = 1 ORDER BY ts_ms").fetchall()
else:
    rows = db.execute("SELECT id, bot, real, ts_ms, closed_ms, data FROM bot_trades WHERE ts_ms >= ? AND (real = 1 OR json_extract(data, '$.twin_of') IS NULL) ORDER BY ts_ms", (NOW - int(HOURS * 3600_000),)).fetchall()
R = [audit(r) for r in rows]
for r in R:
    print(f"\n#{r['id']} {'REAL ' if r['real'] else 'paper'} {r['bot']} {no(r['ts'])} {r['sport']} · {r['home']} v {r['away']} · {r['side']} · {r['reason']} · {r['pnl']:+.2f}" if r['pnl'] is not None else f"\n#{r['id']} open")
    print(f"  trigger {r['trig']:11s} {c100(r['trig_old'])} -> {c100(r['trig_new'])} (bot saw {c100(r['bot_move'])} pt) old price stood {'-' if r['stood_s'] is None else f'{r_:.0f} s' if (r_ := r['stood_s']) < 600 else f'{r_ / 60:.0f} min'}"
          f" | others({r['n_others']}) before {c100(r['others_before'])} +5s {c100(r['others_5s'])} +60s {c100(r['others_60s'])} at end {c100(r['others_end'])} | trigger +60s {c100(r['trig_60s'])} at end {c100(r['trig_300s'])}")
    print(f"  buy     ask 10 s before {c100(r['pre_ask'])} (bid {c100(r['pre_bid'])}) | seen at buy {c100(r['seen_ask'])} | own clock {c100(r['true_ask'])} | filled {c100(r['entry'])} | feed late {r['late_ms']} ms | {r['react_s']:.1f} s after the move | PM half-way at {'-' if r['pm_half_s'] is None else f'{r_:+.0f} s' if (r_ := r['pm_half_s']) is not None else ''} vs move")
    print(f"  hold    target {c100(r['target'])} | bid first there {'never' if r['hit_s'] is None else f'+{r_:.0f} s' if (r_ := r['hit_s']) is not None else ''} | high {c100(r['hi_bid'])} at +{(r['hi_s'] or 0):.0f} s, low {c100(r['lo_bid'])} | sold {c100(r['exit'])} after {r['held_s']:.0f} s (bid shown {c100(r['exit_bid'])})"
          f" | next 5 min high {c100(r['after_hi'])}")
    print(f"  => {r['v_trig']} · {r['v_buy']} · {r['v_sale']}" + (' · ' + '; '.join(r['notes']) if r['notes'] else ''))
if OUT: json.dump(R, open(OUT, 'w'))
