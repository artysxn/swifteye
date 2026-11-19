// Swifteye pages, part 5: Suspicious (heavily bet CS2 games, pre-match line moves) and site search.
'use strict';
(() => {
const { $, $$, esc, ic, api, P } = SE;

const money = (x) => (x == null ? '-' : x >= 1e6 ? '$' + (x / 1e6).toFixed(1) + 'M' : x >= 1e3 ? '$' + (x / 1e3).toFixed(x >= 1e4 ? 0 : 1) + 'k' : '$' + Math.round(x));
const vrs = (r) => (r > 0 ? '#' + r : '<span class="faint">unranked</span>');
const hm = (ms) => { const m = Math.round(Math.abs(ms) / 60000); return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`; };

// ================================================================ Suspicious
SE.pages.suspicious = {
  title: 'Suspicious',
  render(el) {
    el.innerHTML = P.head('Suspicious', '<div id="sDays"></div>') +
      P.stats(
        P.stat('sA', 'local_fire_department', 'Heavily bet CS2 games', 'Lower-tier CS2 games at or above the alert score in the period.', true),
        P.stat('sL', 'trending_up', 'Pre-match line moves', 'Upcoming games where the bookmakers moved a side by the threshold or more.'),
        P.stat('sB', 'stacked_bar_chart', 'Normal games compared', 'Finished lower-tier CS2 games the scores are measured against.'),
        P.stat('sV', 'military_tech', 'VRS', 'Valve Regional Standings used to skip tier-1 games.')) +
      P.sec('local_fire_department', 'Heavily bet lower-tier CS2 games', '<div id="sAT"></div>', '<div id="sAF"></div>',
        'How heavily each Polymarket CS2 game was bet compared with normal lower-tier games. Click a game for its match page, or Polymarket when Swifteye has no match for it.') +
      `<p class="note" style="margin-top:14px">Score 0-100 = average percentile against finished lower-tier games of the baseline period on: total money traded, big underdog bets, big favourite bets and the share of money in big bets (drift from the first trade to the start counts half). From your Fixed / Clean marks these activity measures separated best (AUC about 0.75); account age, past profit and bet price did not help. Two caveats from that study: the marks were made looking at activity charts, so this is partly circular, and the money in Fixed games still did not know the result. A high score means a heavily bet lower-tier game, not proof of a fix. Tier 1 (a VRS top-20 team, or both in the top 30) is skipped.</p>` +
      P.sec('trending_up', 'Pre-match line moves', '<div id="sLT"></div>', '', 'Upcoming games where the bookmakers moved a side a lot before the start. Each independent bookmaker platform\'s opening price against its price now, with its margin removed.') +
      P.sec('tune', 'Thresholds', '<div id="sS"></div>', '', 'Telegram alerts for both use these. Switch the alerts on or off on the Telegram page.');
    // days = 0 means "Upcoming": only games that have not started yet.
    let days = SE.pref('susDays') ?? 7, showT1 = false, onlyFlag = false, data = null;
    SE.seg($('#sDays'), [[1, '24 h'], [7, '7 days'], [30, '30 days'], [0, 'Upcoming']], days, (v) => { days = +v; SE.setPref('susDays', days); load(); });
    $('#sAF').innerHTML = `<label class="muted" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="sOnly">Flagged only</label><label class="muted" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="sT1">Show tier 1</label>`;
    $('#sOnly').addEventListener('change', (e) => { onlyFlag = e.target.checked; draw(); });
    $('#sT1').addEventListener('change', (e) => { showT1 = e.target.checked; draw(); });
    const status = (r) => {
      const now = Date.now();
      if (!r.start_ms) return '';
      if (r.start_ms > now) return `<span class="muted">${SE.fmtKick(r.start_ms)}</span><div class="faint">in ${hm(r.start_ms - now)}</div>`;
      if (!r.final) return `<span class="chip accent">Live / settling</span><div class="faint">${SE.fmtKick(r.start_ms)}</div>`;
      return `<span class="muted">${SE.fmtKick(r.start_ms)}</span><div class="faint">finished</div>`;
    };
    const scoreCell = (r) => {
      if (r.tier1) return '<span class="chip">Tier 1</span>';
      if (r.score == null) return `<span class="faint" data-tip="Not enough normal games to compare against yet, or no trades.">-</span>`;
      const a = data.settings.alert_score;
      return `<span class="chip ${r.score >= a ? 'bad' : r.score >= a - 20 ? 'accent' : ''}">${Math.round(r.score)}</span>`;
    };
    const tA = SE.table($('#sAT'), [
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league, 'cs2'), sort: (r) => r.home },
      { key: 'k', label: 'Kick-off', fmt: status, sort: (r) => r.start_ms },
      { key: 'v', label: 'VRS', w: 110, tip: 'Valve Regional Standings rank of each team.', fmt: (r) => `${vrs(r.vrs[0])} <span class="faint">v</span> ${vrs(r.vrs[1])}` },
      { key: 'vol', label: 'Traded', cls: 'r', w: 90, tip: 'Total money traded on all of the game\'s Polymarket markets.', fmt: (r) => money(r.volume), sort: (r) => r.volume },
      { key: 'dog', label: 'Big dog', cls: 'r', w: 84, tip: 'Big bets bought on the underdog (size and price limits under Thresholds).', fmt: (r) => r.big_dog ?? '-', sort: (r) => r.big_dog },
      { key: 'fav', label: 'Big fav', cls: 'r', w: 84, tip: 'Big bets bought on the favourite (50% or more).', fmt: (r) => r.big_fav ?? '-', sort: (r) => r.big_fav },
      { key: 'sh', label: 'Big share', cls: 'r', w: 92, tip: 'Share of all the money that came in big bets.', fmt: (r) => (r.big_share == null ? '-' : Math.round(r.big_share * 100) + '%'), sort: (r) => r.big_share },
      { key: 'dr', label: 'Drift', cls: 'r', w: 80, tip: 'How far the match price moved from the first trade to the start, in points.', fmt: (r) => (r.drift == null ? '-' : Math.round(r.drift * 100)), sort: (r) => r.drift },
      { key: 's', label: 'Score', cls: 'r', w: 84, tip: 'Average percentile against normal lower-tier games (0-100).', fmt: scoreCell, sort: (r) => (r.tier1 ? -1 : r.score) },
    ], { sort: 's', empty: 'No CS2 games in this period yet. The first scan of the last 14 days takes a few minutes after a start.', emptyIcon: 'local_fire_department',
      onClick: (r) => SE.go('#/susgame/' + r.event_id) });
    const outName = (r) => (r.outcome === 'home' ? r.home : r.outcome === 'away' ? r.away : r.outcome === 'draw' ? 'Draw' : r.outcome);
    const tL = SE.table($('#sLT'), [
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league, r.sport), sort: (r) => r.home },
      { key: 'k', label: 'Kick-off', fmt: (r) => `<span class="muted">${SE.fmtKick(r.start_ms)}</span><div class="faint">${r.start_ms > Date.now() ? 'in ' + hm(r.start_ms - Date.now()) : 'started'}</div>`, sort: (r) => r.start_ms },
      { key: 'o', label: 'Shortened', w: 150, tip: 'The side the bookmakers made more likely.', fmt: (r) => `<b>${esc(outName(r))}</b>` },
      { key: 'p', label: 'Fair odds', w: 130, tip: 'Opening and current price with the bookmaker margin removed (median platform).', fmt: (r) => `${(1 / r.open_p).toFixed(2)} <span class="faint">→</span> <b>${(1 / r.now_p).toFixed(2)}</b>` },
      { key: 'mv', label: 'Move', cls: 'r', w: 84, tip: 'Implied probability points (median platform).', fmt: (r) => `<b class="up">+${(r.move * 100).toFixed(1)}</b>`, sort: (r) => r.move },
      { key: 'a', label: 'Platforms', cls: 'r', w: 96, tip: 'Independent bookmaker platforms that moved it this way by at least half the threshold, of those pricing the game.', fmt: (r) => `${r.agree} <span class="faint">of ${r.platforms}</span>`, sort: (r) => r.agree },
      { key: 'w', label: 'Moved', w: 130, tip: 'When the move happened (median platform crossing halfway), and how long before the start.', fmt: (r) => (r.moved_ms ? `${SE.fmtTime(r.moved_ms)}<div class="faint">${hm(r.start_ms - r.moved_ms)} before start</div>` : '-'), sort: (r) => r.moved_ms },
    ], { sort: 'mv', empty: 'No big pre-match moves in this period.', emptyIcon: 'trending_up', onClick: (r) => SE.go('#/match/' + r.match_id) });
    function draw() {
      if (!data) return;
      const a = data.settings.alert_score;
      let rows = data.activity.filter((r) => showT1 || !r.tier1);
      if (onlyFlag) rows = rows.filter((r) => !r.tier1 && r.score >= a);
      tA.set(rows);
      tL.set(data.lines);
      P.set('sA', data.activity.filter((r) => !r.tier1 && r.score >= a).length, `score ${a}+ · ${data.activity.filter((r) => !r.tier1).length} lower-tier games scanned`);
      P.set('sL', data.lines.length, `${data.settings.line_move_pts}+ points, ${data.settings.line_min_platforms}+ platforms`);
      P.set('sB', data.baseline_games, data.baseline_games < 10 ? 'Scores start at 10 games' : `last ${data.settings.baseline_days} days`);
      P.set('sV', data.vrs.teams ? data.vrs.teams + ' teams' : '-', data.vrs.date ? 'standings of ' + data.vrs.date : 'loading');
    }
    function settingsForm() {
      const s = data.settings;
      const f = (k, label, tip, step = 1, suffix = '') => `<div class="field"><label data-tip="${esc(tip)}">${label}</label><div>${SE.num({ attr: `data-s="${k}"`, value: s[k], step, pre: suffix === '$' ? '$' : '', suf: suffix === '$' ? '' : suffix, w: suffix && suffix !== '$' ? 136 : 110 })}</div></div>`;
      $('#sS').innerHTML = `<div class="grid g-2"><div>
        ${f('big_bet_usd', 'Big bet', 'A bet (fills of one wallet within 60 s) of at least this many dollars.', 50, '$')}
        ${f('underdog_below', 'Underdog below', 'A bet bought below this price is on the underdog (0.40 = 40¢).', 0.05)}
        ${f('alert_score', 'Alert score', 'Games at or above this score are flagged and sent to Telegram.', 5, 'of 100')}
        ${f('baseline_days', 'Normal games from', 'Finished lower-tier games of this many days form the baseline.', 1, 'days')}
      </div><div>
        ${f('vrs_top', 'Tier 1: any team in VRS top', 'A game with a team this high is tier 1 and skipped.', 1)}
        ${f('vrs_both', 'Tier 1: both teams in VRS top', 'A game with both teams this high is tier 1 and skipped.', 1)}
        ${f('line_move_pts', 'Line move', 'Smallest pre-match move (median platform), in probability points.', 1, 'points')}
        ${f('line_min_platforms', 'Platforms agreeing', 'Independent bookmaker platforms that must move it the same way.', 1)}
        ${f('line_window_h', 'Games starting within', 'Only games starting within this many hours are checked.', 1, 'hours')}
      </div></div><div style="margin-top:14px;display:flex;gap:10px;align-items:center"><button class="btn primary" id="sSave">${ic('save')}Save</button><span class="muted" id="sMsg">Scores update within a few minutes of saving.</span></div>`;
      $('#sSave').addEventListener('click', async () => {
        const body = Object.fromEntries($$('[data-s]').map((i) => [i.dataset.s, +i.value]));
        try { await api('/suspicious/settings', { body }); $('#sMsg').textContent = 'Saved'; load(); } catch (e) { $('#sMsg').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
      });
    }
    let formDone = false;
    async function load() {
      data = await api('/suspicious?days=' + (days || 1));
      if (!days) {
        const now = Date.now();
        data.activity = data.activity.filter((r) => r.start_ms > now);
        data.lines = data.lines.filter((r) => r.start_ms > now);
      }
      draw();
      if (!formDone) { settingsForm(); formDone = true; }
    }
    load();
    SE.every(30000, load);
  },
};

// ================================================================ Suspicious: one game
SE.pages.susgame = {
  title: 'Suspicious game',
  render(el, [id]) {
    el.innerHTML = P.head('Loading game', '', ['Suspicious', '#/suspicious']) + `<div class="empty">${ic('hourglass_empty')}Fetching every trade from Polymarket</div>`;
    let chart = null;
    const cssColor = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim() || '#888';
    const pct = (v) => (v == null ? '' : (v * 100).toFixed(0) + '%');
    const money = (x) => (x == null ? '-' : x >= 1e6 ? '$' + (x / 1e6).toFixed(1) + 'M' : x >= 1e3 ? '$' + (x / 1e3).toFixed(x >= 1e4 ? 0 : 1) + 'k' : '$' + Math.round(x));
    (async () => {
      let d;
      try { d = await api('/suspicious/event/' + encodeURIComponent(id)); }
      catch (e) { el.innerHTML = P.head('Game', '', ['Suspicious', '#/suspicious']) + `<div class="empty err">${ic('error')}${esc(e.message)}</div>`; SE.tidyHead(el); return; }
      if (location.hash.indexOf('#/susgame/') !== 0) return;
      const st = d.stored || {};
      const a = d.settings.alert_score;
      const vr = (r) => (r > 0 ? '#' + r : 'unranked');
      el.innerHTML = P.head(`${esc(d.home)} <span class="faint">v</span> ${esc(d.away)}`,
        `<a class="btn" target="_blank" rel="noopener" href="https://polymarket.com/event/${encodeURIComponent(d.slug)}">${ic('open_in_new')}Polymarket</a>${d.match_id ? `<a class="btn" href="#/match/${d.match_id}">${ic('sports_esports')}Match page</a>` : ''}`,
        ['Suspicious', '#/suspicious']) +
        `<p class="page-sub">${esc(d.league)} · ${d.start_ms > Date.now() ? 'starts' : 'started'} ${SE.fmtDateTime(d.start_ms)} · VRS ${vr(d.vrs[0])} v ${vr(d.vrs[1])}</p>` +
        P.stats(
          P.stat('gS', 'local_fire_department', 'Score', 'Average percentile against finished lower-tier games (0-100).', true),
          P.stat('gV', 'payments', 'Traded', 'Money traded on every Polymarket market of the game.'),
          P.stat('gD', 'south_east', 'Big underdog bets', `Bets of $${d.settings.big_bet_usd}+ bought below ${Math.round(d.settings.underdog_below * 100)}%.`),
          P.stat('gF', 'north_east', 'Big favourite bets', `Bets of $${d.settings.big_bet_usd}+ bought at 50% or more.`),
          P.stat('gB', 'pie_chart', 'Share in big bets', 'Share of all the money that came in big bets.')) +
        P.sec('monitoring', `${esc(d.match_home || d.home)} to win`, '<div class="chart" id="gC" style="height:380px"></div><div class="legend" id="gL"></div>', '<div id="gScale"></div>',
          'Every source on one scale: Polymarket from each trade, Kalshi from its own minute-by-minute history, bookmakers from what Swifteye recorded. Dots are big bets, placed at their price. Drag to zoom, double-click to reset, click a legend entry to hide it.') +
        `<p class="note" style="margin-top:10px">${d.kalshi ? `Kalshi from its own history (${esc(d.kalshi.event)}). ` : 'Kalshi does not list this game. '}${d.match_id ? '' : 'Bookmaker prices only exist for games played since Swifteye started recording (17 Nov); none are linked to this game. '}The game ${d.start_ms > Date.now() ? 'starts' : 'started'} at ${SE.fmtTime(d.start_ms).slice(0, 5)}. Map lines: map 1 at the start time; each later map right after the previous one ended (its Polymarket market closed then, a short break later). Opens on the six hours before the start; double-click the chart for the whole history.</p>` +
        `<div class="grid g-2" style="margin-top:40px">${P.sec('balance', 'Money bought per side', '<div id="gM"></div>', '', 'Match market only: dollars spent buying each team.')}${P.sec('schedule', 'Before and during the game', '<div id="gT"></div>', '', 'Big bets placed before the start and once it was under way.')}</div>` +
        P.sec('list', `Big bets ($${d.settings.big_bet_usd}+)`, '<div id="gBets"></div>', '', 'Fills of one wallet on one outcome within 60 seconds count as one bet. Every market of the game.');
      SE.tidyHead(el);
      SE.seg($('#gScale'), [['prob', 'Win %'], ['odds', 'Decimal']], SE.pref('chartOdds'), (v) => { SE.setPref('chartOdds', v); SE.go('#/susgame/' + id); window.dispatchEvent(new HashChangeEvent('hashchange')); });
      P.set('gS', st.tier1 ? 'Tier 1' : st.score == null ? '-' : `<span class="${st.score >= a ? 'neg' : ''}">${Math.round(st.score)}</span>`, st.score >= a ? 'flagged' : `alert at ${a}`);
      P.set('gV', money(st.volume ?? Object.values(d.by_side).reduce((x, y) => x + y, 0)), d.price.length ? SE.fmtInt(d.price.length) + ' match trades' : '');
      P.set('gD', st.big_dog ?? '-', '');
      P.set('gF', st.big_fav ?? '-', '');
      P.set('gB', st.big_share == null ? '-' : Math.round(st.big_share * 100) + '%', st.drift != null ? `price drift ${Math.round(st.drift * 100)} pts` : '');

      // ---- chart: every source as home-win probability
      const lists = [], series = [];
      let books = [];
      if (d.match_id) {
        const ch = (await api(`/matches/${d.match_id}/changes?market=1x2&outcome=home&limit=50000`).catch(() => ({ changes: [] }))).changes || [];
        const by = new Map();
        for (const c of ch) {
          if (c.book === 'polymarket' || c.outcome !== 'home' || (c.book === 'kalshi' && d.kalshi)) continue;
          if (!by.has(c.book)) by.set(c.book, []);
          by.get(c.book).push([c.ts_ms, c.new_price > 1 ? 1 / c.new_price : null]);
        }
        books = [...by.keys()].sort((x, y) => (x === 'kalshi' ? -1 : y === 'kalshi' ? 1 : SE.bookName(x).localeCompare(SE.bookName(y))));
        for (const b of books) { lists.push(by.get(b)); series.push({ label: SE.bookName(b), book: b, color: SE.bookColor(b), width: b === 'kalshi' ? 2.2 : 1.3 }); }
      }
      const flip = (p) => (d.pm_swapped ? 1 - p : p);
      const pmPts = d.price.filter((p) => p[1] > 0 && p[1] < 1).map((p) => [p[0], flip(p[1])]);
      if (d.kalshi?.price?.length) { lists.unshift(d.kalshi.price.map((p) => [p[0], flip(p[1])])); series.unshift({ label: 'Kalshi', book: 'kalshi', color: SE.bookColor('kalshi'), width: 2.2 }); }
      if (pmPts.length) { lists.unshift(pmPts); series.unshift({ label: 'Polymarket', book: 'polymarket', color: SE.bookColor('polymarket'), width: 2.6, linear: true }); }
      // Big bets as dots: buying the home side at p is home at p; buying the other side at p is home at 1 - p.
      const dots = { home: [], away: [] };
      for (const b of d.bets) if (b.match_market && b.side === 'BUY') (b.outcome === d.home ? dots.home : dots.away).push([b.ts, flip(b.outcome === d.home ? b.price : 1 - b.price)]);
      const xsAll = [...new Set([...lists.flatMap((l) => l.map((p) => p[0])), ...dots.home.map((p) => p[0]), ...dots.away.map((p) => p[0])])].sort((x, y) => x - y);
      if (xsAll.length) {
        const al = SE.alignSeries(lists.length ? lists : [[]]);
        // Re-align line series onto the full time axis (dots add timestamps of their own).
        const pos = new Map(al.xs.map((t, i) => [t, i]));
        const values = series.map((_, k) => { let last = null; return xsAll.map((t) => { const i = pos.get(t); if (i != null) last = al.values[k][i]; return last; }); });
        const sparse = (pts) => { const m = new Map(pts.map((p) => [p[0], p[1]])); return xsAll.map((t) => (m.has(t) ? m.get(t) : null)); };
        const all = series.map((s, k) => ({ ...s, values: values[k] }));
        if (dots.home.length) all.push({ label: `Big bets: ${d.home}`, color: cssColor('--up'), points: true, values: sparse(dots.home) });
        if (dots.away.length) all.push({ label: `Big bets: ${d.away}`, color: cssColor('--down'), points: true, values: sparse(dots.away) });
        // Decimal odds: 1 / chance. Above 20.00 (under a 5% chance, the dying minutes of a game)
        // the line is cut so the rest of the chart stays readable.
        const dec = SE.pref('chartOdds') === 'odds';
        // Map markers: map 1 at the scheduled start (Polymarket has no real start time); map N+1
        // right after map N ended (its "Map N Winner" market closed then, a short break later).
        const ends = (d.maps || []).filter((m) => m.end_ms).sort((x, y) => x.map - y.map);
        // Kalshi lists the start time it trades to; Polymarket's is often the scheduled slot,
        // which can be an hour early. Prefer Kalshi's when the game is on Kalshi.
        const k0 = d.kalshi?.start_ms;
        const marks = [k0 ? { x: k0, label: 'Map 1 (start, Kalshi)' } : { x: d.start_ms, label: 'Map 1 (scheduled start)' }];
        for (const m of ends) if (!d.match_end_ms || m.end_ms < d.match_end_ms - 60000) marks.push({ x: m.end_ms, label: `Map ${m.map + 1} (map ${m.map} over)` });
        if (d.match_end_ms) marks.push({ x: d.match_end_ms, label: 'Final', color: cssColor('--accent') });
        if (dec) for (const sr of all) sr.values = sr.values.map((v) => (v == null || v < 0.05 ? null : 1 / v));
        chart = SE.chartKeep($('#gC'), null, xsAll, all, dec ? { yFmt: (v) => (v == null ? '' : v.toFixed(2)), marks } : { yFmt: pct, yRange: [0, 1], marks }, $('#gL'));
        // Open on the game: six hours before the start to the last price (double-click shows it all).
        const lo = Math.max(xsAll[0], d.start_ms - 6 * 3600000), hi = Math.max(xsAll[xsAll.length - 1], d.match_end_ms || 0);
        if (chart && hi > lo) chart.setScale('x', { min: lo, max: hi });
      }
      if (!chart) $('#gC').innerHTML = `<div class="empty">${ic('show_chart')}No prices recorded for this game.</div>`;

      // ---- money per side, before / during
      const tot = Object.values(d.by_side).reduce((x, y) => x + y, 0);
      $('#gM').innerHTML = Object.keys(d.by_side).length ? Object.entries(d.by_side).sort((x, y) => y[1] - x[1]).map(([k, v]) =>
        `<div class="bar-row"><span class="bar-label">${esc(k)}</span><div class="bar-track"><b style="width:${tot ? (v / tot * 100).toFixed(1) : 0}%"></b></div><span class="bar-val">${money(v)} <span class="faint">${tot ? Math.round(v / tot * 100) : 0}%</span></span></div>`).join('') : '<div class="empty">No trades on the match market.</div>';
      const pre = d.bets.filter((b) => b.ts < d.start_ms), live = d.bets.filter((b) => b.ts >= d.start_ms);
      const sum = (l) => l.reduce((x, b) => x + b.cash, 0);
      $('#gT').innerHTML = `<div class="bar-row"><span class="bar-label">Before the start</span><div class="bar-track"><b style="width:${d.bets.length ? (sum(pre) / Math.max(1, sum(d.bets)) * 100).toFixed(1) : 0}%"></b></div><span class="bar-val">${pre.length} bets · ${money(sum(pre))}</span></div>
        <div class="bar-row"><span class="bar-label">During the game</span><div class="bar-track"><b style="width:${d.bets.length ? (sum(live) / Math.max(1, sum(d.bets)) * 100).toFixed(1) : 0}%"></b></div><span class="bar-val">${live.length} bets · ${money(sum(live))}</span></div>`;
      SE.table($('#gBets'), [
        { key: 't', label: 'When', fmt: (b) => `${SE.fmtTime(b.ts).slice(0, 8)}<div class="faint">${b.ts < d.start_ms ? 'before start' : 'in game'}</div>`, sort: (b) => b.ts },
        { key: 'm', label: 'Market', fmt: (b) => esc(b.market) },
        { key: 'o', label: 'Outcome', w: 200, fmt: (b) => `<span class="chip ${b.side === 'BUY' ? 'ok' : 'bad'}">${b.side === 'BUY' ? 'Buy' : 'Sell'}</span> ${esc(b.outcome)}`, sort: (b) => b.outcome },
        { key: 'p', label: 'Price', cls: 'r', w: 80, fmt: (b) => Math.round(b.price * 100) + '%', sort: (b) => b.price },
        { key: 'c', label: 'Amount', cls: 'r', w: 100, fmt: (b) => '$' + SE.fmtInt(Math.round(b.cash)), sort: (b) => b.cash },
      ], { sort: 'c', empty: 'No big bets.' }).set(d.bets);
    })();
    return () => SE.destroyChart(chart);
  },
};

// ================================================================ Snipe window: adaptive paper bot
SE.snipeBot = (el) => {
  let chart = null, lastBank = null, bankSet = false;
  const money = (x) => (x == null ? '-' : (x < 0 ? '-$' : '$') + Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: Math.abs(x) < 100 ? 2 : 0 }));
  const pnl = (x) => (x == null ? '-' : `<b class="${x > 0 ? 'pos' : x < 0 ? 'neg' : ''}">${x > 0 ? '+' : ''}${money(x)}</b>`);
  const pct = (x, d = 1) => (x == null ? '-' : (x > 0 ? '+' : '') + (x * 100).toFixed(d) + '%');
  const give = (s) => (s < 60 ? s + ' s' : s / 60 + ' min');
  const utc = (ms) => { const d = new Date(ms); return `${String(d.getUTCDate()).padStart(2, '0')} ${d.toLocaleString('en-GB', { month: 'short', timeZone: 'UTC' })} ${String(d.getUTCHours()).padStart(2, '0')}:00 UTC`; };
  const RULES = [
    ['min_books', 'Bookmakers that moved', (v) => `${v}+ independent`],
    ['max_followed', 'Market had followed', (v) => `at most ${Math.round(v * 100)}%`],
    ['min_edge', 'Expected gain', (v) => `≥ ${Math.round(v * 100)}¢ a contract`],
    ['max_spread', 'Market spread', (v) => `≤ ${Math.round(v * 100)}¢`],
    ['min_move', 'Bookmaker move', (v) => `≥ ${Math.round(v * 100)} points`],
    ['catch', 'Sell when caught up', (v) => `${Math.round(v * 100)}% of the move`],
    ['give_up_s', 'Give up after', give],
    ['skip_after_pause', 'Right after a market pause', (v) => (v ? 'skip the move' : 'trade it')],
    ['walk_book', 'Order size', (v) => (v === false ? 'best price only' : 'up to 2¢ past the best price')],
  ];
  const rulesHtml = (p, def) => `<div class="rules">${RULES.map(([k, label, f]) => `<div class="rule ${p[k] !== def[k] ? 'changed' : ''}" ${p[k] !== def[k] ? `data-tip="${SE.esc('Default: ' + f(def[k]))}"` : ''}><span>${label}</span><b>${f(p[k])}</b></div>`).join('')}</div>`;
  const short = (p) => `${p.min_books}+ books · ≤${Math.round(p.max_followed * 100)}% followed · ≥${Math.round(p.min_edge * 100)}¢ gain · ≤${Math.round(p.max_spread * 100)}¢ spread · ≥${Math.round(p.min_move * 100)} pts · sell ${Math.round(p.catch * 100)}% · ${give(p.give_up_s)}${p.skip_after_pause ? ' · skips pauses' : ''}${p.walk_book === false ? ' · best price only' : ''}`;
  const st = (s) => (s && s.trades ? `${s.trades} trades, ${pct(s.roi)} a trade` : 'no trades');
  const bookTest = (c, b) => {
    const t = c.book_test;
    if (!t || !t.stake) return '';
    const one = (s) => (s && s.trades ? `${s.trades} trades, ${pnl(s.pnl)} (${pct(s.mean_return)} of the stake a trade)` : 'no trades yet');
    return `<p class="note"><b>Order size</b> at a ${money(t.stake)} stake, on these rules and the ${(b.depth_known || 0).toLocaleString('en-US')} moves with a recorded order book: best price only ${one(t.best_price_only)}; up to 2¢ past it ${one(t.walk_2c)}.</p>`;
  };

  el.innerHTML = `<div class="bank-bar"><label data-tip="Each market (Polymarket, Kalshi) starts with this paper balance.">Starting balance ${SE.num({ id: 'bStart', min: 100, step: 100, pre: '$', w: 110 })}</label><label data-tip="Every trade stakes this share of the balance at that moment (start plus closed P&amp;L), at most the cash not tied up in open trades.">Stake per trade ${SE.num({ id: 'bPct', min: 0.1, max: 100, step: 0.1, suf: '%', w: 86 })}</label><button class="btn" id="bSave">${ic('save')}Save</button><span class="faint">Saving re-runs the whole history with the new money (a few minutes).</span></div>` +
    `<div id="bStatus" class="page-sub" style="margin-top:0"></div>` +
    P.sec('account_balance_wallet', 'Open positions', '<div id="bPos"></div>', '', 'Paper positions not yet paid out: Polymarket × Kalshi arbitrage (auto or sent with Order) until the game ends. Snipe trades open and close within minutes and show under Paper trades.') +
    `<div id="bBases"></div>` +
    P.sec('show_chart', 'Backtest: the current rules on every move recorded', '<div class="chart" id="bChart" style="height:320px"></div><div class="legend" id="bLeg"></div>', '',
      'Every 4 hours the bot re-picks its rules from all the data, then replays the whole dataset with them from the starting balance. Dashed: the default rules replayed the same way. In-sample: the rules were chosen on this same data, so it flatters them. Scroll to zoom, Shift + scroll to move, drag to zoom into a range, double-click to reset.') +
    `<details class="fold" style="margin-top:26px"><summary>${ic('help')}How the paper bot chooses its rules</summary><div class="fold-body"><p class="note" style="margin:0">Every 4 hours (00, 04, 08, 12, 16 and 20 UTC) it tries 25,920 rule combinations per market on every finished trade so far. Measured facts are never tuned: reaction 1 s, a confirming check 0.5 s later, the order landing 0.25 s after that, 1¢ worse on every buy and sale, Kalshi's fee and the real order book. Each trade stakes the share of the balance set above; when the order book was recorded, the order only fills what rested there (before that, it fills in full). One of the tuned rules is the order size: buy only what rests at the best price, or also what rests up to 2¢ worse to fill more of the stake. Rules are scored on the stake the bot would place, so an order that fills only part of it counts for less. Each combination is scored by its average return minus twice its uncertainty, needs 30+ trades to count, and is judged together with its neighbours so a lucky spike cannot win. A candidate must also be profitable, and at least as good as the current rules, on the most recent 30% of the data before the pick. It never adopts rules whose conservative estimate is a loss: with nothing convincingly profitable it keeps the current rules. The rules only change when the new pick is clearly better. When the method changes, the whole history is recomputed from the first data, so the record always uses one method. If the last 24 hours of unseen-hours trading lost money over 8+ trades, it falls back to the default rules until the next pick. Paper money only.</p></div></details>` +
    P.sec('history', 'Re-choices', '<div id="bHist"></div>') +
    P.sec('receipt_long', 'Paper trades', '<div id="bTr"></div>', '', 'The current rules replayed over the whole dataset, newest first.') +
    P.sec('balance', 'Polymarket × Kalshi arbitrage (paper)', '<div id="bArbS"></div><div id="bArb"></div>', '',
      'Every 2 seconds: Yes on one venue plus No on the other for the same outcome. One of the two always pays, so a combined cost under $1 after Kalshi\'s fee is a locked profit. Stakes 12.5% of the starting balance; only games that pay out within 6 hours; at most 4 waiting for payout at once. Needs 1%+, fresh quotes on both, recorded order-book size on both legs (the buy is capped to it), $10+ fillable, and no gap in both directions at once (that is a data error). Not checked: that both venues settle the market the same way, and that both orders fill before the book moves.');

  async function load() {
    let d;
    try { d = await api('/adaptive'); } catch (e) { $('#bStatus').innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
    lastBank = d.bank;
    if (!bankSet && d.bank && $('#bStart')) { bankSet = true; $('#bStart').value = d.bank.start; $('#bPct').value = +(d.bank.pct * 100).toFixed(2); }
    if (!$('#bBases')) return;
    if (d.building) {
      $('#bStatus').innerHTML = `<span class="muted">Rebuilding after a restart: measured ${d.blocks_done} of ${d.blocks_total} four-hour blocks of data. The backtest appears once every block is in (a few minutes).</span>`;
      $('#bBases').innerHTML = '';
      SE.destroyChart(chart); chart = null;
      $('#bChart').innerHTML = `<div class="empty">${ic('hourglass_top')}Rebuilding: ${d.blocks_done} of ${d.blocks_total} blocks</div>`;
      $('#bLeg').innerHTML = '';
      setTimeout(load, 10000);
      return;
    }
    const ready = d.bases.some((b) => b.current);
    $('#bStatus').innerHTML = ready
      ? `Rules re-chosen every 4 hours. Last ${utc(d.bases.map((b) => b.current?.ts || 0).reduce((a, b) => Math.max(a, b), 0))}, next ${utc(d.next_run_ms)}.${d.status !== 'ready' ? ` <span class="faint">${esc(d.status)}</span>` : ''}`
      : `<span class="muted">${esc(d.status || 'Starting')}. The first pick needs 8 hours of data${d.first_run_ms ? `; due ${utc(d.first_run_ms)}` : ''}.</span>`;
    $('#bBases').innerHTML = d.bases.map((b) => {
      const c = b.current, r = b.record, dr = b.defaults_record;
      const chip = b.traded === false ? '<span class="chip bad" data-tip="Unhedged Kalshi snipes lost money (Kalshi often does not catch up, and its spread eats the sale). Kept as research only: no paper trades, no alerts.">Off: research only</span>' : !c ? '' : c.fallback ? '<span class="chip bad" data-tip="The last 24 hours of the honest record lost money, so it trades the default rules until the next pick.">Safety switch: default rules</span>' : c.switched ? '<span class="chip accent">New rules at the last pick</span>' : '<span class="chip">Rules kept at the last pick</span>';
      const ins = c?.insample?.stats, pt = c?.pause_test;
      return `<section class="sec bot-base"><div class="sec-head"><h2>${SE.book(b.base)}</h2><div class="tools">${chip}</div></div>` +
        P.stats(
          `<div class="stat key"><div class="stat-label">${ic('account_balance_wallet')}Paper balance</div><div class="stat-value">${money(b.balance)}</div><div class="stat-foot">${pnl(r.pnl)} on ${r.trades} trades, whole dataset</div></div>`,
          `<div class="stat"><div class="stat-label">${ic('trending_up')}Return per trade</div><div class="stat-value">${pct(r.roi, 2)}</div><div class="stat-foot">on ${money(r.staked)} staked</div></div>`,
          `<div class="stat"><div class="stat-label">${ic('check_circle')}Win rate</div><div class="stat-value">${r.trades ? Math.round((r.wins / r.trades) * 100) + '%' : '-'}</div><div class="stat-foot">${r.wins} of ${r.trades}</div></div>`,
          `<div class="stat" data-tip="The default rules replayed over the same whole dataset."><div class="stat-label">${ic('rule')}Default rules, whole dataset</div><div class="stat-value">${money(b.balance_defaults)}</div><div class="stat-foot">${pnl(dr.pnl)}, ${st(dr)}</div></div>`) +
        (c ? `<div class="bot-rules"><div class="note" style="margin:18px 0 10px">Current rules${c.fallback ? ' (defaults, safety switch)' : ''}, used for the backtest above. Highlighted: changed from the default.</div>${rulesHtml(c.active, b.default)}
          <p class="note" style="margin-top:12px">Picked ${utc(c.ts)} at a ${money(c.insample?.stake)} stake (${money(c.insample?.balance)} balance) from ${ins?.trades ?? 0} finished trades: ${pct(ins?.mean_return)} a trade on average, ${pct(ins?.conservative)} on the conservative estimate. ${c.candidates.toLocaleString('en-US')} moves measured.</p>
          ${bookTest(c, b)}
          <p class="note"><b>Moves right after a market pause</b> (the bookmaker suspended, then reopened at a new price), on these rules: ${st(pt?.pause_moves_alone)}. With them: ${st(pt?.with_pause_moves)}. Skipping them: ${st(pt?.skipping_them)}.</p></div>`
          : '<div class="empty">No pick yet.</div>') +
        '</section>';
    }).join('');

    // ---- chart: walk-forward P&L, adaptive vs defaults
    const colour = { polymarket: '#7aa7ff', kalshi: '#5ff0a8' };
    const lists = [], series = [];
    for (const b of d.bases) {
      if (b.curve.length) { lists.push(b.curve); series.push({ label: `${SE.bookName(b.base)}: paper bot`, book: b.base, color: colour[b.base], width: 2.2, linear: true }); }
      if (b.curve_defaults.length) { lists.push(b.curve_defaults); series.push({ label: `${SE.bookName(b.base)}: default rules`, color: colour[b.base], width: 1.3, dash: [5, 4], linear: true }); }
    }
    if (lists.length) {
      const al = SE.alignSeries(lists);
      chart = SE.chartKeep($('#bChart'), chart, al.xs, series.map((s, i) => ({ ...s, values: al.values[i] })), { yFmt: (v) => (v == null ? '' : money(v)) }, $('#bLeg'));
    } else { SE.destroyChart(chart); chart = null; $('#bChart').innerHTML = `<div class="empty">${ic('show_chart')}No trades yet.</div>`; }

    // ---- re-choices
    const hist = d.bases.flatMap((b) => b.windows.map((w) => ({ ...w, base: b.base }))).sort((a, b) => b.from - a.from || a.base.localeCompare(b.base));
    SE.table($('#bHist'), [
      { key: 't', label: 'When', w: 140, fmt: (w) => utc(w.from), sort: (w) => w.from },
      { key: 'b', label: 'Market', w: 130, fmt: (w) => SE.book(w.base, 'sm') },
      { key: 'r', label: 'Rules picked', cls: 'match', fmt: (w) => `<span class="${w.run.switched ? '' : 'muted'}">${esc(short(w.run.active))}</span>${w.run.fallback ? ' <span class="chip bad">defaults</span>' : w.run.switched ? ' <span class="chip accent">new</span>' : ''}` },
      { key: 'i', label: 'Picked from', w: 170, tip: 'Finished trades before the pick and their average return.', fmt: (w) => `<span class="muted">${st(w.run.insample?.stats)}</span>` },
      { key: 'a', label: 'Whole dataset', cls: 'r', w: 150, tip: 'These rules replayed over every move recorded, from the starting balance.', fmt: (w) => `${pnl(w.whole.pnl)}<div class="faint">${w.whole.trades} trades, ${money(w.whole.balance)}</div>`, sort: (w) => w.whole.pnl },
    ], { sort: 't', empty: 'No re-choices yet.' }).set(hist);

    // ---- trades
    const trades = d.bases.filter((b) => b.traded !== false).flatMap((b) => b.recent).sort((a, b) => b.exit_ts - a.exit_ts);
    SE.table($('#bTr'), [
      { key: 't', label: 'When', w: 130, fmt: (t) => `<span class="muted">${SE.fmtDateTime(t.ts)}</span>`, sort: (t) => t.ts },
      { key: 'b', label: 'Market', w: 120, fmt: (t) => SE.book(t.base, 'sm') },
      { key: 'm', label: 'Match', cls: 'match', fmt: (t) => P.matchCell(t.home, t.away, '', t.sport, t.match_id) },
      { key: 'o', label: 'Bet', w: 130, fmt: (t) => `${t.dir > 0 ? 'Yes' : 'No'} <b>${esc(SE.outcomeLong ? SE.outcomeLong(t.outcome) : t.outcome)}</b>${t.after_pause ? ' <span class="chip" data-tip="Right after a market pause">pause</span>' : ''}` },
      { key: 's', label: 'Stake', cls: 'r', w: 100, tip: 'Dollars actually spent; the share of the intended stake that filled is shown when the order book was recorded.', fmt: (t) => `${money(t.spent)}${t.filled != null && t.filled < 0.999 ? `<div class="faint">${Math.round(t.filled * 100)}% filled</div>` : ''}`, sort: (t) => t.spent },
      { key: 'e', label: 'Entry → exit', cls: 'r', w: 130, fmt: (t) => `${(t.entry * 100).toFixed(1)}¢ → ${(t.exit * 100).toFixed(1)}¢` },
      { key: 'x', label: 'Exit', w: 100, fmt: (t) => `<span class="muted">${esc(t.reason)}</span>` },
      { key: 'p', label: 'P&amp;L', cls: 'r', w: 100, fmt: (t) => pnl(t.pnl), sort: (t) => t.pnl },
    ], { sort: 't', empty: 'No paper trades yet.' }).set(trades);
  }
  async function loadArb() {
    let a;
    try { a = await api('/predarb'); } catch { return; }
    if (!$('#bArbS')) return;
    const rej = Object.entries(a.rejected || {}).sort((x, y) => y[1] - x[1]).map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(', ');
    $('#bArbS').innerHTML = `<p class="note" style="margin-top:0">${a.trades} paper arbitrages (${a.open} waiting for payout, ${money(a.cash)} paper cash free; each takes every book level that still pays ${(a.min_pair * 100).toFixed(1)}%+, re-checked at order time), ${money(a.staked)} staked, <b class="${a.locked_profit > 0 ? 'pos' : ''}">${money(a.locked_profit)} locked</b>. Watching ${a.watching} games listed on both. Gaps seen since start ${a.raw_gaps}${rej ? `; rejected: ${esc(rej)}` : ''}.</p>`;
    const now = Date.now();
    const open = (a.recent || []).filter((p) => (p.payout_ms || 0) > now);
    SE.table($('#bPos'), [
      { key: 't', label: 'Opened', w: 130, fmt: (p) => `<span class="muted">${SE.fmtDateTime(p.ts)}</span>${p.manual ? ' <span class="chip">Order</span>' : ''}`, sort: (p) => p.ts },
      { key: 'm', label: 'Match', cls: 'match', fmt: (p) => P.matchCell(p.home, p.away, '', p.sport, p.match_id) },
      { key: 'l', label: 'Holding', w: 260, fmt: (p) => `${Math.round(p.contracts)} × Yes ${esc(p.outcome)} at ${(p.yes * 100).toFixed(1)}¢ on ${SE.bookName(p.yes_on)}<div class="faint">${Math.round(p.contracts)} × No at ${(p.no * 100).toFixed(1)}¢ on ${SE.bookName(p.yes_on === 'kalshi' ? 'polymarket' : 'kalshi')}</div>` },
      { key: 's', label: 'Staked', cls: 'r', w: 90, fmt: (p) => money(p.cost) },
      { key: 'p', label: 'Locked profit', cls: 'r', w: 120, fmt: (p) => `${pnl(p.profit)}<div class="faint">${pct(p.profit / p.cost)}</div>` },
      { key: 'x', label: 'Pays out', w: 130, fmt: (p) => `<span class="muted">~${SE.fmtDateTime(p.payout_ms)}</span>`, sort: (p) => p.payout_ms },
      { key: 'sell', label: '', w: 90, fmt: (p) => `<button class="btn sm" data-sell="${p.ts}/${p.match_id}" data-tip="Sell both legs now at the latest bids instead of waiting for the payout.">${ic('sell')}Sell</button>` },
    ], { sort: 'x', empty: 'No open positions.' }).set(open);
    if (!$('#bPos').dataset.wired) {
      $('#bPos').dataset.wired = '1';
      $('#bPos').addEventListener('click', async (e) => {
        const b = e.target.closest('[data-sell]');
        if (!b) return;
        e.stopPropagation();
        b.disabled = true;
        try {
          const r = await api('/predarb/sell/' + b.dataset.sell, { method: 'POST' });
          if (r.ok) SE.toast(`Sold for ${money(r.position.close_value)}: ${r.position.pnl >= 0 ? '+' : '-'}${money(Math.abs(r.position.pnl))}`, 6000);
          else SE.toast(r.error, 6000);
        } catch (err) { SE.toast(err.message, 6000); }
        loadArb();
      }, true);
    }
    SE.table($('#bArb'), [
      { key: 't', label: 'When', w: 130, fmt: (t) => `<span class="muted">${SE.fmtDateTime(t.ts)}</span>`, sort: (t) => t.ts },
      { key: 'm', label: 'Match', cls: 'match', fmt: (t) => P.matchCell(t.home, t.away, '', t.sport, t.match_id) + (t.live ? ' <span class="chip">live</span>' : '') },
      { key: 'o', label: 'Outcome', w: 90, fmt: (t) => esc(t.outcome) },
      { key: 'l', label: 'Legs', w: 260, tip: 'Average prices paid. Newer positions walk both order books: book levels taken on each leg, and the return of the last pair bought (the book\'s gradient).', fmt: (t) => `Yes ${(t.yes * 100).toFixed(1)}¢ on ${SE.bookName(t.yes_on)} + No ${(t.no * 100).toFixed(1)}¢ on ${SE.bookName(t.yes_on === 'kalshi' ? 'polymarket' : 'kalshi')}${t.levels ? `<div class="faint">${t.levels[0]} + ${t.levels[1]} book levels, last pair ${pct(t.last_return)}</div>` : ''}` },
      { key: 'r', label: 'Settlement', w: 200, tip: 'Both venues\' rules compared: the result rule they share and, pre-match, what the position would be worth if the game were called off (Polymarket pays 50-50 or No, Kalshi a fair price).', fmt: (t) => (t.settle ? `<span class="muted">${esc(t.settle.result_rule)}</span>${t.settle.void_pnl != null ? `<div class="faint">if called off ${pnl(t.settle.void_pnl)}</div>` : ''}` : '<span class="faint">not checked</span>') },
      { key: 's', label: 'Staked', cls: 'r', w: 90, fmt: (t) => `${money(t.cost)}${t.fees != null ? `<div class="faint">fees ${money(t.fees)}</div>` : ''}` },
      { key: 'p', label: 'Result', cls: 'r', w: 120, fmt: (t) => (t.closed_ms ? `${pnl(t.pnl)}<div class="faint">sold early</div>` : `${pnl(t.profit)}<div class="faint">${pct(t.profit / t.cost)} locked</div>`), sort: (t) => t.pnl ?? t.profit },
    ], { sort: 't', empty: 'No arbitrage taken yet.' }).set(a.recent);
  }
  loadArb();
  SE.every(15000, loadArb);
  $('#bSave').addEventListener('click', async () => {
    const start = +$('#bStart').value, share = +$('#bPct').value / 100;
    try {
      const r = await api('/adaptive/settings', { body: { start, pct: share } });
      SE.toast(r.recomputing ? 'Saved. Re-running the history with the new balance' : 'Saved');
      load();
    } catch (e) { SE.toast(e.message, 5000); }
  });
  load();
  SE.every(60000, load);
};

// ================================================================ Data
SE.pages.data = {
  title: 'Data',
  render(el) {
    el.innerHTML = P.head('Data', `<span class="muted" id="dAt"></span><button class="btn" id="dRe">${ic('refresh')}Recount</button>`) +
      P.stats(P.stat('dT', 'hard_drive', 'On disk', 'The database plus its write-ahead log.', true), P.stat('dC', 'swap_vert', 'Price changes'), P.stat('dP', 'candlestick_chart', 'Prediction-market quotes', 'Polymarket and Kalshi top-of-book rows.'), P.stat('dS', 'date_range', 'Recorded span')) +
      '<p class="note" id="dClean" style="margin-top:12px"></p>' +
      P.sec('table_chart', 'Tables', '<div id="dTab"></div>', '', "Size includes each table's indexes. Click a table to see its rows.") +
      P.sec('data_object', 'Raw rows', `<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px"><div id="dSel"></div><input class="input mono" id="dW" placeholder="Filter, e.g. book = 'unibet' AND match_id = 739" style="flex:1;min-width:260px"><button class="btn" id="dPrev">${ic('chevron_left')}Newer</button><button class="btn" id="dNext">Older${ic('chevron_right')}</button><span class="muted" id="dPg"></span></div><div id="dRaw"></div>`) +
      P.sec('merge', "Merge another machine's data", '<div id="dJob"></div><div id="dFiles"></div>', `<button class="btn" id="dScan">${ic('search')}Scan again</button><button class="btn primary" id="dExp">${ic('ios_share')}Export a snapshot</button>`,
        "Each machine numbers games itself; merging maps them onto this database's games. Where both machines recorded the same source at the same time, this database's rows are kept, so merging twice adds nothing.") +
      `<p class="note" style="margin-top:12px">To combine the Mac and the PC: press <b>Export a snapshot</b> on one machine (a consistent copy goes into its <span class="mono">export</span> folder), copy that file into the other machine's <span class="mono">import</span> folder next to its database (or any folder listed below), then press <b>Merge</b> there. Copy a snapshot rather than the live database file, which may be mid-write.</p>` +
      P.sec('folder', 'Files', '<div id="dF"></div>');
    let stats = null, table = SE.pref('dataTable') || 'changes', page = 0;
    const PAGE = 50;
    const span = (a, b) => (a && b ? `${SE.fmtDateTime(a)} <span class="faint">→</span> ${SE.fmtDateTime(b)}` : '<span class="faint">-</span>');
    async function loadStats(fresh) {
      $('#dAt').textContent = 'Counting';
      stats = await api('/data' + (fresh ? '?fresh=true' : ''));
      if (!$('#dAt')) return;
      $('#dAt').textContent = 'Counted ' + SE.fmtAgo(stats.computed_ms) + ' · ' + stats.host;
      const cl = stats.cleanup, last = cl?.last;
      $('#dClean').innerHTML = cl ? `<b>Cleanup</b> every 4 hours: deletes the price history of games never listed on Polymarket or Kalshi, 3 hours after the last source listed them. ` +
        (last?.error ? `<span class="err">Last run failed: ${esc(last.error)}</span> ` : last?.finished_ms ? `Last run ${SE.fmtAgo(last.finished_ms)}: ${SE.fmtInt(last.rows)} rows of ${SE.fmtInt(last.games)} games deleted. ` : 'First run 2 minutes after start. ') +
        `Next ${SE.fmtDateTime(cl.next_ms)}.` : '';
      const total = stats.files.filter((f) => ['Database', 'Write-ahead log', 'Shared memory'].includes(f.name)).reduce((a, f) => a + f.bytes, 0);
      const T = Object.fromEntries(stats.tables.map((t) => [t.table, t]));
      P.set('dT', SE.fmtBytes(total), esc(stats.db_path));
      P.set('dC', SE.fmtInt(T.changes?.rows), T.changes ? SE.fmtBytes(T.changes.bytes) : '');
      P.set('dP', SE.fmtInt((T.pm_quotes?.rows || 0) + (T.kalshi_quotes?.rows || 0)), SE.fmtBytes((T.pm_quotes?.bytes || 0) + (T.kalshi_quotes?.bytes || 0)));
      P.set('dS', T.changes?.from_ms ? SE.fmtDur(T.changes.to_ms - T.changes.from_ms) : '-', T.changes?.from_ms ? 'since ' + SE.fmtDateTime(T.changes.from_ms) : '');
      const max = Math.max(1, ...stats.tables.map((t) => t.bytes));
      SE.table($('#dTab'), [
        { key: 't', label: 'Table', fmt: (t) => `<b class="mono">${esc(t.table)}</b>`, sort: (t) => t.table },
        { key: 'r', label: 'Rows', cls: 'r', w: 120, fmt: (t) => SE.fmtInt(t.rows), sort: (t) => t.rows },
        { key: 'b', label: 'Size', w: 240, fmt: (t) => `<div style="display:flex;align-items:center;gap:8px"><div style="flex:1;height:6px;background:var(--bg-3);border-radius:3px;overflow:hidden"><b style="display:block;height:100%;width:${(t.bytes / max * 100).toFixed(1)}%;background:var(--accent)"></b></div><span style="min-width:70px;text-align:right">${SE.fmtBytes(t.bytes)}</span></div>`, sort: (t) => t.bytes },
        { key: 'i', label: 'Indexes', cls: 'r', w: 100, fmt: (t) => `<span class="muted">${SE.fmtBytes(t.index_bytes)}</span>`, sort: (t) => t.index_bytes },
        { key: 'p', label: 'Per row', cls: 'r', w: 90, tip: 'Average bytes per row, indexes included.', fmt: (t) => (t.rows ? Math.round(t.bytes / t.rows) + ' B' : '-'), sort: (t) => (t.rows ? t.bytes / t.rows : 0) },
        { key: 's', label: 'Covers', w: 330, fmt: (t) => span(t.from_ms, t.to_ms), sort: (t) => t.from_ms },
      ], { sort: 'b', onClick: (t) => { table = t.table; page = 0; SE.setPref('dataTable', table); drawSel(); loadRaw(); $('#dRaw').scrollIntoView({ behavior: 'smooth', block: 'start' }); } }).set(stats.tables);
      $('#dF').innerHTML = `<table class="tbl compact"><tbody>${stats.files.map((f) => `<tr><td>${esc(f.name)}</td><td class="mono faint" style="white-space:normal;word-break:break-all">${esc(f.path)}</td><td class="r">${SE.fmtBytes(f.bytes)}</td></tr>`).join('')}</tbody></table>`;
      drawSel();
    }
    function drawSel() {
      if (!stats || !$('#dSel')) return;
      SE.select($('#dSel'), { options: stats.tables.map((t) => [t.table, t.table]), value: table, onChange: (v) => { table = v; page = 0; SE.setPref('dataTable', table); loadRaw(); } });
    }
    async function loadRaw() {
      const w = $('#dW').value.trim();
      const sql = `SELECT * FROM "${table.replace(/"/g, '')}"${w ? ' WHERE ' + w : ''} ORDER BY rowid DESC LIMIT ${PAGE} OFFSET ${page * PAGE}`;
      $('#dPg').textContent = `rows ${page * PAGE + 1}-${(page + 1) * PAGE}, newest first`;
      let rows;
      try { rows = await SE.sql(sql, PAGE); } catch (e) { $('#dRaw').innerHTML = `<div class="empty err">${ic('error')}${esc(e.message)}</div>`; return; }
      if (!rows.length) { $('#dRaw').innerHTML = `<div class="empty">${ic('inbox')}No rows.</div>`; return; }
      const cols = Object.keys(rows[0]);
      const cell = (k, v) => {
        if (v == null) return '<span class="faint">null</span>';
        if (typeof v === 'number' && /(_ms|^ts)$/.test(k) && v > 1e12) return `<span data-tip="${v}">${SE.fmtTime(v, true)}</span>`;
        const s = String(v);
        return s.length > 80 ? `<span data-tip="${esc(s.slice(0, 600))}">${esc(s.slice(0, 80))}…</span>` : esc(s);
      };
      $('#dRaw').innerHTML = `<div class="scroll" style="max-height:520px"><table class="tbl compact raw"><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td class="mono">${cell(c, r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    }
    async function loadFiles() {
      const r = await api('/data/scan');
      if (!$('#dFiles')) return;
      const folders = r.folders || [];
      $('#dFiles').innerHTML = `<div class="field" style="grid-template-columns:150px minmax(0,1fr)"><label data-tip="Besides the database folder and its import and export folders.">Also scan</label><div style="display:flex;gap:8px"><input class="input mono" id="dFold" placeholder="Folder path; several separated by ;" value="${esc(folders.join('; '))}"><button class="btn" id="dFoldS">${ic('save')}Save</button></div></div><div id="dFT" style="margin-top:12px"></div>`;
      $('#dFoldS').addEventListener('click', async () => {
        await api('/data/folders', { body: { folders: $('#dFold').value.split(';').map((x) => x.trim()).filter(Boolean) } });
        SE.toast('Saved'); loadFiles();
      });
      SE.table($('#dFT'), [
        { key: 'p', label: 'Dataset', cls: 'match', fmt: (f) => `<div class="mono" style="word-break:break-all">${esc(f.path.split(/[\\/]/).pop())}</div><div class="faint mono" style="word-break:break-all">${esc(f.folder)}</div>` },
        { key: 'b', label: 'Size', cls: 'r', w: 90, fmt: (f) => SE.fmtBytes(f.bytes), sort: (f) => f.bytes },
        { key: 's', label: 'Covers', w: 300, fmt: (f) => (f.swifteye ? span(f.info.from_ms, f.info.to_ms) + `<div class="faint">${SE.fmtInt(f.info.changes)} price changes · ${SE.fmtInt(f.info.pm_quotes + f.info.kalshi_quotes)} PM / Kalshi quotes</div>` : `<span class="faint">${esc(f.error || 'not a Swifteye database')}</span>`) },
        { key: 'm', label: 'Status', w: 150, fmt: (f) => (f.merged_ms ? `<span class="chip ok">Merged</span><div class="faint">${SE.fmtAgo(f.merged_ms)}${f.changed_since_merge ? ', file changed since' : ''}</div>` : f.swifteye ? '<span class="chip">Not merged</span>' : '') },
        { key: 'a', label: '', w: 110, fmt: (f) => (f.swifteye ? `<button class="btn ${f.merged_ms && !f.changed_since_merge ? '' : 'primary'}" data-merge="${esc(f.path)}">${ic('merge')}Merge</button>` : '') },
      ], { empty: 'No other datasets found. Put a snapshot from the other machine in the import folder next to this database, or add its folder above.', emptyIcon: 'merge' }).set(r.files);
      $$('[data-merge]').forEach((b) => b.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm(`Merge ${b.dataset.merge} into this database?\n\nRows are added; nothing here is deleted. Large files take several minutes; recording continues meanwhile.`)) return;
        try { await api('/data/merge', { body: { path: b.dataset.merge } }); pollJob(); } catch (err) { SE.toast(err.message); }
      }));
    }
    let polling = false, refreshedFor = 0;
    async function pollJob() {
      const j = await api('/data/job');
      const box = $('#dJob');
      if (!box) return;
      if (!j.kind) { box.innerHTML = ''; return; }
      const pct = j.total > 0 ? Math.min(100, (j.done / j.total) * 100) : null;
      const fmtV = (v) => (typeof v === 'object' && v ? Object.entries(v).map(([a, b]) => `${a.replace(/_/g, ' ')} ${typeof b === 'number' ? SE.fmtInt(b) : esc(b)}`).join(' · ') : SE.fmtInt(v) + ' rows');
      const res = j.result && typeof j.result === 'object' && Object.keys(j.result).length ? Object.entries(j.result).map(([k, v]) => `<div><b class="mono">${esc(k)}</b> <span class="muted">${fmtV(v)}</span></div>`).join('') : '';
      box.innerHTML = `<div class="tg-msg" style="margin-bottom:14px"><div><b>${j.kind === 'merge' ? 'Merge' : 'Export'}</b> <span class="mono faint">${esc(j.path)}</span></div>
        <div style="margin:6px 0">${j.running ? `${esc(j.stage)}${pct != null ? ` · ${pct.toFixed(0)}%` : ''}` : j.error ? `<span class="err">Failed: ${esc(j.error)}</span>` : `<span class="up">Done</span> ${SE.fmtAgo(j.finished_ms)}`}</div>
        ${pct != null && j.running ? `<div style="height:6px;background:var(--bg-3);border-radius:3px;overflow:hidden"><b style="display:block;height:100%;width:${pct}%;background:var(--accent)"></b></div>` : ''}${res}</div>`;
      if (j.running && !polling) { polling = true; setTimeout(() => { polling = false; pollJob(); }, 1500); }
      else if (!j.running && j.finished_ms && refreshedFor !== j.finished_ms) { refreshedFor = j.finished_ms; loadFiles(); loadStats(true); }
    }
    $('#dRe').addEventListener('click', () => loadStats(true));
    $('#dScan').addEventListener('click', loadFiles);
    $('#dExp').addEventListener('click', async () => {
      try { const r = await api('/data/export', { method: 'POST' }); SE.toast('Exporting to ' + r.path); pollJob(); } catch (e) { SE.toast(e.message); }
    });
    $('#dPrev').addEventListener('click', () => { page = Math.max(0, page - 1); loadRaw(); });
    $('#dNext').addEventListener('click', () => { page += 1; loadRaw(); });
    $('#dW').addEventListener('keydown', (e) => { if (e.key === 'Enter') { page = 0; loadRaw(); } });
    loadStats(false).then(loadRaw);
    loadFiles();
    pollJob();
  },
};

// ================================================================ site search
(function search() {
  const inp = () => $('#gSearch'), pop = () => $('#gPop');
  let items = [], sel = -1, seq = 0;
  const pages = () => $$('.nav a[data-route]').map((a) => ({ label: a.textContent.trim(), href: a.getAttribute('href') }));
  function render() {
    const p = pop();
    if (!items.length) { p.classList.remove('show'); return; }
    let group = '';
    p.innerHTML = items.map((it, i) => {
      const head = it.group !== group ? `<div class="sp-group">${(group = it.group)}</div>` : '';
      return `${head}<a href="${esc(it.href)}" class="sp-item ${i === sel ? 'on' : ''}" data-i="${i}">${it.html}</a>`;
    }).join('');
    p.classList.add('show');
    $$('.sp-item', p).forEach((a) => a.addEventListener('mousedown', (e) => { e.preventDefault(); open(items[+a.dataset.i]); }));
  }
  function open(it) {
    if (!it) return;
    if (it.query != null) SE.pendingQuery = it.query;
    pop().classList.remove('show');
    inp().blur();
    if (location.hash === it.href) window.dispatchEvent(new HashChangeEvent('hashchange')); else location.hash = it.href;
  }
  const run = SE.debounce(async () => {
    const q = inp().value.trim();
    const my = ++seq;
    if (!q) { items = []; render(); return; }
    const ql = q.toLowerCase();
    const pg = pages().filter((p) => p.label.toLowerCase().includes(ql)).slice(0, 5).map((p) => ({ group: 'Pages', href: p.href, html: `${ic('arrow_forward')}${esc(p.label)}` }));
    items = [...pg];
    sel = items.length ? 0 : -1;
    render();
    if (q.length < 2) return;
    let r;
    try { r = await api('/search?q=' + encodeURIComponent(q)); } catch { return; }
    if (my !== seq) return;
    const ms = r.matches.map((m) => ({
      group: 'Matches', href: '#/match/' + m.id,
      html: `${SE.sportIcon(m.sport)}<span class="sp-main">${SE.vs(m.home, m.away)}<span class="faint"> · ${esc(m.league || '')}</span></span><span class="sp-side">${m.live ? '<span class="chip ok">Live</span>' : m.start_ms ? `<span class="faint">${SE.fmtKick(m.start_ms)}</span>` : ''}</span>`,
    }));
    const ls = r.leagues.map((l) => ({ group: 'Leagues', href: '#/live', query: l.league, html: `${SE.sportIcon(l.sport)}<span class="sp-main">${esc(l.league)}</span><span class="sp-side faint">${l.matches} matches</span>` }));
    items = [...pg, ...ms, ...ls];
    sel = items.length ? Math.min(Math.max(sel, 0), items.length - 1) : -1;
    render();
  }, 150);
  window.addEventListener('DOMContentLoaded', () => {
    const i = inp();
    if (!i) return;
    i.addEventListener('input', run);
    i.addEventListener('focus', () => { if (i.value.trim()) run(); });
    i.addEventListener('blur', () => setTimeout(() => pop().classList.remove('show'), 120));
    i.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (items.length) { sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; render(); } }
      else if (e.key === 'Enter') { e.preventDefault(); open(items[sel] || items[0]); }
      else if (e.key === 'Escape') { i.value = ''; items = []; render(); i.blur(); }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && !(e.target instanceof Element && e.target.closest('input, textarea, select, [contenteditable]'))) { e.preventDefault(); i.focus(); i.select(); }
    });
  });
})();
})();
