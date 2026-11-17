// Swifteye pages, part 2: Live matches, Match, Sheet, Charts.
'use strict';
(() => {
const { $, $$, esc, ic, api, P } = SE;
const PM = P.PM, G = P.G;

// ================================================================ Live matches
SE.pages.live = {
  title: 'Live matches',
  render(el) {
    el.innerHTML = P.head('Live matches', `<span class="search">${ic('search')}<input class="input" id="lQ" placeholder="Team or league"></span><div id="lF"></div>`) + '<div id="lT"></div>';
    let filter = 'all';
    SE.seg($('#lF'), [['all', 'All'], ['pm', 'On Polymarket'], ['multi', '3+ sources']], filter, (v) => { filter = v; load(); });
    const t = SE.table($('#lT'), [
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league), sort: (r) => r.home },
      { key: 's', label: 'Score', fmt: (r) => `<b>${esc(r.score || '')}</b> <span class="faint">${esc(r.clock || '')}</span>`, sort: (r) => r.score },
      { key: 'n', label: 'Sources', tip: 'Sources listing this match. Hover a logo for its speed ranking.', fmt: (r) => SE.logos(r.sum.books), sort: (r) => r.sum.n },
      { key: 'h', label: 'Best 1', cls: 'r', tip: 'Highest home-win price among bookmakers.', fmt: (r) => P.bestCell(r.sum.best.home, r, 'home'), sort: (r) => r.sum.best.home?.price },
      { key: 'd', label: 'Best X', cls: 'r', tip: 'Highest draw price among bookmakers.', fmt: (r) => P.bestCell(r.sum.best.draw, r, 'draw'), sort: (r) => r.sum.best.draw?.price },
      { key: 'a', label: 'Best 2', cls: 'r', tip: 'Highest away-win price among bookmakers.', fmt: (r) => P.bestCell(r.sum.best.away, r, 'away'), sort: (r) => r.sum.best.away?.price },
      { key: 'p', label: 'Polymarket', cls: 'r', tip: 'Polymarket 1, X and 2 as odds from the mid price.', fmt: (r) => (r.sum.pm ? `<span class="trio">${['home', 'draw', 'away'].map((o) => `<span ${SE.qattr({ b: PM, h: r.home, a: r.away, m: '1x2', o, p: r.sum.pm[o] })}>${SE.fmtOdds(r.sum.pm[o]) || '-'}</span>`).join('')}</span>` : '<span class="faint">-</span>') },
      { key: 'g', label: 'Largest gap', cls: 'r', tip: G.gap, fmt: (r) => (r.sum.gap == null ? '<span class="faint">-</span>' : `<span class="${Math.abs(r.sum.gap) >= 0.03 ? (r.sum.gap < 0 ? 'up' : 'down') : 'muted'}">${SE.fmtPts(r.sum.gap)} pts</span> <span class="faint">${SE.outcomeLabel(r.sum.gapSide)}</span>`), sort: (r) => (r.sum.gap == null ? null : Math.abs(r.sum.gap)) },
      { key: 'u', label: 'Last change', cls: 'r', fmt: (r) => `<span class="muted">${SE.fmtAgo(r.last_change_ms)}</span>`, sort: (r) => r.last_change_ms },
    ], { sort: 'n', empty: 'No live football right now. Matches appear as soon as a source lists them.', emptyIcon: 'sports_soccer', onClick: (r) => SE.go('#/match/' + r.id) });
    async function load() {
      const b = await api('/board?market=1x2&min_books=1');
      const q = $('#lQ').value.trim().toLowerCase();
      let rows = b.rows.map((r) => ({ ...r, sum: P.summarise(r) }));
      if (q) rows = rows.filter((r) => `${r.home} ${r.away} ${r.league}`.toLowerCase().includes(q));
      if (filter === 'pm') rows = rows.filter((r) => r.sum.hasPm);
      if (filter === 'multi') rows = rows.filter((r) => r.sum.n >= 3);
      t.set(rows);
    }
    $('#lQ').addEventListener('input', SE.debounce(load, 150));
    load();
    SE.every(3000, load);
  },
};

