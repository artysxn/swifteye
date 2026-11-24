"""Checks of the bot's real-money bookkeeping, with no collector and no exchange:  python -I tools/maker/test_real.py

1. A fill the collector reports is booked on the right side at the right price (buying the away token = selling
   home), and the cash moves as the real dollars do.
2. The order a real bot would send for an intent: entries buy the token of their side, exits sell the token held.
3. In real mode a public trade never creates a fill (fills come from the collector only).
4. The program refuses to start in real mode while the collector's Real switch is off (run by hand, see the end)."""
import collections
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paperbot as PB  # noqa: E402


def bot(real=True):
    b = PB.Bot.__new__(PB.Bot)
    b.a = type('A', (), {'data': tempfile.mkdtemp(prefix='maker-test-')})()
    b.cfg = dict(PB.DEFAULTS)
    b.real, b.tag, b.offset = real, 'real_' if real else '', 0.0
    b.tokens = {'7': {'home_token': '111', 'away_token': '222'}}
    b.games = {7: {'title': 'A v B', 'sport': 'cs2', 'home_token': '111', 'away_token': '222'}}
    b.meta = {'7': {'title': 'A v B'}}
    b.cash, b.lots, b.last_fill = 100.0, {}, {}
    b.closed_n, b.closed_won, b.closed_pnl = 0, 0, 0.0
    b.day_pnl = collections.defaultdict(lambda: [0, 0, 0.0])
    b.counts = collections.Counter()
    b.intents, b.hist = {}, collections.defaultdict(lambda: collections.deque(maxlen=40))
    b.tob = {7: {'bid': 0.44, 'ask': 0.50, 'ts': 0, 'any': 0, 'h': collections.deque()}}
    b.ws_state = {'trades': 0, 'buy_n': 0, 'buy_above': 0}
    b.x_seq, b.settled_tokens, b.started = 0, [], 0
    return b


def near(a, b):
    assert abs(a - b) < 1e-9, (a, b)


def fills():
    b = bot()
    f = lambda seq, token, buy, price, shares, key: {'seq': seq, 'ts_ms': 1000 * seq, 'key': key, 'token': token, 'buy': buy, 'price': price, 'shares': shares, 'order_id': f'0x{seq}', 'kind': 'entry'}
    # buy 20 home at 45c: $9 leaves, 20 home shares held
    b.on_real_fill(f(1, '111', True, 0.45, 20.0, '7:1'))
    near(b.cash, 91.0)
    assert b.lots[7] == [[20.0, 0.45, 1000, 1]], b.lots
    # sell them at 50c: $10 back, $1 made, nothing held
    b.on_real_fill(f(2, '111', False, 0.50, 20.0, '7:-1'))
    near(b.cash, 101.0)
    near(b.closed_pnl, 1.0)
    assert b.lots[7] == [] and b.closed_n == 1 and b.closed_won == 1
    # buy 10 of the AWAY token at 60c = selling home at 40c: $6 leaves
    b.on_real_fill(f(3, '222', True, 0.60, 10.0, '7:-1'))
    near(b.cash, 95.0)
    assert b.lots[7][0][3] == -1
    near(b.lots[7][0][1], 0.40)
    near(b.pos(7), -10.0)
    # sell the away token at 65c (in home terms: bought back at 35c): $6.50 back, 50c made
    b.on_real_fill(f(4, '222', False, 0.65, 10.0, '7:1'))
    near(b.cash, 101.5)
    near(b.closed_pnl, 1.5)
    assert b.lots[7] == []
    # a fill for a token of another game is never booked
    try:
        b.on_real_fill(f(5, '999', True, 0.5, 5.0, '7:1'))
        raise SystemExit('a fill for an unknown token was booked')
    except ValueError:
        pass
    near(b.cash, 101.5)
    print('1. fills from the collector are booked on the right side and the cash matches the real dollars: OK')


def orders():
    b = bot()
    it = lambda side, kind, price, size: dict(g=7, side=side, kind=kind, price=price, size=size, funded=True)
    # entry buy home at 45c -> BUY the home token at 45c
    i1 = it(1, 'entry', 0.45, 20.0)
    b.order_action('place', i1, 1)
    assert (i1['order']['token'], i1['order']['side'], i1['order']['price']) == ('111', 'BUY', 0.45), i1['order']
    # entry sell home at 49c -> BUY the away token at 51c
    i2 = it(-1, 'entry', 0.49, 10.0)
    b.order_action('place', i2, 1)
    assert (i2['order']['token'], i2['order']['side'], i2['order']['price']) == ('222', 'BUY', 0.51), i2['order']
    # holding home: the exit sells the home token at its price
    b.lots = {7: [[20.0, 0.45, 0, 1]]}
    i3 = it(-1, 'exit', 0.52, 20.0)
    b.order_action('place', i3, 1)
    assert (i3['order']['token'], i3['order']['side'], i3['order']['price']) == ('111', 'SELL', 0.52), i3['order']
    # holding away (short home): the exit sells the away token at 1 - price
    b.lots = {7: [[10.0, 0.40, 0, -1]]}
    i4 = it(1, 'exit', 0.36, 10.0)
    b.order_action('place', i4, 1)
    assert (i4['order']['token'], i4['order']['side'], i4['order']['price']) == ('222', 'SELL', 0.64), i4['order']
    assert all(i['order']['post_only'] for i in (i1, i2, i3, i4))
    print('2. the orders a real bot sends: entries buy the token of their side, exits sell the token held: OK')


def no_guessed_fills():
    b = bot()
    b.intents[(7, 1)] = dict(g=7, side=1, kind='entry', price=0.45, size=20.0, funded=True, rest=0, end=None, call=1.0, spread_c=6.0, fam30=0, bid=0.44, ask=0.50, as_of=0)
    b.on_trade((5000, 7, -1, 0.44, 50.0))            # a taker sells home at 44c: on paper this would fill our 45c bid
    assert b.lots == {} and b.cash == 100.0, (b.lots, b.cash)
    p = bot(real=False)
    p.intents[(7, 1)] = dict(b.intents[(7, 1)])
    p.on_trade((5000, 7, -1, 0.44, 50.0))
    assert p.lots[7] and p.cash < 100.0
    print('3. in real mode a public trade never becomes a fill (on paper the same trade does): OK')


if __name__ == '__main__':
    fills()
    orders()
    no_guessed_fills()
    print('all OK.  By hand: with "mode": "real" in a config and Real off in the collector, the program must stop with "Real is OFF in the collector".')
