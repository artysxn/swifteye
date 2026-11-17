// Swifteye pages, part 1: shared pieces, Overview, Rankings, Latency.
'use strict';
(() => {
const { $, $$, esc, ic, api } = SE;
const PM = 'polymarket';

// ---------------------------------------------------------------- shared building blocks (SE.P)
const P = (SE.P = {});
P.PM = PM;
P.head = (title, tools = '', back = '') =>
  `<div class="page-head"><div>${back ? `<a class="back" href="${back[1]}">${ic('chevron_left')}${esc(back[0])}</a>` : ''}<h1>${title}</h1></div><div class="tools">${tools}</div></div>`;
P.sec = (icon, title, body, tools = '', tip = '') =>
  `<section class="sec"><div class="sec-head"><h2 ${tip ? `data-tip="${esc(tip)}"` : ''}>${ic(icon)}${title}</h2><div class="tools">${tools}</div></div>${body}</section>`;
P.stat = (id, icon, label, tip = '', hero = false) =>
  `<div class="stat ${hero ? 'key' : ''}" ${tip ? `data-tip="${esc(tip)}"` : ''}><div class="stat-label">${ic(icon)}${label}</div><div class="stat-value" id="${id}"><span class="skeleton">00.0</span></div><div class="stat-foot" id="${id}F"></div></div>`;
P.stats = (...items) => `<div class="stats">${items.join('')}</div>`;
P.matchCell = (home, away, league) => `<div class="match-name">${SE.vs(home, away)}</div><div class="match-sub">${esc(league || '')}</div>`;
P.set = (id, html, foot) => { const e = $('#' + id); if (e) e.innerHTML = html; if (foot !== undefined) { const f = $('#' + id + 'F'); if (f) f.innerHTML = foot; } };

// Words used in tooltips everywhere.
const G = (P.G = {
  jump: 'A jump is a 1X2 move of at least the chosen size (in probability points) within 10 seconds.',
  first: 'How often this source was the first to cross halfway through a move that others also made.',
  behind: 'Median time between the first source crossing halfway through a move and this source doing the same.',
  vsPm: 'Median time between this source moving and Polymarket moving the same way. "Later" means Polymarket came after this source.',
  rank: 'Average finishing position in moves this source took part in. 1 is first.',
  part: 'Moves this source made, out of the moves it was being watched for.',
  lead: 'How long after the first bookmaker Polymarket crossed halfway through the same move.',
  buy: 'Share of bookmaker moves, while Polymarket was watched, where Polymarket was still at least 2 seconds behind. Buying in that gap was possible in principle. Spreads, fees and fill size are not included.',
  left: 'How many probability points Polymarket still had to move at the moment the first bookmaker had moved. This is the most that a trade placed at that moment could have captured, before spread and fees.',
  feed: 'Time from the source stamping a change to Swifteye receiving it. Only sources that stamp their changes can be measured.',
  consensus: 'Median of the bookmakers\' probabilities after removing each one\'s margin.',
  gap: 'Polymarket probability minus bookmaker consensus. Negative means Polymarket prices the outcome as less likely, so it is cheaper there.',
  margin: 'How much the three 1X2 probabilities add up to above 100%. It is the bookmaker\'s built-in edge.',
});

// De-margined 1X2 from {home, draw, away} decimal prices.
P.fair = (p) => {
  if (!p || !p.home || !p.draw || !p.away) return null;
  const r = { home: 1 / p.home, draw: 1 / p.draw, away: 1 / p.away };
  const s = r.home + r.draw + r.away;
  return { home: r.home / s, draw: r.draw / s, away: r.away / s, margin: s - 1 };
};
// Summary of one /board row: best odds, Polymarket, consensus, largest gap.
P.summarise = (row) => {
  const books = Object.entries(row.books || {}).filter(([, v]) => v && typeof v === 'object');
  const best = {}, fairs = [];
  let pm = null;
  for (const [b, p] of books) {
    if (b === PM) { pm = p; continue; }
    for (const o of ['home', 'draw', 'away']) if (p[o] && (!best[o] || p[o] > best[o].price)) best[o] = { price: p[o], book: b };
    const f = P.fair(p);
    if (f) fairs.push(f);
  }
  const cons = fairs.length ? { home: SE.median(fairs.map((f) => f.home)), draw: SE.median(fairs.map((f) => f.draw)), away: SE.median(fairs.map((f) => f.away)) } : null;
  const pmFair = P.fair(pm);
  let gap = null, gapSide = null;
  if (cons && pmFair) for (const o of ['home', 'draw', 'away']) { const g = pmFair[o] - cons[o]; if (gap == null || Math.abs(g) > Math.abs(gap)) { gap = g; gapSide = o; } }
  return { best, pm, pmFair, cons, gap, gapSide, n: books.length, books: books.map(([b]) => b), hasPm: !!pm, nBooks: fairs.length };
};
P.bestCell = (b, row, o) => (b
  ? `<span class="src sm" ${SE.qattr({ b: b.book, h: row.home, a: row.away, m: '1x2', o, p: b.price })}>${SE.logo(b.book)}${SE.fmtOdds(b.price)}</span>`
  : '<span class="faint">-</span>');

/** One event as a timeline: who crossed when, Polymarket ringed. */
P.eventRow = (e, opts = {}) => {
  const t0 = e.order[0][1];
  const span = Math.max(5000, ...e.order.map((x) => x[1] - t0));
  const pins = e.order.map(([b, t], i) => {
    const pct = ((t - t0) / span) * 100;
    const tipTxt = `<h4>${SE.logo(b)}${esc(SE.bookName(b))}</h4>${SE.kv([
      ['Position', `${i + 1} of ${e.order.length}`],
      ['Moved', i === 0 ? 'First' : SE.fmtSpan(t - t0) + ' after first'],
      ['At', SE.fmtTime(t, true)],
      b !== PM && e.pm_ms ? ['Polymarket', SE.fmtRel(e.pm_ms - t)] : null,
    ])}`;
    return `<span class="pin ${b === PM ? 'pm' : ''}" style="left:${Math.min(100, pct)}%" data-tip="${esc(tipTxt)}">${SE.logo(b)}</span>`;
  }).join('');
  const predTxt = (name, ms, lead, short) => (ms == null
    ? (e.missed.includes(name) ? `<div class="faint" data-tip="${esc(SE.bookName(name))} was watched but did not make this move within the window.">${short} did not follow</div>` : '')
    : `<div><b class="${lead > 0 ? 'up' : 'down'}" data-tip="${esc(G.lead)}">${short} ${SE.fmtRel(lead)}</b></div>`);
  const pmTxt = (predTxt(PM, e.pm_ms, e.pm_lead_ms, 'PM') + predTxt('kalshi', e.kalshi_ms, e.kalshi_lead_ms, 'Kalshi')) || '<span class="faint">no prediction market</span>';
  const sel = `${SE.outcomeLong(e.outcome)} ${e.direction > 0 ? ic('north_east', 'up') : ic('south_east', 'down')}`;
  const missed = e.missed.length ? `<div class="match-sub">Missed: ${e.missed.map((m) => esc(SE.bookName(m))).join(', ')}</div>` : '';
  return `<div class="ev" onclick="location.hash='#/match/${e.match_id}'">
    <div><div class="match-name">${SE.vs(e.home, e.away)}</div><div class="match-sub" style="display:flex;align-items:center;gap:6px">${sel}${e.capturable != null ? `<span data-tip="${esc(G.left)}">${SE.fmtPts(e.capturable)} pts left on PM</span>` : ''}</div>${missed}</div>
    <div class="mono faint">${SE.fmtTime(t0)}</div>
    <div class="track">${pins}<span class="tick" style="left:0">0 s</span><span class="tick" style="left:100%">${SE.fmtSpan(span)}</span></div>
    <div class="r">${opts.noPm ? '' : pmTxt}</div>
  </div>`;
};

/** Ladder of shares: rows [[label, count, share, tip]] */
P.ladder = (rows, total) => `<div class="ladder">${rows.map(([l, n, s, t]) =>
  `<div class="ladder-row" ${t ? `data-tip="${esc(t)}"` : ''}><span class="muted">${l}</span><div class="bar"><b style="width:${(s * 100).toFixed(1)}%"></b></div><b class="r">${SE.fmtPct(s)}</b><span class="faint r">${n} of ${total}</span></div>`).join('')}</div>`;

/** Race: who is closest behind the first mover. */
P.race = (sources) => {
  const ranked = sources.filter((s) => s.median_behind_first_ms != null);
  if (!ranked.length) return '<div class="empty">No shared moves yet. Rankings need at least two sources moving on the same match.</div>';
  const max = Math.max(1000, ...ranked.map((s) => s.median_behind_first_ms));
  return ranked.map((s, i) => `<div class="race-row ${i === 0 ? 'lead' : ''}">
    <span class="pos">${i + 1}</span>${SE.book(s.source)}
    <div class="bar" data-tip="${esc(G.behind)}"><b class="${s.source === PM ? '' : 'dim'}" style="width:${Math.max(2, (s.median_behind_first_ms / max) * 100)}%;${s.source === PM ? '' : 'background:' + SE.bookColor(s.source)}"></b></div>
    <span class="r" data-tip="${esc(G.behind)}">${s.median_behind_first_ms === 0 ? '<b class="accent">first</b>' : '+' + SE.fmtSpan(s.median_behind_first_ms)}</span></div>`).join('');
};

P.rangeSince = (h) => Date.now() - h * 3600000;

/** Game picker: every match in scope, with search. value '' = all games. */
P.gameSelect = (el, onChange, width = '280px') => {
  const sel = SE.select(el, { options: [['', 'All games']], value: '', search: true, width, onChange });
  const load = async () => {
    try {
      const d = await api('/matches');
      const cur = sel.value();
      const opts = d.matches.sort((a, b) => (b.live - a.live) || (a.start_ms || 0) - (b.start_ms || 0))
        .map((m) => [String(m.id), `${m.home} v ${m.away}`, m.live ? 'live' : SE.fmtKick(m.start_ms)]);
      sel.set([['', 'All games'], ...opts], opts.some((o) => o[0] === cur) ? cur : '');
    } catch {}
  };
  load();
  return { ...sel, load };
};
/** Baseline picker: Polymarket or Kalshi. */
P.baselineSeg = (el, onChange) => SE.seg(el, [['polymarket', SE.logo('polymarket') + 'Polymarket', 'Compare bookmakers against Polymarket.'], ['kalshi', SE.logo('kalshi') + 'Kalshi', 'Compare bookmakers against Kalshi.']], SE.pref('baseline'), (v) => { SE.setPref('baseline', v); onChange(v); });
P.oppOf = (R) => (SE.pref('baseline') === 'kalshi' ? R.opportunity_kalshi : R.opportunity);
P.baseName = () => SE.bookName(SE.pref('baseline'));

// ================================================================ Overview
SE.pages.dashboard = {
  title: 'Overview',
  render(el) {
    el.innerHTML = P.head('Overview', '<div id="oB"></div><div id="oR"></div>') +
      P.stats(
        P.stat('oLead', 'timer', '<span class="bn">Polymarket</span> after first bookmaker', G.lead + ' Median over the selected range.', true),
        P.stat('oBuy', 'shopping_cart_checkout', 'Buy before <span class="bn">Polymarket</span> reacts', G.buy),
        P.stat('oLeft', 'straighten', '<span class="bn">Polymarket</span> move still to come', G.left),
        P.stat('oFast', 'bolt', 'Fastest source', 'The source with the smallest median time behind the first mover.'),
        P.stat('oLive', 'sports_soccer', 'Live matches', 'Matches at least one source lists as in play right now.'),
      ) +
      `<div class="grid g-main" style="margin-top:40px">
        ${P.sec('leaderboard', 'Who moves first', '<div id="oRace"></div>', '<a class="more" href="#/rankings">Rankings' + ic('chevron_right') + '</a>', G.behind)}
        ${P.sec('timer', 'When <span class="bn">Polymarket</span> reacts', '<div id="oLadder"></div>', '', G.lead)}
      </div>` +
      P.sec('timeline', 'Latest moves', '<div id="oEvents"></div>', '<a class="more" href="#/rankings">All moves' + ic('chevron_right') + '</a>', 'Each row is one move several sources made on the same selection. Logos sit where each source crossed halfway through the move.') +
      `<div class="grid g-main" style="margin-top:40px">
        ${P.sec('sports_soccer', 'Live matches', '<div id="oMatches"></div>', '<a class="more" href="#/live">All' + ic('chevron_right') + '</a>')}
        ${P.sec('bolt', 'Significant moves', '<div id="oFeed"></div>', '', 'Live 1X2 moves of 2 points or more, as they arrive. Refreshed at most once a second.')}
      </div>`;

    let hours = 24;
    SE.seg($('#oR'), [[1, '1h'], [6, '6h'], [24, '24h'], [168, '7d']], hours, (v) => { hours = +v; loadRank(); });
    P.baselineSeg($('#oB'), () => loadRank());
    const names = new Map();

    async function loadRank() {
      const r = await api(`/rankings?since=${P.rangeSince(hours)}&min_jump=${SE.pref('minJump')}&window_s=${SE.pref('windowS')}&limit=8`);
      const R = r.rankings, o = P.oppOf(R);
      $$('.bn', el).forEach((x) => (x.textContent = P.baseName()));
      const ge2 = o.lead_at_least.find((x) => x[0] === 2000);
      P.set('oLead', o.median_lead_ms == null ? '<span class="faint">-</span>' : SE.fmtRel(o.median_lead_ms), o.pm_followed ? `median of ${o.pm_followed} moves` : 'Needs moves while Polymarket is live');
      P.set('oBuy', o.events ? SE.fmtPct(ge2?.[2]) : '<span class="faint">-</span>', o.events ? `${ge2?.[1] ?? 0} of ${o.events} bookmaker moves` : '');
      P.set('oLeft', o.median_capturable_pts == null ? '<span class="faint">-</span>' : `${(o.median_capturable_pts * 100).toFixed(1)}<small>pts</small>`, o.median_capturable_pts == null ? '' : 'median, before spread and fees');
      if (!o.events) P.set('oLead', '<span class="faint">-</span>', `Needs moves while ${P.baseName()} is live`);
      const fast = R.sources.find((s) => s.median_behind_first_ms != null);
      P.set('oFast', fast ? SE.book(fast.source) : '<span class="faint">-</span>', fast ? `first in ${SE.fmtPct(fast.first_share)} of its moves` : '');
      $('#oRace').innerHTML = P.race(R.sources);
      $('#oLadder').innerHTML = o.events
        ? P.ladder([
            ['Later at all', ...o.lead_at_least[0].slice(1), `${P.baseName()} crossed after the first bookmaker.`],
            ['1 s or more', ...o.lead_at_least[1].slice(1)],
            ['2 s or more', ...o.lead_at_least[2].slice(1)],
            ['5 s or more', ...o.lead_at_least[3].slice(1)],
            ['10 s or more', ...o.lead_at_least[4].slice(1)],
            ['30 s or more', ...o.lead_at_least[5].slice(1)],
            ['Did not follow', o.pm_did_not_follow, o.pm_did_not_follow / o.events, `${P.baseName()} was watched but never made the move within the window.`],
          ], o.events)
        : `<div class="empty">No bookmaker moves while ${P.baseName()} was watched in this range.</div>`;
      $('#oEvents').innerHTML = R.events.length ? R.events.slice().reverse().map((e) => P.eventRow(e)).join('') : '<div class="empty">No shared moves in this range yet.</div>';
    }

    const mt = SE.table($('#oMatches'), [
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league) },
      { key: 's', label: 'Status', tip: 'Score and clock when live, kick-off time before.', fmt: (r) => SE.status(r) },
      { key: 'b', label: 'Sources', tip: 'Sources listing this match. Hover a logo for that source\'s speed.', fmt: (r) => SE.logos(r.sum.books) },
      { key: 'g', label: 'Gap', cls: 'r', tip: G.gap, fmt: (r) => (r.sum.gap == null ? '<span class="faint">-</span>' : `<span class="${Math.abs(r.sum.gap) >= 0.03 ? (r.sum.gap < 0 ? 'up' : 'down') : 'muted'}">${SE.fmtPts(r.sum.gap)}</span>`) },
    ], { empty: 'No live football right now.', emptyIcon: 'sports_soccer', onClick: (r) => SE.go('#/match/' + r.id), limit: 8 });
    async function loadMatches() {
      const b = await api('/board?market=1x2&min_books=1');
      for (const r of b.rows) names.set(r.id, r);
      const rows = b.rows.map((r) => ({ ...r, sum: P.summarise(r) })).sort((x, y) => y.sum.n - x.sum.n);
      mt.set(rows);
      P.set('oLive', rows.length, `${rows.filter((r) => r.sum.hasPm).length} on Polymarket`);
    }

    // Live feed, batched: at most one repaint per second, only moves of 2+ points.
    const queue = [];
    const feed = $('#oFeed');
    feed.innerHTML = '<div class="empty">Waiting for the next big move.</div>';
    let painted = false;
    const paint = SE.throttle(() => {
      if (!queue.length) return;
      if (!painted) { feed.innerHTML = ''; painted = true; }
      const frag = queue.splice(0).slice(-12).map((c) => {
        const m = names.get(c.match_id);
        return `<a class="feed-row" href="#/match/${c.match_id}" ${SE.qattr({ b: c.book, h: m?.home, a: m?.away, m: c.market, l: c.line, o: c.outcome, p: c.new_price, pv: c.old_price, u: c.ts_ms, bt: c.book_ts_ms })}>
          <div class="what"><div class="match-name">${m ? SE.vs(m.home, m.away) : 'Match ' + c.match_id}</div><div class="feed-meta"><span class="mono">${SE.fmtTime(c.ts_ms, true)}</span>${SE.book(c.book, 'sm')}<span>${esc(SE.outcomeLong(c.outcome, c.line))}</span></div></div>${SE.move(c.old_price, c.new_price)}</a>`;
      }).reverse().join('');
      feed.insertAdjacentHTML('afterbegin', frag);
      while (feed.children.length > 25) feed.lastElementChild.remove();
    }, 1000);
    const off = SE.onChange((c) => {
      if (c.market !== '1x2' || c.kind !== 'move' || !c.old_price || !c.new_price) return;
      if (Math.abs(1 / c.new_price - 1 / c.old_price) < 0.02) return;
      queue.push(c);
      if (queue.length > 50) queue.shift();
      paint();
    });

    loadRank(); loadMatches();
    SE.every(30000, loadRank);
    SE.every(5000, loadMatches);
    return off;
  },
};