// ================================================================ Match
SE.pages.match = {
  title: 'Match',
  render(el, [idStr]) {
    const id = +idStr;
    el.innerHTML = `<div id="mHead"></div><div class="tabs" id="mTabs">
        <button data-t="prices" class="on">${ic('format_list_numbered')}Prices</button>
        <button data-t="moves">${ic('timeline')}Who moved first</button>
        <button data-t="chart">${ic('monitoring')}Chart</button>
        <button data-t="lines">${ic('stacked_bar_chart')}Lines</button>
        <button data-t="timeline">${ic('history')}Changes</button>
      </div><div id="mBody"></div>`;
    let tab = 'prices', detail = null, chart = null, changes = [];
    const cs = { outcome: 'home', scale: 'prob' };
    $$('#mTabs button').forEach((b) => b.addEventListener('click', () => { tab = b.dataset.t; $$('#mTabs button').forEach((x) => x.classList.toggle('on', x === b)); draw(true); }));

    async function loadDetail() {
      detail = await api('/matches/' + id).catch(() => null);
      if (!detail) { el.innerHTML = `<div class="empty">${ic('search_off')}This match is no longer in memory. Its history is still in Query.</div>`; return false; }
      const m = detail.match || {}, ev = detail.books[0]?.event || {};
      const score = detail.books.map((b) => b.event.score).find(Boolean) || '';
      const clock = detail.books.map((b) => b.event.clock).find(Boolean) || '';
      document.title = `${m.home || ev.home} v ${m.away || ev.away} | Swifteye`;
      $('#mHead').innerHTML = `<a class="back" href="#/live">${ic('chevron_left')}Live matches</a>
        <div class="hero"><div class="team">${esc(m.home || ev.home)}</div><div class="score"><b>${esc(score || 'v')}</b><div>${esc(clock)}${clock ? '<span class="faint">   </span>' : ''}${esc(m.league || ev.league || '')}</div></div><div class="team away">${esc(m.away || ev.away)}</div></div>`;
      return true;
    }
    const quotesOf = (b, market) => { const o = {}; for (const q of b.quotes) if (q.market === market) o[(q.line ?? '') + '|' + q.outcome] = q; return o; };
    const qcell = (b, q, extra = '') => {
      if (!q) return '<span class="faint">-</span>';
      const m = detail.match || {};
      const dir = q.prev == null || q.price == null ? '' : q.price < q.prev ? ic('arrow_upward', 'up') : q.price > q.prev ? ic('arrow_downward', 'down') : '';
      return `<span ${SE.qattr({ b, h: m.home, a: m.away, m: q.market, l: q.line, o: q.outcome, p: q.price, pv: q.prev, u: q.updated_ms, bt: q.book_ts_ms })} style="${extra}">${q.price == null ? '<span class="faint">off</span>' : dir + SE.fmtOdds(q.price)}</span>`;
    };

    function draw(force) {
      if (!detail) return;
      const body = $('#mBody');
      if (tab === 'prices') drawPrices(body);
      else if (tab === 'lines') drawLines(body);
      else if (!force) return;
      else if (tab === 'chart') drawChart(body);
      else if (tab === 'timeline') drawTimeline(body);
      else if (tab === 'moves') drawMoves(body);
    }
    function drawPrices(body) {
      if (!detail.books.length) { body.innerHTML = `<div class="empty">${ic('sports_score')}No source lists this match any more, so it has probably finished. Its history is under Chart, Changes and Who moved first.</div>`; return; }
      const rows = detail.books.map((b) => {
        const q = quotesOf(b, '1x2');
        const p = { home: q['|home']?.price, draw: q['|draw']?.price, away: q['|away']?.price };
        return { book: b.book, q, p, f: P.fair(p), upd: Math.max(0, ...b.quotes.filter((x) => x.market === '1x2').map((x) => x.updated_ms)) };
      });
      const fairs = rows.filter((r) => r.book !== PM && r.f).map((r) => r.f);
      const cons = fairs.length ? ['home', 'draw', 'away'].map((o) => SE.median(fairs.map((f) => f[o]))) : null;
      const best = {};
      for (const r of rows) if (r.book !== PM) for (const o of ['home', 'draw', 'away']) if (r.p[o] && (!best[o] || r.p[o] > best[o])) best[o] = r.p[o];
      const c = (r, o) => qcell(r.book, r.q['|' + o], r.book !== PM && best[o] === r.p[o] ? 'color:var(--accent);font-weight:650' : '');
      body.innerHTML = `<div class="grid g-main">
        ${P.sec('format_list_numbered', 'Full time result', `<table class="tbl"><thead><tr><th>Source</th><th class="r">1</th><th class="r">X</th><th class="r">2</th><th class="r" data-tip="${esc(G.margin)}">Margin</th><th class="r">Updated</th></tr></thead><tbody>${
          rows.map((r) => `<tr><td>${SE.book(r.book)}</td><td class="r">${c(r, 'home')}</td><td class="r">${c(r, 'draw')}</td><td class="r">${c(r, 'away')}</td><td class="r muted">${r.f ? (r.f.margin * 100).toFixed(1) + '%' : ''}</td><td class="r muted">${SE.fmtAgo(r.upd)}</td></tr>`).join('')
        }${cons ? `<tr data-tip="${esc(G.consensus)}"><td><b>Fair price</b></td>${cons.map((x) => `<td class="r"><b>${SE.fmtOdds(1 / x)}</b></td>`).join('')}<td class="r faint">0%</td><td></td></tr>` : ''}</tbody></table>`, '', 'Best bookmaker price in teal. Hover a price for its details and the source\'s speed.')}
        ${P.sec('donut_large', 'Probability by source', cons ? ['home', 'draw', 'away'].map((o, i) => `<div style="margin-bottom:18px"><div style="display:flex;justify-content:space-between;margin-bottom:4px"><b>${SE.outcomeLong(o)}</b><span class="muted" data-tip="${esc(G.consensus)}">fair ${SE.fmtProb(cons[i])}</span></div>${rows.filter((r) => r.f).map((r) => `<div class="race-row" style="grid-template-columns:130px minmax(0,1fr) 54px;padding:6px 0;border:0">${SE.book(r.book, 'sm')}<div class="bar"><b style="width:${(r.f[o] * 100).toFixed(1)}%;background:${SE.bookColor(r.book)}"></b></div><span class="r">${SE.fmtProb(r.f[o])}</span></div>`).join('')}</div>`).join('') : '<div class="empty">No complete 1X2 prices yet.</div>', '', 'Each source\'s probability with its margin removed.')}
      </div>`;
    }
    function drawLines(body) {
      const block = (market, outs, title, icon) => {
        const lines = new Set();
        for (const b of detail.books) for (const q of b.quotes) if (q.market === market) lines.add(q.line);
        const ls = [...lines].sort((a, b) => a - b);
        const books = detail.books.filter((b) => b.quotes.some((q) => q.market === market));
        if (!ls.length) return P.sec(icon, title, '<div class="empty">No source lists this market for this match.</div>');
        return P.sec(icon, title, `<div class="scroll"><table class="tbl pivot"><thead><tr><th>Line</th>${books.map((b) => `<th class="r">${SE.book(b.book, 'sm')}</th>`).join('')}</tr></thead><tbody>${ls.map((l) => `<tr><td><b>${market === 'handicap' && l > 0 ? '+' + l : l}</b></td>${books.map((b) => {
          const q = quotesOf(b, market);
          return `<td class="r">${qcell(b.book, q[l + '|' + outs[0]])} <span class="faint">/</span> ${qcell(b.book, q[l + '|' + outs[1]])}</td>`;
        }).join('')}</tr>`).join('')}</tbody></table></div>`, `<span class="faint">${outs.map((o) => SE.outcomeLong(o)).join(' / ')}</span>`);
      };
      body.innerHTML = block('total', ['over', 'under'], 'Total goals', 'functions') + block('handicap', ['home', 'away'], 'Handicap, home line', 'balance');
    }
    async function drawMoves(body) {
      body.innerHTML = P.sec('timeline', 'Who moved first', '<div id="mEv"><div class="empty">Loading</div></div>', '', 'Every move several sources made on this match, with each source placed where it crossed halfway.');
      const r = await api(`/rankings?since=0&match=${id}&min_jump=${SE.pref('minJump')}&window_s=${SE.pref('windowS')}&limit=500`);
      const ev = r.rankings.events.filter((e) => e.match_id === id).reverse();
      $('#mEv').innerHTML = ev.length ? ev.map((e) => P.eventRow(e)).join('') : '<div class="empty">No shared moves on this match yet.</div>';
    }
    function seriesFor() {
      const by = new Map();
      for (const c of changes) {
        if (c.outcome !== cs.outcome) continue;
        if (!by.has(c.book)) by.set(c.book, []);
        by.get(c.book).push([c.ts_ms, c.new_price == null ? null : cs.scale === 'prob' ? 1 / c.new_price : c.new_price]);
      }
      const books = [...by.keys()].sort((a, b) => (a === PM ? -1 : b === PM ? 1 : a.localeCompare(b)));
      const al = SE.alignSeries(books.map((b) => by.get(b)));
      return { xs: al.xs, series: books.map((b, i) => ({ label: SE.bookName(b), book: b, color: SE.bookColor(b), values: al.values[i], width: b === PM ? 2.6 : 1.4 })) };
    }
    function drawChart(body) {
      body.innerHTML = P.sec('monitoring', '1X2 over time', '<div class="chart" id="mChart" style="height:400px"></div><div class="legend" id="mLegend"></div>', '<div id="cO"></div><div id="cS"></div>', 'Drag across the chart to zoom, double-click to reset. Click a legend entry to hide it.');
      SE.seg($('#cO'), [['home', '1'], ['draw', 'X'], ['away', '2']], cs.outcome, (v) => { cs.outcome = v; renderChart(); });
      SE.seg($('#cS'), [['prob', 'Probability'], ['odds', 'Odds']], cs.scale, (v) => { cs.scale = v; renderChart(); });
      renderChart();
    }
    function renderChart() {
      if (!$('#mChart')) return;
      const { xs, series } = seriesFor();
      SE.destroyChart(chart);
      chart = null;
      if (!xs.length) { $('#mChart').innerHTML = `<div class="empty">${ic('show_chart')}No 1X2 history recorded for this match yet.</div>`; return; }
      chart = SE.chart($('#mChart'), xs, series, { yFmt: cs.scale === 'prob' ? (v) => (v == null ? '' : (v * 100).toFixed(0) + '%') : (v) => (v == null ? '' : v.toFixed(2)) });
      SE.legend($('#mLegend'), series, chart);
    }
    async function drawTimeline(body) {
      body.innerHTML = P.sec('history', 'Every price change', '<div id="tT"></div>', '<div id="tM"></div><button class="btn" id="tCsv">' + ic('download') + 'CSV</button>');
      let mk = '1x2';
      const m = detail.match || {};
      const t = SE.table($('#tT'), [
        { key: 'ts', label: 'Received', tip: 'When Swifteye received the change, clock-corrected.', fmt: (r) => `<span class="mono">${SE.fmtTime(r.ts_ms, true)}</span>`, sort: (r) => r.ts_ms },
        { key: 'b', label: 'Source', fmt: (r) => SE.book(r.book), sort: (r) => r.book },
        { key: 'sel', label: 'Selection', fmt: (r) => `${esc(SE.marketLabel(r.market))} <b>${esc(SE.outcomeLabel(r.outcome, r.line))}</b>` },
        { key: 'mv', label: 'Price', cls: 'r', fmt: (r) => `<span ${SE.qattr({ b: r.book, h: m.home, a: m.away, m: r.market, l: r.line, o: r.outcome, p: r.new_price, pv: r.old_price, u: r.ts_ms, bt: r.book_ts_ms })}>${SE.move(r.old_price, r.new_price)}</span>` },
        { key: 'k', label: 'Kind', fmt: (r) => `<span class="muted">${esc(r.kind)}</span>`, sort: (r) => r.kind },
        { key: 'bt', label: 'Feed delay', cls: 'r', tip: G.feed, fmt: (r) => (r.book_ts_ms ? `<span class="muted">${SE.fmtSpan(r.ts_ms - r.book_ts_ms)}</span>` : '<span class="faint">-</span>'), sort: (r) => (r.book_ts_ms ? r.ts_ms - r.book_ts_ms : null) },
      ], { sort: 'ts', maxHeight: 'calc(100vh - 360px)', limit: 3000, empty: 'No changes recorded.' });
      const load = async () => { const r = await api(`/matches/${id}/changes?limit=20000${mk === 'all' ? '' : '&market=' + mk}`); t.set(r.changes); };
      SE.seg($('#tM'), [['1x2', '1X2'], ['total', 'Totals'], ['handicap', 'Handicap'], ['all', 'All']], mk, (v) => { mk = v; load(); });
      $('#tCsv').addEventListener('click', () => SE.csv(`match-${id}-changes.csv`, ['ts_ms', 'book', 'market', 'line', 'outcome', 'kind', 'old_price', 'new_price', 'book_ts_ms'], t.rows().map((r) => [r.ts_ms, r.book, r.market, r.line, r.outcome, r.kind, r.old_price, r.new_price, r.book_ts_ms])));
      load();
    }

    const redrawChart = SE.debounce(renderChart, 800);
    const off = SE.onChange((c) => { if (c.match_id === id && c.market === '1x2') { changes.push(c); if (tab === 'chart') redrawChart(); } });
    (async () => {
      if (!(await loadDetail())) return;
      changes = (await api(`/matches/${id}/changes?market=1x2&limit=50000`)).changes;
      draw(true);
    })();
    SE.every(2500, async () => { if (await loadDetail()) if (tab === 'prices' || tab === 'lines') draw(false); });
    return () => { off(); SE.destroyChart(chart); };
  },
};

