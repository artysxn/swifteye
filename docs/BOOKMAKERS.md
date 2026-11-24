# Bookmaker connections (2026-11-17)

Probed from the Mac (Norway IP) with `tools/probe_books.mjs` (homepage, protection, platform
markers, 1xBet feed) and `tools/find_apis.mjs` (API hosts in each page's scripts), plus the
browser pane to read which requests a site's own live page makes. **Nothing was bypassed:** a
Cloudflare / WAF / DDoS-Guard challenge, a geo-block or an access-denied page is recorded as such,
and for those the notes say how the odds would be read if the page were reachable.

Legend: **wired** = adapter running in Swifteye; **open** = reachable, feed found or likely,
not wired yet; **blocked** = challenge / geo-block / refused from here.

## Wired 2026-11-23: browser_books.py

Collected by `browser_books.py` (repo root), which oddsfeed starts and which posts to `/ingest`
(`books/browser.rs`). Recorded only. Checked from this PC on 23 Nov: every feed below answered a
plain request with no challenge.

| Book | How | Notes |
|---|---|---|
| BC.Game | Betby `api-k-c7818b61-623.sptpub.com`, brand `2103509236163162112` | the site itself was "connection refused" on 17 Nov; the feed host is open. |
| Rollbit | Betby `api-a-c7818b61-600.sptpub.com`, brand `2103518166582956032` | same host as Duel. |
| Rainbet | Betby `api-a-c7818b61-600.sptpub.com`, brand `2374656571012681728` | the site is Cloudflare-challenged; the feed host is open. |
| CSGO500 | Betby `api-h-c7818b61-608.sptpub.com`, brand `2195256717934206976` | |
| CSGOEmpire | Betby `api-h-c7818b61-608.sptpub.com`, brand `2432911154364948480` | |
| Cloudbet | `www.cloudbet.com/sports-api/c/v6/sports/events?live=true&sports=soccer`, then `/events/{id}?markets=soccer.match_odds&...` per game | fetched inside a headless browser page; 1X2, totals, Asian handicap, BTTS. |
| Shuffle | `shuffle.com/sports?section=live`, rendered and read as text every 60 s | no odds JSON found; 1X2 only, no kick-off time; the page mixes sports (two-way games are dropped). |

Since 23 Nov the five Betby brands are read by the Rust Betby collector instead (every sport, the
whole board, live every 3 s, pre-match every 5 min; all five answered from the Mac too), and
`/ingest` ignores what the script posts for them. The script reads football only, and for the Betby brands only the board's top slice (about half
of the live football: 25 of 53 games on BC.Game, 23 Nov 07:20 UTC).

## Wired in this round

