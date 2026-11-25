/* Transaction entry: searchable selectors, the form (quick sheet / full page / edit),
 * and the offline queue with idempotent synchronisation.
 *
 * Integrity rules enforced here:
 *  - a transaction is never reported as saved until the server answers 200/201;
 *  - every submission carries a client-generated UUID, which the server treats as an
 *    idempotency key, so retries and replays cannot create duplicates;
 *  - queued items are removed only after server confirmation; rejections are shown, never auto-fixed.
 */
(function () {
  'use strict';
  const PL = window.PL; const $ = PL.$; const esc = PL.esc;
  const body = document.body;
  const OPTIONS_URL = body.dataset.optionsUrl; const CREATE_URL = body.dataset.createUrl;
  const EDIT_URL = body.dataset.editUrl; const LOGIN_URL = body.dataset.loginUrl;

  const TYPES = {
    expense: { label: 'Expense', short: 'Expense', icon: 'arrow-up-right', out: true, verb: 'expense' },
    income: { label: 'Income', short: 'Income', icon: 'arrow-down-left', verb: 'income' },
    transfer: { label: 'Transfer', short: 'Transfer', icon: 'arrow-left-right', verb: 'transfer' },
    lent: { label: 'Lend money', short: 'Lend', icon: 'hand-coins', out: true, verb: 'loan' },
    borrowed: { label: 'Borrow money', short: 'Borrow', icon: 'hand-helping', verb: 'loan' },
    repayment_received: { label: 'Got repaid', short: 'Got repaid', icon: 'undo-2', verb: 'repayment' },
    repayment_made: { label: 'I repaid', short: 'I repaid', icon: 'send', out: true, verb: 'repayment' },
    adjustment: { label: 'Balance adjustment', short: 'Adjust', icon: 'scale', verb: 'adjustment' },
  };
  const EVERYDAY = ['expense', 'income', 'transfer'];
  const PEOPLE_TYPES = ['lent', 'borrowed', 'repayment_received', 'repayment_made'];

  const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* storage may be blocked */ } };

  /* ---------------------------------------------------------------- options */
  const Options = {
    data: null, stale: false,
    async load(force) {
      if (this.data && !force) return this.data;
      const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), 6000);
      try {
        const r = await fetch(OPTIONS_URL, { credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: ctl.signal });
        clearTimeout(timer);
        if (r.status === 401) { const e = new Error('auth'); e.auth = true; throw e; }
        if (!r.ok) throw new Error('bad status');
        const j = await r.json();
        this.data = j; this.stale = false; PL.setCurrency(j.currency);
        const cached = Object.assign({}, j, { accounts: j.accounts.map((a) => ({ id: a.id, name: a.name, kind: a.kind, category: a.category, currency: a.currency })) });
        PL.store.optionsPut(cached).catch(() => {});
        return j;
      } catch (err) {
        clearTimeout(timer);
        if (err.auth) throw err;
        let cached = null; try { cached = await PL.store.optionsGet(); } catch (e) { /* no idb */ }
        if (cached) { this.data = cached; this.stale = true; PL.setCurrency(cached.currency); return cached; }
        throw err;
      }
    },
  };
  PL.options = Options;

  /* ---------------------------------------------------------------- searchable select */
  function combo(cfg) {
    const el = document.createElement('div'); el.className = 'combo';
    let items = cfg.items || []; let value = cfg.value == null ? '' : cfg.value; let open = false; let hl = 0; let panel = null; let query = '';
    const btn = document.createElement('button'); btn.type = 'button'; btn.className = 'combo-btn';
    btn.setAttribute('aria-haspopup', 'listbox'); btn.setAttribute('aria-expanded', 'false');
    if (cfg.ariaLabel) btn.setAttribute('aria-label', cfg.ariaLabel);
    el.appendChild(btn);

    const current = () => (value && value.new ? { label: value.new, sub: 'new' } : items.find((i) => String(i.value) === String(value)));
    function paintButton() {
      const c = current();
      btn.innerHTML = `<span class="trunc">${c ? esc(c.label) + (c.sub ? `<span class="sub">${esc(c.sub)}</span>` : '') : `<span class="ph">${esc(cfg.placeholder || 'Select…')}</span>`}</span>${PL.icon('chevrons-up-down')}`;
      PL.icons();
    }
    function visible() {
      const q = query.trim().toLowerCase();
      const list = q ? items.filter((i) => i.label.toLowerCase().includes(q)) : items.slice();
      const out = list.map((i) => ({ kind: 'item', item: i }));
      if (cfg.allowNew && q && !items.some((i) => i.label.toLowerCase() === q)) out.push({ kind: 'new', name: query.trim() });
      if (cfg.allowClear && !q && value) out.unshift({ kind: 'clear' });
      return out;
    }
    function paintList() {
      const ul = panel.querySelector('ul'); const rows = visible();
      if (hl >= rows.length) hl = Math.max(0, rows.length - 1);
      ul.innerHTML = rows.map((r, i) => {
        const cls = (i === hl ? 'hl ' : '') + (r.kind === 'new' ? 'add' : '');
        if (r.kind === 'new') return `<li role="option" class="${cls}" data-i="${i}">+ Add “${esc(r.name)}”</li>`;
        if (r.kind === 'clear') return `<li role="option" class="${cls} muted" data-i="${i}">None</li>`;
        return `<li role="option" class="${cls}" data-i="${i}" aria-selected="${String(r.item.value) === String(value)}"><span>${esc(r.item.label)}</span>${r.item.sub ? `<span class="sub">${esc(r.item.sub)}</span>` : ''}</li>`;
      }).join('') || `<div class="combo-empty">${cfg.allowNew ? 'Type a name to add someone new.' : 'No matches.'}</div>`;
    }
    function choose(r) {
      if (!r) return;
      value = r.kind === 'new' ? { new: r.name } : r.kind === 'clear' ? '' : r.item.value;
      close(); paintButton(); if (cfg.onChange) cfg.onChange(value);
    }
    function onDocDown(e) { if (!el.contains(e.target)) close(); }
    function openPanel() {
      if (open) return; open = true; query = ''; hl = 0;
      btn.setAttribute('aria-expanded', 'true'); el.classList.add('open');
      panel = document.createElement('div'); panel.className = 'combo-panel';
      const searchable = cfg.searchable !== false && (items.length > 5 || cfg.allowNew);
      panel.innerHTML = `${searchable ? `<input class="combo-search" type="search" placeholder="${cfg.allowNew ? 'Search or type a new name' : 'Search'}" autocomplete="off" aria-label="Search">` : ''}<ul class="combo-list" role="listbox"></ul>`;
      el.appendChild(panel); paintList();
      const search = panel.querySelector('input');
      if (search) { search.focus(); search.addEventListener('input', () => { query = search.value; hl = 0; paintList(); }); }
      panel.addEventListener('mousedown', (e) => { e.preventDefault(); });
      panel.addEventListener('click', (e) => { const li = e.target.closest('li[data-i]'); if (li) choose(visible()[Number(li.dataset.i)]); });
      document.addEventListener('mousedown', onDocDown, true);
      setTimeout(() => panel && panel.scrollIntoView({ block: 'nearest' }), 0);
    }
    function close() {
      if (!open) return; open = false; el.classList.remove('open'); btn.setAttribute('aria-expanded', 'false');
      if (panel) panel.remove(); panel = null; document.removeEventListener('mousedown', onDocDown, true);
    }
    btn.addEventListener('click', () => (open ? close() : openPanel()));
    el.addEventListener('keydown', (e) => {
      if (!open) { if (e.key === 'ArrowDown' && document.activeElement === btn) { e.preventDefault(); openPanel(); } return; }
      const rows = visible();
      if (e.key === 'ArrowDown') { e.preventDefault(); hl = Math.min(rows.length - 1, hl + 1); paintList(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); hl = Math.max(0, hl - 1); paintList(); }
      else if (e.key === 'Enter') { e.preventDefault(); choose(rows[hl]); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); btn.focus(); }
      else if (e.key === 'Tab') close();
    });
    paintButton();
    return {
      el,
      get value() { return value; },
      set(v) { value = v == null ? '' : v; paintButton(); },
      setItems(next) { items = next; paintButton(); },
      focus() { btn.focus(); },
    };
  }

  /* ---------------------------------------------------------------- the form */
  function buildForm(cfg) {
    const opts = cfg.options; const tz = opts.timezone;
    const editing = cfg.editingId || null;
    const prefill = cfg.prefill || {};
    const S = {
      type: TYPES[prefill.type] ? prefill.type : 'expense', uuid: prefill.uuid || PL.uuid(),
      amount: prefill.amount || '', source: '', dest: '', account: '', direction: prefill.direction || 'increase',
      person: '', category: '', description: prefill.description || '', notes: prefill.notes || '',
      due_date: prefill.due_date || '', occurred_local: prefill.occurred_local || '',
      reconciliation_id: prefill.reconciliation_id || '', newPersonName: prefill.new_person_name || '',
    };
    S.source = String(prefill.source_account_id || ''); S.dest = String(prefill.dest_account_id || '');
    S.account = String(prefill.account_id || ''); S.person = prefill.person_id ? String(prefill.person_id) : (prefill.new_person_name ? { new: prefill.new_person_name } : '');
    S.category = prefill.category_id ? String(prefill.category_id) : '';
    let touchedTime = !!S.occurred_local;
    const offlineNow = () => !navigator.onLine && !editing;

    const accounts = opts.accounts; const people = opts.people;
    const accItems = accounts.map((a) => ({ value: String(a.id), label: a.name, sub: a.category }));
    const hasMany = accounts.length > 0;

    const root = document.createElement('form'); root.noValidate = true; root.className = 'tx-form'; root.autocomplete = 'off';
    root.setAttribute('aria-label', editing ? 'Edit transaction' : 'Add transaction');

    const tabsHtml = (types, three) => `<div class="type-tabs ${three ? 'three' : ''}" role="tablist">${types.map((t) =>
      `<button type="button" role="tab" data-type="${t}" class="${TYPES[t].out ? 'out' : ''}">${PL.icon(TYPES[t].icon)}<span>${TYPES[t].short}</span></button>`).join('')}</div>`;

    root.innerHTML = `
      ${editing ? `<p class="small muted" style="margin:0 0 .8rem"><strong>${esc(TYPES[S.type].label)}</strong>. To change the type, void this transaction and add a new one.</p>` : `
        ${tabsHtml(EVERYDAY, true)}
        <div class="label xs" style="margin:-.3rem 0 .35rem">With people</div>
        ${tabsHtml(PEOPLE_TYPES, false)}
        ${cfg.allowAdjustment ? `<div class="label xs" style="margin:-.3rem 0 .35rem">Other</div>${tabsHtml(['adjustment'], true)}` : ''}`}
      <div class="field" data-f="amount"><label for="tx-amount">Amount</label>
        <div class="input-money"><span class="sym">${esc(PL.currency.symbol)}</span><input class="input" id="tx-amount" name="amount" inputmode="decimal" autocomplete="off" placeholder="0" aria-describedby="tx-amount-hint"></div>
        <div class="help" id="tx-amount-hint"></div><div class="error" hidden></div></div>
      <div data-fields></div>
      <div class="alert warning hidden" data-preopen role="status"></div>
      <button type="button" class="more-toggle" aria-expanded="false">${PL.icon('chevron-down')}<span>More details</span></button>
      <div class="more-fields hidden">
        <div class="field" data-f="description"><label for="tx-desc">Description</label><input class="input" id="tx-desc" name="description" maxlength="200"><div class="error" hidden></div></div>
        <div class="field" data-f="occurred_local"><label for="tx-when">Date &amp; time</label><input class="input" id="tx-when" name="occurred_local" type="datetime-local"><div class="error" hidden></div></div>
        <div class="field hidden" data-f="due_date"><label for="tx-due">Due date (optional)</label><input class="input" id="tx-due" name="due_date" type="date"><div class="error" hidden></div></div>
        <div class="field" data-f="notes"><label for="tx-notes">Notes</label><textarea class="input" id="tx-notes" name="notes" rows="2" maxlength="2000"></textarea><div class="error" hidden></div></div>
      </div>
      <div class="alert error hidden" data-summary role="alert"></div>
      <div data-netfail class="alert warning hidden"></div>
      <button class="btn primary block" type="submit" data-submit style="margin-top:.5rem"></button>`;

    const amountEl = $('#tx-amount', root); const descEl = $('#tx-desc', root); const whenEl = $('#tx-when', root);
    const dueEl = $('#tx-due', root); const notesEl = $('#tx-notes', root); const fieldsHost = $('[data-fields]', root);
    const submitBtn = $('[data-submit]', root); const summary = $('[data-summary]', root); const netfail = $('[data-netfail]', root);
    amountEl.value = S.amount; descEl.value = S.description; notesEl.value = S.notes; dueEl.value = S.due_date;
    whenEl.value = S.occurred_local || PL.nowLocal(tz);
    whenEl.addEventListener('change', () => { touchedTime = true; });
    amountEl.addEventListener('input', () => { S.amount = amountEl.value; });
    descEl.addEventListener('input', () => { S.description = descEl.value; });
    notesEl.addEventListener('input', () => { S.notes = notesEl.value; });
    dueEl.addEventListener('input', () => { S.due_date = dueEl.value; });
    const moreBox = $('.more-fields', root); const moreBtn = $('.more-toggle', root);
    function setMore(on) { moreBox.classList.toggle('hidden', !on); moreBtn.setAttribute('aria-expanded', String(on)); $('span', moreBtn).textContent = on ? 'Fewer details' : 'More details'; }
    moreBtn.addEventListener('click', () => setMore(moreBox.classList.contains('hidden')));
    if (editing || S.description || S.notes || S.due_date || prefill.occurred_local) setMore(true);

    /* defaults: last used account per role, else a sensible first pick */
    function lastUsed(role) { const v = lsGet('pl_last_' + role); return accounts.some((a) => String(a.id) === v) ? v : ''; }
    function firstCash() { const c = accounts.find((a) => a.kind === 'physical') || accounts[0]; return c ? String(c.id) : ''; }
    function applyDefaults() {
      if (editing) return;
      if (!S.source) S.source = lastUsed('source') || firstCash();
      if (!S.dest) S.dest = lastUsed('dest') || firstCash();
      if (S.type === 'transfer' && S.source === S.dest) {
        const other = accounts.find((a) => String(a.id) !== S.source); S.dest = other ? String(other.id) : '';
      }
      if (!S.account) S.account = lastUsed('source') || firstCash();
    }

    function personItems() {
      let list = people.map((p) => ({ value: String(p.id), label: p.name, _r: p.receivable_minor || 0, _p: p.payable_minor || 0 }));
      if (S.type === 'repayment_received') { const owing = list.filter((p) => p._r > 0); if (owing.length) list = owing; list.forEach((p) => { p.sub = p._r ? `owes you ${PL.fmt(p._r)}` : ''; }); }
      else if (S.type === 'repayment_made') { const owed = list.filter((p) => p._p > 0); if (owed.length) list = owed; list.forEach((p) => { p.sub = p._p ? `you owe ${PL.fmt(p._p)}` : ''; }); }
      else list.forEach((p) => { p.sub = p._r ? `owes you ${PL.fmt(p._r)}` : p._p ? `you owe ${PL.fmt(p._p)}` : ''; });
      return list;
    }

    const combos = {};
    function addField(key, label, widget, required) {
      const wrap = document.createElement('div'); wrap.className = 'field'; wrap.dataset.f = key;
      wrap.innerHTML = `<label>${esc(label)}${required ? '' : ' <span class="faint">(optional)</span>'}</label><div data-slot></div><div class="error" hidden></div>`;
      $('[data-slot]', wrap).appendChild(widget.el || widget); fieldsHost.appendChild(wrap); return wrap;
    }
    const noAccounts = () => { fieldsHost.innerHTML = `<div class="alert warning">${PL.icon('triangle-alert')}<div class="grow">You need an account first. <a href="/accounts/new">Create one</a>.</div></div>`; PL.icons(); };

    function render() {
      applyDefaults(); fieldsHost.innerHTML = '';
      for (const k of Object.keys(combos)) delete combos[k];
      $('[data-f="due_date"]', root).classList.toggle('hidden', !(S.type === 'lent' || S.type === 'borrowed'));
      root.querySelectorAll('[data-type]').forEach((b) => { const on = b.dataset.type === S.type; b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on)); });
      submitBtn.innerHTML = `${PL.icon(offlineNow() ? 'hard-drive-download' : 'check')} ${editing ? 'Save changes' : offlineNow() ? 'Save on this device (offline)' : 'Save ' + TYPES[S.type].verb}`;
      if (!hasMany && S.type !== 'adjustment') { noAccounts(); updateHint(); return; }
      const accCombo = (key, ariaLabel) => (combos[key] = combo({ items: accItems, value: S[key], placeholder: 'Choose account', ariaLabel, onChange: (v) => { S[key] = v; if (key === 'source') lsSet('pl_last_source', v); if (key === 'dest') lsSet('pl_last_dest', v); if (S.type === 'transfer') guardTransfer(key); } }));
      const personCombo = () => (combos.person = combo({ items: personItems(), value: S.person, placeholder: 'Choose or add a person', allowNew: S.type === 'lent' || S.type === 'borrowed', ariaLabel: 'Person', onChange: (v) => { S.person = v; updateHint(); } }));
      const catCombo = (kind) => (combos.category = combo({ items: (opts.categories[kind] || []).map((c) => ({ value: String(c.id), label: c.name })), value: S.category, placeholder: 'Category', allowClear: true, ariaLabel: 'Category', onChange: (v) => { S.category = v; } }));
      switch (S.type) {
        case 'expense': addField('source_account_id', 'Paid from', accCombo('source', 'Paid from'), true); addField('category_id', 'Category', catCombo('expense'), false); break;
        case 'income': addField('dest_account_id', 'Received into', accCombo('dest', 'Received into'), true); addField('category_id', 'Category', catCombo('income'), false); break;
        case 'transfer': addField('source_account_id', 'From', accCombo('source', 'From account'), true); addField('dest_account_id', 'To', accCombo('dest', 'To account'), true); break;
        case 'lent': addField('person_id', 'Lent to', personCombo(), true); addField('source_account_id', 'Paid from', accCombo('source', 'Paid from'), true); break;
        case 'borrowed': addField('person_id', 'Borrowed from', personCombo(), true); addField('dest_account_id', 'Received into', accCombo('dest', 'Received into'), true); break;
        case 'repayment_received': addField('person_id', 'Repaid by', personCombo(), true); addField('dest_account_id', 'Received into', accCombo('dest', 'Received into'), true); break;
        case 'repayment_made': addField('person_id', 'Repaying', personCombo(), true); addField('source_account_id', 'Paid from', accCombo('source', 'Paid from'), true); break;
        case 'adjustment': {
          addField('account_id', 'Account', (combos.account = combo({ items: accItems, value: S.account, placeholder: 'Choose account', ariaLabel: 'Account', onChange: (v) => { S.account = v; } })), true);
          const sel = document.createElement('select'); sel.className = 'input'; sel.innerHTML = '<option value="increase">Balance goes up (found money)</option><option value="decrease">Balance goes down (money missing)</option>';
          sel.value = S.direction; sel.addEventListener('change', () => { S.direction = sel.value; }); addField('direction', 'Direction', { el: sel }, true); break;
        }
      }
      updateHint(); updatePreOpen(); PL.icons();
    }
    function guardTransfer(changed) {
      if (S.source && S.source === S.dest) {
        const other = accounts.find((a) => String(a.id) !== S[changed]);
        const swap = changed === 'source' ? 'dest' : 'source'; S[swap] = other ? String(other.id) : ''; if (combos[swap]) combos[swap].set(S[swap]);
      }
    }
    // Dated before an account was set up? Then it is history only and must not move that balance.
    const preOpen = $('[data-preopen]', root);
    function updatePreOpen() {
      const day = (whenEl.value || '').slice(0, 10);
      const ids = [];
      if (['expense', 'transfer', 'lent', 'repayment_made'].includes(S.type)) ids.push(S.source);
      if (['income', 'transfer', 'borrowed', 'repayment_received'].includes(S.type)) ids.push(S.dest);
      if (S.type === 'adjustment') ids.push(S.account);
      const early = ids.map((id) => accounts.find((a) => String(a.id) === String(id)))
        .filter((a, i, all) => a && a.opened_on && day && day < a.opened_on && all.indexOf(a) === i);
      preOpen.classList.toggle('hidden', !early.length);
      if (!early.length) { preOpen.textContent = ''; return; }
      const names = early.map((a) => `<strong>${esc(a.name)}</strong> (set up ${esc(a.opened_on)})`).join(' and ');
      const person = (S.type === 'lent' || S.type === 'borrowed' || S.type === 'repayment_received' || S.type === 'repayment_made')
        ? ' The person\u2019s balance is still recorded, and later repayments will change the account normally.' : '';
      preOpen.innerHTML = `${PL.icon('info')}<div class="grow">This is dated before ${names}. It is saved as history and will <strong>not</strong> change that balance, because the balance you entered already includes it.${person}</div>`;
      PL.icons();
    }
    whenEl.addEventListener('input', updatePreOpen); whenEl.addEventListener('change', updatePreOpen);
    root.addEventListener('click', () => setTimeout(updatePreOpen, 0));
    root.addEventListener('change', () => setTimeout(updatePreOpen, 0));

    function updateHint() {
      const hint = $('#tx-amount-hint', root); hint.innerHTML = '';
      if (S.type !== 'repayment_received' && S.type !== 'repayment_made') return;
      const p = typeof S.person === 'string' ? people.find((x) => String(x.id) === S.person) : null;
      if (!p) return;
      const out = S.type === 'repayment_received' ? p.receivable_minor : p.payable_minor;
      if (!out) { hint.textContent = `Nothing is outstanding with ${p.name}.`; return; }
      hint.innerHTML = `${S.type === 'repayment_received' ? `${esc(p.name)} owes you` : `You owe ${esc(p.name)}`} <strong>${esc(PL.fmt(out))}</strong>. <button type="button" class="btn ghost sm" data-fill style="min-height:28px;padding:.1rem .5rem">Use full amount</button>`;
      $('[data-fill]', hint).addEventListener('click', () => { amountEl.value = PL.fmt(out, { noSymbol: true }).replace(/,/g, ''); S.amount = amountEl.value; amountEl.focus(); });
    }

    root.querySelectorAll('[data-type]').forEach((b) => b.addEventListener('click', () => {
      S.type = b.dataset.type; S.category = ''; if (!PEOPLE_TYPES.includes(S.type)) S.person = '';
      if (!touchedTime) whenEl.value = PL.nowLocal(tz);
      clearErrors(); netfail.classList.add('hidden'); render(); amountEl.focus();
    }));

    /* ----- validation and submit ----- */
    function clearErrors() { summary.classList.add('hidden'); root.querySelectorAll('.field.has-error').forEach((f) => f.classList.remove('has-error')); root.querySelectorAll('.error').forEach((e) => { e.hidden = true; e.textContent = ''; }); }
    function showError(field, message) {
      const key = { amount: 'amount', source_account_id: 'source_account_id', dest_account_id: 'dest_account_id', account_id: 'account_id', person_id: 'person_id', new_person_name: 'person_id', category_id: 'category_id', occurred_local: 'occurred_local', occurred_at: 'occurred_local', description: 'description', notes: 'notes', due_date: 'due_date', direction: 'direction' }[field];
      const wrap = key && root.querySelector(`[data-f="${key}"]`);
      if (!wrap) { summary.textContent = message; summary.classList.remove('hidden'); return; }
      wrap.classList.add('has-error'); const err = $('.error', wrap); err.textContent = message; err.hidden = false;
      if (wrap.closest('.more-fields')) setMore(true);
    }
    function payload() {
      const p = { uuid: S.uuid, type: S.type, amount: S.amount.trim(), occurred_local: whenEl.value, description: descEl.value.trim(), notes: notesEl.value.trim() };
      if (['expense', 'transfer', 'lent', 'repayment_made'].includes(S.type)) p.source_account_id = S.source;
      if (['income', 'transfer', 'borrowed', 'repayment_received'].includes(S.type)) p.dest_account_id = S.dest;
      if (S.type === 'adjustment') { p.account_id = S.account; p.direction = S.direction; }
      if (PEOPLE_TYPES.includes(S.type)) { if (S.person && S.person.new) p.new_person_name = S.person.new; else p.person_id = S.person; }
      if (S.type === 'expense' || S.type === 'income') p.category_id = S.category;
      if (S.type === 'lent' || S.type === 'borrowed') p.due_date = dueEl.value;
      if (S.reconciliation_id) p.reconciliation_id = S.reconciliation_id;
      return p;
    }
    function localValidate(p) {
      const errs = []; const minor = PL.parseAmount(p.amount);
      if (minor === null || minor <= 0) errs.push(['amount', p.amount ? 'Enter a valid amount, for example 1500 or 1,500.50.' : 'Enter an amount.']);
      if (['expense', 'transfer', 'lent', 'repayment_made'].includes(S.type) && !p.source_account_id) errs.push(['source_account_id', 'Choose an account.']);
      if (['income', 'transfer', 'borrowed', 'repayment_received'].includes(S.type) && !p.dest_account_id) errs.push(['dest_account_id', 'Choose an account.']);
      if (S.type === 'transfer' && p.source_account_id && p.source_account_id === p.dest_account_id) errs.push(['dest_account_id', 'Source and destination must be different.']);
      if (S.type === 'adjustment' && !p.account_id) errs.push(['account_id', 'Choose an account.']);
      if (PEOPLE_TYPES.includes(S.type) && !p.person_id && !p.new_person_name) errs.push(['person_id', 'Choose a person.']);
      if (!p.occurred_local) errs.push(['occurred_local', 'Enter a date and time.']);
      return errs;
    }
    function summaryLabel(p) {
      const nameOf = (id) => (accounts.find((a) => String(a.id) === String(id)) || {}).name; const personName = p.new_person_name || ((people.find((x) => String(x.id) === String(p.person_id)) || {}).name);
      const minor = PL.parseAmount(p.amount);
      const where = S.type === 'transfer' ? `${nameOf(p.source_account_id)} → ${nameOf(p.dest_account_id)}` : (personName || nameOf(p.source_account_id || p.dest_account_id || p.account_id) || '');
      return `${TYPES[S.type].label} ${minor == null ? '' : PL.fmt(minor)}${where ? ' · ' + where : ''}`;
    }
    async function sendOnline(p) {
      const url = editing ? EDIT_URL.replace(/\/0$/, '/' + editing) : CREATE_URL;
      if (editing) { p = Object.assign({}, p, { reason: 'Edited from the app' }); delete p.uuid; }
      const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), 15000);
      try {
        return await fetch(url, { method: editing ? 'PUT' : 'POST', credentials: 'same-origin', signal: ctl.signal,
          headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': PL.csrf() }, body: JSON.stringify(p) });
      } finally { clearTimeout(timer); }
    }
    async function enqueue(p, why) {
      try {
        await PL.queue.add({ uuid: p.uuid, payload: p, label: summaryLabel(p), type: S.type, created_at: new Date().toISOString(), status: 'queued', error: '' });
      } catch (e) { PL.toast('This device could not store the transaction. It was NOT saved anywhere.', 'error'); return false; }
      PL.toast(why || 'Held on this device. NOT saved on the server yet; it will sync when you are online.', 'warning', 7000);
      PL.queue.refreshUI(); PL.queue.sync();
      return true;
    }
    root.addEventListener('submit', async (e) => {
      e.preventDefault(); clearErrors(); netfail.classList.add('hidden');
      const p = payload(); const errs = localValidate(p);
      if (errs.length) { errs.forEach(([f, m]) => showError(f, m)); const first = root.querySelector('.has-error'); if (first) first.scrollIntoView({ block: 'center', behavior: 'smooth' }); return; }
      submitBtn.disabled = true; const label = submitBtn.innerHTML; submitBtn.textContent = 'Saving…';
      try {
        if (!editing && !navigator.onLine) { const ok = await enqueue(p); if (ok) done({ queued: true }); return; }
        let res;
        try { res = await sendOnline(p); }
        catch (err) {
          // Unknown outcome: do NOT claim it was saved. Offer to queue it; the same UUID makes a replay safe.
          if (editing) { summary.textContent = 'Could not reach the server. Nothing was changed. Try again when you are online.'; summary.classList.remove('hidden'); return; }
          netfail.innerHTML = `<div class="grow"><strong>Couldn't confirm with the server.</strong> This transaction is NOT confirmed as saved.<div class="btn-group" style="margin-top:.6rem"><button type="button" class="btn sm primary" data-queue>Keep it on this device and sync later</button></div><div class="xs muted" style="margin-top:.4rem">It is safe to sync: if the server did receive it, it won't be added twice.</div></div>`;
          netfail.classList.remove('hidden'); $('[data-queue]', netfail).addEventListener('click', async () => { if (await enqueue(p)) done({ queued: true }); }); return;
        }
        let data = {}; try { data = await res.json(); } catch (err) { /* non-JSON error page */ }
        if (res.ok && data.ok) {
          try { await PL.store.queueDelete(S.uuid); PL.queue.refreshUI(); } catch (err) { /* nothing queued */ }
          if (data.undo) PL.undoLater(data.undo, data.message || 'Saved.'); else PL.toast(data.message || 'Saved.', 'success'); (data.warnings || []).forEach((w) => PL.toast(w, 'warning', 9000));
          done({ saved: true, id: data.id }); return;
        }
        if (res.status === 422) { showError(data.field || '_', data.error || 'This transaction was rejected.'); return; }
        if (res.status === 401 || (res.status === 400 && data.code === 'csrf')) { summary.innerHTML = `Your session has expired. <a href="${esc(LOGIN_URL)}">Sign in again</a>, then re-enter this transaction. Nothing was saved.`; summary.classList.remove('hidden'); return; }
        summary.textContent = data.error || 'The server could not save this. Nothing was saved.'; summary.classList.remove('hidden');
      } finally { submitBtn.disabled = false; if (submitBtn.textContent === 'Saving…') submitBtn.innerHTML = label; PL.icons(); }
    });
    function done(result) { S.uuid = PL.uuid(); if (cfg.onDone) cfg.onDone(result, payload(), S); }

    const relabel = () => { if (!submitBtn.disabled) { submitBtn.innerHTML = `${PL.icon(offlineNow() ? 'hard-drive-download' : 'check')} ${editing ? 'Save changes' : offlineNow() ? 'Save on this device (offline)' : 'Save ' + TYPES[S.type].verb}`; PL.icons(); } };
    window.addEventListener('online', relabel); window.addEventListener('offline', relabel);
    render(); setTimeout(() => amountEl.focus(), 30);
    return { el: root, state: S };
  }

  /* ---------------------------------------------------------------- queue + sync */
  const Queue = {
    syncing: false, authIssue: false,
    async add(item) { await PL.store.queuePut(item); },
    async all() { try { return await PL.store.queueAll(); } catch (e) { return []; } },
    async refreshUI() {
      const items = await this.all(); const waiting = items.length;
      document.querySelectorAll('[data-sync-pill]').forEach((pill) => {
        const rejected = items.filter((i) => i.status === 'rejected').length;
        if (!waiting && navigator.onLine) { pill.classList.add('hidden'); return; }
        pill.classList.remove('hidden'); pill.classList.toggle('offline', !waiting);
        pill.innerHTML = waiting ? `${PL.icon(this.authIssue ? 'log-in' : 'cloud-upload')} ${waiting} not saved yet${rejected ? ` · ${rejected} need attention` : ''}` : `${PL.icon('wifi-off')} Offline`;
      });
      PL.icons();
      const offlineList = document.getElementById('offline-queue'); if (offlineList) this.render(offlineList, items);
    },
    render(host, items) {
      if (!items.length) { host.innerHTML = '<p class="muted small">Nothing waiting. Everything you recorded has been confirmed by the server.</p>'; return; }
      host.innerHTML = items.map((i) => `<div class="queue-item ${i.status === 'rejected' ? 'failed' : ''}" data-uuid="${esc(i.uuid)}">
        <div class="row between"><strong class="small">${esc(i.label || 'Transaction')}</strong><span class="xs muted">${esc(new Date(i.created_at).toLocaleString())}</span></div>
        ${i.status === 'rejected' ? `<div class="small neg" style="margin:.3rem 0">Rejected by the server: ${esc(i.error)}</div>` : `<div class="xs warn" style="margin:.3rem 0">${PL.icon('clock')} Waiting. NOT saved on the server yet.</div>`}
        <div class="btn-group">${i.status === 'rejected' && !body.dataset.offlinePage ? '<button class="btn sm" data-q-edit>Fix and resend</button>' : ''}<button class="btn sm danger" data-q-discard>Discard</button></div></div>`).join('');
      PL.icons();
      host.querySelectorAll('[data-q-discard]').forEach((b) => b.addEventListener('click', async () => {
        const uuid = b.closest('[data-uuid]').dataset.uuid;
        if (await PL.confirm('Discard this transaction? It was never saved on the server, so it will be gone.', { confirmLabel: 'Discard' })) { await PL.store.queueDelete(uuid); this.refreshUI(); const sheet = document.getElementById('queue-sheet-list'); if (sheet) this.render(sheet, await this.all()); }
      }));
      host.querySelectorAll('[data-q-edit]').forEach((b) => b.addEventListener('click', async () => {
        const item = await PL.store.queueGet(b.closest('[data-uuid]').dataset.uuid); if (!item) return;
        const p = item.payload; PL.quickOpen(p.type, Object.assign({}, p, { amount: p.amount, uuid: item.uuid }));
      }));
    },
    async sync(manual) {
      if (this.syncing || !navigator.onLine || !PL.csrf()) return;
      this.syncing = true; this.authIssue = false; let synced = 0; let rejected = 0;
      const run = async () => {
        const items = (await this.all()).filter((i) => i.status !== 'rejected');
        for (const item of items) {
          let res;
          try {
            const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), 15000);
            res = await fetch(CREATE_URL, { method: 'POST', credentials: 'same-origin', signal: ctl.signal, headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': PL.csrf() }, body: JSON.stringify(item.payload) });
            clearTimeout(timer);
          } catch (e) { break; } // network trouble: keep everything queued, retry later
          let data = {}; try { data = await res.json(); } catch (e) { /* ignore */ }
          if (res.ok && data.ok) { await PL.store.queueDelete(item.uuid); synced += 1; }
          else if (res.status === 422) { item.status = 'rejected'; item.error = data.error || 'Rejected.'; await PL.store.queuePut(item); rejected += 1; }
          else if (res.status === 401 || res.status === 400) { this.authIssue = true; break; }
          else break; // server error: stop, keep queued
        }
      };
      try { if (navigator.locks) await navigator.locks.request('pocketledger-sync', { ifAvailable: true }, async (lock) => { if (lock) await run(); }); else await run(); }
      finally { this.syncing = false; }
      await this.refreshUI();
      if (synced) { PL.toast(`${synced} transaction${synced > 1 ? 's' : ''} synced and confirmed by the server.`, 'success'); setTimeout(() => { if (!document.querySelector('.overlay')) location.reload(); }, 900); }
      if (rejected) PL.toast(`${rejected} queued transaction${rejected > 1 ? 's were' : ' was'} rejected by the server. Open the queue to fix or discard.`, 'error');
      if (this.authIssue) PL.toast('Sign in again to sync your waiting transactions.', 'warning');
      else if (manual && !synced && !rejected && (await this.all()).length) PL.toast('Still waiting for a connection.', 'warning');
    },
    async openSheet() {
      const sheet = document.createElement('div'); sheet.className = 'sheet';
      sheet.innerHTML = `<div class="sheet-head"><h2>Waiting on this device</h2><button class="btn ghost sm" data-x aria-label="Close">${PL.icon('x')}</button></div>
        <div class="sheet-body"><p class="muted small">These transactions are <strong>not saved on the server yet</strong>. They are removed from here only after the server confirms them.</p>
        <div id="queue-sheet-list"></div>${this.authIssue ? `<a class="btn primary block" href="${esc(LOGIN_URL)}">Sign in to sync</a>` : `<button class="btn primary block" data-sync>${PL.icon('refresh-cw')} Sync now</button>`}</div>`;
      const ov = PL.overlay(sheet); $('[data-x]', sheet).addEventListener('click', () => ov.close());
      const list = $('#queue-sheet-list', sheet); this.render(list, await this.all());
      const syncBtn = $('[data-sync]', sheet); if (syncBtn) syncBtn.addEventListener('click', async () => { await this.sync(true); this.render(list, await this.all()); });
    },
  };
  PL.queue = Queue;

  /* ---------------------------------------------------------------- quick-entry sheet + page form */
  async function mountInto(host, cfg) {
    host.innerHTML = '<p class="muted small">Loading…</p>';
    let options;
    try { options = await Options.load(); }
    catch (err) {
      host.innerHTML = err.auth ? `<div class="alert error">Your session has expired. <a href="${esc(LOGIN_URL)}">Sign in again</a>.</div>`
        : '<div class="alert warning">PocketLedger needs to load your accounts once before it can be used offline. Reconnect and open this page again.</div>';
      return null;
    }
    host.innerHTML = '';
    if (Options.stale) { const n = document.createElement('div'); n.className = 'alert warning'; n.innerHTML = 'Using the account list saved on this device. It may be out of date.'; host.appendChild(n); }
    const form = buildForm(Object.assign({ options }, cfg)); host.appendChild(form.el); PL.icons(); return form;
  }

  PL.quickOpen = function (type, prefill) {
    const existing = document.querySelector('.overlay .tx-form');
    if (existing) { return; }
    const sheet = document.createElement('div'); sheet.className = 'sheet';
    sheet.innerHTML = `<div class="sheet-head"><h2>Add transaction</h2><button class="btn ghost sm" data-x aria-label="Close">${PL.icon('x')}</button></div><div class="sheet-body" data-host></div>`;
    const ov = PL.overlay(sheet); $('[data-x]', sheet).addEventListener('click', () => ov.close());
    mountInto($('[data-host]', sheet), {
      prefill: Object.assign({ type: type || 'expense' }, prefill || {}),
      onDone: (result) => { ov.close(); if (result.saved) setTimeout(() => location.reload(), 500); },
    });
  };

  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-quick]');
    if (trigger) { e.preventDefault(); PL.quickOpen(trigger.dataset.quick); }
    const pill = e.target.closest('[data-sync-pill]'); if (pill) Queue.openSheet();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'n' || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target; if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (document.querySelector('.overlay:not(.hidden)')) return;
    if (document.querySelector('[data-quick]')) { e.preventDefault(); PL.quickOpen('expense'); }
  });

  document.addEventListener('DOMContentLoaded', () => {
    const pageHost = document.getElementById('page-form');
    if (pageHost) {
      let prefill = {}; try { prefill = JSON.parse(pageHost.dataset.prefill || '{}'); } catch (e) { /* ignore */ }
      const editingId = pageHost.dataset.editing || null;
      const mountPage = (pf) => mountInto(pageHost, {
        prefill: pf, editingId, allowAdjustment: true,
        onDone: (result, payload, state) => {
          if (result.queued) { mountPage({ type: state.type }); return; } // stay here; a fresh form is ready
          if (state.reconciliation_id) location.href = '/reconcile/' + state.reconciliation_id;
          else location.href = '/transactions/' + result.id;
        },
      });
      mountPage(prefill);
    }
    Queue.refreshUI();
    window.addEventListener('online', () => { Queue.refreshUI(); Queue.sync(); if (body.dataset.offlinePage) setTimeout(() => { location.href = body.dataset.homeUrl || '/'; }, 1200); });
    window.addEventListener('offline', () => Queue.refreshUI());
    if (document.querySelector('meta[name="csrf-token"]') && !body.dataset.offlinePage) {
      Options.load().catch(() => {}); // keep the offline copy of accounts/people fresh
      setTimeout(() => Queue.sync(), 800);
    }
  });
})();