// ================================================================ Sheet
// Grouped by match (expand to see every selection by source), or one flat row per price.
SE.pages.sheet = {
  title: 'Sheet',
  render(el) {
    el.innerHTML = P.head('Sheet', `<span class="search">${ic('search')}<input class="input" id="sQ" placeholder="Team, league or source"></span><div id="sM"></div><div id="sB"></div><div id="sV"></div>
      <button class="btn ghost" id="sAll" data-tip="Expand or collapse every match.">${ic('unfold_more')}Expand all</button><button class="btn" id="sCsv">${ic('download')}CSV</button>`) +
      '<div id="sOut"></div><div class="foot"><span id="sCount"></span><span id="sSync"></span></div>';

    let all = [], byKey = new Map(), view = SE.pref('sheetView') || 'grouped', market = '', book = '';
    const open = new Set();
    const keyOf = (r) => `${r.match_id}|${r.book}|${r.market}|${r.line ?? ''}|${r.outcome}`;
    const flashes = new Map();
    const mSel = SE.select($('#sM'), { options: [['', 'All markets'], ['1x2', '1X2'], ['total', 'Total goals'], ['handicap', 'Handicap'], ['btts', 'Both score']], value: '', onChange: (v) => { market = v; paint(); } });
    const bSel = SE.select($('#sB'), { options: [['', 'All sources']], value: '', onChange: (v) => { book = v; paint(); } });
    SE.seg($('#sV'), [['grouped', 'By match', 'One row per match. Expand to compare every selection across sources.'], ['flat', 'Every price', 'One row per price, sortable like a spreadsheet.']], view, (v) => { view = v; SE.setPref('sheetView', v); paint(); });
    $('#sAll').addEventListener('click', () => {
      const ids = [...new Set(filtered().map((r) => r.match_id))];
      const expand = ids.some((i) => !open.has(i));
      ids.forEach((i) => (expand ? open.add(i) : open.delete(i)));
      $('#sAll').innerHTML = (expand ? ic('unfold_less') + 'Collapse all' : ic('unfold_more') + 'Expand all');
      paint();
    });

    function filtered() {
      const q = $('#sQ').value.trim().toLowerCase();
      return all.filter((r) => (!market || r.market === market) && (!book || r.book === book) &&
        (!q || `${r.home} ${r.away} ${r.league} ${SE.bookName(r.book)}`.toLowerCase().includes(q)));
    }
    const qa = (r) => SE.qattr({ b: r.book, h: r.home, a: r.away, m: r.market, l: r.line, o: r.outcome, p: r.price, pv: r.prev, u: r.updated_ms, bt: r.book_ts_ms });
    const fl = (r) => { const f = flashes.get(keyOf(r)); return f && Date.now() - f.t < 1400 ? (f.up ? 'flash-up' : 'flash-down') : ''; };

    // ---- grouped view
    const ORDER = { '1x2': 0, btts: 1, total: 2, handicap: 3 }, OUT = { home: 0, draw: 1, away: 2, over: 0, under: 1, yes: 0, no: 1 };
    function groupBody(rows) {
      const books = [...new Set(rows.map((r) => r.book))].sort((a, b) => (a === PM ? -1 : b === PM ? 1 : a.localeCompare(b)));
      const sels = new Map();
      for (const r of rows) {
        const k = `${r.market}|${r.line ?? ''}|${r.outcome}`;
        if (!sels.has(k)) sels.set(k, { market: r.market, line: r.line, outcome: r.outcome, by: {} });
        sels.get(k).by[r.book] = r;
      }
      const list = [...sels.values()].sort((a, b) => ORDER[a.market] - ORDER[b.market] || (a.line ?? 0) - (b.line ?? 0) || OUT[a.outcome] - OUT[b.outcome]);
      let lastMk = null, html = '';
      for (const s of list) {
        if (s.market !== lastMk) { html += `<tr class="mk"><td colspan="${books.length + 1}">${esc(SE.marketLabel(s.market))}</td></tr>`; lastMk = s.market; }
        const prices = books.filter((b) => b !== PM).map((b) => s.by[b]?.price).filter(Boolean);
        const best = prices.length > 1 ? Math.max(...prices) : null;
        html += `<tr><td>${esc(SE.outcomeLong(s.outcome, s.line))}</td>${books.map((b) => {
          const r = s.by[b];
          if (!r) return '<td class="p faint">-</td>';
          const cls = r.price == null ? 'off' : b !== PM && r.price === best ? 'best' : '';
          return `<td class="p ${cls} ${fl(r)}" ${qa(r)}>${r.price == null ? 'off' : SE.fmtOdds(r.price)}</td>`;
        }).join('')}</tr>`;
      }
      return `<table class="tbl pivot compact"><thead><tr><th>Selection</th>${books.map((b) => `<th class="r">${SE.book(b, 'sm')}</th>`).join('')}</tr></thead><tbody>${html}</tbody></table>`;
    }
    function paintGrouped(rows) {
      const groups = new Map();
      for (const r of rows) { if (!groups.has(r.match_id)) groups.set(r.match_id, []); groups.get(r.match_id).push(r); }
      const gl = [...groups.entries()].map(([id, rs]) => {
        const books = [...new Set(rs.map((r) => r.book))];
        const x12 = (b, o) => rs.find((r) => r.book === b && r.market === '1x2' && r.outcome === o);
        const best = (o) => rs.filter((r) => r.book !== PM && r.market === '1x2' && r.outcome === o && r.price).sort((a, b) => b.price - a.price)[0];
        return { id, rs, books, last: Math.max(...rs.map((r) => r.updated_ms || 0)), x12, best, head: rs[0] };
      }).sort((a, b) => b.books.length - a.books.length || b.last - a.last);
      $('#sCount').textContent = `${gl.length} matches, ${SE.fmtInt(rows.length)} prices`;
      if (!gl.length) { $('#sOut').innerHTML = '<div class="empty">No prices match these filters.</div>'; return; }
      const trio = (fn) => `<span class="trio">${['home', 'draw', 'away'].map((o) => { const r = fn(o); return r ? `<span ${qa(r)}>${SE.fmtOdds(r.price) || 'off'}</span>` : '<span class="faint">-</span>'; }).join('')}</span>`;
      $('#sOut').innerHTML = `<div class="grp-head grp-cols" style="cursor:default"><span></span><span>Match</span><span>Score</span><span data-tip="Sources pricing this match">Sources</span><span data-tip="Best bookmaker price for 1, X and 2">Best 1 X 2</span><span data-tip="Polymarket 1, X and 2 from the mid price">Polymarket 1 X 2</span><span class="r">Updated</span></div>` +
        gl.map((g) => `<div class="grp ${open.has(g.id) ? 'open' : ''}" data-id="${g.id}">
          <div class="grp-head">${ic('chevron_right', 'chev')}<div>${P.matchCell(g.head.home, g.head.away, g.head.league)}</div>
            <div><b>${esc(g.head.score || '')}</b> <span class="faint">${esc(g.head.clock || '')}</span></div>${SE.logos(g.books)}
            <div>${trio((o) => g.best(o))}</div><div>${g.books.includes(PM) ? trio((o) => g.x12(PM, o)) : '<span class="faint">not listed</span>'}</div>
            <span class="r muted">${SE.fmtAgo(g.last)}</span></div>
          <div class="grp-body">${open.has(g.id) ? groupBody(g.rs) : ''}</div></div>`).join('');
      $$('.grp-head', $('#sOut')).forEach((h) => h.addEventListener('click', (e) => {
        const g = h.parentElement;
        if (!g.dataset.id) return;
        const id = +g.dataset.id;
        if (open.has(id)) open.delete(id); else open.add(id);
        g.classList.toggle('open', open.has(id));
        $('.grp-body', g).innerHTML = open.has(id) ? groupBody(rows.filter((r) => r.match_id === id)) : '';
      }));
    }

    // ---- flat view (virtual rows)
    const COLS = [
      { k: 'rn', l: '' },
      { k: 'match', l: 'Match', v: (r) => r.home + ' v ' + r.away },
      { k: 'league', l: 'League', v: (r) => r.league || '' },
      { k: 'book', l: 'Source', v: (r) => r.book },
      { k: 'market', l: 'Market', v: (r) => r.market },
      { k: 'sel', l: 'Selection', v: (r) => SE.outcomeLabel(r.outcome, r.line) },
      { k: 'price', l: 'Price', v: (r) => r.price, r: 1 },
      { k: 'prev', l: 'Previous', v: (r) => r.prev, r: 1 },
      { k: 'delta', l: 'Move', v: (r) => (r.price && r.prev ? 1 / r.price - 1 / r.prev : null), r: 1, t: 'Change in implied probability at the last move, in points.' },
      { k: 'upd', l: 'Changed', v: (r) => r.updated_ms, r: 1 },
      { k: 'feed', l: 'Feed delay', v: (r) => (r.book_ts_ms ? r.updated_ms - r.book_ts_ms : null), r: 1, t: G.feed },
    ];
    let sortK = 'upd', dir = -1, flat = [];
    const ROW_H = 32;
    function cellHtml(c, r) {
      switch (c.k) {
        case 'match': return SE.vs(r.home, r.away);
        case 'book': return SE.book(r.book, 'sm');
        case 'market': return esc(SE.marketLabel(r.market));
        case 'price': return r.price == null ? '<span class="faint">off</span>' : `<b>${SE.fmtOdds(r.price)}</b>`;
        case 'prev': return `<span class="muted">${SE.fmtOdds(r.prev)}</span>`;
        case 'delta': { const d = c.v(r); return d == null ? '' : `<span class="${d > 0 ? 'up' : d < 0 ? 'down' : 'muted'}">${SE.fmtPts(d)}</span>`; }
        case 'upd': return `<span class="mono">${SE.fmtTime(r.updated_ms, true)}</span>`;
        case 'feed': { const d = c.v(r); return d == null ? '<span class="faint">-</span>' : `<span class="muted">${SE.fmtSpan(d)}</span>`; }
        default: return esc(c.v(r) ?? '');
      }
    }
    function paintFlat(rows) {
      flat = rows.slice();
      const c = COLS.find((x) => x.k === sortK);
      flat.sort((x, y) => { const a = c.v(x), z = c.v(y); if (a == null) return 1; if (z == null) return -1; return (a < z ? -1 : a > z ? 1 : 0) * dir; });
      $('#sCount').textContent = `${SE.fmtInt(flat.length)} prices`;
      if (!$('#sGrid')) {
        $('#sOut').innerHTML = `<div class="sheet" id="sGrid"><table class="tbl"><thead><tr id="sHead"></tr></thead><tbody id="sBody"></tbody></table></div>`;
        $('#sGrid').addEventListener('scroll', () => requestAnimationFrame(rowsPaint));
      }
      $('#sHead').innerHTML = COLS.map((c) => `<th class="${c.k === 'rn' ? '' : 'sortable'} ${c.r ? 'r' : ''}" data-k="${c.k}" ${c.t ? `data-tip="${esc(c.t)}"` : ''}>${c.l}${c.k === sortK ? ' ' + ic(dir < 0 ? 'arrow_downward' : 'arrow_upward') : ''}</th>`).join('');
      $$('#sHead th.sortable').forEach((th) => th.addEventListener('click', () => { if (sortK === th.dataset.k) dir = -dir; else { sortK = th.dataset.k; dir = -1; } paint(); }));
      rowsPaint();
    }
    function rowsPaint() {
      const grid = $('#sGrid');
      if (!grid) return;
      const start = Math.max(0, Math.floor(grid.scrollTop / ROW_H) - 10);
      const end = Math.min(flat.length, start + Math.ceil(grid.clientHeight / ROW_H) + 20);
      let html = `<tr class="sp"><td colspan="${COLS.length}" style="height:${start * ROW_H}px"></td></tr>`;
      for (let i = start; i < end; i++) {
        const r = flat[i];
        html += `<tr ${qa(r)}>${COLS.map((c) => (c.k === 'rn' ? `<td class="rn">${i + 1}</td>` : `<td class="${c.r ? 'r' : ''} ${c.k === 'price' ? fl(r) : ''}">${cellHtml(c, r)}</td>`)).join('')}</tr>`;
      }
      html += `<tr class="sp"><td colspan="${COLS.length}" style="height:${Math.max(0, (flat.length - end) * ROW_H)}px"></td></tr>`;
      $('#sBody').innerHTML = html;
    }

    function paint() {
      const rows = filtered();
      if (view === 'flat') paintFlat(rows);
      else paintGrouped(rows);
      $('#sAll').style.display = view === 'flat' ? 'none' : '';
    }
    async function load() {
      const d = await api('/quotes');
      all = d.rows.map((a) => Object.fromEntries(d.columns.map((c, i) => [c, a[i]])));
      byKey = new Map(all.map((r) => [keyOf(r), r]));
      const books = [...new Set(all.map((r) => r.book))].sort();
      bSel.set([['', 'All sources'], ...books.map((b) => [b, SE.bookName(b)])]);
      $('#sSync').textContent = 'Synced ' + SE.fmtTime(d.now_ms);
      paint();
    }
    const reload = SE.debounce(load, 1500);
    // Live: patch rows and repaint at most twice a second.
    const repaint = SE.throttle(() => {
      if (view === 'flat') { rowsPaint(); return; }
      // Only refresh open groups; headers update on the periodic reload.
      for (const id of open) {
        const g = $(`.grp[data-id="${id}"] .grp-body`);
        if (g) g.innerHTML = groupBody(filtered().filter((r) => r.match_id === id));
      }
    }, 500);
    const off = SE.onChange((c) => {
      const k = `${c.match_id}|${c.book}|${c.market}|${c.line ?? ''}|${c.outcome}`;
      const r = byKey.get(k);
      if (!r || c.kind === 'close') { reload(); return; }
      if (c.new_price != null && r.price != null) flashes.set(k, { t: Date.now(), up: c.new_price < r.price });
      r.prev = c.old_price ?? r.prev;
      r.price = c.new_price;
      r.updated_ms = c.ts_ms;
      r.book_ts_ms = c.book_ts_ms;
      repaint();
    });
    $('#sQ').addEventListener('input', SE.debounce(paint, 150));
    $('#sCsv').addEventListener('click', () => {
      const cols = COLS.filter((c) => c.k !== 'rn');
      SE.csv('swifteye-prices.csv', cols.map((c) => c.l), filtered().map((r) => cols.map((c) => c.v(r))));
    });
    load();
    SE.every(10000, load);
    return off;
  },
};

