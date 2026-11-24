/* Swifteye: the Order desk page (docs/DESK.md). Bookmaker logins are typed here by the owner and kept in
   desk_accounts.json; the page also shows each bookmaker's driver and limits, and the latest tickets.
   Nothing here can switch real money on: that needs the desk key (POST /desk/settings). */
'use strict';
(() => {
const { $, esc, ic, api, P } = SE;

const ago = (s) => (s == null ? '' : s < 90 ? s + ' s ago' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 172800 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago');
const STATE = { '': 'no driver has reported yet', signed_in: 'signed in', signed_out: 'signed out', refused: 'the site refused this login', needs_owner: 'needs you' };
const money = (x) => (x == null ? '' : (+x).toLocaleString('en-US', { maximumFractionDigits: 2 }));

SE.pages.desk = {
  title: 'Order desk',
  render(el) {
    el.innerHTML = P.head('Order desk', '<span class="muted" id="dkMode"></span>') +
      P.sec('key', 'Bookmaker logins', `
        <div class="field"><label for="dkB" data-tip="Swifteye's name for the bookmaker, as on the Feeds page (unibet, leovegas, duel...).">Bookmaker</label><input class="input" id="dkB" list="dkBooks" autocomplete="off" placeholder="unibet"><datalist id="dkBooks"></datalist></div>
        <div class="field"><label for="dkL">Email or username</label><input class="input" id="dkL" autocomplete="off"></div>
        <div class="field"><label for="dkP">Password</label><input class="input" id="dkP" type="password" autocomplete="new-password" placeholder="Leave empty to keep the saved one"></div>
        <div class="field"><label for="dkN">Note</label><input class="input" id="dkN" autocomplete="off" placeholder="Optional"></div>
        <div style="display:flex;gap:8px;margin-top:16px;align-items:center"><button class="btn primary" id="dkSave">${ic('save')}Save login</button><button class="btn" id="dkDel">${ic('delete')}Remove</button><span class="muted" id="dkMsg"></span></div>
        <p class="note" style="margin-top:16px">Stored on this machine in desk_accounts.json next to the database. A saved password is never shown again. Swifteye does not sign in anywhere itself: the bookmaker's driver fetches the login when the site has signed it out. If the site asks for a captcha, a code or an identity check, the driver stops, the login is held back and you are told. Saving a login again lets the driver try once more.</p>
        <div id="dkAcc" style="margin-top:24px"></div>`) +
      P.sec('tune', 'Bookmakers on the desk', '<div id="dkBk"></div>', '', 'Limits and the real-money switches are changed through the API with the desk key (docs/DESK.md), not on this page.') +
      P.sec('receipt_long', 'Latest tickets', '<div id="dkT"></div>');
    const msg = (t, bad) => { $('#dkMsg').innerHTML = bad ? `<span class="err">${esc(t)}</span>` : esc(t); };
    const acc = SE.table($('#dkAcc'), [
      { key: 'b', label: 'Bookmaker', fmt: (a) => esc(a.book), sort: (a) => a.book },
      { key: 'l', label: 'Login', fmt: (a) => (a.has_password ? `<span class="mono">${esc(a.login)}</span>` : '<span class="muted">none saved (signed in by hand)</span>') },
      { key: 's', label: 'Sign-in', fmt: (a) => `<span class="${a.blocked ? 'err' : a.state === 'signed_in' ? '' : 'muted'}">${esc(STATE[a.state] ?? a.state)}${a.detail ? ': ' + esc(a.detail) : ''}</span>` },
      { key: 'w', label: 'Reported', fmt: (a) => `<span class="muted">${esc(ago(a.state_ago_s))}</span>`, sort: (a) => a.state_ago_s ?? 1e12 },
      { key: 'h', label: 'Held back', fmt: (a) => (a.blocked ? 'yes: save the login again once you have looked at the site' : '') },
      { key: 'n', label: 'Note', fmt: (a) => `<span class="muted">${esc(a.note || '')}</span>` },
    ], { empty: 'No bookmaker login saved yet.', emptyIcon: 'key', onClick: (a) => { $('#dkB').value = a.book; $('#dkN').value = a.note || ''; $('#dkL').value = ''; $('#dkP').value = ''; msg('Type a new password, or only a new note, and save.'); $('#dkL').focus(); } });
    const books = SE.table($('#dkBk'), [
      { key: 'b', label: 'Bookmaker', fmt: (b) => esc(b.name), sort: (b) => b.name },
      { key: 'p', label: 'Placed by', fmt: (b) => (b.placed_by === 'swifteye' ? 'Swifteye' : b.driver ? `driver ${esc(b.driver.name)}` : '<span class="muted">no driver has asked for work yet</span>') },
      { key: 'd', label: 'Driver last asked', fmt: (b) => (b.driver ? `<span class="${b.driver.ago_s > 120 ? 'err' : 'muted'}">${esc(ago(b.driver.ago_s))}</span>` : ''), sort: (b) => b.driver?.ago_s ?? 1e12 },
      { key: 'r', label: 'Real money', fmt: (b) => (b.real ? 'on' : '<span class="muted">off</span>') },
      { key: 'm', label: 'Most a bet', cls: 'r', fmt: (b) => money(b.max_stake) + (b.currency ? ' ' + esc(b.currency) : ''), sort: (b) => b.max_stake },
      { key: 'c', label: '24-hour cap', cls: 'r', fmt: (b) => money(b.daily_cap), sort: (b) => b.daily_cap },
      { key: 'u', label: 'Sent in 24 h', cls: 'r', fmt: (b) => money(b.real_24h), sort: (b) => b.real_24h },
    ], { empty: 'No bookmaker has limits or a driver yet.', emptyIcon: 'tune' });
    const legText = (l) => `${esc(l.book)} ${esc(l.outcome)}${l.side === 'no' ? ' (against)' : ''} ${l.odds ? (+l.odds).toFixed(2) : 'at ' + (+l.min_odds).toFixed(2) + '+'}: ${esc(l.state)}`;
    const tickets = SE.table($('#dkT'), [
      { key: 'w', label: 'When', fmt: (t) => `<span class="muted">${esc(SE.fmtAgo(t.created_ms))}</span>`, sort: (t) => -t.created_ms },
      { key: 'm', label: 'Mode', fmt: (t) => esc(t.mode) },
      { key: 's', label: 'State', fmt: (t) => `<span class="${t.state === 'exposed' || t.state === 'check' ? 'err' : ''}">${esc(t.state)}</span>` },
      { key: 'l', label: 'Legs', fmt: (t) => t.legs.map(legText).join('<br>') },
      { key: 'n', label: 'Note', fmt: (t) => `<span class="muted">${esc(t.note || '')}</span>` },
    ], { empty: 'No ticket yet.', emptyIcon: 'receipt_long' });
    async function load() {
      const d = await api('/desk?limit=30');
      $('#dkMode').textContent = (d.settings.real ? 'Real money: ON' : 'Real money: off') + (d.kill ? ' · kill switch on' : '');
      acc.set(d.accounts || []);
      books.set(Object.entries(d.settings.books || {}).map(([name, b]) => ({ name, ...b })));
      tickets.set(d.tickets || []);
    }
    api('/books').then((b) => { $('#dkBooks').innerHTML = (b.books || []).map((x) => x.name).filter((n) => n !== 'polymarket' && n !== 'kalshi').sort().map((n) => `<option value="${esc(n)}">`).join(''); }).catch(() => {});
    $('#dkSave').addEventListener('click', async () => {
      const body = { book: $('#dkB').value.trim(), login: $('#dkL').value.trim(), password: $('#dkP').value, note: $('#dkN').value.trim() };
      try {
        await api('/desk/accounts', { body });
        $('#dkP').value = '';
        $('#dkL').value = '';
        msg('Saved. The password is not shown again.');
        await load();
      } catch (e) { msg(e.message, true); }
    });
    $('#dkDel').addEventListener('click', async () => {
      const book = $('#dkB').value.trim();
      if (!book) return msg('Which bookmaker?', true);
      try { await api('/desk/accounts', { body: { book, remove: true } }); msg(`The login for ${book} is removed.`); await load(); } catch (e) { msg(e.message, true); }
    });
    load().catch((e) => msg(e.message, true));
    SE.every(10000, () => load().catch(() => {}));
  },
};
})();
