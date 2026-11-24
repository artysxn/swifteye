"""The steered resting orders, live, ON PAPER (24 Nov 2026). No order leaves this machine.

  python -I tools/maker/paperbot.py --db run/oddsfeed.db --data run/maker [--api http://127.0.0.1:8787]

What it does, about once a second, for every two-way game Polymarket lists that is being played:
  1. reads the newest Polymarket quotes the collector wrote to its database;
  2. where the game qualifies (book alive, price 10-90c, spread 5c+, or we hold shares in it) asks a worker for a
     DECISION: the worker keeps that game's rows, computes the model's 173 inputs with the research code itself
     (feat.py; parity.py shows they equal the backtest's) and scores them with the exported model;
  3. turns the decision into INTENTS, the orders a real bot would have resting from that moment:
       entry  one cent inside the spread, on a side the bookmakers' last 30 s are not against and where the model
              expects a gain; 10 shares, 20 where the call is above its usual level; at most 2 orders deep a game;
       exit   while we hold shares: the order that sells them, one cent inside any spread of 2c+, except while the
              bookmakers moved OUR way in the last 30 s;
     an intent exists only from the moment its decision is READY (so our own slowness is in the result) plus 0.3 s
     for the trip to the exchange, only while the wallet can pay for it (widest spreads first), and dies when a
     newer decision replaces it or none comes for 5 s (a pulled order stays live 0.3 s more, as a real cancel would);
     every order a real bot would have sent or pulled is written to orders.jsonl (token, side, price, size);
  4. listens to Polymarket's public trade stream (price, size, side, to the millisecond). A real trade that reaches
     the price of an intent resting at that moment FILLS it on paper (min of our size and the trade's; one fill a
     side a game a minute, as in the backtest);
  5. nets buys and sales of a game first in first out; what is left is paid at the result.
Files in --data: config.json (settings), model.npz (export_model.py), state.json (wallet and positions, read again
on a restart), journal.jsonl (every fill, close, result and a line a minute), status.txt (the summary to read),
orders.jsonl (the orders a real bot would have sent), tokens.json (Polymarket token ids per game), bot.log.

THE COLLECTOR'S ORDER KEEPER (src/maker.rs). This program never talks to Polymarket about orders. With `mirror`
on it hands the collector, every pass, the whole set of orders it wants resting; on paper the collector only keeps
a pretend table (a rehearsal of the real path). REAL MONEY = `mode: "real"` here AND Real switched on in the
collector by the owner (typed confirmation; this program never does it and stops at once if Real is off there).
Then the collector keeps exactly that set on Polymarket, its limits guard the money, and the fills it reads from
the venue come back here instead of the paper fills. The real ledger is kept apart from the paper one (files
real_state.json, real_journal.jsonl, real_status.txt, real_orders.jsonl). tools/maker/README.md has the steps."""
import argparse
import asyncio
import collections
import concurrent.futures as cf
import datetime as dt
import difflib
import json
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feat as FT  # noqa: E402
import model as MD  # noqa: E402

GAMMA = 'https://gamma-api.polymarket.com'
WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market'
DEFAULTS = {
    'mode': 'paper',            # 'paper', or 'real' (needs Real switched on in the collector by the owner)
    'mirror': False,            # paper only: rehearse the hand-off to the collector's order keeper (it sends nothing)
    'executor': '',             # the collector that keeps the orders ('' = the one given with --api)
    'wallet': 175.0,            # paper dollars at the first start
    'base_shares': 10,          # an entry order
    'strong_mult': 2,           # x this where the model's call is at or above its usual level
    'min_spread_c': 5,          # entries only in spreads this wide
    'exit_min_spread_c': 2,     # the exit order rests in spreads this wide
    'cap_orders': 2,            # most entry orders deep in one game
    'price_lo': 0.10, 'price_hi': 0.90,
    'max_age_s': 15,            # the game's newest Polymarket row must be this fresh
    'intent_ttl_s': 5,          # an intent not confirmed by a newer decision for this long is pulled
    'throttle_s': 60,           # one fill a side a game in this many seconds (the backtest's rule)
    'min_order_shares': 5,      # Polymarket's smallest order
    'order_delay_ms': 300,    # a new order counts from this long after we decide it, and a pulled one stays live this long (the trip to the exchange)
    'workers': 6,
    'smart_exit': True,         # no exit order while the bookmakers moved our way in the last 30 s
    'min_history_s': 900,       # no decision on a game until this much of it is on record here (after a restart the inputs are cold)
    'sports': sorted(FT.TWO_WAY),
}
_W = {}


