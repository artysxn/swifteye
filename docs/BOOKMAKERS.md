# Bookmaker connections (2026-11-17)

Probed from the Mac (Norway IP) with `tools/probe_books.mjs` (homepage, protection, platform
markers, 1xBet feed) and `tools/find_apis.mjs` (API hosts in each page's scripts), plus the
browser pane to read which requests a site's own live page makes. **Nothing was bypassed:** a
Cloudflare / WAF / DDoS-Guard challenge, a geo-block or an access-denied page is recorded as such,
and for those the notes say how the odds would be read if the page were reachable.

Legend: **wired** = adapter running in Swifteye; **open** = reachable, feed found or likely,
not wired yet; **blocked** = challenge / geo-block / refused from here.

## Wired in this round

| Book | How | Notes |
|---|---|---|
| PariPesa | 1xBet platform, `paripesa.me/service-api/LiveFeed/Get1x2_VZip` + `LineFeed` | Feed refuses requests without the brand's partner id: `partner=188` (`ref` in its live page's own requests). `paripesa.bet` is Cloudflare-challenged, `paripesa.me` is open. |
| 22Bet | 1xBet platform, `22bet21.com`, `partner=151` | `22bet.com` redirects to the numbered mirror. |
| 22Bit | 1xBet platform, `22bit1.com`, `partner=407` | crypto brand. |
| 1xBit | 1xBet platform, `1xbit1.com`, `partner=65` | `1xbit.com` serves a block page. |
| BetKing (Ukraine) | Altenar widget API, integration `betking`, country UA | same adapter as VINN; football. |
| Olimpbet | `olimp.bet/api/v4/0/live/sports-with-competitions-with-events?vids[]=` | all live sports; English names in `names["2"]`; П1/Х/П2 outcomes, price in the field `probability`; two-way winner in market 2 (tennis) / 4366 (basketball). Live only. |

Already wired before: Melbet, BetWinner, Megapari, Linebet (1xBet platform, classic feed, no partner
needed), M88, Fonbet, Pinnacle, Kambi brands, Thunderpick, SportyBet, betPawa, Betika, VINN.

