/* Add an account or a person without a connection.
 *
 * The record is held on this device (the same queue as offline transactions), shown in the pickers as
 * "not synced yet", and created on the server later with its client-generated id, so a retry can never
 * create it twice. Rejections (for example a name that is already taken) are shown, never auto-fixed. */
(function () {
  'use strict';
  const PL = window.PL; const $ = PL.$; const esc = PL.esc;
  const body = document.body;
  const API_BASE = (body.dataset.createUrl || '').replace(/\/transactions\/?$/, '');
  const URLS = { account: API_BASE + '/accounts', person: API_BASE + '/people' };

  async function loadOptions() { try { return await PL.options.load(); } catch (e) { return null; } }

  function sheetShell(title) {
    const sheet = document.createElement('div'); sheet.className = 'sheet';
    sheet.innerHTML = `<div class="sheet-head"><h2>${esc(title)}</h2><button class="btn ghost sm" data-x aria-label="Close">${PL.icon('x')}</button></div><div class="sheet-body" data-host><p class="muted small">Loading…</p></div>`;
    const ov = PL.overlay(sheet); $('[data-x]', sheet).addEventListener('click', () => ov.close());
    return { sheet, ov, host: $('[data-host]', sheet) };
  }

  /* Save online when possible; otherwise (or if the connection drops mid-request) keep it on the device. */
  async function create(kind, p, label, setError) {
    if (navigator.onLine && PL.csrf()) {
      try {
        const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), 15000);
        const res = await fetch(URLS[kind], { method: 'POST', credentials: 'same-origin', signal: ctl.signal,
          headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': PL.csrf() }, body: JSON.stringify(p) });
        clearTimeout(timer);
        let data = {}; try { data = await res.json(); } catch (e) { /* not JSON */ }
        if (res.ok && data.ok) { try { await PL.store.queueDelete(p.uuid); } catch (e) { /* nothing queued */ } return { saved: true, data }; }
        if (res.status === 422) { setError(data.field, data.error || 'The server rejected this.'); return { error: true }; }
        if (res.status === 401 || res.status === 400) { setError('_', 'Your session has expired. Sign in again; nothing was saved.'); return { error: true }; }
        setError('_', data.error || 'The server could not save this. Nothing was saved.'); return { error: true };
      } catch (e) { /* network failure: the outcome is unknown, so queue it; the same id makes a replay safe */ }
    }
    try {
      await PL.queue.add({ uuid: p.uuid, kind, payload: p, label, type: kind, created_at: new Date().toISOString(), status: 'queued', error: '' });
    } catch (e) { setError('_', 'This device could not store it. It was NOT saved anywhere.'); return { error: true }; }
    await PL.options.refreshPending(); PL.queue.refreshUI(); PL.queue.sync();
    return { queued: true };
  }

  function finish(kind, name, result, ov, rec, cb) {
    ov.close();
    if (cb && cb.onDone) {
      // opened from inside an entry form: keep that form as it is and hand the new record back to it
      if (result.queued) { PL.toast(`\u201c${name}\u201d is held on this device (not on the server yet). You can use it right away.`, 'warning', 6000); cb.onDone(rec.pendingId); }
      else { PL.options.addKnown(kind, Object.assign({ id: result.data.id }, rec.known)); PL.toast(result.data.message || 'Saved.', 'success'); cb.onDone(String(result.data.id)); }
      return;
    }
    if (result.queued) { PL.toast(`“${name}” is held on this device. NOT saved on the server yet; it will sync when you are online. You can use it right away.`, 'warning', 8000); return; }
    PL.undoLater(result.data.undo, result.data.message || 'Saved.');
    if (document.body.dataset.offlinePage) { PL.options.load(true).catch(() => {}); PL.showPending(); } else location.reload();
  }

  function errorsFor(root) {
    return (field, message) => {
      const wrap = root.querySelector(`[data-f="${field}"]`);
      if (!wrap) { const s = $('[data-summary]', root); s.textContent = message; s.classList.remove('hidden'); return; }
      wrap.classList.add('has-error'); const e = $('.error', wrap); e.textContent = message; e.hidden = false;
    };
  }
  function clearErrors(root) {
    $('[data-summary]', root).classList.add('hidden');
    root.querySelectorAll('.has-error').forEach((f) => f.classList.remove('has-error'));
    root.querySelectorAll('.error').forEach((e) => { e.hidden = true; e.textContent = ''; });
  }
  const taken = (list, name, uuid) => list.some((x) => x.name.trim().toLowerCase() === name.toLowerCase() && String(x.id) !== 'acct:' + uuid && String(x.id) !== 'person:' + uuid);
  const needsOnline = '<div class="alert warning">PocketLedger needs to load your account types once before this works offline. Reconnect, open PocketLedger once, then try again.</div>';

  /* ------------------------------------------------------------- account */
  PL.addAccountSheet = async function (prefill, cb) {
    prefill = prefill || {};
    const { host, ov } = sheetShell('New account');
    const opts = await loadOptions();
    if (!opts || !(opts.account_categories || []).length) { host.innerHTML = needsOnline; return; }
    const uuid = prefill.uuid || PL.uuid(); const cur = (opts.currencies || []).length ? opts.currencies : [opts.currency];
    const today = PL.nowLocal(opts.timezone).slice(0, 10);
    const f = document.createElement('form'); f.noValidate = true; f.autocomplete = 'off';
    f.innerHTML = `
      <div class="alert warning hidden" data-summary role="alert"></div>
      <div class="field" data-f="name"><label for="oa-name">Account name</label><input class="input" id="oa-name" maxlength="60" placeholder="e.g. Pocket, bKash, DBBL Savings"><div class="error" hidden></div></div>
      <div class="field" data-f="category_id"><label for="oa-cat">Type</label><select class="input" id="oa-cat">${opts.account_categories.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select><div class="error" hidden></div></div>
      <div class="${cur.length > 1 ? 'two-col' : ''}">
        <div class="field" data-f="opening_balance"><label for="oa-bal">Current balance</label><div class="input-money"><span class="sym">${esc(PL.currency.symbol)}</span><input class="input" id="oa-bal" inputmode="decimal" placeholder="0"></div><div class="help">What it holds right now. This is the opening balance.</div><div class="error" hidden></div></div>
        ${cur.length > 1 ? `<div class="field" data-f="currency"><label for="oa-cur">Currency</label><select class="input" id="oa-cur">${cur.map((c) => `<option value="${esc(c.code)}">${esc(c.code)} (${esc(c.symbol)})</option>`).join('')}</select><div class="error" hidden></div></div>` : ''}
      </div>
      <div class="field" data-f="opened_on"><label for="oa-date">Opening date</label><input class="input" id="oa-date" type="date" max="${esc(today)}"><div class="help">The balance above is what it held at the start of this day. Defaults to today on this device.</div><div class="error" hidden></div></div>
      <div class="field" data-f="description"><label for="oa-desc">Description (optional)</label><textarea class="input" id="oa-desc" rows="2" maxlength="300"></textarea><div class="error" hidden></div></div>
      <button class="btn primary block" type="submit" data-submit></button>`;
    host.innerHTML = ''; host.appendChild(f);
    const el = (id) => f.querySelector('#' + id);
    el('oa-name').value = prefill.name || ''; el('oa-cat').value = String(prefill.category_id || opts.account_categories[0].id);
    el('oa-bal').value = prefill.opening_balance || ''; el('oa-date').value = prefill.opened_on || today; el('oa-desc').value = prefill.description || '';
    if (el('oa-cur') && prefill.currency) el('oa-cur').value = prefill.currency;
    const btn = $('[data-submit]', f); const label = () => { btn.innerHTML = `${PL.icon(navigator.onLine ? 'check' : 'hard-drive-download')} ${navigator.onLine ? 'Create account' : 'Save on this device (offline)'}`; PL.icons(); };
    label(); setTimeout(() => el('oa-name').focus(), 30);
    f.addEventListener('submit', async (e) => {
      e.preventDefault(); clearErrors(f); const err = errorsFor(f);
      const p = { uuid, name: el('oa-name').value.trim(), category_id: el('oa-cat').value, opening_balance: el('oa-bal').value.trim(),
        currency: el('oa-cur') ? el('oa-cur').value : opts.currency.code, opened_on: el('oa-date').value || today, description: el('oa-desc').value.trim() };
      let bad = false;
      if (!p.name) { err('name', 'Enter the account name.'); bad = true; }
      else if (taken(opts.accounts, p.name, uuid)) { err('name', 'You already have an account with that name (it may not be synced yet).'); bad = true; }
      if (p.opening_balance && PL.parseAmount(p.opening_balance) === null) { err('opening_balance', 'Enter a valid amount, for example 1500 or 1,500.50.'); bad = true; }
      if (p.opened_on > today) { err('opened_on', 'The opening date can’t be in the future.'); bad = true; }
      if (bad) return;
      btn.disabled = true; btn.textContent = 'Saving…';
      const minor = PL.parseAmount(p.opening_balance || '0') || 0;
      const r = await create('account', p, `New account “${p.name}” · ${PL.fmt(minor)}`, err);
      btn.disabled = false; label();
      if (!r.error) {
        const cat = opts.account_categories.find((c) => String(c.id) === String(p.category_id)) || {};
        finish('account', p.name, r, ov, { pendingId: 'acct:' + uuid, known: { name: p.name, kind: cat.kind || '', category: cat.name || '', currency: p.currency, balance_minor: minor, opened_on: p.opened_on } }, cb);
      }
    });
  };

  /* ------------------------------------------------------------- person */
  PL.addPersonSheet = async function (prefill, cb) {
    prefill = prefill || {};
    const { host, ov } = sheetShell('Add person');
    const opts = await loadOptions();
    if (!opts) { host.innerHTML = needsOnline; return; }
    const uuid = prefill.uuid || PL.uuid();
    const f = document.createElement('form'); f.noValidate = true; f.autocomplete = 'off';
    f.innerHTML = `
      <div class="alert warning hidden" data-summary role="alert"></div>
      <div class="field" data-f="name"><label for="op-name">Name</label><input class="input" id="op-name" maxlength="80"><div class="error" hidden></div></div>
      <div class="field" data-f="contact"><label for="op-contact">Contact (optional)</label><input class="input" id="op-contact" maxlength="120" placeholder="Phone, email, address…"><div class="error" hidden></div></div>
      <div class="field" data-f="notes"><label for="op-notes">Notes (optional)</label><textarea class="input" id="op-notes" rows="2" maxlength="1000"></textarea><div class="error" hidden></div></div>
      <button class="btn primary block" type="submit" data-submit></button>`;
    host.innerHTML = ''; host.appendChild(f);
    const el = (id) => f.querySelector('#' + id);
    el('op-name').value = prefill.name || ''; el('op-contact').value = prefill.contact || ''; el('op-notes').value = prefill.notes || '';
    const btn = $('[data-submit]', f); const label = () => { btn.innerHTML = `${PL.icon(navigator.onLine ? 'check' : 'hard-drive-download')} ${navigator.onLine ? 'Add person' : 'Save on this device (offline)'}`; PL.icons(); };
    label(); setTimeout(() => el('op-name').focus(), 30);
    f.addEventListener('submit', async (e) => {
      e.preventDefault(); clearErrors(f); const err = errorsFor(f);
      const p = { uuid, name: el('op-name').value.trim(), contact: el('op-contact').value.trim(), notes: el('op-notes').value.trim() };
      if (!p.name) { err('name', 'Enter the person’s name.'); return; }
      if (taken(opts.people, p.name, uuid)) { err('name', 'Someone with that name already exists (they may not be synced yet). Add a detail, e.g. “Rahim (cousin)”.'); return; }
      btn.disabled = true; btn.textContent = 'Saving…';
      const r = await create('person', p, `New person “${p.name}”`, err);
      btn.disabled = false; label();
      if (!r.error) finish('person', p.name, r, ov, { pendingId: 'person:' + uuid, known: { name: p.name, receivable_minor: 0, payable_minor: 0 } }, cb);
    });
  };

  /* ------------------------------------------------------------- entry points */
  document.addEventListener('click', (e) => {
    const direct = e.target.closest('[data-add-account], [data-add-person]');
    if (direct) {
      e.preventDefault();
      const cb = direct.closest('.tx-form') ? { onDone: () => {} } : undefined;   // inside an entry form: don't reload the page
      (direct.hasAttribute('data-add-account') ? PL.addAccountSheet : PL.addPersonSheet)({}, cb); return;
    }
    if (navigator.onLine) return;                       // online: the normal pages are used
    const a = e.target.closest('a[href]'); if (!a) return;
    let path = ''; try { path = new URL(a.href, location.href).pathname; } catch (err) { return; }
    if (path.endsWith('/accounts/new')) { e.preventDefault(); PL.addAccountSheet(); }
    else if (path.endsWith('/people/new')) { e.preventDefault(); PL.addPersonSheet(); }
  }, true);
})();
