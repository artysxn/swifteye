#!/usr/bin/env python3
"""Watch Swifteye's health once a minute. Exits with one block of text on the first thing that looks wrong, or at the
deadline. Reads only: the Bots panel, a few indexed rows of the database, the process table, the end of the log.

usage: watch_health.py <deadline, epoch seconds> [--seen-first-trade]
"""
import sys, os, time, json, sqlite3, shutil, subprocess, urllib.request, statistics as st, datetime as dt

END = float(sys.argv[1]); SEEN_FIRST = '--seen-first-trade' in sys.argv
HOME = os.path.expanduser('~'); DB = HOME + '/swifteye/oddsfeed.db'; LOG = HOME + '/swifteye/logs/oddsfeed.log'
now_ms = lambda: int(time.time() * 1000)
U = lambda: dt.datetime.now(dt.UTC).strftime('%H:%M:%S UTC')


def panel():
    with urllib.request.urlopen('http://127.0.0.1:8787/bots/panel?real=1', timeout=8) as r: return json.loads(r.read())


def pid():
    out = subprocess.run(['launchctl', 'list'], capture_output=True, text=True).stdout
    for l in out.splitlines():
        if 'com.swifteye.oddsfeed' in l:
            p = l.split()[0]
            return int(p) if p.isdigit() else None
    return None


def q(sql, args=()):
    c = sqlite3.connect('file:' + DB + '?mode=ro', uri=True, timeout=5)
    try: return c.execute(sql, args).fetchall()
    finally: c.close()


def stop(msg):
    print(f"{U()} {msg}", flush=True); sys.exit(0)


base = panel(); real0 = base['mode']['real']; run_end = ((base.get('run') or {}).get('end_ms') or 0)
STATE_P = HOME + '/swifteye/research/watch_state.json'
state = json.load(open(STATE_P)) if os.path.exists(STATE_P) else dict(since=now_ms(), opened=[], closed=[], stuck=[], last_order=q("SELECT COALESCE(MAX(id), 0) FROM bot_orders")[0][0])
log_pos = os.path.getsize(LOG) if os.path.exists(LOG) else 0
streak = {}; unfilled = 0


def bad(name, cond, need):
    streak[name] = streak.get(name, 0) + 1 if cond else 0
    return streak[name] >= need


