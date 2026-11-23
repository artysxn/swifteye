// Swifteye pages, part 7: Trade review (#/review, server side in review.rs, wave maths in waves.js).
// The owner picks a game (one a bot or the replay traded, one imported from another machine's trades
// export, or one no bot traded that had Polymarket and a bookmaker). A bought game opens on the trade
// chart (the Telegram trade chart's look) with the bot's plan; the owner drags their own version of it
// into place. Marks, each saved by itself:
//   * a verdict on a bot's trade and your version of it (one mark per trade);
//   * buys of your own ("Buy here", B): where a bot should have bought and sold, and the most to pay;
//   * line pairs (K bookmaker, P Polymarket): one bookmaker move and the same move on Polymarket, as many
//     pairs as you like (the server measures the delay, size ratio and correlation of each);
//   * a note.
// Analysis (toggle) draws the superline and the waves (waves.js, the browser port of tools/waves) and
// shows the measured delay and correlation of every line, for the game and over all games.
// Marks are saved to trade_labels (and <db dir>/trade_labels.json) to tune the move detection with.
'use strict';
(() => {
const { $, $$, esc, ic, api } = SE;
const P = SE.P;
const PMK = 'polymarket';
const SUPER = 'superline';
const OUTS = [['home', '1'], ['draw', 'X'], ['away', '2']];
const KINDS = [
  ['move', 'Same move', 'Only the two painted lines: this bookmaker move and this Polymarket move are the same movement.'],
  ['buy', 'Should have bought', 'A moment a bot should have bought: the buy point, the sell point and the most to pay.'],
  ['adjust', 'Fix bot trade', 'A bot trade at the wrong moment or price: where it should have bought and sold instead.'],
  ['no_buy', 'Wrong bot trade', 'A bot trade that should not have happened.'],
  ['ok', 'Right bot trade', 'A bot trade that was right.'],
  ['note', 'Note', 'A note without a verdict.'],
];
const KIND_NAME = Object.fromEntries(KINDS.map((k) => [k[0], k[1]]));
const VERDICTS = ['ok', 'no_buy', 'adjust'];
const cents = (p) => (p == null ? '-' : (p * 100).toFixed(1) + '¢');
const pct = (p) => (p == null ? '-' : (p * 100).toFixed(0) + '%');
const secs = (ms) => (ms == null ? '-' : (ms > 0 ? '+' : '') + (ms / 1000).toFixed(1) + ' s');
// "8.0 s after the bookmaker" / "3.5 s before the bookmaker" / "at the same time as the bookmaker".
const rel = (ms, who = 'the bookmaker') => (ms == null ? '' : Math.abs(ms) < 50 ? `at the same time as ${who}` : `${(Math.abs(ms) / 1000).toFixed(1)} s ${ms > 0 ? 'after' : 'before'} ${who}`);
const usd = (v) => (v == null ? '' : (v >= 0 ? '+' : '−') + '$' + Math.abs(v).toFixed(2));
const outName = (o) => ({ home: '1', draw: 'X', away: '2' }[o] || o);
const enc = encodeURIComponent;
const fade = (hex, a) => { const m = /^#?([0-9a-f]{6})$/i.exec(hex || ''); if (!m) return hex; const n = parseInt(m[1], 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };
const median = (a) => { const s = [...a].sort((x, y) => x - y), n = s.length; return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null; };
const who = (s) => (s === PMK ? 'Polymarket' : s === SUPER ? 'Superline' : SE.bookName(s));
// Bookmaker lines stay at 10% until selected in the legend; Polymarket is always full.
const DIM = 0.1;

SE.pages.review = {
  title: 'Trade review',
  render(el, [id]) { return id ? game(el, +id) : list(el); },
};

// ---------------------------------------------------------------- game picker
function list(el) {
  const st = { kind: SE.pref('reviewKind') || 'bought', q: '', book: '', marked: SE.pref('reviewMarked') || 'all' };
  el.innerHTML = P.head('Trade review', `<div id="rvKind"></div><span class="search">${ic('search')}<input class="input" id="rvQ" placeholder="Team or league"></span><div id="rvBook"></div><div id="rvMk"></div><button class="btn" id="rvRand">${ic('casino')}Random game</button><button class="btn" id="rvImp" data-tip="Load a Swifteye trades export (the .html page, its .zip, or .json) from another machine to grade its trades here">${ic('upload')}Import trades</button><input type="file" id="rvFile" accept=".html,.htm,.json,.zip" hidden><button class="btn" id="rvExp">${ic('download')}Export marks</button>`) +
    `<p class="muted rv-intro">Open a game, then judge a bot's trade and drag your own version of it, place buys of your own (B), or paint the same movement on a bookmaker (K) and on Polymarket (P), as many pairs as you like. Analysis shows the superline, the waves and each line's measured delay and correlation. Everything saves by itself.</p>` +
    P.sec('sports_soccer', 'Games', '<div id="rvT"></div>', '<span id="rvN" class="muted"></span>');
  const kindSeg = () => SE.seg($('#rvKind'), [['bought', 'Bought', 'Games a bot traded (paper or real), imported trades from another machine, and games the replay paper bot traded: the snipe bot\'s current rules over every recorded game'], ['other', 'Not bought', 'Started games with a Polymarket market and at least one bookmaker that no bot and no replay trade touched']], st.kind, (v) => { st.kind = v; SE.setPref('reviewKind', v); load(); });
  kindSeg();
  SE.seg($('#rvMk'), [['all', 'All'], ['no', 'Not marked'], ['yes', 'Marked']], st.marked, (v) => { st.marked = v; SE.setPref('reviewMarked', v); show(); });
  const bookSel = $('#rvBook');
  SE.select(bookSel, { options: [['', 'Any bookmaker']], value: '', width: '190px', onChange: (v) => { st.book = v; load(); } });
  api('/review/books').then((r) => {
    if (!document.body.contains(bookSel)) return;
    SE.select(bookSel, { options: [['', 'Any bookmaker'], ...r.books.map((b) => [b.book, `${SE.bookName(b.book)} (${b.games})`, '', SE.logo(b.book)])], value: st.book, width: '190px', search: true, onChange: (v) => { st.book = v; load(); } });
  }).catch(() => {});
  $('#rvQ').addEventListener('input', SE.debounce((e) => { st.q = e.target.value; load(); }, 300));
  const qs = () => `kind=${st.kind}&q=${enc(st.q)}&book=${enc(st.book)}`;
  $('#rvRand').addEventListener('click', async () => {
    const pick = (kind) => api(`/review/games?kind=${kind}&q=${enc(st.q)}&book=${enc(st.book)}&random=1&limit=1`).catch((e) => { SE.toast(e.message); return null; });
    let r = await pick(st.kind);
    // Nothing bought here: pick from the games no bot traded instead of doing nothing.
    if (r && !r.games.length && st.kind === 'bought') { r = await pick('other'); if (r?.games?.length) SE.toast('No bought game matches; this one is from Not bought.'); }
    if (r?.games?.length) SE.go('#/review/' + r.games[0].id); else if (r) SE.toast('No game matches these filters.');
  });
  $('#rvImp').addEventListener('click', () => $('#rvFile').click());
  $('#rvFile').addEventListener('change', async (e) => {
    const f = e.target.files[0];
    e.target.value = '';
    if (!f) return;
    const btn = $('#rvImp');
    btn.disabled = true;
    SE.toast(`Importing ${f.name}…`, 8000);
    try {
      const zip = /\.zip$/i.test(f.name) || (await f.slice(0, 2).text()) === 'PK';
      const body = zip ? await unzipFirst(f) : f;
      const res = await fetch('/review/import', { method: 'POST', body, headers: { 'Content-Type': 'application/octet-stream' } });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
      SE.toast(`Imported ${j.trades} trades in ${j.games} games. They are under Bought, marked Mac export.`, 6000);
      st.kind = 'bought';
      SE.setPref('reviewKind', 'bought');
      kindSeg();
      load();
    } catch (err) { SE.toast('Import failed: ' + err.message, 8000); }
    if (btn) btn.disabled = false;
  });
  $('#rvExp').addEventListener('click', async () => {
    const r = await api('/review/labels').catch((e) => { SE.toast(e.message); return null; });
    if (!r) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(r.labels, null, 2)], { type: 'application/json' }));
    a.download = `trade-labels-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    SE.toast(`${r.labels.length} marks exported.`);
  });
  const match = { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league, r.sport), sort: (r) => r.home };
  const kick = { key: 'k', label: 'Kick-off', fmt: (r) => (r.start_ms ? SE.fmtDateTime(r.start_ms) : '-'), sort: (r) => r.start_ms };
  // Marked or not: a bought game counts as marked when every trade has a mark.
  const isMarked = (r) => (st.kind === 'bought' ? r.marked_trades >= r.trades && r.trades > 0 : r.labels > 0);
  const VERDICT = { ok: ['Right', 'ok'], no_buy: ['Wrong', 'bad'], adjust: ['Fix', 'accent'], buy: ['Own buy', 'accent'], move: ['Lines', ''], note: ['Note', ''] };
  const marks = { key: 'mk', label: 'Status', w: 170, sort: (r) => (isMarked(r) ? 2 : r.labels ? 1 : 0), fmt: (r) => {
    const state = isMarked(r) ? `<span class="chip ok">${ic('check')}Marked</span>` : st.kind === 'bought' && r.marked_trades > 0 ? `<span class="chip accent">${r.marked_trades} of ${r.trades} marked</span>` : '<span class="chip">Not marked</span>';
    return state + ' ' + (r.kinds || []).map((k) => `<span class="chip ${VERDICT[k]?.[1] || ''}">${VERDICT[k]?.[0] || k}</span>`).join(' ');
  } };
  const books = { key: 'b', label: 'Bookmakers', w: 170, fmt: (r) => `${SE.logos(r.books, 4)} <span class="muted">${r.books.length}</span>`, sort: (r) => r.books.length };
  let tbl = null, kindShown = null;
  async function load() {
    const r = await api(`/review/games?${qs()}&limit=300`).catch((e) => { SE.toast(e.message); return null; });
    if (!r || !$('#rvT')) return;
    if (kindShown !== st.kind) {
      kindShown = st.kind;
      tbl = SE.table($('#rvT'), st.kind === 'bought'
        ? [match, marks, kick, { key: 't', label: 'Trades', cls: 'r', w: 80, sort: (r) => r.trades, fmt: (r) => r.trades }, { key: 'bot', label: 'Bots', w: 170, fmt: (r) => esc(r.bots || '') + (r.real ? ' <span class="chip bad">real</span>' : '') + (r.imported ? ' <span class="chip" data-tip="Imported from a trades export">Mac export</span>' : '') }, { key: 'p', label: 'P&amp;L', cls: 'r', fmt: (r) => `<span class="${r.pnl > 0 ? 'up' : r.pnl < 0 ? 'down' : 'muted'}">${usd(r.pnl)}</span>`, sort: (r) => r.pnl }, books]
        : [match, marks, kick, books],
        { sort: 'k', maxHeight: '70vh', onClick: (r) => SE.go('#/review/' + r.id), empty: st.kind === 'bought' ? 'No bot, replay or imported trades match. Import trades loads another machine\'s export; the replay needs a few minutes after a start to rebuild; use Not bought meanwhile.' : 'No game matches these filters.', emptyIcon: 'rate_review' });
    }
    // First visit with nothing bought here: open on Not bought instead of an empty list.
    if (st.kind === 'bought' && !r.games.length && !st.q && !st.book && SE.pref('reviewKind') == null) { st.kind = 'other'; kindSeg(); load(); return; }
    rows = r.games;
    show();
  }
  let rows = [];
  function show() {
    if (!tbl) return;
    const shown = rows.filter((r) => st.marked === 'all' || (st.marked === 'yes') === isMarked(r));
    tbl.set(shown);
    const done = rows.filter(isMarked).length;
    $('#rvN').textContent = `${shown.length} shown · ${done} of ${rows.length}${rows.length >= 300 ? '+' : ''} marked`;
  }
  load();
}

// The first .html / .json file in a zip (stored or deflated), read in the browser.
async function unzipFirst(file) {
  const buf = await file.arrayBuffer(), dv = new DataView(buf);
  let e = -1;
  for (let i = buf.byteLength - 22; i >= Math.max(0, buf.byteLength - 65557); i--) if (dv.getUint32(i, true) === 0x06054b50) { e = i; break; }
  if (e < 0) throw new Error('not a zip file');
  const count = dv.getUint16(e + 10, true), dec = new TextDecoder();
  let p = dv.getUint32(e + 16, true);
  for (let k = 0; k < count; k++) {
    if (dv.getUint32(p, true) !== 0x02014b50) break;
    const method = dv.getUint16(p + 10, true), size = dv.getUint32(p + 20, true), nl = dv.getUint16(p + 28, true), xl = dv.getUint16(p + 30, true), cl = dv.getUint16(p + 32, true), off = dv.getUint32(p + 42, true);
    const name = dec.decode(new Uint8Array(buf, p + 46, nl));
    p += 46 + nl + xl + cl;
    if (!/\.(html?|json)$/i.test(name)) continue;
    const start = off + 30 + dv.getUint16(off + 26, true) + dv.getUint16(off + 28, true);
    const data = new Uint8Array(buf, start, size);
    if (method === 0) return new Blob([data]);
    if (method !== 8) throw new Error('the zip uses a compression the browser cannot read; unzip it and import the .html');
    return new Response(new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'))).blob();
  }
  throw new Error('no .html or .json file in the zip');
}

// ---------------------------------------------------------------- one game
// A bought game opens on its trade, drawn like the Telegram trade chart: the token bought, in cents,
// from 10 minutes before the buy to past the sale; Polymarket's sell and buy price and the bookmaker that
// triggered; the bot's buy, sale and plan (cash-out price, give-up time, break-even, the expected path).
// Your version sits on top of the bot's (shown faint) and each part of it is dragged into place.
// "Whole game" shows the full game, for games no bot traded and buys the bot missed.
const TC = { buy: '#34c759', sell: '#ff5a52', move: '#f08a2e', pmSell: '#4a8cff', pmBuy: '#a9bfee', trig: '#f08a2e', all: '#b37be0', cash: '#34c759', cap: '#f5b83d', give: '#ff5a52', be: '#9a9aa0', pm: '#4a8cff', sup: '#2ec4d6' };
const HOLD_MS = 70000; // a typical catch-up, for the expected path (the Telegram chart's default)
const GIVE_MS = 120000; // a buy of your own gives up after 2 minutes unless dragged (a wave is over in seconds)
const GHOST = 0.3;
const ALL = '__all'; // the middle of all bookmakers, as an extra line on a trade
const MIN_MOVES = 8; // a line's lead-lag and ρ on one game need at least this many moves
let SCORES = null, scoresAsked = false; // tools/waves/run.py over all games (GET /review/wave_scores)

function game(el, id) {
  let G = null, u = null, series = [], anaCache = {}, dead = false; // dead: left the page (late saves must not draw on the next one)
  const st = {
    view: 'game', ti: -1, o: 'home', side: 'yes', sig: false,
    ana: !!SE.pref('reviewAna'), // Analysis: superline, waves, measured delay and correlation
    sel: new Set(SE.pref('reviewSel') || []),
    tsel: new Set(), // a trade's extra lines (bookmakers, ALL), added from the legend; empty when it opens
    // The marks on this view, each saved by itself:
    //   main  { lid, trade, kind (verdict), mine, note, pairIn }  your version of the bot's trade (one per trade)
    //   buy   { lid, side, mine, note, pairIn }                    a buy of your own
    //   pair  { lid, pm, book, note, analysis, host }               a line pair (segments in Yes probability)
    //   note  { lid, note }                                         a note on the game
    // mine = { buy_ms, sell_ms, target, give_ms, cap } (token prices 0..1). A pair saved inside an older
    // mark (before pairs were marks of their own) keeps living there: host = that mark's item.
    items: [], plan: null, pair: null, focus: null, pairPick: false,
    tool: null, paint: null, drag: null,
  };
  const saveSel = () => SE.setPref('reviewSel', [...st.sel]);
  el.innerHTML = `<div class="empty">${ic('hourglass_empty')}Loading the game</div>`;

  const onKey = (e) => {
    if (!G || e.target.closest('input, textarea, select, [contenteditable]') || e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    const map = { k: 'book', p: 'pm', b: 'buy' };
    if (map[k]) { setTool(map[k]); e.preventDefault(); }
    else if (k === 'escape') { st.paint = null; setTool(null); u?.redraw(false, false); }
  };
  document.addEventListener('keydown', onKey);

  api('/review/game/' + id).then((g) => {
    G = g;
    if (G.trades.length) openTrade(0, false);
    else { st.view = 'game'; st.o = OUTS.map((x) => x[0]).find((o) => G.pm[o]?.length) || 'home'; loadItems(); }
    page();
  }).catch((e) => { el.innerHTML = `<div class="empty err">${ic('error')}${esc(e.message)}</div>`; });
  if (!scoresAsked) { scoresAsked = true; api('/review/wave_scores').then((s) => { SCORES = s; renderAnalysis(); }).catch(() => {}); }

  // ---- what is shown
  const trade = () => (st.view === 'trade' && st.ti >= 0 ? G.trades[st.ti] : null);
  const td = () => trade()?.data || {};
  const tradeSide = (tr) => ((tr?.data?.dir ?? 1) < 0 ? 'no' : 'yes');
  // The chart is drawn in the trade's token, or in the outcome's Yes on the whole game.
  const chartSide = () => (trade() ? tradeSide(trade()) : 'yes');
  const outcome = () => (trade() ? td().outcome : st.o);
  const planSide = () => st.plan?.side ?? (trade() ? chartSide() : st.side);
  // Prices: Yes probability -> chart cents; a token price (of `side`) -> chart cents and back.
  const yC = (p) => (p == null ? null : (chartSide() === 'yes' ? p : 1 - p) * 100);
  const tokC = (p, side = planSide()) => (p == null ? null : (side === chartSide() ? p : 1 - p) * 100);
  const fromC = (c, side = planSide()) => (c == null ? null : side === chartSide() ? c / 100 : 1 - c / 100);
  const yesFromC = (c) => (chartSide() === 'yes' ? c / 100 : 1 - c / 100);
  const tradeName = (t) => (t.bot === 'replay' ? 'Replay' : `${t.bot} #${t.id}`);
  const ana = () => { if (!st.ana || !SE.waves || !G) return null; const o = outcome(); return (anaCache[o] ||= SE.waves.analyse(G, o)); };

  // ---- prices
  const pmAt = (t, o = outcome()) => {
    const rows = G.pm[o] || [];
    let lo = 0, hi = rows.length - 1, k = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (rows[m][0] <= t) { k = m; lo = m + 1; } else hi = m - 1; }
    return k < 0 ? null : { bid: rows[k][1], ask: rows[k][2] };
  };
  // What buying the token pays (Yes: the ask; No: 1 - the Yes bid) and what selling it gets.
  const buyPx = (t, side = planSide()) => { const q = pmAt(t); if (!q) return null; return side === 'yes' ? q.ask : q.bid == null ? null : 1 - q.bid; };
  const sellPx = (t, side = planSide()) => { const q = pmAt(t); if (!q) return null; return side === 'yes' ? q.bid : q.ask == null ? null : 1 - q.ask; };
  const rowSell = (r, side) => (side === 'yes' ? r[1] : r[2] == null ? null : 1 - r[2]);

  // The bot's trade and plan, as the Telegram chart reads them (telegram.rs pays_at / break_even).
  function botPlan(tr) {
    const d = tr.data || {}, yes = (d.dir ?? 1) > 0;
    const buy = d.ts ?? tr.ts_ms, sell = d.exit_ts ?? tr.closed_ms ?? null;
    let target = null;
    if (d.pre_mid != null && d.move != null) {
      const need = (d.catch ?? 1) * d.move, sp = d.spread ?? 0;
      target = Math.min(0.99, Math.max(0.01, yes ? d.pre_mid + need - sp / 2 : 1 - d.pre_mid + need - sp / 2));
    }
    const give = d.give_up_s != null ? buy + d.give_up_s * 1000 : null;
    const per = d.contracts > 0 && d.cost != null ? d.cost / d.contracts : d.entry;
    let be = null;
    if (per != null) { const rate = d.fee_rate ?? 0.05; be = per; for (let i = 0; i < 20; i++) be = per + rate * be * (1 - be); }
    return { buy_ms: buy, sell_ms: sell, entry: d.entry ?? null, exit: d.exit ?? null, target, give_ms: give, cap: d.limit ?? null, be, move_ms: d.move_ms ?? d.t0 ?? null };
  }
  // Where a plan sells: the first Polymarket sell price at or above its cash-out price, else at the give-up.
  function planSale(m, side = planSide()) {
    if (!m || m.buy_ms == null) return null;
    const end = m.give_ms ?? m.buy_ms + 900000;
    if (m.target != null) for (const r of G.pm[outcome()] || []) {
      if (r[0] <= m.buy_ms) continue;
      if (r[0] > end) break;
      const px = rowSell(r, side);
      if (px != null && px >= m.target - 1e-9) return { t: r[0], px, why: 'cash-out' };
    }
    return { t: end, px: sellPx(end, side), why: 'give-up' };
  }
  const tradeWindow = () => {
    const b = botPlan(trade()), start = b.buy_ms - 600000;
    let end = Math.max(b.buy_ms + 300000, ...[b.sell_ms, b.give_ms].filter((x) => x != null).map((x) => x + 120000));
    // Still open (a favourites hold, or not sold yet): everything known after the buy, up to now or to the
    // moment an export was made, since what the price did later is the point of looking.
    if (b.sell_ms == null && b.give_ms == null) end = Math.max(end, Math.min(G.match.until_ms ?? Date.now(), Date.now(), dataEnd() || Infinity));
    for (const it of st.items) if (it.mine?.buy_ms != null && it.mine.buy_ms >= start && it.mine.buy_ms <= end) for (const x of [it.mine.sell_ms, it.mine.give_ms]) if (x != null) end = Math.max(end, x + 120000);
    return [start, end];
  };

  // ---- the marks on this view
  const linked = (l) => l.trade_id != null || !!l.trade;
  const botMine = (tr) => { const b = botPlan(tr); return { buy_ms: b.buy_ms, sell_ms: b.sell_ms, target: b.target, give_ms: b.give_ms, cap: b.cap ?? b.entry }; };
  const sameTrade = (l, t) => (l.trade_id != null ? l.trade_id === t.id : l.trade?.bot === 'replay' && t.bot === 'replay' && l.trade.data?.ts === t.data?.ts && l.trade.data?.outcome === t.data?.outcome);
  // The mark kept for a trade (the latest one if there are several).
  const markFor = (t) => G.labels.filter((l) => sameTrade(l, t)).sort((a, b) => b.updated_ms - a.updated_ms)[0] || null;
  const segIn = (sg) => sg && { t0: sg.t0, t1: sg.t1, y0: sg.y0, y1: sg.y1, ...(sg.book ? { book: sg.book } : {}) };
  const mineOf = (l, base = null) => {
    const m = { buy_ms: l.buy_ms, sell_ms: l.sell_ms ?? null, target: l.target ?? base?.target ?? null, give_ms: l.give_up_ms ?? base?.give_ms ?? null, cap: l.cap ?? base?.cap ?? null };
    // Marks from before sell_set existed: dragged by hand if the sale is not where the plan sells.
    if (l.sell_set != null && 'sell_set' in l) m.sellSet = !!l.sell_set;
    else { const ps = planSale(m, l.side || 'yes'); m.sellSet = !(ps && m.sell_ms != null && Math.abs(ps.t - m.sell_ms) <= 1000); }
    return m;
  };
  function pairOf(l, host) {
    const p = { type: 'pair', lid: host ? null : l.id, o: l.outcome, pm: segIn(l.pm_seg), book: segIn(l.book_seg), note: host ? '' : l.note || '', analysis: l.analysis || null, host };
    if (host) host.pairIn = p;
    return p;
  }
  // This view's marks from the saved ones: the trade's own mark, then your buys, line pairs and the game note
  // on this outcome (those show on the trade and on the whole game).
  function loadItems() {
    const tr = trade(), o = outcome(), items = [];
    if (tr) {
      const l = markFor(tr);
      const main = { type: 'main', lid: l?.id ?? null, o, side: chartSide(), trade: tr, mine: botMine(tr), kind: null, note: '', pairIn: null };
      if (l) { main.kind = VERDICTS.includes(l.kind) ? l.kind : null; main.note = l.note || ''; if (l.buy_ms != null) main.mine = mineOf(l, main.mine); }
      items.push(main);
      if (l && (l.pm_seg || l.book_seg)) items.push(pairOf(l, main));
    }
    for (const l of G.labels) {
      if (l.outcome !== o || linked(l)) continue;
      if (l.kind === 'buy' && l.buy_ms != null) {
        const b = { type: 'buy', lid: l.id, o, side: l.side || 'yes', mine: mineOf(l), note: l.note || '', pairIn: null };
        items.push(b);
        if (l.pm_seg || l.book_seg) items.push(pairOf(l, b));
      } else if (l.pm_seg || l.book_seg) items.push(pairOf(l, null));
      else if (l.kind === 'note') items.push({ type: 'note', lid: l.id, o, note: l.note || '' });
    }
    st.items = items;
    const plans = items.filter((i) => i.type === 'main' || i.type === 'buy');
    st.plan = plans.find((i) => i.type === 'main') || plans[plans.length - 1] || null;
    st.pair = null;
    st.focus = st.plan || items.find((i) => i.type === 'note') || null;
    st.pairPick = false;
  }
  const nOf = (it) => st.items.filter((i) => i.type === it.type).indexOf(it) + 1;
  const noteItem = () => (st.focus ? st.focus.host || st.focus : null);
  const itemName = (it) => (!it ? 'this game' : it.type === 'main' ? 'your version of the bot\'s trade' : it.type === 'buy' ? `your buy ${nOf(it)}` : it.type === 'pair' ? `line pair ${nOf(it)}` : 'this game');
  const pairDelay = (p) => (p.analysis?.delay_measured_ms ?? (p.pm && p.book ? (p.pm.t0 + p.pm.t1) / 2 - (p.book.t0 + p.book.t1) / 2 : null));

  function openTrade(i, render = true) {
    const prev = trade();
    st.view = i >= 0 ? 'trade' : 'game';
    st.ti = i;
    st.tsel = new Set();
    if (i < 0 && prev) st.o = prev.data?.outcome || st.o;
    loadItems();
    setTool(null);
    if (render) renderAll(true);
  }

  // ---- lines
  // Bookmakers as the bot reads them: 1/odds of the outcome, margin included, on the chart's side.
  // A trade opens with only Polymarket and the bookmaker the bot acted on; other bookmakers and the
  // middle of all of them are added from the legend, for that trade only. The whole game shows every
  // bookmaker, faint until picked, and the middle of all (each feed family's mean, the median family).
  // Analysis adds the superline (margin removed, like the wave maths reads every line).
  const lineColor = (s) => (s.key !== 'book' || s.trig || trade() || st.sel.has(s.book) ? s.base : fade(s.base, DIM));
  // The newest price row of the game (any line).
  let newest = null;
  const dataEnd = () => {
    if (newest == null) {
      newest = 0;
      for (const rows of Object.values(G.pm)) if (rows.length) newest = Math.max(newest, rows[rows.length - 1][0]);
      for (const outs of Object.values(G.books)) for (const rows of Object.values(outs)) if (rows.length) newest = Math.max(newest, rows[rows.length - 1][0]);
    }
    return newest;
  };
  function seriesFor() {
    const o = outcome(), side = chartSide(), win = trade() ? tradeWindow() : null;
    // A price that stopped changing still stands: each line is carried flat to the end of the view, so a
    // quiet line does not look like missing data. A trade: to the end of its window, but never past now or
    // past the moment an imported export was made. The whole game: to its newest price.
    const end = win ? Math.min(win[1], G.match.until_ms ?? Date.now(), Date.now()) : dataEnd();
    const clip = (pts) => {
      let out = pts;
      if (win) {
        out = [];
        let before = null;
        for (const p of pts) { if (p[0] < win[0]) before = p; else if (p[0] <= win[1]) out.push(p); }
        if (before) out.unshift([win[0], before[1]]);
      }
      const last = out[out.length - 1];
      if (last && last[1] != null && last[0] < end) out = [...out, [end, last[1]]];
      return out;
    };
    const c = (p) => (p == null ? null : p * 100);
    const rows = G.pm[o] || [];
    const lists = [clip(rows.map((r) => [r[0], c(rowSell(r, side))])), clip(rows.map((r) => [r[0], c(side === 'yes' ? r[2] : r[1] == null ? null : 1 - r[1])]))];
    const meta = [{ label: 'Polymarket sell', key: 'pm', base: TC.pmSell, width: 2.6 }, { label: 'Polymarket buy', key: 'pm', base: TC.pmBuy, width: 1.1 }];
    const trig = trade() ? td().trigger : null;
    const bk = {};
    for (const b of Object.keys(G.books)) {
      const pts = (G.books[b][o] || []).map(([t, p]) => [t, p > 1 ? yC(1 / p) : null]);
      if (pts.length) bk[b] = clip(pts);
    }
    const names = Object.keys(bk).filter((b) => bk[b].length).sort((a, b) => SE.bookName(a).localeCompare(SE.bookName(b)));
    for (const b of names.filter((b) => (trade() ? b === trig || st.tsel.has(b) : true))) {
      lists.push(bk[b]);
      meta.push({ label: SE.bookName(b) + (b === trig ? ' (triggered)' : ''), key: 'book', book: b, trig: b === trig, base: b === trig ? TC.trig : SE.bookColor(b), width: b === trig ? 2.2 : 1.3 });
    }
    const A = ana();
    if (A && A.superline.length) {
      const sl = clip(A.superline.map(([t, p]) => [t, yC(p)]));
      if (sl.length) { lists.push(sl); meta.push({ label: 'Superline', key: 'super', base: TC.sup, width: 2.2 }); }
    }
    const first = lists.length;
    const al = SE.alignSeries([...lists, ...names.map((b) => bk[b])]);
    const all = al.xs.map((_, i) => {
      const fam = {};
      names.forEach((b, j) => { const v = al.values[first + j][i]; if (v != null) (fam[G.families?.[b] || b] ||= []).push(v); });
      const means = Object.values(fam).map((a) => a.reduce((x, y) => x + y, 0) / a.length).sort((a, b) => a - b);
      return means.length ? means[Math.floor(means.length / 2)] : null;
    });
    const out = meta.map((m, i) => ({ ...m, values: al.values[i] }));
    if (names.length && (!trade() || st.tsel.has(ALL))) out.splice(2, 0, { label: 'All bookmakers', key: 'all', base: TC.all, width: 1.6, values: all });
    return { xs: al.xs, series: out.map((s) => ({ ...s, color: lineColor(s) })) };
  }

  // ---- page
  function page() {
    const m = G.match;
    document.title = `Review: ${m.home} v ${m.away} | Swifteye`;
    const tools = `<div id="rvO"></div><button class="chip-btn" id="rvSig">${ic('visibility')}Skipped moves</button><button class="chip-btn${st.ana ? ' on' : ''}" id="rvAna" data-tip="The superline, the waves (who moved first, how far behind Polymarket was) and each line's measured delay and correlation">${ic('insights')}Analysis</button><button class="btn" id="rvAll">All lines</button><button class="btn" id="rvNone">Polymarket only</button>`;
    el.innerHTML = P.head(`${esc(m.home)} <span class="faint">v</span> ${esc(m.away)}`, tools, ['Trade review', '#/review']) +
      `<p class="muted rv-intro">${esc(m.league || '')}${m.start_ms ? ' · kick-off ' + SE.fmtDateTime(m.start_ms) : ''} · ${G.imported ? '<span class="chip" data-tip="Imported from a trades export: prices from 15 minutes before each buy to 10 minutes after its sale">Mac export</span>' : `<a href="#/match/${m.id}">match page</a>`}</p>` +
      `<div class="rv-trades" id="rvTrades"></div>` +
      `<section class="sec"><div class="rv-cap" id="rvCap"></div><div class="chart" id="rvChart" style="height:460px"></div><div class="rv-strip" id="rvStripWrap" hidden><canvas id="rvStrip"></canvas><div class="rv-strip-info" id="rvStripInfo"></div></div><div class="legend" id="rvLegend"></div><div class="muted rv-hint" id="rvHint"></div></section>` +
      `<div id="rvAnaWrap"${st.ana ? '' : ' hidden'}>${P.sec('insights', 'Measured: waves, delay and correlation', '<div id="rvAnaBox"></div>')}</div>` +
      `<div class="grid g-main">${P.sec('tune', 'Your marks', '<div id="rvPanel"></div>', '<span id="rvSave" class="muted rv-save"></span>')}${P.sec('bookmarks', 'Marks on this game', '<div id="rvMarks"></div>')}</div>`;
    $('#rvSig').addEventListener('click', () => { st.sig = !st.sig; $('#rvSig').classList.toggle('on', st.sig); u?.redraw(false, false); });
    $('#rvAna').addEventListener('click', () => { st.ana = !st.ana; SE.setPref('reviewAna', st.ana); $('#rvAna').classList.toggle('on', st.ana); rebuild(); renderAnalysis(); });
    $('#rvAll').addEventListener('click', () => { series.forEach((s) => { if (s.key === 'book') st.sel.add(s.book); }); if (!trade()) for (const b of Object.keys(G.books)) st.sel.add(b); saveSel(); rebuild(); });
    $('#rvNone').addEventListener('click', () => { st.sel.clear(); saveSel(); rebuild(); });
    renderAll(true);
  }
  function renderAll(fresh) {
    const whole = !trade();
    $('#rvO').hidden = $('#rvSig').hidden = $('#rvAll').hidden = $('#rvNone').hidden = !whole;
    if (whole) SE.seg($('#rvO'), OUTS, st.o, (v) => { st.o = v; loadItems(); rebuild(true); renderPanel(); renderAnalysis(); });
    renderTrades();
    rebuild(fresh);
    renderPanel();
    renderAnalysis();
    renderMarks();
  }

  function renderTrades() {
    const box = $('#rvTrades');
    if (!box || dead) return;
    if (!G.trades.length) { box.innerHTML = ''; return; }
    box.innerHTML = G.trades.map((t, i) => {
      const d = t.data || {}, mk = markFor(t);
      return `<button class="chip-btn${st.view === 'trade' && st.ti === i ? ' on' : ''}" data-i="${i}" ${mk ? 'data-tip="You marked this trade"' : ''}>${mk ? ic('check_circle', 'up') : ''}${esc(tradeName(t))}${t.real ? ' <span class="chip bad">real</span>' : ''} · ${SE.fmtTime(d.ts ?? t.ts_ms).slice(0, 5)} · ${tradeSide(t) === 'no' ? 'No' : 'Yes'} ${outName(d.outcome)} · <span class="${d.pnl > 0 ? 'up' : d.pnl < 0 ? 'down' : ''}">${usd(d.pnl)}</span></button>`;
    }).join('') + `<button class="chip-btn${trade() ? '' : ' on'}" data-i="-1">${ic('timeline')}Whole game</button>`;
    $$('button', box).forEach((b) => b.addEventListener('click', () => openTrade(+b.dataset.i)));
  }

  function rebuild(fresh = false) {
    const sv = seriesFor();
    series = sv.series;
    // The y range: every line and plan level, with a little room (a loop: a whole game has too many
    // points to spread into Math.min).
    const b = trade() ? botPlan(trade()) : null;
    let vmin = Infinity, vmax = -Infinity;
    const take = (v) => { if (v != null) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; } };
    for (const s of series) for (const v of s.values) take(v);
    [b?.target, b?.be, b?.cap].forEach((p) => take(p == null ? null : tokC(p, chartSide())));
    for (const it of st.items) if (it.mine) [it.mine.target, it.mine.cap].forEach((p) => take(p == null ? null : tokC(p, it.side)));
    const lo = Math.max(0, Math.floor(vmin - 3)), hi = Math.min(100, Math.ceil(vmax + 3));
    if (fresh) { SE.destroyChart(u); u = null; }
    const keep = u;
    u = SE.chartKeep($('#rvChart'), u, sv.xs, sv.series, { yFmt: (v) => (v == null ? '' : v.toFixed(0) + '¢'), draw: drawLayer, yRange: vmin <= vmax ? [lo, hi] : undefined });
    if (!u) { $('#rvChart').innerHTML = `<div class="empty">${ic('show_chart')}Nothing recorded for this outcome.</div>`; $('#rvLegend').innerHTML = ''; return; }
    if (u !== keep) attach(u);
    else { series.forEach((s, i) => { const col = lineColor(s); u.series[i + 1].stroke = () => col; }); u.redraw(true, false); }
    renderLegend();
    renderCaption();
  }
  function renderCaption() {
    const cap = $('#rvCap');
    if (!cap) return;
    const tr = trade();
    if (!tr) { cap.innerHTML = `<b>Whole game</b> · chance of ${st.o === 'home' ? 'a home win' : st.o === 'draw' ? 'a draw' : 'an away win'} (Yes ${outName(st.o)}, cents)`; return; }
    const b = botPlan(tr);
    cap.innerHTML = `<b>${tradeSide(tr) === 'no' ? 'No' : 'Yes'} ${outName(td().outcome)}</b> · ${tr.bot === 'replay' ? 'replay (paper)' : tr.real ? 'REAL' : 'paper'} · cents of the token bought · ${b.target != null ? `cashes out at ${cents(b.target)}` : 'no cash-out price recorded'}${b.give_ms ? `, gives up ${SE.fmtTime(b.give_ms).slice(0, 5)}` : ''}${td().trigger ? '' : ' · no triggering bookmaker recorded'}${b.sell_ms == null ? ` · still open${G.match.until_ms ? ` when the export was made (${SE.fmtTime(G.match.until_ms).slice(0, 5)})` : ''}` : ''}`;
  }
  function renderLegend() {
    const lg = $('#rvLegend');
    if (!lg) return;
    const sw = (c, dash) => `<i style="background:${dash ? `repeating-linear-gradient(90deg, ${c} 0 5px, transparent 5px 8px)` : c}"></i>`;
    const plan = trade() ? [['Buy', TC.buy], ['Sell', TC.sell], ['Bookmaker move', TC.move], ['Cash-out', TC.cash, 1], ['Price cap', TC.cap, 1], ['Give-up', TC.give, 1], ['Break-even', TC.be, 1]] : st.items.some((i) => i.mine) ? [['Buy', TC.buy], ['Sell', TC.sell], ['Cash-out', TC.cash, 1], ['Price cap', TC.cap, 1], ['Give-up', TC.give, 1]] : [];
    const waves = ana() ? [['Wave: bookmakers ahead of Polymarket', 'rgba(240,138,46,.45)'], ['Wave: Polymarket first', 'rgba(74,140,255,.6)']] : [];
    const others = trade() ? [...(st.tsel.has(ALL) ? [] : [[ALL, 'All bookmakers (middle)']]), ...Object.keys(G.books).filter((b) => G.books[b][outcome()] && b !== td().trigger && !st.tsel.has(b)).sort().map((b) => [b, SE.bookName(b), '', SE.logo(b)])] : [];
    const removable = (s) => trade() && ((s.key === 'book' && !s.trig) || s.key === 'all');
    const tip = (s) => (s.key === 'super' ? 'data-tip="The superline: each bookmaker family\'s quickest move, taken at once and kept only when other families confirm it (margin removed)"' : removable(s) ? 'data-tip="Click to take this line off"' : '');
    lg.innerHTML = series.map((s, i) => `<label data-i="${i}" class="${s.key === 'book' ? (lineColor(s) === s.base ? 'sel' : 'dim') : 'pm'}" ${s.book ? `data-tipsrc="${esc(s.book)}"` : ''} ${tip(s)}>${sw(s.base)}${s.book ? SE.logo(s.book) : ''}${esc(s.label)}${removable(s) ? ' ×' : ''}</label>`).join('') +
      plan.map(([l, c, d]) => `<label class="pm">${sw(c, d)}${l}</label>`).join('') +
      waves.map(([l, c]) => `<label class="pm">${sw(c)}${l}</label>`).join('') +
      (others.length ? '<span id="rvAdd"></span>' : '');
    $$('label[data-i]', lg).forEach((lab) => lab.addEventListener('click', () => {
      const s = series[+lab.dataset.i];
      if (!s) return;
      if (trade()) {
        if (!removable(s)) return;
        st.tsel.delete(s.key === 'all' ? ALL : s.book);
        rebuild();
        return;
      }
      if (s.key !== 'book') return;
      if (st.sel.has(s.book)) st.sel.delete(s.book); else st.sel.add(s.book);
      saveSel();
      rebuild();
    }));
    if (others.length) SE.select($('#rvAdd', lg), { options: [['', 'Add a line'], ...others], value: '', width: '190px', search: others.length > 10, onChange: (v) => { if (v) { st.tsel.add(v); rebuild(); } } });
  }

  // ---- the wave a trade bought: same direction, started at most 3 minutes before the buy, the one with the
  // triggering bookmaker in it near the bot's move if there is one, else the one nearest the move.
  function tradeWave() {
    const A = ana(), tr = trade();
    if (!A || !tr) return null;
    const b = botPlan(tr), d = td(), dir = (d.dir ?? 1) > 0 ? 1 : -1, mv = b.move_ms ?? b.buy_ms;
    const near = A.waves.filter((w) => w.dir === dir && w.T <= b.buy_ms + 5000 && w.T >= b.buy_ms - SE.waves.P.WINDOW);
    if (!near.length) return null;
    const withTrig = d.trigger ? near.filter((w) => w.on[d.trigger] != null && Math.abs(w.on[d.trigger] - mv) <= 30000) : [];
    return (withTrig.length ? withTrig : near).reduce((a, w) => (Math.abs(w.T - mv) < Math.abs(a.T - mv) ? w : a));
  }

  // ---- drawing (one canvas layer over the lines)
  function drawLayer(uu) {
    if (!G) return;
    const c = uu.ctx, dpr = devicePixelRatio || 1, { left, top, width, height } = uu.bbox;
    const X = (t) => uu.valToPos(t, 'x', true), Y = (v) => uu.valToPos(v, 'y', true);
    c.save();
    c.beginPath(); c.rect(left, top, width, height); c.clip();
    c.font = `${11 * dpr}px ${getComputedStyle(document.documentElement).getPropertyValue('--font')}`;
    c.textBaseline = 'top';
    c.textAlign = 'left'; // uPlot leaves it right-aligned from drawing the axes
    const tag = (x, y, text, color, right = false) => {
      const w = c.measureText(text).width + 10 * dpr;
      const bx = right ? Math.max(left, x - w - 4 * dpr) : Math.min(Math.max(x + 4 * dpr, left), left + width - w);
      c.fillStyle = color; c.globalAlpha *= 0.9; c.fillRect(bx, y, w, 16 * dpr); c.globalAlpha /= 0.9;
      c.fillStyle = '#fff'; c.fillText(text, bx + 5 * dpr, y + 2.5 * dpr);
    };
    const hline = (v, color, dash, x0 = left, x1 = left + width, w = 1.5) => {
      if (v == null) return;
      const y = Math.round(Y(v)) + 0.5;
      c.strokeStyle = color; c.lineWidth = w * dpr; c.setLineDash(dash.map((d) => d * dpr));
      c.beginPath(); c.moveTo(x0, y); c.lineTo(x1, y); c.stroke(); c.setLineDash([]);
    };
    const vline = (t, color, dash) => {
      if (t == null) return;
      const x = Math.round(X(t)) + 0.5;
      c.strokeStyle = color; c.lineWidth = 1.5 * dpr; c.setLineDash(dash.map((d) => d * dpr));
      c.beginPath(); c.moveTo(x, top); c.lineTo(x, top + height); c.stroke(); c.setLineDash([]);
    };
    const mark = (t, v, color, shape, r = 8) => {
      if (t == null || v == null) return;
      const x = X(t), y = Y(v), s = r * dpr;
      c.fillStyle = color; c.strokeStyle = 'rgba(0,0,0,.6)'; c.lineWidth = dpr;
      c.beginPath();
      if (shape === 'up') { c.moveTo(x, y - s); c.lineTo(x - s, y + s * 0.75); c.lineTo(x + s, y + s * 0.75); }
      else if (shape === 'diamond') { c.moveTo(x, y - s); c.lineTo(x + s, y); c.lineTo(x, y + s); c.lineTo(x - s, y); }
      else c.arc(x, y, s * 0.8, 0, 7);
      c.closePath(); c.fill(); c.stroke();
    };
    const path = (pts, color) => {
      const ok = pts.filter((p) => p[0] != null && p[1] != null);
      if (ok.length < 2) return;
      c.strokeStyle = color; c.lineWidth = 2.5 * dpr; c.setLineDash([2 * dpr, 4 * dpr]);
      c.beginPath(); ok.forEach(([t, v], i) => (i ? c.lineTo(X(t), Y(v)) : c.moveTo(X(t), Y(v)))); c.stroke(); c.setLineDash([]);
    };
    const seg = (sg, color) => {
      if (!sg) return;
      const x0 = X(sg.t0), y0 = Y(sg.y0), x1 = X(sg.t1), y1 = Y(sg.y1);
      c.lineCap = 'round';
      c.strokeStyle = 'rgba(0,0,0,.7)'; c.lineWidth = 7 * dpr; c.beginPath(); c.moveTo(x0, y0); c.lineTo(x1, y1); c.stroke();
      c.strokeStyle = color; c.lineWidth = 4 * dpr; c.beginPath(); c.moveTo(x0, y0); c.lineTo(x1, y1); c.stroke();
    };
    // A plan: cash-out, price cap, give-up, expected path, buy and sale.
    const drawPlan = (p, side, alpha, labels) => { // labels: draw the price and time tags
      c.globalAlpha = alpha;
      const end = p.sell_ms ?? p.give_ms;
      const buyV = p.entry != null ? tokC(p.entry, side) : tokC(buyPx(p.buy_ms, side), side);
      const sellV = p.sell_ms == null ? null : p.exit != null ? tokC(p.exit, side) : tokC(sellPx(p.sell_ms, side), side);
      hline(tokC(p.target, side), TC.cash, [7, 5]);
      if (p.cap != null && p.buy_ms != null) hline(tokC(p.cap, side), TC.cap, [4, 4], X(p.buy_ms), end != null ? X(end) : left + width, 2);
      vline(p.give_ms, TC.give, [7, 5]);
      if (p.buy_ms != null && p.target != null) path([[p.buy_ms, buyV], [Math.min(p.buy_ms + HOLD_MS, p.give_ms ?? Infinity), tokC(p.target, side)], [p.give_ms, tokC(p.target, side)]], fade(TC.cash, 0.75));
      mark(p.buy_ms, buyV, TC.buy, 'up');
      mark(p.sell_ms, sellV, TC.sell, 'diamond');
      if (labels) {
        // A plan in the other token than the chart's: its own price, and where that is on this chart.
        const px = (v) => (side === chartSide() ? cents(v) : `${cents(v)} ${side === 'no' ? 'No' : 'Yes'} (${cents(1 - v)} here)`);
        if (p.target != null) tag(left + width, Y(tokC(p.target, side)) - 18 * dpr, `cash out ${px(p.target)}`, TC.cash, true);
        if (p.cap != null && p.buy_ms != null) tag(X(p.buy_ms), Y(tokC(p.cap, side)) + 3 * dpr, `cap ${px(p.cap)}`, '#b7841d');
        if (p.give_ms != null) tag(X(p.give_ms), top + 4 * dpr, `gives up ${SE.fmtTime(p.give_ms).slice(0, 5)} · sells at any price`, TC.give, true);
      }
      c.globalAlpha = 1;
    };

    // Waves (Analysis): shaded from the first onset in any line to Polymarket's when bookmakers were ahead
    // (the window a bot can use), a thin blue mark when Polymarket moved first; ticks at the top where each
    // line joined the wave (Polymarket and the superline taller). The trade's own wave is outlined.
    const A = ana();
    if (A) {
      const xmin = uu.scales.x.min, xmax = uu.scales.x.max, flip = chartSide() === 'yes' ? 1 : -1, tw = tradeWave();
      const rowEnd = [-1e9];
      for (const w of A.waves) {
        let tEnd = w.T;
        for (const t of Object.values(w.on)) if (t > tEnd) tEnd = t;
        if (tEnd < xmin || w.T > xmax) continue;
        const x0 = X(w.T), xp = w.pm_on != null ? X(w.pm_on) : null, booksFirst = w.leader !== PMK;
        if (booksFirst && xp != null && xp > x0) { c.fillStyle = w === tw ? 'rgba(240,138,46,.28)' : 'rgba(240,138,46,.12)'; c.fillRect(x0, top, xp - x0, height); }
        else { c.fillStyle = w === tw ? 'rgba(74,140,255,.6)' : 'rgba(74,140,255,.25)'; c.fillRect(x0 - dpr, top, 2 * dpr, height); }
        for (const [s, t] of Object.entries(w.on)) {
          const big = s === PMK || s === SUPER;
          c.fillStyle = s === PMK ? TC.pm : s === SUPER ? TC.sup : SE.bookColor(s);
          c.fillRect(X(t) - 1.5 * dpr, top, 3 * dpr, (big ? 14 : 8) * dpr);
        }
        if (w === tw) { c.strokeStyle = '#fff'; c.lineWidth = dpr; c.setLineDash([3 * dpr, 3 * dpr]); c.strokeRect(x0, top + dpr, Math.max(3 * dpr, X(tEnd) - x0), height - 2 * dpr); c.setLineDash([]); }
        if (tw && w !== tw) continue; // on a trade only its own wave is labelled
        const text = `${w.dir * flip > 0 ? '▲' : '▼'} ${(w.amp * 100).toFixed(0)} pt · ${who(w.leader)} first${w.pm_on == null ? ' · PM did not move' : booksFirst ? ` · PM +${((w.pm_on - w.T) / 1000).toFixed(1)} s` : ''}`;
        const tw2 = c.measureText(text).width + 14 * dpr;
        const row = rowEnd.findIndex((end) => x0 >= end);
        if (row < 0) continue;
        rowEnd[row] = x0 + tw2;
        tag(x0, top + (22 + row * 18) * dpr, text, booksFirst ? '#8a5a1e' : '#2c4f8f');
      }
    }

    // Skipped moves (whole game): short ticks at the bottom, labelled where there is room.
    if (st.sig && !trade()) {
      const rowEnd = [-1e9, -1e9, -1e9];
      for (const [t, o, why] of G.signals) {
        if (o && o !== st.o) continue;
        const x = Math.round(X(t)) + 0.5;
        if (x < left || x > left + width) continue;
        c.strokeStyle = 'rgba(160,160,170,.8)'; c.lineWidth = dpr;
        c.beginPath(); c.moveTo(x, top + height - 14 * dpr); c.lineTo(x, top + height); c.stroke();
        const text = why.replace(/_/g, ' '), w = c.measureText(text).width + 14 * dpr;
        const row = rowEnd.findIndex((end) => x >= end);
        if (row < 0) continue;
        rowEnd[row] = x + w;
        tag(x, top + height - (32 + row * 18) * dpr, text, '#555');
      }
    }
    const tr = trade();
    if (tr) {
      const b = botPlan(tr), side = chartSide();
      if (b.be != null) { hline(tokC(b.be, side), TC.be, [2, 4], left, left + width, 1); tag(left, Y(tokC(b.be, side)) + 3 * dpr, `break-even ${cents(b.be)}`, '#666'); }
      // The bookmaker update the bot reacted to, on the triggering bookmaker's line.
      const ti = series.findIndex((s) => s.trig);
      if (b.move_ms != null && ti >= 0) {
        const xs = uu.data[0];
        let k = -1;
        for (let i = 0; i < xs.length && xs[i] <= b.move_ms + 1500; i++) k = i;
        if (k >= 0) mark(b.move_ms, uu.data[ti + 1][k], TC.move, 'dot', 7);
      }
      drawPlan(b, side, GHOST, false);
    }
    // Your plans: the one being edited in full, the others faint (your version only when it differs).
    for (const it of st.items) {
      if (!it.mine || it === st.plan || (it.type === 'main' && !differs(it))) continue;
      drawPlan({ ...it.mine, entry: null, exit: null }, it.side, 0.45, false);
      if (it.type === 'buy' && it.mine.buy_ms != null) tag(X(it.mine.buy_ms), Y(tokC(buyPx(it.mine.buy_ms, it.side), it.side)) + 11 * dpr, `buy ${nOf(it)}`, '#2b7a3d');
    }
    if (st.plan?.mine) drawPlan({ ...st.plan.mine, entry: null, exit: null }, st.plan.side, 1, true);
    // The line pairs (each the same movement on both markets), numbered; the one picked in full.
    const trig = tr ? td().trigger : null;
    const inC = (sg) => sg && { t0: sg.t0, t1: sg.t1, y0: yC(sg.y0), y1: yC(sg.y1) };
    for (const p of st.items) {
      if (p.type !== 'pair') continue;
      c.globalAlpha = p === st.pair ? 1 : 0.55;
      seg(inC(p.book), p.book?.book ? (p.book.book === trig ? TC.trig : SE.bookColor(p.book.book)) : '#fff');
      seg(inC(p.pm), TC.pm);
      const s0 = p.book || p.pm;
      if (s0) tag(X(s0.t0) - 18 * dpr, Y(yC(s0.y0)) - 8 * dpr, String(nOf(p)), p === st.pair ? '#000' : '#444');
      c.globalAlpha = 1;
    }
    if (st.paint) seg(st.paint, st.tool === 'pm' ? TC.pm : '#fff');
    c.restore();
    drawStrip(uu);
  }

  // ---- the lead strip (Analysis): who is ahead each second, and each line's up / down waves
  // Row 1, Lead: green = the superline moved and Polymarket has not caught up yet (expects Polymarket up),
  // red = the same downward, blue = Polymarket moved first (the superline is behind), grey = a gap open over
  // 3 minutes (a disagreement, not a lead), empty = level. Rows 2-3: trend waves (CUSUM), solid from onset to
  // the peak, faint until the evidence is spent, a white tick where the wave was detected.
  const LEAD_COL = { 1: 'rgba(52,199,89,.9)', 2: 'rgba(255,90,82,.9)', 3: 'rgba(74,140,255,.6)', 4: 'rgba(150,150,160,.35)' };
  const STRIP = { row: 15, gap: 5 };
  function drawStrip(uu) {
    const wrap = $('#rvStripWrap'), cv = $('#rvStrip'), A = ana(), Ld = A?.lead;
    if (!wrap || !cv) return;
    wrap.hidden = !Ld;
    if (!Ld) return;
    const dpr = devicePixelRatio || 1, W = uu.ctx.canvas.width, H = STRIP.gap + 3 * (STRIP.row + STRIP.gap);
    if (cv.width !== W || cv.height !== Math.round(H * dpr)) { cv.width = W; cv.height = Math.round(H * dpr); cv.style.width = W / dpr + 'px'; cv.style.height = H + 'px'; }
    const c = cv.getContext('2d'), { left, width } = uu.bbox, X = (t) => uu.valToPos(t, 'x', true);
    c.clearRect(0, 0, cv.width, cv.height);
    const rowY = (r) => (STRIP.gap + r * (STRIP.row + STRIP.gap)) * dpr, rh = STRIP.row * dpr;
    c.font = `${10 * dpr}px ${getComputedStyle(document.documentElement).getPropertyValue('--font')}`;
    c.textBaseline = 'middle';
    c.textAlign = 'right';
    ['Lead', 'Superline', 'Polymarket'].forEach((name, r) => {
      c.fillStyle = 'rgba(255,255,255,.035)'; c.fillRect(left, rowY(r), width, rh);
      c.fillStyle = r === 1 ? TC.sup : r === 2 ? TC.pm : '#9a9aa0'; c.fillText(name, left - 6 * dpr, rowY(r) + rh / 2);
    });
    c.save();
    c.beginPath(); c.rect(left, 0, width, cv.height); c.clip();
    const T = (i) => Ld.t0 + i * Ld.step;
    const i0 = Math.max(0, Math.floor((uu.scales.x.min - Ld.t0) / Ld.step)), i1 = Math.min(Ld.n - 1, Math.ceil((uu.scales.x.max - Ld.t0) / Ld.step));
    // Lead runs: consecutive seconds in one state are one bar.
    for (let i = i0; i <= i1;) {
      const v = Ld.state[i];
      let j = i;
      while (j + 1 <= i1 && Ld.state[j + 1] === v) j++;
      if (LEAD_COL[v]) { c.fillStyle = LEAD_COL[v]; const x0 = X(T(i)), x1 = X(T(j + 1)); c.fillRect(x0, rowY(0), Math.max(dpr, x1 - x0), rh); }
      i = j + 1;
    }
    // Trend waves; flipped colours when the chart is drawn in the No token (down there is up here).
    const flip = chartSide() === 'yes' ? 1 : -1;
    const waves = (eps, r) => {
      for (const e of eps) {
        if (e.end < i0 || e.on > i1) continue;
        const up = e.dir * flip > 0, col = up ? '52,199,89' : '255,90,82';
        const xa = X(T(e.on)), xp = X(T(e.peak)), xe = X(T(e.end)), xd = X(T(e.alarm));
        c.fillStyle = `rgba(${col},.85)`; c.fillRect(xa, rowY(r), Math.max(2 * dpr, xp - xa), rh);
        c.fillStyle = `rgba(${col},.25)`; c.fillRect(xp, rowY(r), Math.max(0, xe - xp), rh);
        c.fillStyle = '#fff'; c.fillRect(xd - dpr / 2, rowY(r), dpr, rh);
        if (xp - xa > 40 * dpr) { c.textAlign = 'left'; c.fillStyle = '#fff'; c.fillText(`${up ? '▲' : '▼'} ${(Math.abs(e.p_peak - e.p_on) * 100).toFixed(1)} pt`, xa + 4 * dpr, rowY(r) + rh / 2); }
      }
    };
    waves(Ld.sl_trends, 1);
    waves(Ld.pm_trends, 2);
    c.restore();
  }
  const STRIP_KEY = `<i class="rv-sw" style="background:${'rgba(52,199,89,.9)'}"></i>superline ahead, Polymarket should rise <i class="rv-sw" style="background:rgba(255,90,82,.9)"></i>should fall <i class="rv-sw" style="background:rgba(74,140,255,.6)"></i>Polymarket ahead <i class="rv-sw" style="background:rgba(150,150,160,.45)"></i>stale gap · waves: solid to the peak, white tick = detected. Hover for the numbers.`;
  // What the strip says at one moment (hover), in words.
  function leadAt(t) {
    const Ld = ana()?.lead;
    if (!Ld) return null;
    const i = Math.round((t - Ld.t0) / Ld.step);
    if (i < 0 || i >= Ld.n) return null;
    const v = Ld.state[i], pm = Ld.pm[i], sl = Ld.sl[i];
    const wave = (eps) => { const e = eps.find((w) => w.on <= i && i <= w.end); return e ? { e, live: i <= e.peak } : null; };
    return { i, t: Ld.t0 + i * Ld.step, state: v, pm, sl, z: Ld.z[i], exp: Ld.exp[i], age: Ld.age[i], slw: wave(Ld.sl_trends), pmw: wave(Ld.pm_trends), Ld };
  }
  const STATE_TXT = { 0: 'level: no lead', 1: 'superline ahead: Polymarket should rise', 2: 'superline ahead: Polymarket should fall', 3: 'Polymarket ahead: the superline is behind', 4: 'stale gap (open over 3 min): a disagreement, not a lead', 9: 'no price on one side' };
  function leadText(a) {
    if (!a) return '';
    const T = (i) => SE.fmtTime(a.Ld.t0 + i * a.Ld.step);
    const w = (x, name) => (x ? `${name} ${x.e.dir > 0 ? '▲' : '▼'} wave from ${T(x.e.on)} (detected ${T(x.e.alarm)}, ${x.live ? 'still moving' : `peaked ${T(x.e.peak)}`}, ${(Math.abs(x.e.p_peak - x.e.p_on) * 100).toFixed(1)} pt)` : `${name}: no wave`);
    return `<b>${SE.fmtTime(a.t)}</b> · ${STATE_TXT[a.state]}` +
      (Number.isFinite(a.pm) && Number.isFinite(a.sl) ? ` · superline ${(a.sl * 100).toFixed(1)}%, Polymarket ${(a.pm * 100).toFixed(1)}%${Number.isFinite(a.z) ? `, gap z ${a.z.toFixed(1)}` : ''}${a.age ? ` (open ${a.age} s)` : ''}` : '') +
      (Number.isFinite(a.exp) ? ` · expected Polymarket ${a.exp >= 0 ? '+' : ''}${(a.exp * 100).toFixed(1)} pt within 30 s` : '') +
      `<br>${w(a.slw, 'Superline')} · ${w(a.pmw, 'Polymarket')}`;
  }
  function stripHover(t) { const e = $('#rvStripInfo'); if (e) e.innerHTML = t == null ? STRIP_KEY : leadText(leadAt(t)) || STRIP_KEY; }

  // ---- dragging your plans, painting line pairs, placing buys
  function hitPlan(uu, it, x, y) {
    const m = it.mine;
    if (!m) return null;
    const X = (t) => uu.valToPos(t, 'x'), Y = (v) => uu.valToPos(v, 'y'), side = it.side;
    const near = (a, b, d) => a != null && b != null && Math.abs(a - b) <= d;
    if (m.buy_ms != null && near(x, X(m.buy_ms), 10) && near(y, Y(tokC(buyPx(m.buy_ms, side), side)), 14)) return 'buy';
    if (m.sell_ms != null && near(x, X(m.sell_ms), 10) && near(y, Y(tokC(sellPx(m.sell_ms, side), side)), 14)) return 'sell';
    if (it !== st.plan) return null; // a faint plan is picked by its buy or sale only
    if (m.give_ms != null && near(x, X(m.give_ms), 6)) return 'give';
    const end = m.sell_ms ?? m.give_ms;
    if (m.cap != null && near(y, Y(tokC(m.cap, side)), 6) && x >= X(m.buy_ms) - 6 && (end == null || x <= X(end) + 6)) return 'cap';
    if (m.target != null && near(y, Y(tokC(m.target, side)), 6)) return 'target';
    return null;
  }
  function hit(uu, x, y) {
    if (st.plan) { const h = hitPlan(uu, st.plan, x, y); if (h) return { it: st.plan, part: h }; }
    for (const it of st.items) if (it !== st.plan && (it.type === 'buy' || it.type === 'main')) { const h = hitPlan(uu, it, x, y); if (h) return { it, part: h }; }
    return null;
  }
  const CURSOR = { buy: 'ew-resize', sell: 'ew-resize', give: 'ew-resize', cap: 'ns-resize', target: 'ns-resize' };
  function attach(uu) {
    const over = uu.over;
    const ov = document.createElement('div');
    ov.className = 'rv-paint' + (st.tool ? ' on' : '');
    over.appendChild(ov);
    uu._rvOv = ov;
    const at = (e) => { const r = over.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    over.addEventListener('mousemove', (e) => { if (st.ana) stripHover(uu.posToVal(at(e).x, 'x')); });
    over.addEventListener('mouseleave', () => stripHover(null));
    const sc = $('#rvStrip');
    if (sc && !sc._rv) {
      sc._rv = true;
      sc.addEventListener('mousemove', (e) => { const r = over.getBoundingClientRect(); if (u) stripHover(u.posToVal(e.clientX - r.left, 'x')); });
      sc.addEventListener('mouseleave', () => stripHover(null));
    }
    stripHover(null);
    const val = (p) => ({ t: uu.posToVal(p.x, 'x'), v: uu.posToVal(p.y, 'y') });
    // Dragging a part of a plan: caught before uPlot sees the press (so it does not zoom).
    over.addEventListener('pointerdown', (e) => {
      if (st.tool || e.button !== 0) return;
      const p = at(e), h = hit(uu, p.x, p.y);
      if (!h) return;
      e.preventDefault(); e.stopPropagation();
      st.drag = h;
      if (h.it !== st.plan) { st.plan = h.it; st.focus = h.it; renderPanel(); uu.redraw(false, false); }
      over.setPointerCapture(e.pointerId);
    }, true);
    over.addEventListener('pointermove', (e) => {
      const p = at(e);
      if (!st.drag) { if (!st.tool) over.style.cursor = CURSOR[hit(uu, p.x, p.y)?.part] || ''; return; }
      const { t, v } = val(p), { it, part } = st.drag, m = it.mine, side = it.side;
      const tt = Math.round(t);
      if (part === 'buy') { m.buy_ms = tt; if (m.sell_ms != null && m.sell_ms <= tt) m.sell_ms = tt + 1000; if (m.give_ms != null && m.give_ms <= tt) m.give_ms = tt + 60000; }
      else if (part === 'sell') { m.sell_ms = Math.max(tt, (m.buy_ms ?? tt) + 1000); m.sellSet = true; }
      else if (part === 'give') m.give_ms = Math.max(tt, (m.buy_ms ?? tt) + 1000);
      else if (part === 'cap') m.cap = Math.min(0.99, Math.max(0.01, Math.round(fromC(v, side) * 1000) / 1000));
      else if (part === 'target') m.target = Math.min(0.99, Math.max(0.01, Math.round(fromC(v, side) * 1000) / 1000));
      // The sale follows the plan (cash-out price, give-up) until it is dragged by hand.
      if ((part === 'buy' || part === 'give' || part === 'target') && !m.sellSet) { const ps = planSale(m, side); if (ps) m.sell_ms = ps.t; }
      uu.redraw(false, false);
      renderPanel(true);
    });
    const endDrag = () => {
      if (!st.drag) return;
      const { it, part } = st.drag;
      st.drag = null;
      if (it.type === 'main' && differs(it)) it.kind = 'adjust';
      // A cash-out dragged below what the buy paid means the move is expected the other way: a Down buy
      // (the No token) of an Up one, and the other way round. The line stays where it was dropped.
      if (it.type === 'buy' && part === 'target') {
        const px = buyPx(it.mine.buy_ms, it.side);
        if (px != null && it.mine.target < px - 0.005) { setBuySide(it, it.side === 'yes' ? 'no' : 'yes', tokC(it.mine.target, it.side), true); rebuild(); }
      }
      renderPanel();
      changed(it);
    };
    over.addEventListener('pointerup', endDrag);
    over.addEventListener('pointercancel', endDrag);
    // Painting (K / P) and placing a buy (B) on the layer over the chart, while a tool is on.
    ov.addEventListener('mousedown', (e) => { if (st.tool) e.stopPropagation(); });
    ov.addEventListener('dblclick', (e) => { if (st.tool) e.stopPropagation(); });
    ov.addEventListener('pointerdown', (e) => {
      if (!st.tool || e.button !== 0) return;
      e.preventDefault(); e.stopPropagation();
      const { t, v } = val(at(e));
      if (st.tool === 'buy') { placeBuy(Math.round(t)); return; }
      st.paint = { t0: t, y0: v, t1: t, y1: v };
      ov.setPointerCapture(e.pointerId);
    });
    ov.addEventListener('pointermove', (e) => { if (!st.paint) return; const { t, v } = val(at(e)); st.paint.t1 = t; st.paint.y1 = v; uu.redraw(false, false); });
    ov.addEventListener('pointerup', () => {
      const p = st.paint;
      if (!p) return;
      st.paint = null;
      if (Math.abs(p.t1 - p.t0) < 1000) { uu.redraw(false, false); SE.toast('Press where the move starts and drag to where it ends.'); return; }
      const s = p.t1 < p.t0 ? { t0: Math.round(p.t1), y0: p.y1, t1: Math.round(p.t0), y1: p.y0 } : { t0: Math.round(p.t0), y0: p.y0, t1: Math.round(p.t1), y1: p.y1 };
      const yes = { t0: s.t0, t1: s.t1, y0: yesFromC(s.y0), y1: yesFromC(s.y1) };
      const slot = st.tool === 'pm' ? 'pm' : 'book';
      // Into the picked pair while it lacks this line (or was picked to repaint it); else a new pair.
      let pr = st.pair;
      if (!pr || (pr[slot] && !st.pairPick)) { pr = { type: 'pair', lid: null, o: outcome(), pm: null, book: null, note: '', analysis: null, host: null }; st.items.push(pr); }
      st.pairPick = false;
      if (slot === 'pm') pr.pm = yes;
      else { const bk = nearestBook(s) || pr.book?.book || null; pr.book = { ...yes, book: bk }; if (!bk) SE.toast('Pick the bookmaker below: no bookmaker line follows the painted one.'); }
      pr.analysis = null;
      st.pair = pr;
      st.focus = pr;
      setTool(pr[slot === 'pm' ? 'book' : 'pm'] ? null : slot === 'pm' ? 'book' : 'pm');
      renderPanel();
      uu.redraw(false, false);
      changed(pr);
    });
    ov.addEventListener('pointercancel', () => { st.paint = null; uu.redraw(false, false); });
  }
  const HINT = { book: 'Painting a bookmaker line: press where its move starts, drag to where it ends.', pm: 'Painting the Polymarket line: press where its move starts, drag to where it ends.', buy: 'Click the moment it should have bought.' };
  function setTool(t) {
    st.tool = t;
    u?._rvOv?.classList.toggle('on', !!t);
    const h = $('#rvHint');
    if (h) h.textContent = t ? HINT[t] + ' (Esc to stop)' : `${st.plan?.mine ? 'Drag the buy ▲, the sale ◆, the cash-out and cap lines or the give-up line. ' : ''}B places a buy of your own (drag its cash-out below the price for a Down buy); K and P paint the same move on a bookmaker and on Polymarket (K again starts another pair). Everything saves by itself. Drag elsewhere to zoom, double-click to reset.`;
    $$('.rv-tool').forEach((b) => b.classList.toggle('on', b.dataset.tool === t));
  }
  // A buy of your own: buy there, cash out 5c higher, give up after 2 minutes, pay at most the price there.
  function defaultPlan(t, side) {
    const px = buyPx(t, side);
    if (px == null) return null;
    const m = { buy_ms: t, sell_ms: null, target: Math.min(0.99, Math.round((px + 0.05) * 1000) / 1000), give_ms: t + GIVE_MS, cap: Math.round(px * 1000) / 1000 };
    const ps = planSale(m, side);
    if (ps) m.sell_ms = ps.t;
    return m;
  }
  // Up (buy Yes of the outcome) or Down (buy its No): the cap becomes the price there of the new token, the
  // cash-out either the line where it was dropped (targetC, chart cents) or 5c over the new price.
  function setBuySide(it, side, targetC = null, auto = false) {
    const m = it.mine, px = buyPx(m.buy_ms, side);
    if (px == null) { SE.toast('No Polymarket price there for that side.'); return false; }
    it.side = side;
    m.cap = Math.round(px * 1000) / 1000;
    m.target = Math.min(0.99, Math.max(0.01, Math.round((targetC != null ? fromC(targetC, side) : px + 0.05) * 1000) / 1000));
    if (!m.sellSet) { const ps = planSale(m, side); if (ps) m.sell_ms = ps.t; }
    if (auto) SE.toast(`Buy ${nOf(it)} is now a ${side === 'no' ? 'Down' : 'Up'} buy: its cash-out is on the ${side === 'no' ? 'falling' : 'rising'} side of the price.`, 5000);
    return true;
  }
  function placeBuy(t) {
    const side = trade() ? chartSide() : st.side, mine = defaultPlan(t, side);
    if (!mine) { SE.toast('No Polymarket price there.'); return; }
    const it = { type: 'buy', lid: null, o: outcome(), side, mine, note: '', pairIn: null };
    st.items.push(it);
    st.plan = it;
    st.focus = it;
    setTool(null);
    rebuild();
    renderPanel();
    changed(it);
  }
  // The bookmaker whose line follows a painted line best (13 points along it); the shown ones first.
  function nearestBook(sg) {
    if (!u) return null;
    const xs = u.data[0];
    const idx = (t) => { let lo = 0, hi = xs.length - 1, k = 0; while (lo <= hi) { const m = (lo + hi) >> 1; if (xs[m] <= t) { k = m; lo = m + 1; } else hi = m - 1; } return k; };
    const pts = Array.from({ length: 13 }, (_, j) => { const f = j / 12; return [idx(sg.t0 + f * (sg.t1 - sg.t0)), sg.y0 + f * (sg.y1 - sg.y0)]; });
    const fit = (ok) => {
      let best = null;
      series.forEach((s, i) => {
        if (s.key !== 'book' || !ok(s)) return;
        let sum = 0, n = 0;
        for (const [k, y] of pts) { const v = u.data[i + 1][k]; if (v != null) { sum += Math.abs(v - y); n++; } }
        if (n >= 7 && (!best || sum / n < best.d)) best = { b: s.book, d: sum / n };
      });
      return best;
    };
    const shown = fit((s) => lineColor(s) === s.base);
    if (shown && shown.d <= 8) return shown.b;
    return fit(() => true)?.b || shown?.b || null;
  }

  // ---- your marks: the plan's numbers, the verdict, the pairs, the note
  function renderPanel(numbersOnly = false) {
    const box = $('#rvPanel');
    if (!box || !G || dead) return;
    const tr = trade(), it = st.plan, m = it?.mine, side = it?.side ?? planSide();
    const b = it?.type === 'main' ? botPlan(it.trade) : null;
    const t5 = (t) => (t == null ? '-' : SE.fmtTime(t));
    const mineBuy = m?.buy_ms != null ? buyPx(m.buy_ms, side) : null, mineSell = m?.sell_ms != null ? sellPx(m.sell_ms, side) : null;
    const res = (a, z) => (a != null && z != null ? `<span class="${z - a > 0 ? 'up' : z - a < 0 ? 'down' : ''}">${z - a >= 0 ? '+' : ''}${((z - a) * 100).toFixed(1)}¢</span>` : '-');
    const ps = planSale(m, side);
    const row = (label, bot, yours, tip = '') => `<tr${tip ? ` data-tip="${esc(tip)}"` : ''}><td class="muted">${label}</td>${b ? `<td class="r">${bot}</td>` : ''}<td class="r"><b>${yours}</b></td></tr>`;
    const dirName = (p) => `${p.side === 'no' ? 'Down' : 'Up'} (${p.side === 'no' ? 'No' : 'Yes'} ${outName(p.o)})`;
    const head = it?.type === 'buy' ? `Your buy ${nOf(it)} · ${dirName(it)}` : 'Yours';
    const table = m ? `<table class="tbl rv-cmp"><thead><tr><th></th>${b ? '<th class="r">Bot</th>' : ''}<th class="r">${head}</th></tr></thead><tbody>
      ${row('Buy', b ? `${t5(b.buy_ms)} · ${cents(b.entry)}` : '', m.buy_ms != null ? `${t5(m.buy_ms)} · ${cents(mineBuy)}` : '-')}
      ${row('Sell', b ? (b.sell_ms != null ? `${t5(b.sell_ms)} · ${cents(b.exit)}` : 'open') : '', m.sell_ms != null ? `${t5(m.sell_ms)} · ${cents(mineSell)}` : '-')}
      ${row('Result a share', b ? res(b.entry, b.exit) : '', res(mineBuy, mineSell), 'Sell price minus buy price, before fees')}
      ${row('Cash out at', b ? cents(b.target) : '', cents(m.target), 'It sells as soon as Polymarket\'s sell price reaches this')}
      ${row('Price cap', b ? cents(b.cap) : '', cents(m.cap), 'The most it pays for the token')}
      ${row('Gives up', b ? t5(b.give_ms) : '', t5(m.give_ms), 'From here it sells at any price')}
      ${ps ? row('Plan sells', '', `${t5(ps.t)} · ${cents(ps.px)} (${ps.why})`, 'Where this cash-out price and give-up time would have sold') : ''}
      </tbody></table>` : '<p class="muted">No buy of your own on this outcome yet: press B (or Buy here) and click where it should have bought.</p>';
    if (numbersOnly && $('#rvCmp', box)) { $('#rvCmp', box).innerHTML = table; return; }
    const plans = st.items.filter((i) => i.type === 'main' || i.type === 'buy'), pairs = st.items.filter((i) => i.type === 'pair');
    const ix = (i) => st.items.indexOf(i);
    const planChip = (p) => `<button class="chip-btn${p === st.plan ? ' on' : ''}" data-plan="${ix(p)}">${p.type === 'main' ? `${ic('tune')}Your version of the bot's trade` : `${ic(p.side === 'no' ? 'trending_down' : 'trending_up')}Buy ${nOf(p)} · ${p.side === 'no' ? 'Down' : 'Up'} ${SE.fmtTime(p.mine.buy_ms).slice(0, 8)}<span class="rv-x" data-del="${ix(p)}" data-tip="Delete this buy">×</span>`}</button>`;
    const pairChip = (p) => { const d = pairDelay(p); return `<button class="chip-btn${p === st.pair ? ' on' : ''}" data-pair="${ix(p)}" data-tip="Pick this pair (to see it, or repaint one of its lines with K / P)">${nOf(p)} · ${p.book?.book ? esc(SE.bookName(p.book.book)) : p.book ? 'which bookmaker?' : 'no bookmaker line'}${p.pm ? (d != null ? ` · PM ${secs(d)}` : '') : ' · no Polymarket line'}<span class="rv-x" data-del="${ix(p)}" data-tip="Delete this pair">×</span></button>`; };
    const main = st.items.find((i) => i.type === 'main');
    const verdict = main ? `<div class="rv-row"><span class="muted">Verdict on the bot's trade</span><div id="rvVerdict"></div><button class="btn sm" id="rvReset" data-tip="Your version back to the bot's">Reset to the bot's</button></div>` : '';
    const n = noteItem();
    box.innerHTML = `<div class="rv-row rv-plans">${plans.map(planChip).join('')}<button class="btn sm rv-tool" data-tool="buy">${ic('add_shopping_cart')}Buy here <kbd>B</kbd></button></div>
      <div id="rvCmp">${table}</div>
      ${it?.type === 'buy' ? `<div class="rv-row"><span class="muted">Buy ${nOf(it)} expects the chance of ${outName(it.o)} to go</span><div id="rvDir"></div>${it.mine.sellSet ? '<button class="btn sm" id="rvSellPlan" data-tip="Put the sale back where the cash-out price and give-up time sell">Sale by the plan</button>' : ''}</div>` : ''}
      ${verdict}
      <div class="rv-row"><span class="muted">Same move</span><button class="btn sm rv-tool" data-tool="book">${ic('brush')}Bookmaker line <kbd>K</kbd></button><button class="btn sm rv-tool" data-tool="pm">${ic('brush')}Polymarket line <kbd>P</kbd></button>${pairs.map(pairChip).join('')}</div>
      <div class="rv-read" id="rvPairRead"></div>
      <textarea id="rvNote" class="rv-note-in" placeholder="Note on ${esc(itemName(n))} (optional): what the bot should have seen">${esc(n?.note || '')}</textarea>`;
    if (it?.type === 'buy') {
      SE.seg($('#rvDir', box), [['yes', 'Up', 'Buy Yes: the chance goes up'], ['no', 'Down', 'Buy No: the chance goes down']], it.side, (v) => { if (v !== it.side && setBuySide(it, v)) { rebuild(); renderPanel(); changed(it); } });
      $('#rvSellPlan', box)?.addEventListener('click', () => { it.mine.sellSet = false; const ps = planSale(it.mine, it.side); if (ps) it.mine.sell_ms = ps.t; u?.redraw(false, false); renderPanel(); changed(it); });
    }
    if (main) SE.seg($('#rvVerdict', box), [['', 'None'], ['ok', 'Right'], ['no_buy', 'Wrong: shouldn\'t have bought'], ['adjust', 'Fix: my version']], main.kind || '', (v) => { main.kind = v || null; changed(main); });
    $$('.rv-tool', box).forEach((b2) => b2.addEventListener('click', () => setTool(st.tool === b2.dataset.tool ? null : b2.dataset.tool)));
    $$('[data-plan]', box).forEach((b2) => b2.addEventListener('click', () => { st.plan = st.items[+b2.dataset.plan]; st.focus = st.plan; renderPanel(); u?.redraw(false, false); }));
    $$('[data-pair]', box).forEach((b2) => b2.addEventListener('click', () => { const p = st.items[+b2.dataset.pair]; st.pair = p; st.pairPick = true; st.focus = p; renderPanel(); u?.redraw(false, false); }));
    $$('.rv-x', box).forEach((x) => x.addEventListener('click', (e) => { e.stopPropagation(); removeItem(st.items[+x.dataset.del]); }));
    $('#rvReset', box)?.addEventListener('click', () => { main.mine = botMine(main.trade); if (main.kind === 'adjust') main.kind = null; st.plan = main; rebuild(); renderPanel(); setTool(null); changed(main); });
    $('#rvNote', box).addEventListener('input', (e) => {
      let ni = noteItem();
      if (!ni) { ni = { type: 'note', lid: null, o: outcome(), note: '' }; st.items.push(ni); st.focus = ni; }
      ni.note = e.target.value;
      changed(ni);
    });
    renderPairText();
    setTool(st.tool);
  }
  function renderPairText() {
    const e = $('#rvPairRead');
    if (!e || dead) return;
    const p = st.pair, pairs = st.items.filter((i) => i.type === 'pair');
    if (!p) { e.innerHTML = `<span class="faint">${pairs.length ? 'Click a pair to see it or repaint one of its lines. ' : ''}K paints a bookmaker move, P the same move on Polymarket; K again starts another pair.</span>`; return; }
    const name = p.book?.book ? SE.bookName(p.book.book) : 'the bookmaker';
    let text;
    if (!p.pm || !p.book) text = `<span class="faint">Pair ${nOf(p)}: ${p.pm ? 'Polymarket' : 'bookmaker'} line painted; paint the ${p.pm ? 'bookmaker' : 'Polymarket'} line too (${p.pm ? 'K' : 'P'}) for the delay.</span>`;
    else text = `Pair ${nOf(p)}: Polymarket moved ${rel((p.pm.t0 + p.pm.t1) / 2 - (p.book.t0 + p.book.t1) / 2, esc(name))} as painted. ${p.analysis ? 'On the recorded prices: ' + analysisText(p.analysis) : 'Measured on the recorded prices once saved.'}`;
    e.innerHTML = text + (p.book ? ' <span id="rvBk"></span>' : '');
    if (p.book) SE.select($('#rvBk', e), { options: Object.keys(G.books).filter((b2) => G.books[b2][outcome()]).sort().map((b2) => [b2, SE.bookName(b2), '', SE.logo(b2)]), value: p.book.book || '', placeholder: 'Which bookmaker?', width: '170px', onChange: (v) => { p.book.book = v; p.analysis = null; renderPanel(); u?.redraw(false, false); changed(p); } });
  }

  // ---- saving: every mark is saved by itself, half a second after its last change, one at a time
  const differs = (it) => {
    if (!it || it.type !== 'main' || !it.mine) return false;
    const b = botMine(it.trade), m = it.mine, near = (x, y, d) => (x == null && y == null) || (x != null && y != null && Math.abs(x - y) <= d);
    return !(near(m.buy_ms, b.buy_ms, 500) && near(m.sell_ms, b.sell_ms, 500) && near(m.give_ms, b.give_ms, 500) && near(m.target, b.target, 0.0005) && near(m.cap, b.cap, 0.0005));
  };
  const segOut = (s) => ({ t0: s.t0, t1: s.t1, y0: s.y0, y1: s.y1, ...(s.book ? { book: s.book } : {}) });
  const planOut = (m) => ({ buy_ms: m.buy_ms, sell_ms: m.sell_ms ?? null, cap: m.cap, target: m.target ?? null, give_up_ms: m.give_ms ?? null, sell_set: !!m.sellSet });
  const hasLines = (p) => !!(p && (p.pm || p.book?.book));
  const linesOut = (p) => { const o = {}; if (p?.pm) o.pm_seg = segOut(p.pm); if (p?.book?.book) o.book_seg = segOut(p.book); return o; };
  // What a mark sends (null = nothing left: delete it).
  function bodyOf(it) {
    if (it.dead) return null;
    const books_shown = (trade() ? [td().trigger, ...st.tsel] : [...st.sel]).filter((x) => x && G.books[x]);
    const base = { id: it.lid, match_id: G.match.id, outcome: it.o, note: it.note || '', books_shown };
    if (it.type === 'main') {
      const d = differs(it), kind = it.kind || (d ? 'adjust' : hasLines(it.pairIn) ? 'move' : it.note.trim() ? 'note' : null);
      if (!kind) return null;
      const b = { ...base, kind, side: it.side, ...linesOut(it.pairIn) };
      if ((kind === 'adjust' || d) && it.mine) Object.assign(b, planOut(it.mine));
      if (it.trade.bot === 'replay') b.replay_trade = it.trade.data; else b.trade_id = it.trade.id;
      return b;
    }
    if (it.type === 'buy') return it.mine?.buy_ms != null ? { ...base, kind: 'buy', side: it.side, ...planOut(it.mine), ...linesOut(it.pairIn) } : null;
    if (it.type === 'pair') return hasLines(it) ? { ...base, kind: 'move', side: 'yes', ...linesOut(it) } : null;
    return it.note.trim() ? { ...base, kind: 'note', side: 'yes' } : null;
  }
  const timers = new Map();
  let chain = Promise.resolve();
  function setStatus(text, bad = false) { const e = $('#rvSave'); if (e && !dead) { e.textContent = text; e.classList.toggle('down', bad); } }
  function changed(it) {
    const tgt = it.host || it;
    clearTimeout(timers.get(tgt));
    setStatus('Saving…');
    timers.set(tgt, setTimeout(() => { timers.delete(tgt); chain = chain.then(() => saveItem(tgt)); }, 500));
  }
  async function saveItem(it) {
    const b = bodyOf(it);
    try {
      if (!b) {
        if (it.lid) {
          const lid = it.lid;
          await api('/review/labels/' + lid, { method: 'DELETE' });
          G.labels = G.labels.filter((l) => l.id !== lid);
          it.lid = null;
          setStatus(`Mark #${lid} deleted`);
        } else setStatus('Nothing to save');
      } else {
        const saved = await api('/review/labels', { body: b });
        it.lid = saved.id;
        const i = G.labels.findIndex((l) => l.id === saved.id);
        if (i >= 0) G.labels[i] = saved; else G.labels.push(saved);
        const p = it.type === 'pair' ? it : it.pairIn;
        if (p) p.analysis = saved.analysis || null;
        if (it.dead) chain = chain.then(() => saveItem(it)); // deleted while it was saving
        setStatus(`Saved ${SE.fmtTime(Date.now())}${saved.analysis ? ' · ' + analysisText(saved.analysis) : ''}`);
      }
      renderMarks();
      renderTrades();
      renderPairText();
    } catch (e) { setStatus('Not saved: ' + e.message, true); }
  }
  // Off this view (and deleted, through the save chain); a pair kept inside an older mark is taken out of it.
  function dropItems(gone) {
    st.items = st.items.filter((i) => !gone.has(i));
    if (gone.has(st.plan)) st.plan = st.items.find((i) => i.type === 'main') || null;
    if (gone.has(st.pair)) st.pair = null;
    if (gone.has(st.focus)) st.focus = st.plan;
  }
  function removeItem(it) {
    if (!it) return;
    const lid = it.lid ?? it.host?.lid;
    if (lid && !confirm(`Delete ${itemName(it)} (mark #${lid})?${it.pairIn ? ' Its painted lines go with it.' : ''}`)) return;
    if (it.host) { it.host.pairIn = null; changed(it.host); }
    else { it.dead = true; clearTimeout(timers.get(it)); timers.delete(it); chain = chain.then(() => saveItem(it)); }
    dropItems(new Set([it, it.pairIn].filter(Boolean)));
    rebuild();
    renderPanel();
  }
  async function delMark(lid) {
    if (!lid || !confirm(`Delete mark #${lid}?`)) return;
    try {
      await api('/review/labels/' + lid, { method: 'DELETE' });
      G.labels = G.labels.filter((l) => l.id !== lid);
      const gone = new Set();
      for (const it of st.items) {
        if (it.lid !== lid) continue;
        clearTimeout(timers.get(it));
        timers.delete(it);
        if (it.pairIn) { gone.add(it.pairIn); it.pairIn = null; }
        if (it.type === 'main') { it.lid = null; it.kind = null; it.note = ''; it.mine = botMine(it.trade); }
        else { it.lid = null; it.dead = true; gone.add(it); }
      }
      dropItems(gone);
      rebuild();
      renderPanel();
      renderMarks();
      renderTrades();
      setStatus(`Mark #${lid} deleted`);
    } catch (e) { SE.toast(e.message); }
  }
  const analysisText = (a) => {
    const delay = a.delay_measured_ms ?? a.delay_painted_ms;
    const warn = a.warnings?.length ? ` Check this mark: ${a.warnings.join('; ')}.` : '';
    return `Polymarket moved ${rel(delay)}${a.delay_measured_ms == null ? ' (as painted)' : ''}${a.ratio != null ? `, ${a.ratio.toFixed(2)}× as far` : ''}${a.corr_best != null ? `; best correlation ${a.corr_best.toFixed(2)} with Polymarket ${rel(a.best_lag_ms).replace(' the bookmaker', '')}` : ''}.${warn}`;
  };
  // A saved mark back into the editor: its trade (or the whole game on its outcome), picked.
  function edit(l) {
    const ti = G.trades.findIndex((t) => sameTrade(l, t));
    if (ti >= 0) { if (st.view !== 'trade' || st.ti !== ti) openTrade(ti, false); }
    else if (outcome() !== l.outcome) { st.view = 'game'; st.ti = -1; st.o = l.outcome; st.tsel = new Set(); loadItems(); }
    const it = st.items.find((i) => i.lid === l.id);
    if (it) {
      if (it.type === 'main' || it.type === 'buy') st.plan = it;
      if (it.type === 'pair') st.pair = it; else if (it.pairIn) st.pair = it.pairIn;
      st.focus = it;
    }
    renderAll(true);
    $('#rvChart')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  function renderMarks() {
    const box = $('#rvMarks');
    if (!box || dead) return;
    if (!G.labels.length) { box.innerHTML = '<div class="empty">No marks on this game yet.</div>'; return; }
    const onView = new Set(st.items.map((i) => i.lid).filter(Boolean));
    box.innerHTML = G.labels.map((l) => {
      const bits = [];
      if (l.buy_ms != null) bits.push(`buy ${SE.fmtTime(l.buy_ms)} at ${cents(l.snapshot?.buy?.price)}`);
      if (l.sell_ms != null) bits.push(`sell ${SE.fmtTime(l.sell_ms)} at ${cents(l.snapshot?.sell?.price)}`);
      if (l.target != null) bits.push(`cash out ${cents(l.target)}`);
      if (l.cap != null) bits.push(`cap ${cents(l.cap)}`);
      if (l.give_up_ms != null) bits.push(`gives up ${SE.fmtTime(l.give_up_ms).slice(0, 5)}`);
      const about = l.trade_id != null ? `bot trade #${l.trade_id}` : l.trade?.bot === 'replay' ? `replay trade at ${SE.fmtTime(l.trade.data?.ts).slice(0, 5)}` : '';
      return `<div class="rv-mark${onView.has(l.id) ? ' on' : ''}">
        <div class="rv-mark-head"><b>#${l.id}</b><span class="chip ${l.kind === 'no_buy' ? 'bad' : l.kind === 'ok' ? 'ok' : 'accent'}">${esc(KIND_NAME[l.kind] || l.kind)}</span><span>${l.side === 'no' ? 'No' : 'Yes'} ${outName(l.outcome)}</span>${about ? `<span class="muted">${about}</span>` : ''}
          <span class="rv-acts" style="margin-left:auto"><button class="btn sm" data-e="${l.id}">Open</button><button class="btn sm" data-d="${l.id}">${ic('delete')}</button></span></div>
        ${bits.length ? `<div class="rv-read">${bits.join(' · ')}</div>` : ''}
        ${l.book_seg ? `<div class="rv-read">${esc(SE.bookName(l.book_seg.book))} and Polymarket: ${l.analysis ? analysisText(l.analysis) : ''}</div>` : ''}
        ${l.note ? `<div class="rv-note">${esc(l.note)}</div>` : ''}</div>`;
    }).join('');
    $$('button[data-e]', box).forEach((b) => b.addEventListener('click', () => { const l = G.labels.find((x) => x.id === +b.dataset.e); if (l) edit(l); }));
    $$('button[data-d]', box).forEach((b) => b.addEventListener('click', () => delMark(+b.dataset.d)));
  }

  // ---- Analysis: the trade's wave, and every line's measured delay and correlation on this game
  function tradeWaveHtml(A) {
    const tr = trade(), b = botPlan(tr), d = td(), trig = d.trigger, w = tradeWave();
    if (!w) return `<div class="rv-wave"><b>No wave before this buy.</b> <span class="muted">Nothing moved ${(d.dir ?? 1) > 0 ? 'up' : 'down'} by 3 points or more (and 0.12 in log-odds) within 20 s and held, in any line, in the 3 minutes before the buy: the bot bought a move too small or too slow to count as a wave.</span></div>`;
    const flip = chartSide() === 'yes' ? 1 : -1;
    const ev = Object.entries(w.on).map(([s, t]) => ({ s, t })).concat([{ s: '', t: b.buy_ms }]).sort((x, y) => x.t - y.t);
    const chip = (e) => {
      const off = `${e.t - w.T >= 0 ? '+' : ''}${((e.t - w.T) / 1000).toFixed(1)} s`;
      if (!e.s) return `<span class="chip ok">${ic('shopping_cart')}Bot bought ${off}</span>`;
      const color = e.s === PMK ? TC.pm : e.s === SUPER ? TC.sup : SE.bookColor(e.s);
      return `<span class="chip${e.s === trig ? ' accent' : ''}"><i class="rv-sw" style="background:${color}"></i>${esc(who(e.s))}${e.s === trig ? ' (trigger)' : ''} ${off}</span>`;
    };
    const pmAtBuy = SE.waves.valueAt(A.pm, b.buy_ms);
    const done = w.pm_base != null && pmAtBuy != null ? (pmAtBuy - w.pm_base) * w.dir / w.amp : null;
    const lines = [];
    if (w.leader === PMK) lines.push(`Polymarket moved first${w.first_book ? `, ${((w.on[w.first_book] - w.pm_on) / 1000).toFixed(1)} s before the first bookmaker (${esc(who(w.first_book))})` : ''}: the bookmakers were catching up with a move Polymarket had already made.`);
    else if (w.pm_on == null) lines.push('Polymarket did not join this wave (no move of its own within 3 minutes).');
    else lines.push(`${esc(who(w.leader))} moved first, ${((w.pm_on - w.T) / 1000).toFixed(1)} s before Polymarket.`);
    if (trig) lines.push(w.on[trig] == null ? `The trigger (${esc(who(trig))}) is not in this wave: its move was too small or too slow to count.` : w.pm_on != null ? `The trigger (${esc(who(trig))}) moved ${rel(w.on[trig] - w.pm_on, 'Polymarket')}.` : '');
    if (done != null) lines.push(`When the bot bought, Polymarket had made ${pct(Math.max(-1, Math.min(2, done)))} of the wave${done >= 0.7 ? ': little was left to catch.' : done <= 0.3 ? ': most of it was still to come.' : '.'}`);
    return `<div class="rv-wave"><div><b>The wave this trade bought: ${w.dir * flip > 0 ? '▲' : '▼'} ${(w.amp * 100).toFixed(1)} points</b> at ${SE.fmtTime(w.T)} <span class="muted">(${Object.keys(w.on).length} lines, ${w.families.length} bookmaker ${w.families.length === 1 ? 'family' : 'families'}; times from its first onset)</span></div><div class="rv-order">${ev.map(chip).join('')}</div><div class="rv-read">${lines.filter(Boolean).join(' ')}</div></div>`;
  }
  function renderAnalysis() {
    const wrap = $('#rvAnaWrap'), box = $('#rvAnaBox');
    if (!wrap || !box || !G || dead) return;
    wrap.hidden = !st.ana;
    if (!st.ana) return;
    const A = ana();
    if (!A || !A.pm.length) { box.innerHTML = '<p class="muted">No usable Polymarket price on this outcome (not recorded, or the spread was over 6¢), so there is nothing to measure against.</p>'; return; }
    const tr = trade(), trig = tr ? td().trigger : null;
    const stat = (s) => {
      const part = A.waves.filter((w) => w.on[s] != null), leads = part.filter((w) => w.pm_on != null).map((w) => w.pm_on - w.on[s]);
      return { n: part.length, first: part.length ? part.filter((w) => w.leader === s).length / part.length : null, before: leads.length ? leads.filter((x) => x > 0).length / leads.length : null, lead: median(leads) };
    };
    const count = (s) => A.waves.filter((w) => w.on[s] != null).length;
    const order = Object.keys(A.books).sort((a, b) => count(b) - count(a));
    const srcs = [...(A.superline.length ? [SUPER] : []), ...(trig && A.books[trig] ? [trig] : []), ...order.filter((b) => b !== trig)];
    const g = SCORES?.sources || {};
    const both = A.waves.filter((w) => w.pm_on != null && Object.keys(w.on).length > 1);
    // Hayashi-Yoshida's normalised value can pass ±1 on few moves: shown from MIN_MOVES on, capped.
    const rhoTxt = (cr) => (cr?.rho == null ? '-' : cr.n < MIN_MOVES ? `<span class="faint" data-tip="Only ${cr.n} moves on this line: too few to measure">-</span>` : Math.max(-1, Math.min(1, cr.rho)).toFixed(2));
    const th = (label, tip) => `<th class="r" data-tip="${esc(tip)}">${label}</th>`;
    let html = tr ? tradeWaveHtml(A) : '';
    const Ld = A.lead;
    if (Ld) {
      if (tr) { const a = leadAt(botPlan(tr).buy_ms); if (a) html += `<p class="rv-read"><b>At the bot's buy:</b> ${leadText(a)}</p>`; }
      // Superline-led gaps on this game: did Polymarket move half the gap their way within 60 s?
      let n = 0, right = 0, wrong = 0;
      const stc = [0, 0, 0, 0, 0];
      for (let i = 0; i < Ld.n; i++) {
        const v = Ld.state[i];
        if (v <= 4) stc[v]++;
        const ahead = v === 1 || v === 2;
        if (!ahead || (i > 0 && Ld.state[i - 1] === v) || i + 60 >= Ld.n || !Number.isFinite(Ld.e[i])) continue;
        n++;
        const x0 = SE.waves.logit(Ld.pm[i]), sg = Math.sign(Ld.e[i]);
        let hit = 0;
        for (let j = i + 1; j <= i + 60 && !hit; j++) { if (!Number.isFinite(Ld.pm[j])) continue; const m = (SE.waves.logit(Ld.pm[j]) - x0) * sg; if (m >= 0.5 * Math.abs(Ld.e[i])) hit = 1; else if (m <= -0.5 * Math.abs(Ld.e[i])) hit = -1; }
        if (hit > 0) right++; else if (hit < 0) wrong++;
      }
      const tot = stc.reduce((a, b) => a + b, 0) || 1;
      const sl = Ld.sl_trends, pm = Ld.pm_trends;
      const followed = sl.filter((s) => pm.some((q) => q.dir === s.dir && q.on - s.on >= -60 && q.on - s.on <= 180));
      const leads = followed.map((s) => { const q = pm.filter((q) => q.dir === s.dir && q.on - s.on >= -60 && q.on - s.on <= 180).sort((a, b) => Math.abs(a.on - s.on) - Math.abs(b.on - s.on))[0]; return q.on - s.on; });
      html += `<p class="muted">Lead on this game: superline ahead ${pct((stc[1] + stc[2]) / tot)} of the time, Polymarket ahead ${pct(stc[3] / tot)}, stale ${pct(stc[4] / tot)}. ` +
        `${n} superline leads opened: Polymarket moved half the gap their way within 60 s in ${right} (${pct(n ? right / n : null)}), the other way first in ${wrong}. ` +
        `Waves: superline ${sl.length}, Polymarket ${pm.length}; Polymarket followed ${followed.length} of the superline's (median onset lead ${leads.length ? secs(median(leads) * 1000) : '-'}, + = superline first). ` +
        `All games: right in 50%, wrong first in 24%; Polymarket follows 74% of superline waves, first by about 1 s at the onset (tools/waves/lead_fit.py).</p>`;
    }
    html += `<p class="muted">${A.waves.length} waves on ${outName(outcome())} in this game; Polymarket moved first in ${pct(both.length ? both.filter((w) => w.leader === PMK).length / both.length : null)} of the ${both.length} where it and a bookmaker both moved.${tr ? '' : ' Click a row to show that line.'}</p>`;
    html += `<table class="tbl rv-ana"><thead><tr><th>Line</th>${th('Waves', 'Waves this line took part in, on this game and outcome')}${th('First', 'Share of those it was the first of all lines to move')}${th('Before PM', 'Share of the waves both moved in where it moved before Polymarket')}${th('Lead', 'Median of Polymarket\'s onset minus this line\'s onset, over the waves both moved in: + = ahead of Polymarket')}${th('Lead-lag', 'Hayashi-Yoshida lead-lag on the whole game\'s moves (log-odds): the shift with the strongest co-movement; + = this line leads Polymarket')}${th('ρ', 'Correlation of its moves with Polymarket\'s at that lead-lag (1 = moves together); shown from 8 moves on, capped at ±1')}${th('All games', 'The same over every recorded game (tools/waves/run.py): median lead and ρ')}</tr></thead><tbody>` +
      srcs.map((s) => {
        const x = stat(s), cr = A.corr[s], gl = g[s];
        const name = s === SUPER ? `<i class="rv-sw" style="background:${TC.sup}"></i>Superline` : `${SE.logo(s)}${esc(SE.bookName(s))}${s === trig ? ' <span class="chip accent">trigger</span>' : ''}`;
        return `<tr data-src="${esc(s)}" class="${s === SUPER || s === trig ? 'rv-hl' : ''}"><td>${name}</td><td class="r">${x.n}</td><td class="r">${pct(x.first)}</td><td class="r">${pct(x.before)}</td><td class="r">${secs(x.lead)}</td><td class="r">${cr && cr.n >= MIN_MOVES ? secs(cr.lag) : '-'}</td><td class="r">${rhoTxt(cr)}</td><td class="r muted">${gl ? `${secs(gl.lead_ms)} · ρ ${gl.hy_rho != null ? gl.hy_rho.toFixed(2) : '-'}` : '-'}</td></tr>`;
      }).join('') + '</tbody></table>';
    html += `<p class="muted rv-hint">${SCORES ? `All games: ${SCORES.games} games, ${SCORES.waves} waves${SCORES.written_ms ? `, measured ${SE.fmtDateTime(SCORES.written_ms)}` : ''}.` : 'All games: not measured yet (run tools/waves/run.py).'} Lines are read margin-free, as shares of the bookmaker's book; Polymarket at its mid.</p>`;
    box.innerHTML = html;
    $$('tr[data-src]', box).forEach((r) => r.addEventListener('click', () => {
      const s = r.dataset.src;
      if (s === SUPER || !G.books[s]) return;
      if (trade()) st.tsel.add(s); else { st.sel.add(s); saveSel(); }
      rebuild();
    }));
  }

  return () => { dead = true; document.removeEventListener('keydown', onKey); SE.destroyChart(u); };
}
})();