// ================================================================ Rankings
SE.pages.rankings = {
  title: 'Rankings',
  render(el) {
    el.innerHTML = P.head('Rankings', '<div id="kG"></div><div id="kB"></div><div id="kR"></div><div id="kJ"></div><div id="kW"></div><div id="kC"></div>') +
      P.stats(
        P.stat('kE', 'timeline', 'Moves measured', 'Moves at least two sources made, or a bookmaker made while Polymarket was watched.'),
        P.stat('kM', 'sports_soccer', 'Matches', 'Matches those moves came from.'),
        P.stat('kL', 'timer', '<span class="bn">Polymarket</span> after first bookmaker', G.lead, true),
        P.stat('kBy', 'shopping_cart_checkout', 'Buy before <span class="bn">Polymarket</span> reacts', G.buy),
        P.stat('kP', 'straighten', '<span class="bn">Polymarket</span> move still to come', G.left),
      ) +
      P.sec('leaderboard', 'Leaderboard', '<div id="kT"></div>', '', 'Sources ordered by how close they are, in the median, to whoever moved first.') +
      `<div class="grid g-2" style="margin-top:40px">
        ${P.sec('speed', 'Behind the first mover', '<div id="kRace"></div>', '', G.behind)}
        ${P.sec('shopping_cart_checkout', 'Room to buy before <span class="bn">Polymarket</span>', '<div id="kOpp"></div>', '', G.buy)}
      </div>` +
      P.sec('timeline', 'Every move', '<div id="kEv"></div>', '<div id="kEvF"></div>');

    let hours = 24, jump = SE.pref('minJump'), win = SE.pref('windowS'), clock = 'receive', evFilter = 'all', last = null, game = '';
    P.gameSelect($('#kG'), (v) => { game = v; load(); });
    P.baselineSeg($('#kB'), () => load());
    SE.seg($('#kR'), [[1, '1h'], [6, '6h'], [24, '24h'], [168, '7d']], hours, (v) => { hours = +v; load(); });
    SE.seg($('#kJ'), [[0.02, '2 pts'], [0.04, '4 pts'], [0.06, '6 pts'], [0.1, '10 pts']], jump, (v) => { jump = +v; load(); });
    SE.seg($('#kW'), [[30, '30 s'], [60, '60 s'], [90, '90 s'], [180, '3 min']], win, (v) => { win = +v; load(); });
    SE.seg($('#kC'), [['receive', 'Our clock', 'Times are when Swifteye received each change. This is what you could act on.'], ['source', 'Source clock', 'Uses each source\'s own change stamp where it has one, removing its feed delay.']], clock, (v) => { clock = v; load(); });
    SE.seg($('#kEvF'), [['all', 'All'], ['pm', 'With a prediction market'], ['buy', 'Buyable gap']], evFilter, (v) => { evFilter = v; drawEvents(); });

    const t = SE.table($('#kT'), [
      { key: 'p', label: '#', fmt: (r) => `<b class="${r.pos === 1 ? 'accent' : 'faint'}">${r.pos}</b>` },
      { key: 's', label: 'Source', fmt: (r) => SE.book(r.source) },
      { key: 'f', label: 'Moves first', cls: 'r', tip: G.first, fmt: (r) => (r.first_share == null ? '' : `<span class="trio" style="align-items:center;gap:10px"><span class="bar" style="width:70px;display:inline-block"><b style="width:${r.first_share * 100}%"></b></span>${SE.fmtPct(r.first_share)}</span>`), sort: (r) => r.first_share },
      { key: 'a', label: 'Avg position', cls: 'r', tip: G.rank, fmt: (r) => (r.avg_rank == null ? '' : r.avg_rank.toFixed(2)), sort: (r) => (r.avg_rank == null ? null : -r.avg_rank) },
      { key: 'b', label: 'Behind first', cls: 'r', tip: G.behind, fmt: (r) => (r.median_behind_first_ms == null ? '' : r.median_behind_first_ms === 0 ? '<b class="accent">first</b>' : SE.fmtSpan(r.median_behind_first_ms)), sort: (r) => (r.median_behind_first_ms == null ? null : -r.median_behind_first_ms) },
      { key: 'q', label: 'Slow quarter', cls: 'r', tip: 'Three in four of this source\'s moves came within this time of the first mover.', fmt: (r) => `<span class="muted">${SE.fmtSpan(r.p75_behind_first_ms)}</span>` },
      { key: 'v', label: 'Polymarket', cls: 'r', tip: G.vsPm, fmt: (r) => (r.source === PM ? '<span class="faint">baseline</span>' : r.median_vs_pm_ms == null ? '<span class="faint">-</span>' : `<b class="${r.median_vs_pm_ms > 0 ? 'up' : 'down'}">${SE.fmtRel(r.median_vs_pm_ms)}</b>`), sort: (r) => r.median_vs_pm_ms },
      { key: 'h', label: 'Ahead of PM', cls: 'r', tip: 'Share of shared moves where this source moved before Polymarket.', fmt: (r) => (r.ahead_of_pm_share == null ? '' : `${SE.fmtPct(r.ahead_of_pm_share)} <span class="faint">of ${r.vs_pm_events}</span>`), sort: (r) => r.ahead_of_pm_share },
      { key: 'k', label: 'Kalshi', cls: 'r', tip: 'Median time between this source moving and Kalshi moving the same way.', fmt: (r) => (r.source === 'kalshi' ? '<span class="faint">baseline</span>' : r.median_vs_kalshi_ms == null ? '<span class="faint">-</span>' : `<b class="${r.median_vs_kalshi_ms > 0 ? 'up' : 'down'}">${SE.fmtRel(r.median_vs_kalshi_ms)}</b>`), sort: (r) => r.median_vs_kalshi_ms },
      { key: 'n', label: 'Took part', cls: 'r', tip: G.part, fmt: (r) => `${r.moved} <span class="faint">of ${r.watched}</span>`, sort: (r) => r.moved },
    ], { sort: 'p', dir: 1, empty: 'No shared moves in this range yet.' });

    function drawEvents() {
      if (!last) return;
      let ev = last.events.slice().reverse();
      const k = SE.pref('baseline') === 'kalshi';
      if (evFilter === 'pm') ev = ev.filter((e) => e.pm_ms != null || e.kalshi_ms != null || e.missed.includes(PM) || e.missed.includes('kalshi'));
      if (evFilter === 'buy') ev = ev.filter((e) => { const l = k ? e.kalshi_lead_ms : e.pm_lead_ms; return l != null && l >= 2000; });
      $('#kEv').innerHTML = ev.length ? ev.slice(0, 150).map((e) => P.eventRow(e)).join('') : '<div class="empty">No moves match this filter.</div>';
    }
    async function load() {
      const r = await api(`/rankings?since=${P.rangeSince(hours)}&min_jump=${jump}&window_s=${win}&clock=${clock}&limit=400${game ? '&match=' + game : ''}`);
      const R = (last = r.rankings), o = P.oppOf(R);
      $$('.bn', el).forEach((x) => (x.textContent = P.baseName()));
      P.set('kE', SE.fmtInt(r.event_count));
      P.set('kM', R.matches);
      P.set('kL', o.median_lead_ms == null ? '<span class="faint">-</span>' : SE.fmtRel(o.median_lead_ms), o.pm_followed ? `median of ${o.pm_followed}` : '');
      const ge2 = o.lead_at_least.find((x) => x[0] === 2000);
      P.set('kBy', o.events ? SE.fmtPct(ge2?.[2]) : '<span class="faint">-</span>', o.events ? `${ge2?.[1]} of ${o.events} moves` : '');
      P.set('kP', o.median_capturable_pts == null ? '<span class="faint">-</span>' : `${(o.median_capturable_pts * 100).toFixed(1)}<small>pts</small>`, 'median');
      t.set(R.sources.map((s, i) => ({ ...s, pos: i + 1 })));
      $('#kRace').innerHTML = P.race(R.sources);
      $('#kOpp').innerHTML = o.events
        ? `<div class="note" style="margin-bottom:8px">${P.baseName()} still behind when the first bookmaker had moved</div>` +
          P.ladder(o.lead_at_least.map(([ms, n, s]) => [ms === 0 ? 'Any gap' : SE.fmtSpan(ms) + ' or more', n, s]), o.events) +
          `<div class="note" style="margin:22px 0 8px">${P.baseName()} move still to come at that moment</div>` +
          P.ladder(o.capturable_at_least.map(([pts, n, s]) => [`${Math.round(pts * 100)} pts or more`, n, s, G.left]), o.events)
        : `<div class="empty">No bookmaker moves while ${P.baseName()} was watched in this range.</div>`;
      drawEvents();
    }
    load();
    SE.every(30000, load);
  },
};

