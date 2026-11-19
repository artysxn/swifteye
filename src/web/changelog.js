/* Swifteye: the Changelog page. A hand-kept list, newest first: add an entry at the top of
   SE.CHANGELOG when something a user would notice ships (date in UTC, a Material icon name,
   the area it belongs to, a short title, and a few plain lines). */
(() => {
const { esc, ic } = SE;
const P = SE.P;

SE.CHANGELOG = [
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
