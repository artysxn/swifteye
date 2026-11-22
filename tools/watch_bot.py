"""Keep an eye on the live bot (owner asleep, 21 Nov 2026). Checks once a minute and EXITS, with the reason on its
last line, as soon as something needs a look, so whoever started it is called back:
  SERVICE DOWN     the bot's API did not answer for 3 minutes
  FEED LATE        the Polymarket feed was over 1.5 s late on 5 checks in a row
  RUN ENDED        real money is off (the test run ended, or a safety stop switched it off)
  LOSS             one real trade lost $6 or more, or the run is $10 below its best point
  UNFILLED         3 real buy orders in a row found nothing to fill
  STUCK            a real position is still open 4 minutes after its give-up time
  TRADE            (with the word 'first') the first real trade closed
  BUY / SOLD       (with the word 'each') every buy and every sale of the snipe bot, real money and the paper
                   trial alike, so each one's chart can be checked; told once each (watch_seen.json)
  DISK             under 5 GB free
  CHECKPOINT       nothing wrong: the planned look after N minutes, or at a clock time given as HH:MM
It changes nothing itself. A line goes to ~/swifteye/research/watch.log every 10 minutes and for every real trade.
usage: watch_bot.py [checkpoint minutes or HH:MM, default 120] [first] [each] [knownlate]
  knownlate   do not call back for a late feed (it is already being worked on); everything else as usual"""
import sys, os, json, time, sqlite3, subprocess, shutil, datetime as dt
NO = dt.timezone(dt.timedelta(hours=1))
def _check(a):
    if ':' not in a: return float(a)
    h, m = map(int, a.split(':')); now = dt.datetime.now(NO); t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return ((t if t > now else t + dt.timedelta(days=1)) - now).total_seconds() / 60
CHECK = _check(sys.argv[1]) if len(sys.argv) > 1 else 120
EACH = 'each' in sys.argv[2:]     # exit on every snipe buy and sale (real and trial), each told once
SEEN_FILE = os.path.expanduser('~/swifteye/research/watch_seen.json')
KNOWN_LATE = 'knownlate' in sys.argv[2:]
FIRST = 'first' in sys.argv[2:]   # also exit when the first real trade closes (to check a change end to end)
LOG = os.path.expanduser('~/swifteye/research/watch.log')
DB = os.path.expanduser('~/swifteye/oddsfeed.db')
t_start = time.time(); now_ms = lambda: int(time.time() * 1000)
start_ms = now_ms()


def log(s):
    line = f"{dt.datetime.now(NO):%d %H:%M} {s}"
    open(LOG, 'a').write(line + '\n'); print(line, flush=True)


def bots():
    try: return json.loads(subprocess.run(['curl', '-s', '-m', '10', 'http://127.0.0.1:8787/bots'], capture_output=True, text=True).stdout)
    except Exception: return None


def q(sql, args=()):
    try:
        c = sqlite3.connect('file:' + DB + '?mode=ro', uri=True, timeout=5); r = c.execute(sql, args).fetchall(); c.close(); return r
    except Exception:
        return None


def done(why):
    log('EXIT: ' + why); sys.exit(0)


down = late = 0; peak = None; seen = set(); n = 0; was_real = None
try: told = set(map(tuple, json.load(open(SEEN_FILE))))
except Exception: told = set()
def tell(i, what, text):
    """Exit once for this trade's buy or sale, so its chart gets checked."""
    if (i, what) in told: return
    told.add((i, what)); json.dump(sorted(told), open(SEEN_FILE, 'w')); done(text)