while time.time() < END:
    t = now_ms(); P = None
    try: P = panel()
    except Exception as e: err = repr(e)[:120]
    p = pid()
    if bad('service', P is None or p is None, 6): stop(f"SERVICE: the app does not answer or has no process (pid {p})")
    if P:
        if P['mode'].get('kill'): stop("KILL SWITCH is on")
        if real0 and not P['mode']['real'] and t < run_end - 60_000: stop(f"REAL MONEY WENT OFF before the run's end (balance {P.get('balance')})")
    try:
        # decisions
        n, late = q("SELECT COUNT(*), COALESCE(SUM(result = 'measured_too_late'), 0) FROM bot_signals WHERE ts_ms >= ?", (t - 900_000,))[0]
        last_dec = q("SELECT COALESCE(MAX(ts_ms), 0) FROM bot_signals")[0][0]
        # Polymarket feed
        mx = q("SELECT MAX(rowid) FROM pm_quotes")[0][0]
        rows = q("SELECT ts_ms, ts_ms - server_ts_ms FROM pm_quotes WHERE rowid > ? AND ts_ms >= ?", (mx - 4000, t - 300_000))
        pm_age = (t - max(r[0] for r in rows)) / 1000 if rows else 999
        d = sorted(r[1] for r in rows if r[1] is not None)
        pm_med = st.median(d) if d else 0; pm_slow = sum(x > 2000 for x in d) / len(d) if d else 0
        # bookmaker feeds
        mc = q("SELECT MAX(rowid) FROM changes")[0][0]
        bk = q("SELECT MAX(ts_ms) FROM changes WHERE rowid > ? AND book NOT IN ('polymarket', 'kalshi')", (mc - 3000,))[0][0] or 0
        if os.environ.get('WATCH_DEBUG'): print(f'debug: decisions 15 min {n}, late {late}, last decision {(t - last_dec) / 1000:.0f} s ago, pm rows {len(rows)}, pm age {pm_age:.0f} s, pm median {pm_med:.0f} ms, over 2 s {pm_slow * 100:.1f}%, bookmaker price {(t - bk) / 1000:.0f} s ago, pid {p}', file=sys.stderr, flush=True)
        with open(HOME + '/swifteye/research/watch_log.jsonl', 'a') as f:
            f.write(json.dumps(dict(t=t, n=n, late=late, last_dec_s=round((t - last_dec) / 1000), pm_rows=len(rows), pm_med=round(pm_med), pm_slow=round(pm_slow, 3), pm_age=round(pm_age), book_s=round((t - bk) / 1000), real=(P or {}).get('mode', {}).get('real'), bal=(P or {}).get('balance'))) + '\n')
        if bad('pm_quiet', pm_age > 180, 4): stop(f"POLYMARKET FEED QUIET: no price for {pm_age:.0f} s")
        if bad('books_quiet', t - bk > 240_000, 4): stop(f"BOOKMAKER FEEDS QUIET: no bookmaker price for {(t - bk) / 1000:.0f} s")
        if bad('no_decisions', t - last_dec > 900_000 and pm_age < 60, 4): stop(f"NO DECISIONS for {(t - last_dec) / 60000:.0f} min while prices are flowing")
        if bad('late', n >= 80 and late / n >= 0.15, 6): stop(f"LATE DECISIONS: {late} of {n} in the last 15 min ({late * 100 // n}%)")
        if bad('pm_late', len(d) >= 200 and (pm_med > 1500 or pm_slow > 0.35), 6): stop(f"POLYMARKET FEED LATE: median {pm_med:.0f} ms, {pm_slow * 100:.0f}% over 2 s (last 5 min)")
        # every trade: one event when it opens, one when it closes (won or lost); a paper twin of a real trade is not repeated
        events = []
        for tid, bot, real, ts, closed, data in q("SELECT id, bot, real, ts_ms, closed_ms, data FROM bot_trades WHERE ts_ms >= ? OR closed_ms IS NULL", (state['since'],)):
            v = json.loads(data); give = int(v.get('give_up_s') or 900)
            if bot not in ('snipe', 'snipe-trial'): continue
            who = 'REAL' if real else ('paper trial' if bot == 'snipe-trial' else 'paper')
            desc = f"#{tid} {who} | {v.get('sport')} | {v.get('home')} v {v.get('away')} | {v.get('outcome')} | {v.get('contracts')} at {float(v.get('entry') or 0):.3f} | cost ${float(v.get('cost') or 0):.2f} | move {float(v.get('move') or 0) * 100:.0f} pt"
            if ts >= state['since'] and v.get('twin_of') is None:
                if tid not in state['opened']: state['opened'].append(tid); events.append('TRADE OPENED ' + desc)
                if closed is not None and tid not in state['closed']:
                    state['closed'].append(tid); pnl = float(v.get('pnl') or 0)
                    events.append(f"TRADE {'WON' if pnl > 0 else 'LOST'} {pnl:+.2f} {desc} | sold at {float(v.get('exit') or 0):.3f} ({v.get('reason')}) after {(closed - ts) / 1000:.0f} s")
            if closed is None and ts > t - 6 * 3600_000 and t - ts > (give + (240 if real else 900)) * 1000 and tid not in state['stuck']:
                state['stuck'].append(tid); events.append(f"TRADE STUCK OPEN: #{tid} ({who}) {v.get('home')} v {v.get('away')}, open {(t - ts) / 60000:.0f} min, give-up was {give} s")
        for oid, ok, data in q("SELECT id, ok, data FROM bot_orders WHERE id > ?", (state['last_order'],)):
            state['last_order'] = max(state['last_order'], oid); v = json.loads(data)
            if v.get('intent') or v.get('side') == 'buy-intent': continue          # the note written beside every real buy, not an order
            if not ok: events.append(f"REAL ORDER {'DID NOT FILL' if (v.get('error') or '') == 'nothing filled' else 'FAILED'}: #{oid} {v.get('side')} limit {v.get('limit')} x {v.get('shares')} | {str(v.get('error'))[:200]}")
        json.dump(state, open(STATE_P, 'w'))
        if events: stop('\n'.join(events))
    except SystemExit: raise
    except Exception as e:
        if os.environ.get('WATCH_DEBUG'): print('debug: check error', repr(e)[:200], file=sys.stderr, flush=True)
        if bad('db', True, 8): stop(f"DATABASE CHECK FAILING: {repr(e)[:160]}")
    else: streak['db'] = 0
    # machine
    try:
        wal = os.path.getsize(DB + '-wal')
        if wal > 400e6: stop(f"WRITE-AHEAD FILE is {wal / 1e6:.0f} MB")
        free = shutil.disk_usage(HOME).free
        if free < 5e9: stop(f"DISK: {free / 1e9:.1f} GB free")
        if p:
            rss = int(subprocess.run(['ps', '-o', 'rss=', '-p', str(p)], capture_output=True, text=True).stdout.strip() or 0) / 1024
            if bad('rss', rss > 3000, 6): stop(f"MEMORY: the app holds {rss:.0f} MB")
        if os.path.exists(LOG):
            size = os.path.getsize(LOG)
            if size > log_pos:
                with open(LOG, 'rb') as f:
                    f.seek(max(log_pos, size - 400_000)); chunk = f.read().decode('utf-8', 'replace')
                log_pos = size
                hit = [l for l in chunk.splitlines() if 'panicked' in l or 'PANIC' in l]
                if hit: stop("PANIC in the log: " + hit[-1][:300])
            elif size < log_pos: log_pos = size
    except SystemExit: raise
    except Exception: pass
    time.sleep(30)
print(f"{U()} DEADLINE reached: {len(state['opened'])} trades opened and {len(state['closed'])} closed during the watch", flush=True)