| Book | How | Notes |
|---|---|---|
| PariPesa | 1xBet platform, `paripesa.me/service-api/LiveFeed/Get1x2_VZip` + `LineFeed` | Feed refuses requests without the brand's partner id: `partner=188` (`ref` in its live page's own requests). `paripesa.bet` is Cloudflare-challenged, `paripesa.me` is open. |
| 22Bet | 1xBet platform, `22bet21.com`, `partner=151` | `22bet.com` redirects to the numbered mirror. |
| 22Bit | 1xBet platform, `22bit1.com`, `partner=407` | crypto brand. |
| 1xBit | 1xBet platform, `1xbit1.com`, `partner=65` | `1xbit.com` serves a block page. |
| BetKing (Ukraine) | Altenar widget API, integration `betking`, country UA | same adapter as VINN; football. |
| Olimpbet | `olimp.bet/api/v4/0/live/sports-with-competitions-with-events?vids[]=` | all live sports; English names in `names["2"]`; П1/Х/П2 outcomes, price in the field `probability`; two-way winner in market 2 (tennis) / 4366 (basketball). Live only. |
| HKJC (Hong Kong Jockey Club) | `info.cld.hkjc.com/graphql/base-get/` GraphQL GET (bet.hkjc.com's own query), header `Content-Type: application/json` | Added 2026-11-22, record-only until its timing vs Polymarket is tested. In-play 1X2 + handicap + total, pre-match 1X2. Asian tote-style fixed odds; few in-play games (49 listed, 2 live on a Sunday morning). |
| Duel | Betby platform (`books/betby.rs`): `api-a-c7818b61-600.sptpub.com/api/v4/{live,prematch}/brand/2482975601191952386/en/0` → `top_events_versions` + `rest_events_versions` → `.../en/{version}` slices | Sportradar market ids (1 = 1X2; 186 / 219 / 406 / 251 / 340 = two-way winner, outcomes 4/5). Any other Betby brand needs only its API host + brand id from its page's requests. |
| Mostbet | `GET /api/v3/user/line/list?t=2&l=100&of=N` (t=2 live, t=1 pre-match; ≤ 100 per page) on its current mirror (`0djawb3.com`) | outcomes alias 1/x/2, `odd`, `active`. Mirror is the `param` (mostbet.com sends browsers to it). |
| Leon | `leon.bet/api-2/betline/events/inplayupcomingall?ctag=en-US-7da6p&hideClosed=true&flags=reg,urlv2,orn2,cn,mm2,rrc,cmg` | the `ctag` build suffix is required (plain `en-US` → INVALID_CODE; `param` overrides it); without `nodup` each event carries its league/sport; e-sports game in `league.region.name`. |
| Marathonbet | `marathonbet.com/eag/event-line/api/v1/sports/menu` + `/sports/{treeId}/all-tournaments/live` with the site's app headers `X-PAN-SOURCE: REDESIGN_WEB`, `X-PAN-TARGET: BROWSER`, `X-PAN-VERSION: 2.8.2` | fractional prices `{n,d}`; `Match_Result` / `Match_Winner_OT`. Live only. |
| Betcity | `ad.betcity.ru/d/on_air/bets` (all live with markets) | `main["69"]` P1/X/P2 with Betcity's own change stamp `md`; Russian names (e-sports teams are Latin), so non-e-sports games link only when transliteration is close. |
| PARI | Fonbet platform: `line-vk02-w.pb06e2-resources.ru/events/list?lang=en&version=N&scopeMarket=2300` | same adapter as Fonbet (now all sports for both). |

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
| Olimpbet KZ / UZ | same platform as olimp.bet (`/api/v4/0/...`) | `olimpbet.kz` is Cloudflare-challenged from here; `olimp.bet` (RU) is wired. |
| Baltbet | server-rendered pages + `/api/grouping/events`, `/api/events` (POST from `uniru.bundle.js`), SignalR hub `/api/signalr` for live | trace the POST bodies on its live page. |
| Zenit (zenit.win / zenitbet.com) | "almara" platform: socket at `/wss` (`/wss/xhr/` long-poll fallback), routes `/live/26`, `/line/football/...` | decode the socket protocol. |
| Winline | binary WebSocket `wss://wss.winline.ru/data_ng?client=newsite&nb=true` | decode the binary frames (as noted in AGENTS.md). |
| 1win (1win.com / 1win.pro) | matches: `POST api-gateway.top-parser.com/matches/get-many?l=en-001&p=44ba10e5-7df2-47ab-a44d-dc93803c7a6e` body `{"service":"live","sportId":18,"limit":100}` (open, teams + start, no prices); odds are not in any request its page makes (likely a worker / socket) | find the odds channel. |
| GG.BET | everything over GraphQL on a WebSocket `wss://gg-b-gql.gg.bet/graphql` (subprotocol graphql-ws) with persisted queries (`GetSportEventListByFilters` sha256 `44839cf8…`, `OnUpdateSportEvent`); a fresh socket is refused `connection_error: unauthorized` without the session token the page holds; `gg.bet/graphql` over HTTP does not know the persisted query | would need the page's guest session token in `connection_init` (not taken: that is the site's credential). |
| BetKing (Nigeria) | `sportsapicdn-desktop.betking.com/api/feeds/prematch/...` (open JSON: `lastminute/en/1/15/`, `mostpopularsports/...`, `matches/custommenu/en/4`); live page redirects to the sports home | pre-match only so far; live feed not seen. |
| Dafabet, Fun88, Bet25 | homepages open; the browser pane refuses gambling domains like these (and Marathonbet), so their requests could not be traced there | trace from a normal browser. |

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
