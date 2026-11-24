"""Is our Kalshi recording right, and on time? Held against Kalshi's own public trades (24 Nov 2026).

  oddsfeed kalshi-probe 12 <folder> <folder holding kalshi.json>      (the Kalshi feed alone, no collector)
  python tools/kalshi_feed_check.py <folder> [--markets 60]

Every public trade names the price a taker paid and the side he took, so it says what the best resting price WAS at
that instant: a taker who bought Yes paid the best Yes ask, a taker who bought No sold into the best Yes bid.
For each trade (prints of one sweep are taken together) the check asks two things of our recording:
  RIGHT    going by KALSHI'S clock: was the best price in our order book, just before the trade, the price traded at?
           (This is the book itself: are the levels rebuilt correctly, with nothing lost.)
  ON TIME  going by OUR clock: at the moment the trade happened, was that price what we were showing?
           (This adds our delay: a late feed still shows the price from before the last move.)
On 23 Nov the old feed scored 1% on the second question at busy moments (tools/signals/kalshi_prints.py).
Also printed: how far behind Kalshi's clock messages arrived, lost messages, reconnects, the longest silence.
Public data only (no key); paced.
"""
import argparse
import bisect
import collections
import datetime as dt
import json
import os
import time
import urllib.request

API = 'https://api.elections.kalshi.com/trade-api/v2'