// ================================================================ Charts
SE.pages.charts = {
  title: 'Charts',
  render(el) {
    el.innerHTML = P.head('Charts', '<div id="gMatch"></div><div id="gSel"></div><div id="gScale"></div>') +
      P.sec('monitoring', '<span id="gTitle">Price by source</span>', '<div class="chart" id="gChart" style="height:420px"></div><div class="legend" id="gLegend"></div>', '<span class="muted" id="gInfo"></span>', 'Drag to zoom, double-click to reset, click a legend entry to hide a source.') +
      `<div class="grid g-2" style="margin-top:40px">
        ${P.sec('expand', 'Disagreement between sources', '<div class="chart sm" id="gSpread"></div>', '', 'Highest minus lowest implied probability across sources at each moment. Spikes show one source moving before the others.')}
        ${P.sec('format_list_numbered', 'Moves by source', '<div id="gMoves"></div>')}
      </div>`;
    let scale = 'prob', charts = [], hist = [], matchId = null, sel = null;
    SE.seg($('#gScale'), [['prob', 'Probability'], ['odds', 'Odds']], scale, (v) => { scale = v; render(); });
    const mSel = SE.select($('#gMatch'), { options: [], placeholder: 'Choose a match', width: '340px', search: true, onChange: () => loadMatch() });
    const sSel = SE.select($('#gSel'), { options: [], placeholder: 'Selection', width: '220px', onChange: () => loadSel() });

    async function loadMatches() {
      const b = await api('/board?market=1x2&min_books=1');
      const rows = b.rows.sort((x, y) => Object.keys(y.books).length - Object.keys(x.books).length);
      const cur = mSel.value();
      mSel.set(rows.map((r) => [String(r.id), `${r.home} v ${r.away}`, `${Object.keys(r.books).length} sources`]), cur && rows.some((r) => String(r.id) === cur) ? cur : rows[0] ? String(rows[0].id) : undefined);
      return rows.length;
    }
    async function loadMatch() {
      matchId = +mSel.value();
      if (!matchId) return;
      const d = await api('/matches/' + matchId);
      const opts = new Map();
      for (const b of d.books) for (const q of b.quotes) opts.set(`${q.market}|${q.line ?? ''}|${q.outcome}`, `${SE.marketLabel(q.market)}: ${SE.outcomeLong(q.outcome, q.line)}`);
      const order = ['1x2', 'btts', 'total', 'handicap'];
      const keys = [...opts.keys()].sort((a, b) => { const [ma, la] = a.split('|'), [mb, lb] = b.split('|'); return order.indexOf(ma) - order.indexOf(mb) || (+la || 0) - (+lb || 0) || a.localeCompare(b); });
      const cur = sSel.value();
      sSel.set(keys.map((k) => [k, opts.get(k)]), keys.includes(cur) ? cur : keys[0]);
      await loadSel();
    }
    async function loadSel() {
      sel = sSel.value();
      if (!sel) return;
      const [m, line, o] = sel.split('|');
      hist = (await api(`/matches/${matchId}/changes?market=${m}&outcome=${o}${line !== '' ? '&line=' + line : ''}&limit=50000`)).changes;
      render();
    }
    function render() {
      charts.forEach(SE.destroyChart);
      charts = [];
      const by = new Map();
      for (const c of hist) { if (!by.has(c.book)) by.set(c.book, []); by.get(c.book).push([c.ts_ms, c.new_price == null ? null : scale === 'prob' ? 1 / c.new_price : c.new_price]); }
      const books = [...by.keys()].sort((a, b) => (a === PM ? -1 : b === PM ? 1 : a.localeCompare(b)));
      const opt = sel ? sel.split('|') : [];
      $('#gTitle').textContent = sel ? `${SE.marketLabel(opt[0])}: ${SE.outcomeLong(opt[2], opt[1] === '' ? null : +opt[1])}` : 'Price by source';
      $('#gInfo').textContent = hist.length ? `${SE.fmtInt(hist.length)} changes since ${SE.fmtTime(hist[0].ts_ms)}` : '';
      if (!books.length) { $('#gChart').innerHTML = `<div class="empty">${ic('show_chart')}No history for this selection yet.</div>`; $('#gSpread').innerHTML = ''; $('#gMoves').innerHTML = ''; return; }
      const al = SE.alignSeries(books.map((b) => by.get(b)));
      const series = books.map((b, i) => ({ label: SE.bookName(b), book: b, color: SE.bookColor(b), values: al.values[i], width: b === PM ? 2.6 : 1.4 }));
      const yFmt = scale === 'prob' ? (v) => (v == null ? '' : (v * 100).toFixed(0) + '%') : (v) => (v == null ? '' : v.toFixed(2));
      charts.push(SE.chart($('#gChart'), al.xs, series, { yFmt }));
      SE.legend($('#gLegend'), series, charts[0]);
      const pal = SE.alignSeries(books.map((b) => by.get(b).map(([t, v]) => [t, v == null ? null : scale === 'prob' ? v : 1 / v])));
      const spread = pal.xs.map((_, i) => { const v = pal.values.map((s) => s[i]).filter((x) => x != null); return v.length >= 2 ? Math.max(...v) - Math.min(...v) : null; });
      charts.push(SE.chart($('#gSpread'), pal.xs, [{ label: 'Spread', color: SE.bookColor(PM), values: spread }], { height: 190, yFmt: (v) => (v == null ? '' : (v * 100).toFixed(1) + ' pts') }));
      SE.table($('#gMoves'), [
        { key: 'b', label: 'Source', fmt: (r) => SE.book(r.b) },
        { key: 'n', label: 'Moves', cls: 'r', fmt: (r) => SE.fmtInt(r.n), sort: (r) => r.n },
        { key: 'l', label: 'Last change', cls: 'r', fmt: (r) => `<span class="muted">${SE.fmtAgo(r.last)}</span>`, sort: (r) => r.last },
      ], { sort: 'n' }).set(books.map((b) => ({ b, n: hist.filter((c) => c.book === b && c.kind === 'move').length, last: Math.max(...hist.filter((c) => c.book === b).map((c) => c.ts_ms)) })));
    }
    const reRender = SE.debounce(render, 800);
    const off = SE.onChange((c) => {
      if (c.match_id !== matchId || !sel) return;
      const [m, line, o] = sel.split('|');
      if (c.market === m && c.outcome === o && String(c.line ?? '') === line) { hist.push(c); reRender(); }
    });
    (async () => { if (await loadMatches()) loadMatch(); })();
    SE.every(20000, loadMatches);
    return () => { off(); charts.forEach(SE.destroyChart); };
  },
};
})();