# ------------------------------------------------------------------ workers: one game's rows, features, model
def _init(db_path, model_path):
    _W['db'] = sqlite3.connect('file:' + db_path + '?mode=ro', uri=True, timeout=10)
    _W['model'] = MD.Model(model_path)
    _W['games'] = {}
    _W['research'] = bool(_W['db'].execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'changes_research'").fetchone())


def decide(g, sport, start_ms, T):
    t0 = time.time()
    db, st = _W['db'], _W['games'].setdefault(g, {'pm': [], 'bk': [], 'pm_id': 0, 'changes': 0, 'changes_research': 0, 'pm_ts': 0, 'changes_ts': 0, 'changes_research_ts': 0})
    rows = db.execute('SELECT id, ts_ms, server_ts_ms, outcome, bid, ask, last, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND ts_ms >= ? AND id > ? ORDER BY id', (g, st['pm_ts'], st['pm_id'])).fetchall()
    if rows:
        st['pm'] += [r[1:] for r in rows]
        st['pm_id'], st['pm_ts'] = rows[-1][0], max(r[1] for r in rows)
        if len(st['pm']) > len(rows) and rows[0][1] < st['pm'][-len(rows) - 1][0]:
            st['pm'].sort(key=lambda r: r[0])
    for table in ('changes', 'changes_research') if _W['research'] else ('changes',):
        rows = db.execute(f"SELECT id, ts_ms, book, outcome, new_price FROM {table} WHERE match_id = ? AND ts_ms >= ? AND id > ? AND market = '1x2' AND book NOT IN ('polymarket', 'kalshi') ORDER BY id", (g, st[table + '_ts'], st[table])).fetchall()
        if rows:
            st['bk'] += [r[1:] for r in rows]
            st[table], st[table + '_ts'] = rows[-1][0], max(r[1] for r in rows)
    out = {'g': g, 'T': T, 'ok': False, 'n_pm': len(st['pm']), 'n_bk': len(st['bk'])}
    try:
        d = FT.at(st['pm'], st['bk'], sport, start_ms, T)
    except Exception as e:                       # one odd game must not stop the bot
        out['err'] = repr(e)[:200]
        d = None
    if d is None:
        home = [r for r in st['pm'] if r[2] == 'home']
        out['why'] = ('error in the inputs' if out.get('err') else 'no Polymarket quotes for the home side' if not home else 'no bookmaker prices on record' if not st['bk']
                      else 'one side of the book is empty' if home[-1][3] is None or home[-1][4] is None else 'under 5 minutes on record, or no bookmaker line with two prices yet')
    if d is not None:
        m = _W['model']
        out.update(ok=True, grid_s=d['grid_s'], books=d['books'], bid=d['bid'], ask=d['ask'], qb=d['qb'], qa=d['qa'], spread_c=d['spread_c'], mid=d['mid'], inplay=d['inplay'], fam30=d['fam30'], lead=d['lead'], age_s=d['age_s'],
                   live_min=d['live_min'], call_buy=m.call(d['row_buy']), call_sell=m.call(d['row_sell']))
    out['took'] = time.time() - t0
    return out


# ------------------------------------------------------------------ the bot
def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_post(url, body, timeout=4):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers={'User-Agent': 'research-script/1.0', 'Content-Type': 'application/json', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


norm = lambda s: ' '.join(re.findall(r'[a-z0-9]+', (s or '').lower()))
sim = lambda a, b: difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


class Bot:
    def __init__(self, a):
        self.a = a
        os.makedirs(a.data, exist_ok=True)
        cp = os.path.join(a.data, 'config.json')
        self.cfg = dict(DEFAULTS)
        if os.path.exists(cp):
            self.cfg.update(json.load(open(cp)))
        json.dump(self.cfg, open(cp, 'w'), indent=1)
        if self.cfg['mode'] not in ('paper', 'real'):
            raise SystemExit('config.json: mode must be "paper" or "real" (tools/maker/README.md).')
        self.real = self.cfg['mode'] == 'real'
        self.tag = 'real_' if self.real else ''          # the real ledger never mixes with the paper one
        self.xurl = (self.cfg.get('executor') or a.api).rstrip('/')
        self.x, self.x_ms, self.x_wait, self.x_seq, self.settled_tokens = {}, collections.deque(maxlen=400), 0.0, 0, []
        if self.real:
            try:
                v = http_json(self.xurl + '/maker', 5)
            except Exception as e:
                raise SystemExit(f'mode "real": the collector does not answer on /maker ({e!r}). Nothing started.')
            if not v.get('real'):
                raise SystemExit('mode "real": Real is OFF in the collector. The owner switches it on there (tools/maker/README.md); this program never does. Nothing started.')
            self.x_seq = int(v.get('seq') or 0)            # fills before this start are not ours to book again (state below overrides)
        if not os.path.exists(MD.default_path(a.data)) and os.path.exists(os.path.join(HERE, 'model.npz')):
            shutil.copyfile(os.path.join(HERE, 'model.npz'), MD.default_path(a.data))     # the model that ships with the code (export_model.py makes a new one)
        self.model = MD.Model(MD.default_path(a.data))
        self.offset = 0.0
        self.sync_clock()
        self.db = sqlite3.connect('file:' + a.db + '?mode=ro', uri=True, timeout=10)
        self.ex = [cf.ProcessPoolExecutor(max_workers=1, initializer=_init, initargs=(a.db, MD.default_path(a.data))) for _ in range(int(self.cfg['workers']))]
        self.games, self.tob, self.pending = {}, {}, {}
        self.intents, self.hist = {}, collections.defaultdict(lambda: collections.deque(maxlen=40))
        self.trades = queue.Queue()
        self.tokens = json.load(open(os.path.join(a.data, 'tokens.json'))) if os.path.exists(os.path.join(a.data, 'tokens.json')) else {}
        self.tok_miss = {}
        self.asset = {}
        self.stop = False
        self.ws_state = {'up': 0, 'err': '', 'msgs': 0, 'trades': 0, 'buy_above': 0, 'buy_n': 0, 'last': 0}
        self.lat = collections.deque(maxlen=400)
        self.cyc = collections.deque(maxlen=400)
        self.last_err = ''
        self.took = collections.deque(maxlen=400)
        self.counts = collections.Counter()
        sp = os.path.join(a.data, self.tag + 'state.json')
        if os.path.exists(sp):
            st = json.load(open(sp))
            self.x_seq = int(st.get('x_seq', self.x_seq))
            self.cash, self.lots = st['cash'], {int(k): v for k, v in st['lots'].items()}
            self.last_fill = {tuple(json.loads(k)): v for k, v in st.get('last_fill', {}).items()}
            self.started, self.closed_n, self.closed_won, self.closed_pnl = st['started'], st['closed_n'], st['closed_won'], st['closed_pnl']
            self.decided_since = {}
            self.meta = st.get('meta', {})
        else:
            self.cash, self.lots, self.last_fill = float(self.cfg['wallet']), {}, {}
            self.started, self.closed_n, self.closed_won, self.closed_pnl = self.now(), 0, 0, 0.0
            self.decided_since, self.meta = {}, {}
        self.day_pnl = collections.defaultdict(lambda: [0, 0, 0.0])
        self.pm_id = self.db.execute('SELECT COALESCE(MAX(id), 0) FROM pm_quotes').fetchone()[0]
        self.t_disc = self.t_status = self.t_beat = self.t_clock = self.t_gamma = 0
        self.log(f'{"REAL MONEY" if self.real else "paper"} maker bot: wallet ${self.cash:.2f}, {len(self.lots)} games with open positions; model: {self.model.meta}')

    # ---- time, files
    def now(self):
        return int(time.time() * 1000 + self.offset)

    def sync_clock(self):
        try:
            self.offset = float(http_json(self.a.api + '/clock', 5).get('offset_ms', 0))
        except Exception:
            pass

    def log(self, s):
        print(f'{dt.datetime.now().strftime("%d %b %H:%M:%S")} {s}', flush=True)

    def journal(self, kind, **kw):
        with open(os.path.join(self.a.data, self.tag + 'journal.jsonl'), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'t': self.now(), 'kind': kind, **kw}) + '\n')

    def save(self):
        st = {'cash': self.cash, 'lots': {str(k): v for k, v in self.lots.items() if v}, 'last_fill': {json.dumps(list(k)): v for k, v in self.last_fill.items()}, 'started': self.started,
              'closed_n': self.closed_n, 'closed_won': self.closed_won, 'closed_pnl': self.closed_pnl, 'meta': self.meta, 'x_seq': self.x_seq}
        p = os.path.join(self.a.data, self.tag + 'state.json')
        json.dump(st, open(p + '.tmp', 'w'))
        os.replace(p + '.tmp', p)

    # ---- games
    def discover(self):
        now = self.now()
        q = """SELECT m.id, m.sport, m.home, m.away, m.start_ms, e.book_event_id, e.last_seen_ms FROM matches m JOIN book_events e ON e.match_id = m.id
               WHERE e.book = 'polymarket' AND m.start_ms IS NOT NULL AND m.start_ms <= ? AND m.start_ms >= ? AND e.last_seen_ms >= ?"""
        seen, looked = set(), 0
        for g, sport, home, away, start, ev, last_seen in self.db.execute(q, (now, now - 10 * 3600_000, now - 180_000)).fetchall():
            if sport not in self.cfg['sports']:
                continue
            seen.add(g)
            if g not in self.games:
                tk = self.tokens.get(str(g))
                if not tk:
                    if looked >= 3:                      # a lookup is a web request: a few a pass, the rest in 3 s, so decisions are never held up
                        self.t_disc = time.time() - 27
                        continue
                    looked += 1
                    tk = self.lookup(g, ev, home, away)
                if not tk:
                    continue
                self.games[g] = {'sport': sport, 'home': home, 'away': away, 'start': start, 'title': f'{home} v {away}', **tk}
                self.asset[tk['home_token']] = (g, 1)
                self.asset[tk['away_token']] = (g, 0)
                r = self.db.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = 'home' ORDER BY ts_ms DESC LIMIT 1", (g,)).fetchone()
                a_ = self.db.execute('SELECT MAX(ts_ms) FROM pm_quotes WHERE match_id = ?', (g,)).fetchone()[0]
                self.tob[g] = {'bid': r[1] if r else None, 'ask': r[2] if r else None, 'ts': r[0] if r else 0, 'any': a_ or 0, 'h': collections.deque(maxlen=200)}
                self.meta[str(g)] = {'title': f'{home} v {away}', 'sport': sport}
        for g in list(self.games):
            if g not in seen and not self.lots.get(g):
                self.drop_intents(g, self.now())
                for t_ in (self.games[g]['home_token'], self.games[g]['away_token']):
                    self.asset.pop(t_, None)
                del self.games[g]
                self.tob.pop(g, None)
        # a game we still hold shares in after a restart: keep it known
        for g in list(self.lots):
            if self.lots[g] and g not in self.games and str(g) in self.tokens:
                r = self.db.execute('SELECT sport, home, away, start_ms FROM matches WHERE id = ?', (g,)).fetchone()
                if r:
                    tk = self.tokens[str(g)]
                    self.games[g] = {'sport': r[0], 'home': r[1], 'away': r[2], 'start': r[3], 'title': f'{r[1]} v {r[2]}', **tk}
                    q_ = self.db.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = 'home' ORDER BY ts_ms DESC LIMIT 1", (g,)).fetchone()
                    self.tob[g] = {'bid': q_[1] if q_ else None, 'ask': q_[2] if q_ else None, 'ts': q_[0] if q_ else 0, 'any': q_[0] if q_ else 0, 'h': collections.deque(maxlen=200)}

    def lookup(self, g, ev, home, away):
        if time.time() - self.tok_miss.get(g, 0) < 300:
            return None
        try:
            e = http_json(f'{GAMMA}/events/{ev}')
            mk = [x for x in e.get('markets', []) if x.get('sportsMarketType') == 'moneyline'] or e.get('markets', [])[:1]
            outs, toks = json.loads(mk[0].get('outcomes') or '[]'), json.loads(mk[0].get('clobTokenIds') or '[]')
            if len(outs) != 2 or len(toks) != 2:
                raise ValueError('not a two-outcome market')
            hi = 0 if sim(outs[0], home) + sim(outs[1], away) >= sim(outs[1], home) + sim(outs[0], away) else 1
            tk = {'home_token': toks[hi], 'away_token': toks[1 - hi], 'condition': mk[0].get('conditionId'), 'event': str(ev)}
            self.tokens[str(g)] = tk
            json.dump(self.tokens, open(os.path.join(self.a.data, 'tokens.json'), 'w'))
            time.sleep(0.15)
            return tk
        except Exception as ex:
            self.tok_miss[g] = time.time()
            self.counts['token lookups failed'] += 1
            self.log(f'no tokens for game {g} ({home} v {away}): {repr(ex)[:120]}')
            return None

    def poll_quotes(self):
        rows = self.db.execute('SELECT id, ts_ms, match_id, outcome, bid, ask FROM pm_quotes WHERE id > ? ORDER BY id LIMIT 50000', (self.pm_id,)).fetchall()
        for i, ts, g, o, bid, ask in rows:
            t = self.tob.get(g)
            if t is None:
                continue
            t['any'] = max(t['any'], ts)
            if o == 'home':
                t['bid'], t['ask'], t['ts'] = bid, ask, ts
                t['h'].append((ts, bid, ask))
        if rows:
            self.pm_id = rows[-1][0]

    # ---- intents
    def pos(self, g):
        return sum(l[0] * l[3] for l in self.lots.get(g, []))

    def close_intent(self, key, now):
        it = self.intents.pop(key, None)
        if it:
            it['end'] = now
            self.hist[key].append(it)
            if it['funded'] and not it.get('filled'):
                self.order_action('cancel', it, now)

    def order_action(self, what, it, now):
        """What a real bot would send to Polymarket for this intent, written to orders.jsonl (nothing is sent).
        Selling the home side without holding it = buying the away token at 1 - price; an exit sells the token held."""
        G = self.games.get(it['g'])
        if what == 'place':
            if not G:
                return
            home = (self.pos(it['g']) > 0) if it['kind'] == 'exit' else (it['side'] > 0)
            it['order'] = {'game': it['g'], 'title': G['title'], 'token': G['home_token' if home else 'away_token'], 'side': 'SELL' if it['kind'] == 'exit' else 'BUY',
                           'price': round(it['price'] if home else 1 - it['price'], 2), 'size': it['size'], 'post_only': True, 'kind': it['kind']}
        if not it.get('order'):
            return
        self.counts[f'orders a real bot would have sent: {what}'] += 1
        with open(os.path.join(self.a.data, self.tag + 'orders.jsonl'), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'t': now, 'do': what, **it['order']}) + '\n')

    def drop_intents(self, g, now):
        for s in (1, -1):
            self.close_intent((g, s), now)

    def on_decision(self, r):
        g, now, c = r['g'], self.now(), self.cfg
        self.took.append(r['took'])
        if g not in self.games:
            return
        want = {}
        self.counts['decisions'] += 1
        if r.get('err'):
            self.counts['decision errors'] += 1
            self.last_err = r['err']
        if not r['ok']:
            self.counts['no decision: ' + r.get('why', '?')] += 1
        elif r['grid_s'] < c['min_history_s']:
            self.counts[f'no decision: under {c["min_history_s"] // 60} min of this game on record'] += 1
        elif r['books'] < 1:
            self.counts['no decision: no bookmaker line now'] += 1
        elif r['inplay'] and r['age_s'] <= c['max_age_s'] and c['price_lo'] <= r['mid'] <= c['price_hi']:
            p, sp, fam = self.pos(g), r['spread_c'], r['fam30']
            dust = abs(p) < c['min_order_shares'] - 1e-9       # Polymarket takes no order under 5 shares: a smaller holding cannot be offered; it waits for the result or is netted by a later entry
            if not dust:
                s = -1 if p > 0 else 1
                if sp >= c['exit_min_spread_c'] - 1e-6 and (not c['smart_exit'] or s * fam >= 0):
                    want[s] = dict(kind='exit', price=r['qb'] if s > 0 else r['qa'], size=abs(p), call=r['call_buy'] if s > 0 else r['call_sell'])
            if sp >= c['min_spread_c'] - 1e-6:
                for s, call in ((1, r['call_buy']), (-1, r['call_sell'])):
                    if s in want or (not dust and np.sign(p) == -s) or s * fam < 0 or not (call > 0):
                        continue
                    size = c['base_shares'] * (c['strong_mult'] if call >= self.model.c0 else 1)
                    if abs(p + s * size) > max(c['base_shares'] * c['cap_orders'], size) + 1e-9:
                        continue
                    want[s] = dict(kind='entry', price=r['qb'] if s > 0 else r['qa'], size=float(size), call=call)
        for s in (1, -1):
            key, w = (g, s), want.get(s)
            old = self.intents.get(key)
            if w is not None and self.real and now - self.last_fill.get(key, -1e18) < c['throttle_s'] * 1000:
                w = None
            if w is None:
                self.close_intent(key, now)
                continue
            if old and old['kind'] == w['kind'] and abs(old['price'] - w['price']) < 1e-9 and abs(old['size'] - w['size']) < 1e-9:
                old['seen'], old['as_of'], old['call'] = now, r['T'], w['call']
                continue
            self.close_intent(key, now)
            self.intents[key] = dict(g=g, side=s, ready=now, seen=now, as_of=r['T'], end=None, funded=False, rest=None, spread_c=r['spread_c'], fam30=r['fam30'], bid=r['bid'], ask=r['ask'], **w)
            self.lat.append(now - r['T'])
            self.counts['intents made'] += 1

    def fund(self, now):
        ttl = self.cfg['intent_ttl_s'] * 1000
        for key in [k for k, it in self.intents.items() if now - it['seen'] > ttl]:
            self.close_intent(key, now)
            self.counts['intents timed out'] += 1
        order = sorted(self.intents.values(), key=lambda it: (0 if it['kind'] == 'exit' else 1, -it['spread_c'], it['size'] * (it['price'] if it['side'] > 0 else 1 - it['price'])))
        free = self.cash
        for it in order:
            need = 0.0 if it['kind'] == 'exit' else it['size'] * (it['price'] if it['side'] > 0 else 1 - it['price'])
            ok = need <= free + 1e-9
            if ok:
                free -= need
                if not it['funded']:
                    it['funded'], it['rest'] = True, now
                    self.order_action('place', it, now)
            elif it['funded']:
                self.order_action('cancel', it, now)
                it['funded'], it['rest'] = False, None
                self.counts['intents unfunded'] += 1

    # ---- fills
    def apply(self, g, s, sh, px, t, how):
        dq, rem = self.lots.setdefault(g, []), sh
        while rem > 1e-9 and dq and dq[0][3] == -s:
            lot = dq[0]
            c_ = min(rem, lot[0])
            pnl = (px - lot[1]) * c_ if lot[3] > 0 else (lot[1] - px) * c_
            self.cash += c_ * px if s < 0 else c_ * (1 - px)
            self.close_trade(g, pnl, c_, t - lot[2], how, lot[1], px, lot[3])
            lot[0] -= c_
            rem -= c_
            if lot[0] <= 1e-9:
                dq.pop(0)
        if rem > 1e-9:
            self.cash -= rem * px if s > 0 else rem * (1 - px)
            dq.append([rem, px, t, s])

    def close_trade(self, g, pnl, sh, held_ms, how, p_in, p_out, side):
        self.closed_n += 1
        self.closed_won += pnl > 1e-9
        self.closed_pnl += pnl
        d = self.day_pnl[dt.datetime.now().strftime('%d %b')]
        d[0] += 1
        d[1] += pnl > 1e-9
        d[2] += pnl
        self.journal('close', g=g, title=self.meta.get(str(g), {}).get('title'), pnl=round(pnl, 4), shares=sh, held_s=round(held_ms / 1000), how=how, side=side, p_in=p_in, p_out=p_out)

    def on_trade(self, tr):
        tau, g, taker, p, size = tr
        self.ws_state['trades'] += 1
        t = self.tob.get(g)
        if t and t['bid'] is not None and t['ask'] is not None and taker > 0:
            self.ws_state['buy_n'] += 1
            self.ws_state['buy_above'] += p >= (t['bid'] + t['ask']) / 2 - 1e-9
        if self.real:
            return                                       # real fills come from the collector, never from a guess
        s = -taker
        key = (g, s)
        it, d = self.intents.get(key), self.cfg['order_delay_ms']
        if not (it and it['funded'] and it['rest'] is not None and it['rest'] + d <= tau):
            it = next((h for h in reversed(self.hist[key]) if h['funded'] and h['rest'] is not None and h['rest'] + d <= tau < h['end'] + d and not h.get('filled')), None)
        if it is None:
            return
        if not ((p <= it['price'] + 1e-9) if s > 0 else (p >= it['price'] - 1e-9)):
            return
        if tau - self.last_fill.get(key, -1e18) < self.cfg['throttle_s'] * 1000:
            self.counts['fills skipped (one a minute)'] += 1
            return
        sh = min(it['size'], size)
        touch = next(((b, a_) for ts, b, a_ in reversed(t['h']) if ts <= tau - 1000), (it['bid'], it['ask'])) if t else (it['bid'], it['ask'])
        inside = None if touch[0] is None or touch[1] is None else round(((p - touch[0]) if s > 0 else (touch[1] - p)) * 100, 2)
        self.apply(g, s, sh, it['price'], tau, 'exit fill')
        self.last_fill[key] = tau
        it['filled'] = True
        if self.intents.get(key) is it:
            self.close_intent(key, tau)
        self.counts[f'{it["kind"]} fills'] += 1
        self.journal('fill', g=g, title=self.games[g]['title'] if g in self.games else None, sport=self.games[g]['sport'] if g in self.games else None, what=it['kind'], side='buy' if s > 0 else 'sell',
                     price=it['price'], shares=sh, call=round(it['call'], 3), spread_c=round(it['spread_c'], 2), fam30=it['fam30'], decided_ms_before=tau - it['as_of'], resting_ms=tau - it['rest'],
                     trade={'price': p, 'size': size, 'ts': tau}, trade_inside_our_recorded_quote_c=inside, cash=round(self.cash, 2))
        self.log(f'FILL {it["kind"]} {"buy" if s > 0 else "sell"} {sh:g} @ {it["price"] * 100:.0f}c  {self.games[g]["title"] if g in self.games else g}  (call {it["call"]:+.2f}c, spread {it["spread_c"]:.0f}c)  cash ${self.cash:.2f}')

    # ---- results
    def settle(self, now):
        for g in [g for g, l in self.lots.items() if l]:
            t = self.tob.get(g)
            res = None
            if t:
                if (t['bid'] is not None and t['bid'] >= 0.97) or (t['ask'] is None and t['bid'] is not None and t['bid'] >= 0.9):
                    res = 1.0
                elif (t['ask'] is not None and t['ask'] <= 0.03) or (t['bid'] is None and t['ask'] is not None and t['ask'] <= 0.1):
                    res = 0.0
            if res is None:
                self.decided_since.pop(g, None)
                if (not t or now - t['any'] > 1800_000) and time.time() - self.t_gamma > 600 and str(g) in self.tokens:
                    self.t_gamma = time.time()
                    res = self.official(g)
                    if res is None:
                        continue
                else:
                    continue
            else:
                first = self.decided_since.setdefault(g, (now, res))
                if first[1] != res:
                    self.decided_since[g] = (now, res)
                    continue
                if now - first[0] < 300_000 and now - t['any'] < 900_000:
                    continue
            for sh, px, t0, s in self.lots[g]:
                self.cash += sh * res if s > 0 else sh * (1 - res)
                self.close_trade(g, (res - px) * sh if s > 0 else (px - res) * sh, sh, now - t0, 'result', px, res, s)
            self.lots[g] = []
            if self.real and str(g) in self.tokens:        # the collector's books drop these shares too
                self.settled_tokens += [self.tokens[str(g)]['home_token'], self.tokens[str(g)]['away_token']]
            self.log(f'RESULT {self.meta.get(str(g), {}).get("title", g)}: home {"won" if res else "lost"}; cash ${self.cash:.2f}')
            self.journal('result', g=g, home_won=res, cash=round(self.cash, 2))

    # ---- the collector's order keeper
    def sync_executor(self, now):
        """Hand the collector the whole set of orders we want resting (src/maker.rs). On paper with `mirror` this is a
        rehearsal: it keeps a pretend table and sends nothing. In real mode it keeps exactly this set on Polymarket
        and the fills it read from the venue come back here."""
        if not (self.real or self.cfg['mirror']) or (not self.real and time.time() < self.x_wait):
            return
        orders = [{'key': f'{g}:{s}', **{k: it['order'][k] for k in ('token', 'side', 'price', 'size', 'game', 'kind', 'title')}}
                  for (g, s), it in self.intents.items() if it['funded'] and it.get('order')]
        body = {'mode': 'real' if self.real else 'paper', 'orders': orders, 'since': self.x_seq, 'settled': self.settled_tokens[:50]}
        t0 = time.time()
        try:
            r = http_post(self.xurl + '/maker/sync', body, 4)
        except Exception as e:
            self.counts['order keeper: calls failed'] += 1
            self.x = {'err': repr(e)[:160], 't': time.time()}
            if not self.real:
                self.x_wait = time.time() + 30                # a rehearsal must never slow the bot: try again in 30 s
            return
        self.x_ms.append((time.time() - t0) * 1000)
        self.x = {'r': r, 't': time.time(), 'err': '' if r.get('ok') else str(r.get('error'))[:200]}
        if not r.get('ok'):
            self.counts['order keeper: syncs refused'] += 1
            return
        self.settled_tokens = self.settled_tokens[50:]
        for x in r.get('refused', []):
            why = x.get('why', '')
            self.counts['order keeper refused: ' + ('the price moved through ours first' if 'crosses' in why else re.sub(r'[0-9][0-9.,]*', 'N', why)[:70])] += 1
        if self.real:
            for f in r.get('fills', []):
                if int(f['seq']) > self.x_seq:
                    self.x_seq = int(f['seq'])
                    try:
                        self.on_real_fill(f)
                    except Exception as e:
                        self.counts['real fills not booked'] += 1
                        self.log(f'A REAL FILL COULD NOT BE BOOKED ({e!r}): {f}')
                    self.save()

    def on_real_fill(self, f):
        g = int(f['key'].split('#')[0].split(':')[0])
        tk = self.tokens.get(str(g))
        if not tk or f['token'] not in (tk['home_token'], tk['away_token']):
            raise ValueError('a fill for a game or token this bot does not know')
        home = f['token'] == tk['home_token']
        s = (1 if f['buy'] else -1) * (1 if home else -1)           # in home terms: buying the away token = selling home
        px, sh, t = (f['price'] if home else 1 - f['price']), float(f['shares']), int(f['ts_ms'])
        self.apply(g, s, sh, px, t, 'exit fill')
        self.last_fill[(g, s)] = t
        self.counts[f'{f.get("kind") or "?"} fills'] += 1
        title = self.meta.get(str(g), {}).get('title', g)
        self.journal('fill', real=True, g=g, title=title, what=f.get('kind'), side='buy' if s > 0 else 'sell', price=px, shares=sh, order_id=f.get('order_id'),
                     venue={'token': f['token'], 'side': 'BUY' if f['buy'] else 'SELL', 'price': f['price']}, cash=round(self.cash, 2))
        self.log(f'REAL FILL {f.get("kind")} {"buy" if s > 0 else "sell"} {sh:g} @ {px * 100:.0f}c  {title}  cash ${self.cash:.2f}')

    def keeper_lines(self):
        if not (self.real or self.cfg['mirror']):
            return []
        r, age = self.x.get('r') or {}, time.time() - self.x.get('t', 0)
        if not self.x:
            return ['order keeper (collector): no answer yet']
        held = {}
        for g, ls in self.lots.items():
            for l in ls:
                tk = self.tokens.get(str(g))
                if tk:
                    k = tk['home_token' if l[3] > 0 else 'away_token']
                    held[k] = held.get(k, 0) + l[0]
        theirs = {k: v.get('shares', 0) for k, v in (r.get('positions') or {}).items()}
        diff = sum(1 for k in set(held) | set(theirs) if abs(held.get(k, 0) - theirs.get(k, 0)) > 0.02)
        L = [f'order keeper (collector): {"REAL: these orders are on Polymarket" if r.get("real") else "rehearsal, nothing is sent"}; {len(r.get("orders", []))} orders in its table, '
             f'${r.get("open_usd", 0):.2f} at risk by its count; a hand-off takes {np.median(self.x_ms) if self.x_ms else 0:.0f} ms (slowest {max(self.x_ms) if self.x_ms else 0:.0f}); last answer {age:.1f} s ago'
             + (f'; ERROR: {self.x["err"]}' if self.x.get('err') else '')]
        if self.real:
            L.append('books: ' + ('this bot and the collector hold the same shares' if not diff else f'THIS BOT AND THE COLLECTOR DISAGREE ON {diff} TOKENS (netted pairs waiting for a result are counted by the collector, not here)'))
        return L

    def official(self, g):
        try:
            e = http_json(f'{GAMMA}/events/{self.tokens[str(g)]["event"]}')
            for mk in e.get('markets', []):
                toks, pr = json.loads(mk.get('clobTokenIds') or '[]'), json.loads(mk.get('outcomePrices') or '[]')
                if self.tokens[str(g)]['home_token'] in toks and mk.get('closed') and len(pr) == 2:
                    v = float(pr[toks.index(self.tokens[str(g)]['home_token'])])
                    return 1.0 if v > 0.99 else 0.0 if v < 0.01 else None
        except Exception:
            pass
        return None

    # ---- status
    def status(self, now):
        inv = sum(l[0] * (l[1] if l[3] > 0 else 1 - l[1]) for ls in self.lots.values() for l in ls)
        elig = sum(1 for g in self.games if self.eligible(g, now))
        med = lambda v: (f'{np.median(v):.2f}' if len(v) else '-')
        L = [f'STEERED RESTING ORDERS, {"REAL MONEY" if self.real else "PAPER"}  ({dt.datetime.now().strftime("%d %b %Y %H:%M:%S")})   '
             + ('orders are kept on Polymarket by the collector' if self.real else 'no order leaves this machine'),
             f'wallet: cash ${self.cash:.2f} + shares held at cost ${inv:.2f} = ${self.cash + inv:.2f}   (started with ${self.cfg["wallet"]:.2f} on {dt.datetime.fromtimestamp(self.started / 1000).strftime("%d %b %H:%M")})',
             f'closed trades: {self.closed_n}, won {self.closed_won} ({self.closed_won / max(self.closed_n, 1) * 100:.0f}%), result ${self.closed_pnl:+.2f}',
             'by day: ' + ('; '.join(f'{d}: {v[0]} trades, won {v[1]}, ${v[2]:+.2f}' for d, v in self.day_pnl.items()) or 'none yet (since this start)'),
             f'now: {len(self.games)} games being played and watched, {elig} qualify for a decision, {len(self.intents)} orders would be resting ({sum(1 for i in self.intents.values() if i["funded"])} paid for)',
             f'speed: a decision takes {med(self.took)} s to compute; an order is in place {med([x / 1000 for x in self.lat])} s after the data it was decided on (median); newest Polymarket row in the database is {(now - max([t["any"] for t in self.tob.values()] or [now])) / 1000:.1f} s old',
             f'trade stream: {"connected" if time.time() - self.ws_state["last"] < 30 else "NOT connected"}, {self.ws_state["trades"]} trades seen since start'
             + (f'; taker buys printed at or above the middle of the book {self.ws_state["buy_above"] / self.ws_state["buy_n"] * 100:.0f}% of the time (a check that buy / sell is read the right way round: should be well above 50%)' if self.ws_state['buy_n'] >= 20 else '')
             + (f'; last error: {self.ws_state["err"]}' if self.ws_state['err'] else ''),
             f'loop: one pass takes {med(self.cyc)} s (median), slowest of the last 400 {max(self.cyc) if self.cyc else 0:.2f} s' + (f'; last input error: {self.last_err}' if self.last_err else ''),
             'counts since start: ' + ('; '.join(f'{k} {v}' for k, v in sorted(self.counts.items())) or 'nothing yet'),
             *self.keeper_lines(),
             '', 'shares held:']
        for g, ls in self.lots.items():
            if ls:
                p = sum(l[0] * l[3] for l in ls)
                t = self.tob.get(g, {})
                L.append(f'   {self.meta.get(str(g), {}).get("title", g)}: {"home" if p > 0 else "away"} token x{abs(p):g}, paid {np.mean([l[1] if l[3] > 0 else 1 - l[1] for l in ls]) * 100:.0f}c; book now {t.get("bid")} / {t.get("ask")}')
        L += ['', 'orders that would be resting now:']
        for (g, s), it in sorted(self.intents.items(), key=lambda kv: -kv[1]['spread_c'])[:25]:
            L.append(f'   {"BUY " if s > 0 else "SELL"} {it["size"]:g} @ {it["price"] * 100:.0f}c  {it["kind"]:<5} {"" if it["funded"] else "(no money) "}{self.games[g]["title"] if g in self.games else g}  book {it["bid"] * 100:.0f}/{it["ask"] * 100:.0f}, call {it["call"]:+.2f}c')
        with open(os.path.join(self.a.data, self.tag + 'status.txt'), 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(L) + '\n')

    def eligible(self, g, now):
        t, G, c = self.tob.get(g), self.games[g], self.cfg
        if not t or t['bid'] is None or t['ask'] is None or now - t['any'] > c['max_age_s'] * 1000 or now < G['start']:
            return False
        sp, mid = (t['ask'] - t['bid']) * 100, (t['bid'] + t['ask']) / 2
        if not (c['price_lo'] <= mid <= c['price_hi']):
            return False
        return sp >= c['min_spread_c'] - 1e-6 or (abs(self.pos(g)) >= c['min_order_shares'] - 1e-9 and sp >= c['exit_min_spread_c'] - 1e-6)

    # ---- the loop
    def run(self):
        threading.Thread(target=lambda: asyncio.run(ws_main(self)), daemon=True).start()
        while not self.stop:
            t0 = time.time()
            if t0 - self.t_clock > 300:
                self.t_clock = t0
                self.sync_clock()
            if t0 - self.t_disc > 30:
                self.t_disc = t0
                self.discover()
            self.poll_quotes()
            now = self.now()                             # after the reads: a decision is asked for as of the newest data, never as of the start of a slow pass
            for g, (fut, T) in list(self.pending.items()):
                if fut.done():
                    del self.pending[g]
                    try:
                        self.on_decision(fut.result())
                    except Exception as e:
                        self.counts['decision failures'] += 1
                        self.log(f'decision failed for game {g}: {repr(e)[:200]}')
            for g in self.games:
                if self.eligible(g, now):
                    if g not in self.pending:
                        G = self.games[g]
                        k = g % len(self.ex)
                        try:
                            self.pending[g] = (self.ex[k].submit(decide, g, G['sport'], G['start'], now), now)
                        except Exception as e:                      # a worker died: start a new one, it reloads its games
                            self.log(f'worker {k} is gone ({repr(e)[:80]}): starting a new one')
                            self.counts['workers restarted'] += 1
                            self.ex[k] = cf.ProcessPoolExecutor(max_workers=1, initializer=_init, initargs=(self.a.db, MD.default_path(self.a.data)))
                else:
                    self.drop_intents(g, now)
            self.fund(now)
            self.sync_executor(now)
            while True:
                try:
                    tr = self.trades.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.on_trade(tr)
                except Exception as e:
                    self.log(f'trade handling failed: {repr(e)[:200]}')
            self.settle(now)
            if t0 - self.t_status > 5:
                self.t_status = t0
                self.status(now)
                self.save()
            if t0 - self.t_beat > 60:
                self.t_beat = t0
                inv = sum(l[0] * (l[1] if l[3] > 0 else 1 - l[1]) for ls in self.lots.values() for l in ls)
                self.journal('minute', cash=round(self.cash, 2), held=round(inv, 2), games=len(self.games), resting=len(self.intents), closed=self.closed_n, pnl=round(self.closed_pnl, 2), ws_trades=self.ws_state['trades'])
            self.cyc.append(time.time() - t0)
            time.sleep(max(0.05, 0.5 - (time.time() - t0)))


