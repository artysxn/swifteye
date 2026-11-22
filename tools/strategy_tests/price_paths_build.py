"""Data for the pure-price tests (no bookmakers): Polymarket's bid and ask in 5-second steps, from 10 min before
the start to 4 h after, for settled two-way games. One outcome per game (the other is its mirror: checked on the
first games read). Read-only and throttled; stops by itself if the live bot's Polymarket feed runs late.

usage: price_paths_build.py [esports|tennis|basketball|all] [every Nth game, default 1] [slice 0..N-1] [output tag]
output: price_paths_<set>.json  (appends to an existing file: games already in it are skipped)
"""
from sx import *
import sys, urllib.request
SET = sys.argv[1] if len(sys.argv) > 1 else 'esports'
NTH = int(sys.argv[2]) if len(sys.argv) > 2 else 1
REM = int(sys.argv[3]) if len(sys.argv) > 3 else 0      # which of the NTH slices (0 = the first)
TAG = sys.argv[4] if len(sys.argv) > 4 else SET          # output name, to keep a held-out slice in its own file
SPORTS = dict(esports=('cs2', 'dota2', 'valorant', 'lol'), tennis=('tennis',), basketball=('basketball',), all=('cs2', 'dota2', 'valorant', 'lol', 'tennis', 'basketball'))[SET]
STEP, BEFORE, SPAN, STALE = 5_000, 10 * M, 4 * H, 120_000
OUT = f'price_paths_{TAG}.json'
done = json.load(open(OUT)) if os.path.exists(OUT) else []
have = {g['id'] for g in done}
G = [g for g in games() if g[1] in SPORTS and len(g[3]) == 2 and g[0] not in have and int(hashlib.md5(str(g[0]).encode()).hexdigest()[:6], 16) % NTH == REM]
print(f"{len(G)} games to read ({SET}, every {NTH}), {len(done)} already saved", flush=True)


def feed_late():
    try:
        d = json.load(urllib.request.urlopen('http://127.0.0.1:8787/bots', timeout=8))
        return d['snipe']['status'].get('pm_lag_ms') or 0
    except Exception:
        return 0


t_start = time.time(); nr = 0; mirror = []
for gi, (mid, sport, S, outs, win) in enumerate(G):
    o = outs[0]
    q = c.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (mid, o, S - BEFORE - STALE, S + SPAN)).fetchall()
    nr += len(q); rest(len(q))
    if len(q) < 100: continue
    if len(mirror) < 12:   # is the other outcome the mirror image? (bid of one + ask of the other = 1)
        t_mid = q[len(q) // 2][0]; a = pm(mid, outs[0], t_mid); b = pm(mid, outs[1], t_mid)
        if a and b and abs(a[2] - b[2]) < 3000: mirror.append(round(a[0] + b[1] - 1, 3))
    bid, ask, k = [], [], 0
    for t in range(S - BEFORE, S + SPAN + 1, STEP):
        while k < len(q) and q[k][0] <= t: k += 1
        if k and t - q[k - 1][0] <= STALE: bid.append(round(q[k - 1][1], 3)); ask.append(round(q[k - 1][2], 3))
        else: bid.append(None); ask.append(None)
    done.append(dict(id=mid, sport=sport, S=S, o=o, win=(win == o), t0=S - BEFORE, bid=bid, ask=ask, n=len(q)))
    if gi % 10 == 9:
        json.dump(done, open(OUT, 'w'))
        late = feed_late()
        print(f"  {gi + 1}/{len(G)} games, {nr:,} quotes, {time.time() - t_start:.0f} s, feed delay {late} ms", flush=True)
        if late > 800:
            time.sleep(20)
            if feed_late() > 800: print('feed is late: stopping here', flush=True); break
json.dump(done, open(OUT, 'w'))
print(f"saved {len(done)} games to {OUT}; {nr:,} quotes read in {time.time() - t_start:.0f} s; mirror check (bid A + ask B - 1): {mirror}", flush=True)
print(late_check(int(t_start * 1000)), flush=True)