// ================================================================ Latency
SE.pages.latency = {
  title: 'Latency',
  render(el) {
    el.innerHTML = P.head('Latency', '<div id="yG"></div><div id="yR"></div><div id="yJ"></div><div id="yW"></div><div id="yC"></div>') +
      P.stats(
        P.stat('yK1', 'timer', 'Polymarket vs fastest bookmaker', 'Median time between the fastest bookmaker and Polymarket on moves Polymarket made first or alone.', true),
        P.stat('yK2', 'rss_feed', 'Fastest feed', G.feed),
        P.stat('yK3', 'swap_vert', 'Jumps analysed', G.jump),
        P.stat('yK4', 'sports_soccer', 'Matches with Polymarket', 'Matches where Polymarket and at least one bookmaker were both recorded.'),
      ) +
      `<div class="grid g-2" style="margin-top:40px">
        ${P.sec('rss_feed', 'Feed delay', '<div id="yFeed"></div>', '', G.feed)}
        ${P.sec('timer', 'Polymarket against each bookmaker', '<div id="yPm"></div>', '', G.vsPm)}
      </div>` +
      P.sec('grid_view', 'Bookmaker against bookmaker', '<div id="yHeat" class="scroll"></div>', '', 'Each cell: median time the column source moved after the row source moved first. Green means the column source was earlier.') +
      P.sec('format_list_numbered', 'Jumps', '<div id="yEv"></div>', '<div id="yEvT"></div>');

    let range = 24, jump = SE.pref('minJump'), win = SE.pref('windowS'), clock = 'receive', evTrig = 'all', events = [], game = '';
    P.gameSelect($('#yG'), (v) => { game = v; load(); });
    SE.seg($('#yR'), [[1, '1h'], [6, '6h'], [24, '24h'], [168, '7d']], range, (v) => { range = +v; load(); });
    SE.seg($('#yJ'), [[0.02, '2 pts'], [0.04, '4 pts'], [0.06, '6 pts'], [0.1, '10 pts']], jump, (v) => { jump = +v; load(); });
    SE.seg($('#yW'), [[30, '30 s'], [60, '60 s'], [90, '90 s'], [180, '3 min']], win, (v) => { win = +v; load(); });
    SE.seg($('#yC'), [['receive', 'Our clock'], ['source', 'Source clock']], clock, (v) => { clock = v; load(); });
    SE.seg($('#yEvT'), [['all', 'All'], ['book', 'Bookmaker moves'], ['polymarket', 'Polymarket moves']], evTrig, (v) => { evTrig = v; drawEvents(); });

    const rel = (ms) => (ms == null ? '<span class="faint">-</span>' : `<b class="${ms > 0 ? 'up' : 'down'}">${SE.fmtRel(ms)}</b>`);
    const pmT = SE.table($('#yPm'), [
      { key: 'd', label: 'Detected on', tip: 'Which side\'s jump started the comparison.', fmt: (r) => (r.trigger === 'book' ? 'Bookmaker move' : 'Polymarket move') },
      { key: 'b', label: 'Bookmaker', fmt: (r) => (r.book === 'first book' ? '<b>Fastest of all</b>' : SE.book(r.book)) },
      { key: 'j', label: 'Jumps', cls: 'r', tip: G.jump, fmt: (r) => r.jumps, sort: (r) => r.jumps },
      { key: 'f', label: 'Matched', cls: 'r', tip: 'Jumps where the other side made the same move within the window.', fmt: (r) => `${r.followed} <span class="faint">${r.jumps ? SE.fmtPct(r.followed / r.jumps) : ''}</span>`, sort: (r) => r.followed },
      { key: 'm', label: 'Polymarket', cls: 'r', tip: G.vsPm, fmt: (r) => rel(r.median_delay_ms), sort: (r) => r.median_delay_ms },
    ], { sort: 'j', empty: 'No jumps yet. They need a live match on Polymarket and a real move such as a goal.' });

    const evT = SE.table($('#yEv'), [
      { key: 't', label: 'Time', fmt: (r) => `<span class="mono">${SE.fmtDateTime(r.pm_ms || r.book_ms)}</span>`, sort: (r) => r.pm_ms || r.book_ms },
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => `<div class="match-name">${SE.vs(r.home, r.away)}</div>` },
      { key: 'o', label: 'Selection', fmt: (r) => esc(SE.outcomeLong(r.outcome)) },
      { key: 'w', label: 'Detected on', fmt: (r) => (r.trigger === 'polymarket' ? 'Polymarket' : 'Bookmaker') },
      { key: 'b', label: 'Bookmaker', fmt: (r) => (r.book.startsWith('first') ? esc(r.book.replace('first book', 'Fastest')) : SE.book(r.book)) },
      { key: 'bm', label: 'Book move', cls: 'r', fmt: (r) => (r.book_from != null ? `<span class="muted">${SE.fmtProb(r.book_from, 0)} to ${SE.fmtProb(r.book_to, 0)}</span>` : '') },
      { key: 'pm', label: 'Polymarket move', cls: 'r', fmt: (r) => (r.pm_from != null ? `<span class="muted">${SE.fmtProb(r.pm_from, 0)} to ${SE.fmtProb(r.pm_to, 0)}</span>` : '') },
      { key: 'd', label: 'Polymarket', cls: 'r', tip: G.vsPm, fmt: (r) => (r.delay_ms == null ? '<span class="faint">did not follow</span>' : rel(r.delay_ms)), sort: (r) => r.delay_ms },
    ], { sort: 't', maxHeight: '640px', empty: 'No jumps in this range.', onClick: (r) => SE.go('#/match/' + r.match_id) });
    function drawEvents() { evT.set(evTrig === 'all' ? events : events.filter((e) => e.trigger === evTrig)); }

    async function load() {
      const since = P.rangeSince(range);
      const p = `since=${since}&min_jump=${jump}&window_s=${win}&clock=${clock}${game ? '&match=' + game : ''}`;
      const [sum, ev, bb, feed] = await Promise.all([
        api('/lag?' + p), api('/lag/events?limit=3000&' + p), api('/lag/books?' + p).catch(() => ({ summary: [] })),
        SE.sql(`SELECT book, ts_ms - book_ts_ms AS d FROM changes WHERE book_ts_ms IS NOT NULL AND kind = 'move' AND ts_ms >= ${since}${game ? ' AND match_id = ' + (+game) : ''} ORDER BY id DESC LIMIT 60000`, 60000),
      ]);
      const f = sum.summary.find((s) => s.trigger === 'polymarket' && s.book === 'first book');
      P.set('yK1', f?.median_delay_ms != null ? SE.fmtRel(f.median_delay_ms) : '<span class="faint">-</span>', f ? `${f.followed} of ${f.jumps} Polymarket moves` : '');
      P.set('yK3', SE.fmtInt(ev.count));
      P.set('yK4', sum.matches);
      const by = new Map();
      for (const r of feed) { if (!by.has(r.book)) by.set(r.book, []); by.get(r.book).push(r.d); }
      const rows = [...by.entries()].map(([b, d]) => ({ b, n: d.length, med: SE.median(d), p10: SE.quantile(d, 0.1), p90: SE.quantile(d, 0.9) })).sort((a, b) => a.med - b.med);
      const books = rows.filter((r) => r.b !== PM);
      P.set('yK2', books.length ? `${SE.book(books[0].b)} <small>${SE.fmtSpan(books[0].med)}</small>` : '<span class="faint">-</span>');
      const max = Math.max(1, ...rows.map((r) => r.p90 || 0));
      $('#yFeed').innerHTML = rows.length ? rows.map((r) => `<div class="race-row" style="grid-template-columns:150px minmax(0,1fr) 70px 70px" data-tip="${esc(`<h4>${SE.logo(r.b)}${esc(SE.bookName(r.b))}</h4>${SE.kv([['Median', SE.fmtSpan(r.med)], ['Fastest tenth', SE.fmtSpan(r.p10)], ['Slowest tenth from', SE.fmtSpan(r.p90)], ['Samples', SE.fmtInt(r.n)]])}`)}">
          ${SE.book(r.b)}<div class="bar"><b style="left:${(Math.max(0, r.p10) / max) * 100}%;width:${(Math.max(1, r.p90 - Math.max(0, r.p10)) / max) * 100}%;background:${SE.bookColor(r.b)};opacity:.3"></b><b style="left:${(Math.max(0, r.med) / max) * 100}%;width:3px;background:${SE.bookColor(r.b)}"></b></div>
          <b class="r">${SE.fmtSpan(r.med)}</b><span class="r faint">${SE.fmtSpan(r.p90)}</span></div>`).join('') : '<div class="empty">No stamped changes in this range.</div>';
      pmT.set(sum.summary);
      const names = [...new Set(bb.summary.flatMap((s) => [s.trigger_book, s.follower_book]))].sort();
      const cell = new Map(bb.summary.map((s) => [s.trigger_book + '>' + s.follower_book, s]));
      const tint = (ms) => { if (ms == null) return ''; const a = Math.min(1, Math.abs(ms) / 30000) * 0.42 + 0.05; return ms > 0 ? `background:rgba(255,95,87,${a.toFixed(2)})` : `background:rgba(80,228,178,${a.toFixed(2)})`; };
      $('#yHeat').innerHTML = names.length ? `<table class="tbl heat"><thead><tr><th>Moved first</th>${names.map((n) => `<th class="c"><span data-tipsrc="${esc(n)}" class="src sm">${SE.logo(n)}</span></th>`).join('')}</tr></thead><tbody>${names.map((a) => `<tr><td>${SE.book(a)}</td>${names.map((b) => {
        if (a === b) return '<td class="cell self">-</td>';
        const s = cell.get(a + '>' + b);
        const t = s ? `<h4>${SE.logo(a)} then ${SE.logo(b)}</h4>${SE.kv([['After ' + SE.bookName(a) + ' moved', SE.bookName(b) + ' ' + SE.fmtRel(s.median_delay_ms)], ['Followed', `${s.followed} of ${s.jumps}`], ['Middle half', `${SE.fmtDelay(s.p25_delay_ms)} to ${SE.fmtDelay(s.p75_delay_ms)}`]])}` : '';
        return s && s.median_delay_ms != null ? `<td class="cell" style="${tint(s.median_delay_ms)}" data-tip="${esc(t)}">${SE.fmtDelay(s.median_delay_ms)}</td>` : '<td class="cell faint">-</td>';
      }).join('')}</tr>`).join('')}</tbody></table>` : '<div class="empty">No bookmaker jumps in this range.</div>';
      events = ev.events.slice().reverse();
      drawEvents();
    }
    load();
    SE.every(30000, load);
  },
};
})();