async def ws_main(bot):
    import websockets
    while not bot.stop:
        toks = sorted(bot.asset)
        if not toks:
            await asyncio.sleep(3)
            continue
        try:
            async with websockets.connect(WS_URL, ping_interval=None, max_size=2 ** 24) as ws:
                await ws.send(json.dumps({'assets_ids': toks, 'type': 'market', 'custom_feature_enabled': True}))
                sub, t_conn, t_ping = set(toks), time.time(), time.time()
                bot.ws_state['err'] = ''
                while not bot.stop:
                    if time.time() - t_conn > 20 and set(bot.asset) != sub:
                        break                                     # the games changed: subscribe again
                    if time.time() - t_ping > 10:
                        await ws.send('PING')
                        t_ping = time.time()
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=3)
                    except asyncio.TimeoutError:
                        continue
                    bot.ws_state['last'] = time.time()
                    if not msg or msg == 'PONG':
                        continue
                    bot.ws_state['msgs'] += 1
                    try:
                        data = json.loads(msg)
                    except Exception:
                        continue
                    for it in (data if isinstance(data, list) else [data]):
                        if not isinstance(it, dict) or it.get('event_type') != 'last_trade_price':
                            continue
                        a_ = bot.asset.get(str(it.get('asset_id')))
                        if not a_:
                            continue
                        try:
                            p, size, side = float(it['price']), float(it.get('size') or 0), str(it.get('side') or '').upper()
                            ts = int(float(it.get('timestamp') or 0))
                        except Exception:
                            continue
                        if size <= 0 or side not in ('BUY', 'SELL'):
                            continue
                        ts = ts * 1000 if ts < 10 ** 11 else ts
                        recv = bot.now()
                        tau = ts if abs(recv - ts) < 10_000 else recv
                        g, is_home = a_
                        taker = (1 if side == 'BUY' else -1) * (1 if is_home else -1)
                        bot.trades.put((tau, g, taker, p if is_home else 1 - p, size))
        except Exception as e:
            bot.ws_state['err'] = repr(e)[:160]
            await asyncio.sleep(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--data', required=True)
    ap.add_argument('--api', default='http://127.0.0.1:8787')
    a = ap.parse_args()
    a.db, a.data = os.path.abspath(a.db), os.path.abspath(a.data)
    Bot(a).run()


if __name__ == '__main__':
    main()
