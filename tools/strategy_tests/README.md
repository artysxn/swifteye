# Strategy tests on the recording

Each test is two scripts, run from this folder: `X_build.py` reads the database (read-only, throttled so the
live bot keeps its speed) and saves a JSON file here; `X_an.py` prints the result from that file.

| Test | Scripts | Result on 17-20 Nov 2026 |
|---|---|---|
| Near-certain sides in play | `near_certain_*` (t2.json) | no clear edge, about +1% a bet at best |
| Pre-match bookmaker line moves | `prematch_moves_*` (t1.json) | none: Polymarket has already moved |
| Fading Polymarket-only drops | `pm_only_drops_*` (t3.json) | none: the price does not come back |
| Kalshi as the place to bet | `kalshi_value_*` (t4.json) | none: Kalshi is as sharp as the bookmakers and Polymarket |
| Buying Kalshi's old price after Polymarket jumps | `kalshi_speed_build.py`, then `kalshi_speed_check.py` (k1.json; `kalshi_speed_an.py` is the naive version) | about +2% a $ if the order lands within 3 s, unproven |
| Positive-EV on Polymarket | `../ev_snapshots.py`, `../ev_analyse.py` | none |

`sx.py` holds the shared parts: bookmaker groups, margin removal (power method), quotes, the list of settled
games (cached in games.json; delete it to refresh) and the pause between reads.

Traps these tests ran into: removing the margin proportionally overstates underdogs by about 2 points; a
high ask with the bid pulled is not a price (filter on the spread); stale quotes (filter on their age);
`kalshi_quotes` keeps Kalshi's own home/away order, and its bid/ask come from Kalshi's late ticker feed while the size beside them comes from the real-time book (a price with no size beside it is already gone); never pick "the first row of a game" from a list whose
order depends on the result.