log(f"watch started (checkpoint in {CHECK:.0f} min{', every buy and sale' if EACH else ''})")
while True:
    d = bots()
    if d is None:
        down += 1
        if down >= 3: done('SERVICE DOWN: the bot did not answer for 3 minutes')
    else:
        down = 0
        st = d['settings']; sn = st['snipe']; lag = d['snipe']['status'].get('pm_lag_ms') or 0
        real_on = bool(sn.get('real')); run = st.get('run'); pnl = d['snipe']['real'].get('closed_pnl') or 0.0
        if was_real is None: was_real = real_on
        if was_real and not real_on: done(f"RUN ENDED: real money is off (run result ${pnl:+.2f})")
        late = late + 1 if lag > 1500 else 0
        if late >= 5 and not KNOWN_LATE: done(f"FEED LATE: over 1.5 s late on 5 checks in a row (now {lag} ms)")
        peak = pnl if peak is None else max(peak, pnl)
        if peak - pnl >= 10: done(f"LOSS: the run is ${peak - pnl:.2f} below its best point (now ${pnl:+.2f})")
        rows = q("SELECT id, closed_ms, data, ts_ms FROM bot_trades WHERE real = 1 AND ts_ms >= ?", (start_ms - 600_000,)) or []
        for i, cl, data, ts in rows:
            v = json.loads(data); key = (i, cl is not None)
            # A position still open well past its give-up time: the sale is not going through.
            if cl is None and now_ms() - ts > ((v.get('give_up_s') or 300) + 240) * 1000:
                done(f"STUCK: real trade #{i} ({v.get('home')} v {v.get('away')}) is still open {(now_ms() - ts) / 60000:.0f} min after the buy")
            if key in seen: continue
            seen.add(key)
            if cl is None: log(f"real BUY #{i}: {v.get('sport')} {v.get('home')} v {v.get('away')} at {100 * v.get('entry', 0):.0f}c, ${v.get('cost', 0):.2f} (stake x{v.get('stake_x', 1)})")
            else:
                log(f"real SOLD #{i}: {v.get('home')} v {v.get('away')} {v.get('reason')} {v.get('pnl', 0):+.2f}")
                if (v.get('pnl') or 0) <= -6: done(f"LOSS: trade #{i} lost ${-v['pnl']:.2f}")
                if FIRST: done(f"TRADE: real trade #{i} closed {v.get('pnl', 0):+.2f} (cost ${v.get('cost', 0):.2f}, stake x{v.get('stake_x', 1)})")
        if EACH:
            for i, bot, real, cl, data in q("SELECT id, bot, real, closed_ms, data FROM bot_trades WHERE (real = 1 OR bot = 'snipe-trial') AND bot LIKE 'snipe%' AND ts_ms >= ? ORDER BY id", (start_ms - 900_000,)) or []:
                v = json.loads(data); who = 'REAL' if real else 'trial (paper)'
                if cl is not None: tell(i, 'sold', f"SOLD: {who} trade #{i} {v.get('home')} v {v.get('away')} closed {v.get('pnl', 0):+.2f} ({v.get('reason')}): run chart_check.py {i}")
                else: tell(i, 'buy', f"BUY: {who} trade #{i} {v.get('sport')} {v.get('home')} v {v.get('away')} at {100 * v.get('entry', 0):.0f}c, ${v.get('cost', 0):.2f}, stake x{v.get('stake_x', 1)}: run chart_check.py {i}")
        od = q("SELECT ok FROM bot_orders WHERE bot = 'snipe' AND ts_ms >= ? AND json_extract(data, '$.side') = 'buy' AND json_extract(data, '$.intent') IS NULL ORDER BY ts_ms DESC LIMIT 3", (start_ms,)) or []
        if len(od) == 3 and not any(r[0] for r in od): done('UNFILLED: 3 real buy orders in a row found nothing to fill')
        if n % 10 == 0:
            fav = d.get('favs') or {}
            log(f"ok: feed {lag} ms, real {'on' if real_on else 'off'}, run ${pnl:+.2f}, favourites paper {len(fav.get('open', []))} open / {fav.get('settled', 0)} settled (${fav.get('closed_pnl', 0):+.2f})")
    if shutil.disk_usage(os.path.expanduser('~')).free < 5 * 2 ** 30: done('DISK: under 5 GB free')
    if time.time() - t_start >= CHECK * 60: done('CHECKPOINT: nothing wrong')
    n += 1; time.sleep(60)
