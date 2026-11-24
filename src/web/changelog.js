/* Swifteye: the Changelog page. A hand-kept list, newest first: add an entry at the top of
   SE.CHANGELOG when something a user would notice ships (date in UTC, a Material icon name,
   the area it belongs to, a short title, and a few plain lines). */
(() => {
const { esc, ic } = SE;
const P = SE.P;

SE.CHANGELOG = [
  { date: '2026-11-24', icon: 'sync_problem', area: 'Collector', title: 'Kalshi feed: no more drop-outs',
    lines: [
      'The Kalshi connection used to drop itself whenever new games were added, then wait up to a minute before coming back: over 1,500 times in five days. It mistook one of Kalshi\'s own replies for a lost message. Fixed.',
      'If a message really is lost, the order books are refreshed on the same connection instead of reconnecting, and no old order book is shown as current while that happens.',
      'Kalshi is read on its own thread, so a busy app no longer makes its prices late. The Kalshi rows now keep Kalshi\'s own time of each order-book change.',
      'Kalshi games no longer vanish and come back: when Kalshi answered "too many requests" for a league, that league\'s games used to drop off the list for a few minutes.',
    ] },
  { date: '2026-11-24', icon: 'receipt_long', area: 'Bots', title: 'Order desk: place a bet at any book through one call',
    lines: [
      'A ticket holds one or more bets that belong together. The desk places them one at a time and tells you when a bet stands with nothing against it.',
      'Polymarket bets are placed by Swifteye. Every other bookmaker is placed by your own driver, a small program in a browser where you are signed in; a template is in tools/desk.',
      'Paper by default. A dry run reads the real bet slip (odds and stake limit) without confirming. Real money stays off until you switch it on, per bookmaker, with its own stake limit and daily cap.',
      'New Order desk page: type each bookmaker\'s email and password once. They stay on this machine, are never shown again, and only that bookmaker\'s driver can fetch them to sign in. If a site asks for a captcha or a code, the driver stops and you are told.',
      'Tickets are handed in through the API. docs/DESK.md explains it, and "oddsfeed desk-sandbox" runs it with pretend bookmakers for trying things out.',
    ] },
  { date: '2026-11-23', icon: 'stacked_line_chart', area: 'Bots', title: 'Trade review: who is ahead, second by second',
    lines: [
      'With Analysis on, a strip under the chart shows each second whether the superline is ahead of Polymarket (green: Polymarket should rise, red: should fall), Polymarket is ahead (blue), or the two simply disagree for minutes (grey).',
      'Two more rows show the up and down waves of the superline and of Polymarket: solid until the peak, with a tick where the wave was first detected. Hover anywhere for the numbers: both prices, the gap, the expected catch-up and the wave in progress.',
      'The Measured panel gives the lead at the bot\'s buy and how often the superline\'s leads came true on that game.',
      'Your own buys can now be Up or Down; drag a buy\'s cash-out below what it paid and it becomes a Down buy.',
    ] },
  { date: '2026-11-23', icon: 'insights', area: 'Bots', title: 'Trade review: imported trades, Analysis, more buys and line pairs',
    lines: [
      'Import trades loads a trades export from another machine (the .html page, its .zip or the JSON). Its games show under Bought with a Mac export tag, each trade ready to grade like any other.',
      'Analysis draws the superline (each bookmaker family\'s quickest move, kept only when other families confirm it) and the waves: shaded where bookmakers were ahead of Polymarket, a thin blue mark where Polymarket moved first.',
      'Under the chart, Measured shows the wave a trade bought (who moved first, when the trigger moved, how much of the move Polymarket had already made when the bot bought) and, for every line, how often it beat Polymarket, its median lead, its lead-lag and correlation, on this game and over all games.',
      'B now places a buy of your own on a bought game too, as many as you like; click a buy to edit it.',
      'Paint as many bookmaker and Polymarket line pairs as you like: K and P paint a pair, K again starts the next. Each pair is measured and saved by itself.',
    ] },
  { date: '2026-11-23', icon: 'rate_review', area: 'Bots', title: 'Trade review: mark what the bots should have done',
    lines: [
      'A new page under Markets. Open a game a bot traded (its real and paper trades, and the replay paper bot\'s trades over everything recorded), or one no bot traded that had Polymarket and a bookmaker: search it, filter by bookmaker, or take a random one.',
      'Paint one line along a bookmaker\'s move and one along Polymarket\'s, and they are saved as the same movement: how many seconds apart they moved, how far Polymarket went against the bookmaker, and how closely the two followed each other.',
      'A bought game opens on its trade, drawn like the Telegram trade chart: the token in cents, Polymarket\'s sell and buy price and only the bookmaker the bot acted on (add others, or the middle of all bookmakers, from the legend), and the bot\'s buy, sale, cash-out price, break-even and give-up time.',
      'Drag the buy, the sale, the cash-out and price-cap lines or the give-up line to make your version; the bot\'s stays faint behind it, with both side by side under the chart. Give a verdict (Right, Wrong, Fix) if you like. Everything saves by itself and comes back when you open the trade again.',
      'Set where a bot should have bought and sold and the most it should have paid, or mark a bot\'s own trade as right, wrong, or right at other points. Keys: K, P, B and S, Esc to stop.',
      'Marks are kept with the prices they were made on, for working out what the move detection misses.',
    ] },
  { date: '2026-11-23', icon: 'sports_esports', area: 'Bots', title: 'Real money trades CS2 and tennis only; Valorant and LoL tested on paper',
    lines: [
      'In basketball, football, Dota 2 and American football Polymarket moves before the bookmakers in two thirds to three quarters of games, and buying its apparent lag lost 5-16% a dollar in the replay. Those sports now trade on the trial\'s paper wallet only.',
      'CS2 (looser rules) and tennis (strict rules, 5-minute give-up) keep real money.',
      'Valorant and League of Legends run on CS2\'s rules on the paper wallet, to see whether the same lag shows there.',
    ] },
  { date: '2026-11-23', icon: 'tune', area: 'Bots', title: 'Rules by sport: CS2 looser, tennis gives up after 5 minutes',
    lines: [
      'CS2 no longer needs Polymarket to have lagged earlier in the game, has no price cap in weak games, and may buy when Polymarket has already made up to 10% of the move.',
      'Tennis keeps the common rules but gives up after 5 minutes instead of 15. Every other sport is unchanged.',
      'In the replay of 17-22 November ($12 a trade) CS2 went from 15 trades and +$11.94 to 73 trades and +$49.63, and tennis from +$8.07 to +$30.97 on the same 23 trades. Chosen on those same days, so it runs on paper first.',
    ] },
  { date: '2026-11-23', icon: 'add_circle', area: 'Feeds', title: 'BC.Game, Rollbit, Rainbet, CSGO500 and CSGOEmpire read on the Mac too',
    lines: [
      'The five Betby bookmakers are now read by Swifteye itself, the same way as Duel, so they run on any computer without the browser script.',
      'Every sport and the whole live list (the script read football only, about half the list), every 3 seconds; the pre-match list every 5 minutes. About 75 kB a second for all five.',
      'They list the same games as Duel. Many prices are identical, others differ, sometimes by a lot when one updates before another. Still recorded only.',
    ] },
  { date: '2026-11-23', icon: 'add_circle', area: 'Feeds', title: 'Seven crypto bookmakers added, recorded only',
    lines: [
      'BC.Game, Rollbit, Rainbet, CSGO500 and CSGOEmpire (live football from their Betby feeds, every 5 seconds), Cloudbet (live football, every 5 seconds) and Shuffle (its live page, once a minute).',
      'They are collected by browser_books.py, which the collector now starts by itself and restarts if it stops. Its errors show on the Feeds page under each bookmaker.',
      'Like SBOBET and HKJC, their prices are kept apart. No bot, replay or chart uses them until a test shows how they move against Polymarket.',
    ] },
  { date: '2026-11-22', icon: 'healing', area: 'Feeds', title: '1xBet mirrors no longer lose live games on busy afternoons',
    lines: [
      '1xBet\'s live feed returns at most 200 games per request. One request for every sport was filled by football: on a Sunday afternoon the eight mirrors showed 2 of 18 live e-sports games and 5 of 12 tennis matches.',
      'Each mirror now asks hockey, basketball, tennis, baseball, American football and e-sports together every second, and football and the minor sports (volleyball, handball, table tennis) every third second.',
      'About the same amount of data as before. If a group still reaches 200 games it is split in two by itself.',
    ] },
  { date: '2026-11-22', icon: 'login', area: 'Feeds', title: 'Pinnacle as a signed-in member, recorded only',
    lines: [
      'Pinnacle\'s free guest feed turned out to be a copy kept up to 15 minutes. Signed-in members get the live server.',
      'Type your Pinnacle login in Settings and Swifteye reads the member server every 3 seconds. It never bets.',
      'Its prices are kept apart until a test shows how far ahead of the guest copy and the other bookmakers it is.',
      'If Pinnacle refuses the sign-in or asks for a captcha, Swifteye stops and waits for you.',
    ] },
  { date: '2026-11-22', icon: 'network_check', area: 'Feeds', title: 'Less data downloaded',
    lines: [
      'Pinnacle\'s pre-match lists are read without player and team props (4.8 MB of the 5.3 MB each time) and once a minute instead of every 15 seconds.',
      'Polymarket\'s game list re-reads games that are live or start within 2 hours every minute, and the rest every 10 minutes.',
      'Together about 450 kB a second less, of roughly 2.5 MB a second.',
    ] },
  { date: '2026-11-22', icon: 'add_circle', area: 'Feeds', title: 'SBOBET added, recorded only',
    lines: [
      'SBOBET\'s public live pages are now read every 4 seconds: winner prices for tennis, basketball, football, e-sports, hockey, volleyball, American football and baseball.',
      'Its prices are kept apart from the other bookmakers\'. No bot, replay or chart uses them until a test shows whether SBOBET moves before Polymarket.',
      'If SBOBET refuses us, the feed waits 10 to 30 minutes before asking again.',
    ] },
  { date: '2026-11-22', icon: 'scoreboard', area: 'Feeds', title: 'Live scores are saved',
    lines: [
      'Each time a bookmaker\'s live score of a game changes, the new score is saved with the game clock and the moment it arrived. About 26 bookmakers send scores.',
      'The 1xBet sites also send the detail: games of each set and the points in tennis, rounds of each map, quarters, red cards and penalties.',
      'This is for research (which price jumps follow a goal, a break or a won map, and which do not). No bot reads it.',
    ] },
  { date: '2026-11-22', icon: 'tune', area: 'Bots', title: 'Polymarket\'s earlier moves get a 5-second allowance, and a 15-minute give-up',
    lines: [
      'The lagged rule looks at how Polymarket moved after the bookmakers\' earlier jumps in the same game. It now reads Polymarket\'s own clock and allows 5 seconds, so a Polymarket move made within 5 seconds of a jump counts as following it. On 17-18 Nov a late feed did this by accident, and those extra trades made money.',
      'A trade that has not caught up is now sold after 15 minutes instead of 5.',
      'On the replay of 17-22 Nov at $12 a trade: 41 trades, 76% won, +$90.60, against 31 trades, 61% won, +$33.05 before. With each day left out of the choice and scored on its own: +$78.96.',
      'This applies to real money and paper alike. Stakes are unchanged.',
    ] },
  { date: '2026-11-22', icon: 'healing', area: 'Feeds', title: 'Leon backs off when it starts refusing us',
    lines: [
      'Leon polls every 3 seconds. When it redirects or refuses a request, the feed now waits 10 to 15 minutes instead of retrying within seconds, which had got our address blocked overnight.',
      'Every feed is running again, and Pinnacle is polled every 4 seconds.',
    ] },
  { date: '2026-11-21', icon: 'speed', area: 'Feeds', title: 'Page warm-ups no longer slow the Polymarket feed',
    lines: [
      'On Saturday 21 Nov the Polymarket feed ran late from about 15:00 CET: over half of the prices arrived more than a second after Polymarket stamped them, with bursts of 10 to 15 seconds, and the connection dropped 12 to 20 times an hour (about once an hour the afternoon before). A profile of the collector showed its reader threads queuing on one lock inside the database library, while a background job re-read a whole day of price changes without a break.',
      'That background job only keeps the Rankings and Snipe window pages ready to open. It now rests while real money is on; those two pages are worked out when someone opens them, so their first load takes longer.',
      'The database library no longer keeps running memory totals, which put one shared lock on every allocation. Nothing used those totals.',
      'No trading rule, stake or order changed.',
    ] },
  { date: '2026-11-21', icon: 'build', area: 'Bots', title: 'Half-size real orders are no longer refused',
    lines: [
      'In a weak game the bot stakes half (about $5.50 of a $44 wallet), but a later check still refused any fill under $10 and reported it as "book too thin". So real money and the trial could not take any weak-game trade from 02:23 to 12:42 on 21 Nov. Two moves were refused this way; by the recorded prices neither would have made money (Valorant 03:37 at 52 cents: about -$0.50 at the give-up; tennis 06:06 at 36 cents: the offer was gone within a second, so the order would not have filled).',
      'Now real money and the trial need the book to hold five sixths of the order (the same share the $10 floor is of a $12 stake) and at least Polymarket\'s 5 shares. Paper keeps the $10 floor.',
      'Nothing about which moves are bought has changed, and no order is larger than before.',
    ] },
  { date: '2026-11-21', icon: 'filter_alt', area: 'Bots', title: 'One losing group dropped, two winning groups added',
    lines: [
      'Dropped: a weak game (Polymarket does not trail the bookmakers there) at a price over 60 cents. Under today\'s rules it was the one group that lost money both in the clean replay (8 trades, -$1.58) and with real money (3 trades, 0 won, -$3.15).',
      'Added: basketball outside the US now trades real money (5 clean replay trades, 4 won, +$7.62; the paper trial agreed). NBA and college basketball stay on paper.',
      'Added: Dota 2 trades real money again when its spread is 2 cents or less (4 clean replay trades, 4 won, +$14.22). With a wider spread it stays on paper.',
      'All three rest on a handful of trades each. The replay of 17-20 Nov with all three: see the Backtest page once it has been measured again.',
    ] },
  { date: '2026-11-21', icon: 'star', area: 'Bots', title: 'Favourites bot, on paper only',
    lines: [
      'A new paper bot: before a game starts it buys a favourite priced 55 to 90 cents on Polymarket when the bookmakers (the middle of 3 or more independent ones, margin removed) rate it at least 1 point above the price plus the fee. It holds the bet to the result: one fee, no sale.',
      'On the games of 17-20 Nov this made 70 bets, 84% won where 75% was needed. It was found after trying several cuts and has only 5 bets on games never looked at, so this bot is the test.',
      'It does not bet on CS2 games with a suspicion score at the alert level, stakes 4% of the wallet on a bet, and never ties up more than 80% of it.',
      'No real order can come from it. /favs in Telegram shows its wallet and bets.',
    ] },
  { date: '2026-11-21', icon: 'tune', area: 'Bots', title: 'Moves from 8 points, and a stricter test that Polymarket lags',
    lines: [
      'The bot now takes bookmaker moves of 8 points or more in every sport it trades (it was 10). The paper trial of 8-point CS2 moves ends: they are part of the normal rules.',
      'Polymarket must have made up at least half of the earlier jumps in the game after the bookmakers (it was 30%).',
      'On the replay of 17-20 Nov without the moves inside recording gaps: 29 trades and +$32 before, 34 trades, 76% won and +$61 with both changes, and more on each of the four days. The worst trade was -$3.37.',
      'This applies to real money and paper alike. Stakes are unchanged.',
    ] },
  { date: '2026-11-21', icon: 'healing', area: 'Backtest', title: 'The replay no longer counts buys inside gaps in the recording',
    lines: [
      'On 17 and 18 Nov the price feed ran 5 to 30 seconds late, and whole stretches of Polymarket quotes never arrived. The replay bought the last price before such a gap and sold the first one after it.',
      'Of the 70 replay trades under the bot\'s rules, the 37 inside a gap made +$103 and the 33 outside made +$5. Every replay figure shown before today was inflated by this.',
      'A move is now left out when no game had a Polymarket quote for 2 seconds or more around the buy. The whole history is measured again as soon as real money is off.',
      'Paper and trial wallets no longer buy while the feed is over 1 second late: they were filling at prices that had already gone. Real orders are unchanged, because their price limit already refuses a price that has gone.',
      'Dota 2 moves to the paper trial wallet. With clean prices it won 2 of 8 replay trades and lost $20, while the other sports won 16 of 25. Real money no longer trades it.',
    ] },
  { date: '2026-11-21', icon: 'percent', area: 'Bots', title: 'The real stake is a share of the wallet',
    lines: [
      'A real trade is the real stake share of what the real wallet holds at that moment: 30% is $12 of $40 and $120 of $400. It shrinks after losses as well.',
      'Real max per trade is only a ceiling. It is still $12, so nothing changes until you raise it on the Bots page.',
      'Size by edge no longer touches real money: at 3x it would have put 90% of a real wallet on one trade once the ceiling was raised.',
      'The trial wallet stakes exactly what real money would.',
    ] },
  { date: '2026-11-21', icon: 'account_tree', area: 'Recording', title: 'Sister markets are recorded (not traded)',
    lines: [
      'Besides each game\'s winner market, the recorder now keeps Polymarket\'s other markets: map winners, totals and map handicaps for e-sports, and the spread and the total line nearest to even for basketball and American football.',
      'From Fonbet it also keeps the prices for parts of a game: maps, sets, halves, quarters and periods.',
      'These are for research only. They have their own connection and their own tables; no bot sees them and nothing is bet on them.',
      'The sister connection steps aside for 10 minutes whenever the main price feed runs late.',
    ] },
  { date: '2026-11-21', icon: 'tune', area: 'Bots', title: 'Half stake where Polymarket is not behind, and 8-point CS2 moves on paper',
    lines: [
      'Each game has a delay score: how much Polymarket moved after earlier bookmaker jumps, less how much it had already moved before them. Where that score is low the bot now buys with half its stake. It still takes the trade.',
      'Measured on 3,356 recorded moves: Polymarket caught up 8% of a move at the lowest scores and 28% at the highest. On the 17-20 Nov backtest the same 70 trades lost $36 instead of $65.',
      'CS2 moves of 8 to 10 points (the rules need 10) are now taken by the paper trial wallet. No real money goes on them until they have a record.',
      'Every trade stores its score and the share of the stake it used.',
    ] },
  { date: '2026-11-21', icon: 'payments', area: 'Bots', title: 'Real trades can be as small as $5',
    lines: [
      'The smallest real trade was $10. It is now $5, so the real stake can be set lower than before (Bots, Real max per trade).',
      'Polymarket itself needs 5 shares an order, about $4.60 at the highest price the bot pays.',
      'Nothing changes until the stake is lowered: the current setting stays at $12.',
    ] },
  { date: '2026-11-21', icon: 'show_chart', area: 'Bots', title: 'Charts keep the trades from before a restart',
    lines: [
      'The balance chart used to begin again whenever the paper wallet was restarted or a real test run started, so it showed only the latest stretch.',
      'Today, 7 days and All now draw every trade since the first one. Across a restart the chart shows profit since the start of the period, with a dashed line where the wallet restarted.',
      'A new range, Since restart, shows the current wallet alone, as before. The numbers above the chart still belong to the current wallet.',
      'The balance chart in Telegram does the same: earlier trades in grey, the current run or wallet in colour, and one more line with the total since the first trade.',
    ] },
  { date: '2026-11-20', icon: 'schedule', area: 'Bots', title: 'Late-feed check tried and removed',
    lines: [
      'For a few hours the bot skipped a buy when Polymarket\'s prices were arriving more than a second late. That check is gone.',
      'Tested on 17-19 Nov: 43 of the 54 trades were bought under a late feed, and they made $74 of the $89.',
      'A buy at a price that has already gone still does not fill: the limit price takes care of that.',
      'The five moves it skipped stay listed under "Polymarket prices were arriving late".',
    ] },
  { date: '2026-11-20', icon: 'science', area: 'Bots', title: 'More sports recorded, on a paper trial first',
    lines: [
      'NFL, college football and UFC prices from Polymarket are recorded now.',
      'They and basketball trade on a separate paper wallet, with the real bot\'s rules and its stake per trade.',
      'Real money cannot buy in these sports.',
      'The Telegram command /trial shows the results by sport.',
    ] },
  { date: '2026-11-20', icon: 'balance', area: 'Arbitrage Scout', title: 'Only arbitrage you could take',
    lines: [
      'A price far better than every other source says is left out: it is a wrong link, a swapped side or a feed error.',
      'Each set shows how much its Polymarket and Kalshi legs can take, in dollars.',
      'The list shows checked sets by default. "All" also shows the ones nobody else could confirm.',
      'History keeps its length across restarts and no longer lists zero-second entries.',
    ] },
  { date: '2026-11-20', icon: 'timer', area: 'Telegram', title: 'Control the real run from the chat',
    lines: [
      '/runstatus shows the time left, the result so far and what you can do next.',
      '/extend 4 keeps the run going 4 more hours. /until 22:00 sets the time it ends.',
      '/runs lists the earlier runs.',
      'A message comes 30 minutes before a run ends.',
    ] },
  { date: '2026-11-20', icon: 'query_stats', area: 'Collector', title: 'Kalshi prices in real time',
    lines: ['The best prices from Kalshi\'s live order book are recorded beside its ticker\'s, which can be seconds late.'] },
  { date: '2026-11-20', icon: 'speed', area: 'Collector', title: 'Faster decisions, smaller database files',
    lines: [
      'A price lookup that took minutes when a bookmaker had only just listed a game now takes milliseconds. Decisions that waited on it arrived late and were dropped.',
      'The database\'s write-ahead file is started over every few seconds. It used to grow without end, about 65 MB a minute.',
      'A tab left open in the background no longer asks the server for data. It refreshes when you come back to it.',
    ] },
  { date: '2026-11-20', icon: 'history', area: 'Site', title: 'This changelog',
    lines: ['A page that lists what changed and when.'] },
  { date: '2026-11-20', icon: 'verified_user', area: 'Snipe bot', title: 'Safer real-money runs',
    lines: [
      'Real money stops itself at the daily loss limit, after 3 losing trades in a row, or when under half the trades win after 10.',
      'The loss limit counts open positions at what they would sell for now.',
      'A decision that comes more than a second late is dropped.',
      'Every real buy is recorded as it goes out. A buy with no recorded result is reported at the next start.',
      'The replay stops rebuilding while real money is on, so decisions stay fast.',
    ] },
  { date: '2026-11-20', icon: 'my_location', area: 'Snipe bot', title: 'Buys only fresh moves',
    lines: [
      'A buy needs Polymarket not to have moved since the first bookmaker of the move. A slow bookmaker updating late no longer looks like a new move.',
      'A buy is skipped when Polymarket already sat 10 or more points away from the bookmakers.',
      'A game is locked for 2 minutes only after a trade, not after every move the bot looked at.',
      'A trade is given up after 5 minutes.',
      'The bot trades one fixed, tested rule set. The rule picker still runs but no longer steers it.',
    ] },
  { date: '2026-11-20', icon: 'replay', area: 'Replay', title: 'The replay follows the live bot',
    lines: [
      'It reacts to every bookmaker update, as the bot does, and measures each move with the same steps.',
      'A trade whose game ended before it could be sold now counts as the loss it is.',
      'A move the rules pass but an order cannot fill still locks the game.',
    ] },
  { date: '2026-11-20', icon: 'show_chart', area: 'Telegram', title: 'Charts with every buy and sale',
    lines: [
      'A chart comes with each buy: the expected path, the cash-out price, the break-even and the give-up time.',
      'Charts show the bookmaker that moved and all bookmakers on the same axis as Polymarket.',
      '/realon starts a real run from the chat, in two steps. /realoff and /kill stop it.',
      'When real money is on, the paper copy of a trade takes the real fill and the real sale.',
    ] },
  { date: '2026-11-19', icon: 'account_balance_wallet', area: 'Snipe bot', title: 'Real money has its own wallet',
    lines: [
      'The real bot has its own amount and its own stakes. Nothing is shared with the paper wallet.',
      'A real test run has a set length and safety stops, and can be extended.',
      'A fill that Polymarket\'s reply missed is found and tracked.',
      'Nothing is bought above 90¢, and a position is sold at 99¢ or more.',
    ] },
  { date: '2026-11-19', icon: 'tune', area: 'Bots', title: 'Bots page as a control panel',
    lines: [
      'Paper, real or both in one view, with a balance chart and open positions.',
      'A projection of where the wallet could go, from its own results or from the replay.',
      'Trades made under earlier rules are marked as such.',
    ] },
  { date: '2026-11-19', icon: 'send', area: 'Telegram', title: 'Trade messages and commands',
    lines: [
      'Buys and sales with the game, price and result, and a balance chart after each sale.',
      'About 25 commands: /status, /today, /safety, /rules, /projected and more.',
      'Times are shown in Norwegian time by default.',
    ] },
  { date: '2026-11-18', icon: 'smart_toy', area: 'Bots', title: 'Snipe bot and arbitrage bot',
    lines: [
      'A paper snipe bot with its own balance, and a real-money layer behind a switch.',
      'A paper arbitrage bot between Polymarket and Kalshi, with a backtest view.',
    ] },
  { date: '2026-11-18', icon: 'gpp_maybe', area: 'Markets', title: 'Suspicious games',
    lines: ['A page of games with unusual betting, with a filter for upcoming ones.'] },
  { date: '2026-11-17', icon: 'sports_esports', area: 'Sources', title: 'More sports and bookmakers',
    lines: [
      'E-sports (CS2, Dota 2, LoL and others), tennis, basketball, hockey and more alongside football.',
      'About 28 bookmaker feeds, with their logos.',
      'Kalshi prices, pushed over a WebSocket.',
    ] },
  { date: '2026-11-17', icon: 'leaderboard', area: 'Delay', title: 'The Swifteye web app',
    lines: [
      'Delay rankings: which source moves first and how far behind the others are.',
      'A snipe window that shows how long Polymarket stays behind a bookmaker move.',
      'Matches, charts, movers, edges and an arbitrage scout.',
    ] },
  { date: '2026-11-17', icon: 'rocket_launch', area: 'Collector', title: 'First version',
    lines: [
      'Records every bookmaker and Polymarket price change with a millisecond timestamp.',
      'Links the same game across sources and serves everything over a local API.',
    ] },
];

const dayLabel = (d) => new Date(d + 'T00:00:00Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });

SE.pages.changelog = {
  title: 'Changelog',
  render(el) {
    const days = [];
    for (const e of SE.CHANGELOG) {
      const last = days[days.length - 1];
      if (last && last.date === e.date) last.items.push(e);
      else days.push({ date: e.date, items: [e] });
    }
    el.innerHTML = P.head('Changelog') + days.map((d) =>
      `<section class="sec cl-day"><div class="sec-head"><h2>${ic('event')}${esc(dayLabel(d.date))}</h2></div>` +
      d.items.map((e) =>
        `<article class="cl-item">${ic(e.icon)}<div><div class="cl-title">${esc(e.title)}<span class="cl-area">${esc(e.area)}</span></div>` +
        `<ul class="cl-lines">${e.lines.map((l) => `<li>${esc(l)}</li>`).join('')}</ul></div></article>`).join('') +
      `</section>`).join('');
  },
};
})();