The newer 1xBet front-ends also call `service-api/main-live-feed/v3/games1x2?cfView=3&count=40&fcountry=137&gr=<brand>&grMode=4&lng=en&ref=<partner>`
(`gr` = the page's `"rp"` value: PariPesa 756, 22Bet 151, 22Bit 2017, 1xBit 933, Linebet 650). It
returns richer JSON (all main lines, scores, timers) but only for one sport per call; its sport
filter parameter is not identified yet (`sport`, `sports`, `sportIds` all give 400), so the classic
feed is used.

## Open, feed found, not wired yet

| Book | What was found | What wiring needs |
|---|---|---|
| Betcity (betcity.ru) | `ad.betcity.ru/d/on_air/events?rev=8&ver=124&csn=ooca9s` = open JSON of every live event (ids, teams, score, timer) | odds come from a per-event call (not traced); names are Russian only (`name_ht`/`name_at`, no `lang` switch) → needs transliteration/alias matching. |
| Mostbet | redirects to rotating mirrors (`0djawb3.com` today); `GET /api/v3/user/line/list?t=1\|2` open JSON (line hierarchy with outcome aliases P1/X/P2), `/api/v1/allsports/sports`, `/api/v1/websocket/credentials` (live updates by socket) | pagination (20 lines per page), mirror discovery, then parse `line_dto_collection`. |
| Leon (leon.bet) | open SPA; all sportsbook data is `POST /api-1` (GraphQL-style, operations defined in its JS bundle); the old `/api-2/betline/*` answers `INVALID_CODE` | read the operation documents from the bundle and replay the events query. |
| Olimpbet KZ / UZ | same platform as olimp.bet (`/api/v4/0/...`) | `olimpbet.kz` is Cloudflare-challenged from here; `olimp.bet` (RU) is wired. |
| Baltbet | server-rendered pages + `/api/grouping/events`, `/api/events` (POST from `uniru.bundle.js`), SignalR hub `/api/signalr` for live | trace the POST bodies on its live page. |
| Zenit (zenit.win / zenitbet.com) | "almara" platform: socket at `/wss` (`/wss/xhr/` long-poll fallback), routes `/live/26`, `/line/football/...` | decode the socket protocol. |
| Winline | binary WebSocket `wss://wss.winline.ru/data_ng?client=newsite&nb=true` | decode the binary frames (as noted in AGENTS.md). |
| 1win (1win.com / 1win.pro) | open Vue SPA; sportsbook loads only after its app bundle runs (`/resources/v1/app/assets/bff-*.js`) | read the bff client to find the live-events endpoint. |
| Pari (pari.ru) | open, a JS-only shell (no data in the HTML) | trace its live page requests in a browser. |
| Marathonbet (.com) | open (Cloudflare in front but no challenge); live pages are server-rendered HTML with odds in data attributes | HTML parsing of `/en/live/...`; `.ru` is challenged. |
| GG.BET | `gg.bet` answers 200 today (it was 403 earlier); markers suggest its own platform | trace requests. |
| Duel, Dafabet, Fun88, Bet25, BetKing (Nigeria) | homepages open | not traced yet. |

## Blocked from here (not bypassed)

| Book | What answers | How it would work if reachable |
|---|---|---|
| Stake | Cloudflare challenge (also on `/_api/graphql`) | everything is GraphQL at `stake.com/_api/graphql` (sportsbook queries `sportFixtureList` etc.), live updates over its GraphQL websocket. |
| 1xBet (1xbet.com) | redirect to `/en/block` + challenge | same 1xBet feed as the brands above (`/service-api/LiveFeed/Get1x2_VZip`, partner id of 1xBet); a reachable mirror would plug straight into the existing adapter. |
| BetBoom | 401 / 403 with a bot check | own platform; unknown. |
| Liga Stavok | 401 challenge | own platform. |
| Sportbet.one, CMD368, BK8, 3ET, Rainbet, Dexsport | Cloudflare "Attention Required" / "Just a moment" | CMD368 / BK8 / 3ET are Asian books whose sportsbooks are usually third-party iframes (Saba / IM / BTi); Rainbet, Dexsport and Sportbet.one are crypto books (often Betby or own). |
| Tennisi | 403 (bet) / timeout (kz) | — |
| Cosmolot | "WAF Challenge" | Ukrainian, own platform. |
| VBET | "Country was blocked" | BetConstruct Swarm WebSocket (`wss://eu-swarm-*.betconstruct.com`, `request_session` with the site id, then `get` with a `game` filter) — same for any BetConstruct brand. |
| Nova88 | access denied (Saba "accessDenied" page) | Saba / OneBook sportsbook (IBC). |
| W88 | redirect to `forbidden.w88in.com` (geo) | own sportsbook frame (A-Sports / Saba). |
| Gamdom, BC.Game, coins.game, betandyou.com, betm.ru | connection refused from this network (likely DNS or ISP-level block) | BC.Game and Gamdom sportsbooks are Betby (`sptpub.com` widget API, needs the brand id); betandyou is a 1xBet brand (classic feed + its partner id). |
| 24bet, battery (batery.ru / battery.bet), tennisi.kz | timeouts / resets | — |
| 12bet, 188bet, 198bet, maxbet | domain does not resolve from here | Asian, typically mirror domains + Saba/IM/own feeds. |
| Singbet, Penta88 | TLS errors | Singbet is the Crown (hga) agent network; Penta88 an Asian agent site. |
| Spobet | `ww17.spobet.com` parked domain (no sportsbook) | — |
| bet-m.com | 114-byte page | probably a redirect stub to a mirror. |

Duplicates in the request (melbet, 1xbet, 1win, mostbet, paripesa, dafabet, betwinner, m88) are
listed once. Re-run `node tools/probe_books.mjs probe.json` to refresh the status column.