def get(path):
    for attempt in range(4):
        try:
            req = urllib.request.Request(API + path, headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
            time.sleep(0.12)
            return d
        except Exception as e:
            err = e
            time.sleep(1.5 * (attempt + 1))
    return {'error': str(err)}


def ms(iso):
    return int(dt.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() * 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('folder')
    ap.add_argument('--markets', type=int, default=60)
    a = ap.parse_args()
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    tk_of = {(e, o): t for t, (e, o) in json.load(open(os.path.join(a.folder, 'tickers.json'))).items()}
    rows = collections.defaultdict(list)                # ticker -> [our time, book bid, book ask, Kalshi's time of the change]
    first = last = None
    n_rows = 0
    all_t = []
    for line in open(os.path.join(a.folder, 'kquotes.jsonl')):
        try:
            t, st, ev, o, bid, ask, bb, ba, bt = json.loads(line)
        except Exception:
            continue                                     # a line cut off when the probe stopped
        n_rows += 1
        first = t if first is None else first
        last = t
        all_t.append(t)
        tk = tk_of.get((ev, o))
        if tk:
            rows[tk].append((t, bb, ba, bt))
    stats = [json.loads(x) for x in open(os.path.join(a.folder, 'stats.jsonl')) if x.strip()]
    s = stats[-1]
    say(f'recording: {n_rows:,} rows over {(last - first) / 60000:.1f} min, {len(rows)} contracts with rows')
    say(f'socket: {s["messages"]:,} messages ({s["book_changes"]:,} book changes, {s["book_snapshots"]:,} snapshots, {s["ticker_messages"]:,} ticker), '
        f'{s["books_in_step"]} of {s["contracts_subscribed"]} books in step at the end')
    say(f'        lost messages {s["lost_messages"]} | reconnects {max(0, s["connects"] - 1)} | errors from Kalshi {s["errors_from_kalshi"]} | '
        f'changes dropped while a book was out of step {s["book_changes_dropped_not_in_step"]} | snapshots asked after a disagreement {s["snapshots_after_disagreement"]} | largest batch {s["largest_batch"]}')
    f = lambda d: 'n/a' if not d else f'median {d["p50"]} ms, 9 in 10 under {d["p90"]} ms, 99 in 100 under {d["p99"]} ms, worst {d["max"]} ms (last {d["n"]:,})'
    say(f'behind Kalshi\'s SEND time:        {f(s["behind_kalshi_send_ms"])}')
    say(f'behind the exchange\'s CHANGE time: {f(s["behind_exchange_change_ms"])}')
    worst = max((x['behind_exchange_change_ms'] or {}).get('p99', 0) for x in stats)
    say(f'the worst 10-second reading of "99 in 100" during the run: {worst} ms')
    all_t.sort()
    gaps = sorted(((b - x, x) for x, b in zip(all_t, all_t[1:])), reverse=True)[:3]
    say('longest silences of the whole recording: ' + ', '.join(f'{g / 1000:.1f} s at {dt.datetime.fromtimestamp(x / 1000).strftime("%H:%M:%S")}' for g, x in gaps))
    up = [x for x in stats if x['contracts_subscribed'] > 0]
    unsynced = sum(1 for x in up if x['books_in_step'] < 0.9 * x['contracts_subscribed'])
    say(f'10-second readings with under 90% of the books in step: {unsynced} of {len(up)} (after the subscription began)')

    # the busiest contracts by rows, their public trades in the window (after the first minute: the snapshots)
    t_lo, t_hi = first + 60_000, last - 2_000
    busy = sorted(rows, key=lambda k: -len(rows[k]))[:a.markets]
    res = collections.Counter()
    detail = []
    n_tr = 0
    for tk in busy:
        tr, cursor = [], ''
        for _ in range(20):
            d = get(f'/markets/trades?ticker={tk}&min_ts={t_lo // 1000}&max_ts={t_hi // 1000 + 1}&limit=1000' + (f'&cursor={cursor}' if cursor else ''))
            tr += d.get('trades') or []
            cursor = d.get('cursor') or ''
            if not cursor or not d.get('trades'):
                break
        r = rows[tk]
        by_k = sorted((x for x in r if x[3] is not None), key=lambda x: x[3])       # by Kalshi's time of the change
        kt = [x[3] for x in by_k]
        ot = [x[0] for x in r]
        groups = collections.defaultdict(list)                                    # one sweep = same side within the same 5 ms
        for x in tr:
            T = ms(x['created_time'])
            if t_lo <= T <= t_hi:
                groups[(T // 5, x.get('taker_side'))].append((T, float(x['yes_price_dollars']), float(x.get('count_fp') or 0)))
        for (_, side), g in groups.items():
            T = min(x[0] for x in g)
            if sum(x[2] for x in g) < 1.0:
                continue                                                          # under one contract: our book skips such crumbs
            n_tr += 1
            # The best resting price the sweep started at. Our book calls a level a price only with a whole contract on
            # it (crumbs of 0.01-0.5 contracts sit in front of the real prices), so the same floor is used here: the
            # best price at which the sweep's prints add up to one contract or more.
            at_px = collections.Counter()
            for x in g:
                at_px[x[1]] += x[2]
            real = [p for p, n in at_px.items() if n >= 1.0 - 1e-9]
            if not real:
                n_tr -= 1
                continue
            px = min(real) if side == 'yes' else max(real)
            col = 2 if side == 'yes' else 1                                        # yes taker lifts the Yes ask; no taker hits the Yes bid
            i = bisect.bisect_left(kt, T) - 1                                      # last change stamped BEFORE the trade, Kalshi's clock
            j = bisect.bisect_right(ot, T) - 1                                     # last row we had RECEIVED by the trade's moment
            # Kalshi stamps the book change a trade causes a few ms BEFORE the trade's own time, so "the last change
            # before the trade" is often the book AFTER it. RIGHT therefore asks: was the traded price our best at any
            # point in the 300 ms up to the trade (Kalshi's clock)? ON TIME has no such slack: what we showed AT the moment.
            i0 = bisect.bisect_left(kt, T - 300) - 1
            seen = [x[col] for x in by_k[max(i0, 0):i + 1]]
            for w in (100, 250, 1000):                                            # and how much a little patience would add
                j0 = bisect.bisect_left(ot, T - w) - 1
                res[f'late{w}_ok'] += any(x[col] is not None and abs(x[col] - px) < 1e-4 for x in r[max(j0, 0):j + 1]) if j >= 0 else 0
            for name, row in (('right', by_k[i] if i >= 0 else None), ('ontime', r[j] if j >= 0 else None)):
                if row is None:
                    res[name + '_none'] += 1
                elif row[col] is None:
                    res[name + '_unknown'] += 1
                elif abs(row[col] - px) < 1e-4 or (name == 'right' and any(x is not None and abs(x - px) < 1e-4 for x in seen)):
                    res[name + '_ok'] += 1
                else:
                    res[name + '_off'] += 1
                    if len(detail) < 12:
                        detail.append(f'   {name:<6} {tk[:44]:<44} {dt.datetime.fromtimestamp(T / 1000).strftime("%H:%M:%S.%f")[:-3]} taker {side:<3} traded {px:.3f}, we had {row[col]:.3f} '
                                      f'(row received {T - row[0]:+d} ms before, change stamped {T - (row[3] or 0):+d} ms before)')
    say(f'\npublic trades in the {len(busy)} busiest contracts, {dt.datetime.fromtimestamp(t_lo / 1000).strftime("%H:%M")}-{dt.datetime.fromtimestamp(t_hi / 1000).strftime("%H:%M")}: {n_tr} (sweeps counted once)')
    for name, title in (('right', 'RIGHT   (Kalshi\'s clock: our book just before the trade had the traded price as its best)'),
                        ('ontime', 'ON TIME (our clock: at the trade\'s moment we were showing that price)')):
        ok, off, unk, none = res[name + '_ok'], res[name + '_off'], res[name + '_unknown'], res[name + '_none']
        known = ok + off
        say(f'  {title}: {ok} of {known} = {100 * ok / max(known, 1):.1f}%   (book not in step {unk}, no row yet {none})')
    known = res['ontime_ok'] + res['ontime_off']
    say('  shown at some moment in the last 100 ms / 250 ms / 1 s before the trade (our clock): ' + ' / '.join(f'{100 * res[f"late{w}_ok"] / max(known, 1):.1f}%' for w in (100, 250, 1000)))
    if detail:
        say('  the first misses:')
        for x in detail:
            say(x)
    open(os.path.join(a.folder, 'check.txt'), 'w').write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
