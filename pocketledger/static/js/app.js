/* PocketLedger shared utilities: DOM helpers, money formatting, toasts, dialogs, navigation. */
(function () {
  'use strict';
  const PL = (window.PL = window.PL || {});
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  PL.$ = $; PL.$$ = $$;

  PL.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  PL.csrf = () => { const m = document.querySelector('meta[name="csrf-token"]'); return m ? m.content : ''; };
  PL.uuid = () => {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    const b = new Uint8Array(16); (window.crypto || window.msCrypto).getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  };
  PL.icons = () => { try { if (window.lucide) window.lucide.createIcons(); } catch (e) { /* icons are decorative */ } };
  PL.icon = (name, cls) => `<i data-lucide="${name}"${cls ? ` class="${cls}"` : ''}></i>`;

  /* ---------- money (integers in minor units; never floats for arithmetic) ---------- */
  PL.currency = { code: 'BDT', symbol: '৳', minor_unit: 2, grouping: 'indian' };
  PL.setCurrency = (c) => { if (c && c.symbol) PL.currency = c; };
  PL.group = (digits, grouping) => {
    if (grouping === 'indian' && digits.length > 3) {
      let head = digits.slice(0, -3); const tail = digits.slice(-3); const parts = [];
      while (head.length > 2) { parts.unshift(head.slice(-2)); head = head.slice(0, -2); }
      if (head) parts.unshift(head);
      return parts.concat(tail).join(',');
    }
    return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  };
  PL.fmt = (minor, opts) => {
    opts = opts || {};
    const c = PL.currency; const mu = c.minor_unit;
    const neg = minor < 0; const abs = Math.abs(Math.round(minor));
    const whole = Math.floor(abs / Math.pow(10, mu)); const frac = abs % Math.pow(10, mu);
    let s = PL.group(String(whole), c.grouping);
    if (mu && !opts.noDecimals) s += '.' + String(frac).padStart(mu, '0');
    return (neg ? '−' : '') + (opts.noSymbol ? '' : c.symbol) + s;
  };
  PL.fmtCompact = (minor) => {
    const v = Math.abs(minor) / Math.pow(10, PL.currency.minor_unit);
    const sign = minor < 0 ? '−' : '';
    const sym = PL.currency.symbol;
    if (v >= 1e7) return `${sign}${sym}${(v / 1e7).toFixed(1).replace(/\.0$/, '')}Cr`;
    if (v >= 1e5) return `${sign}${sym}${(v / 1e5).toFixed(1).replace(/\.0$/, '')}L`;
    if (v >= 1e3) return `${sign}${sym}${(v / 1e3).toFixed(1).replace(/\.0$/, '')}k`;
    return `${sign}${sym}${Math.round(v)}`;
  };
  /** Parse '1,500.50' or '১২৫০' into integer minor units, or null if invalid. */
  PL.parseAmount = (text) => {
    if (text == null) return null;
    let s = String(text).trim().replace(/[০-৯]/g, (d) => '০১২৩৪৫৬৭৮৯'.indexOf(d)).replace(/[৳,\s]|BDT|Tk\.?/gi, '');
    if (!/^(\d+(\.\d*)?|\.\d+)$/.test(s)) return null;
    const mu = PL.currency.minor_unit; const [w, f = ''] = s.split('.');
    if (f.length > mu) return null;
    const minor = parseInt(w || '0', 10) * Math.pow(10, mu) + parseInt((f + '0'.repeat(mu)).slice(0, mu) || '0', 10);
    return Number.isSafeInteger(minor) ? minor : null;
  };
  /** Current wall-clock time in the app's configured timezone as 'YYYY-MM-DDTHH:MM'. */
  PL.nowLocal = (tz) => {
    try { return new Date().toLocaleString('sv-SE', { timeZone: tz || undefined }).slice(0, 16).replace(' ', 'T'); }
    catch (e) { const d = new Date(Date.now() - new Date().getTimezoneOffset() * 60000); return d.toISOString().slice(0, 16); }
  };

  /* ---------- toasts ---------- */
  PL.toast = (message, kind, ms) => {
    let host = $('#toasts');
    if (!host) { host = document.createElement('div'); host.id = 'toasts'; document.body.appendChild(host); }
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || 'success'); el.setAttribute('role', 'status');
    const ico = { success: 'circle-check', error: 'circle-alert', warning: 'triangle-alert' }[kind || 'success'] || 'info';
    el.innerHTML = PL.icon(ico) + '<div class="grow"></div>';
    el.lastChild.textContent = message;
    host.appendChild(el); PL.icons();
    setTimeout(() => el.remove(), ms || (kind === 'error' || kind === 'warning' ? 7000 : 4000));
  };

  /* ---------- overlays & confirm dialog ---------- */
  PL.overlay = (contentEl, opts) => {
    opts = opts || {};
    const previous = document.activeElement;
    const ov = document.createElement('div'); ov.className = 'overlay'; ov.setAttribute('role', 'dialog'); ov.setAttribute('aria-modal', 'true');
    ov.appendChild(contentEl); document.body.appendChild(ov);
    document.body.style.overflow = 'hidden';
    const api = {
      el: ov,
      close() {
        ov.remove(); document.removeEventListener('keydown', onKey, true);
        if (!$('.overlay:not(.hidden)')) document.body.style.overflow = '';
        if (previous && previous.focus) { try { previous.focus(); } catch (e) { /* ignore */ } }
        if (opts.onClose) opts.onClose();
      },
    };
    const onKey = (e) => { if (e.key === 'Escape' && ov === Array.from(document.querySelectorAll('.overlay')).pop()) { e.stopPropagation(); api.close(); } };
    document.addEventListener('keydown', onKey, true);
    ov.addEventListener('mousedown', (e) => { if (e.target === ov) api.close(); });
    PL.icons();
    return api;
  };

  PL.confirm = (message, opts) => new Promise((resolve) => {
    opts = opts || {};
    const sheet = document.createElement('div'); sheet.className = 'sheet'; sheet.style.maxWidth = '420px';
    sheet.innerHTML = `<div class="sheet-body" style="padding-top:1.2rem"><h2 style="margin-bottom:.5rem">${PL.esc(opts.title || 'Are you sure?')}</h2>
      <p class="muted" style="white-space:pre-line">${PL.esc(message)}</p>
      <div class="form-actions"><button type="button" class="btn ${opts.danger === false ? 'primary' : 'danger'}" data-ok>${PL.esc(opts.confirmLabel || 'Confirm')}</button>
      <button type="button" class="btn ghost" data-cancel>Cancel</button></div></div>`;
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    const ov = PL.overlay(sheet, { onClose: () => finish(false) });
    $('[data-ok]', sheet).addEventListener('click', () => { finish(true); ov.close(); });
    $('[data-cancel]', sheet).addEventListener('click', () => ov.close());
    $('[data-cancel]', sheet).focus();
  });

  /* ---------- page wiring ---------- */
  document.addEventListener('submit', (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute('data-logout') && PL.store) { try { PL.store.clearOptions(); } catch (err) { /* ignore */ } }
    const msg = form.getAttribute('data-confirm');
    if (!msg || form._confirmed) return;
    e.preventDefault();
    const submitter = e.submitter;
    PL.confirm(msg, { confirmLabel: submitter && submitter.textContent.trim() ? submitter.textContent.trim() : 'Confirm' }).then((ok) => {
      if (!ok) return;
      form._confirmed = true;
      if (form.requestSubmit) form.requestSubmit(submitter || undefined); else form.submit();
    });
  });

  document.addEventListener('DOMContentLoaded', () => {
    PL.icons();
    // "More" sheet on mobile
    const more = $('#more-sheet');
    if (more) {
      const show = () => { more.classList.remove('hidden'); document.body.style.overflow = 'hidden'; };
      const hide = () => { more.classList.add('hidden'); document.body.style.overflow = ''; };
      $$('[data-more]').forEach((b) => b.addEventListener('click', show));
      more.addEventListener('mousedown', (e) => { if (e.target === more) hide(); });
      $$('[data-close]', more).forEach((b) => b.addEventListener('click', hide));
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !more.classList.contains('hidden')) hide(); });
    }
    // online/offline banner
    const banner = $('#offline-banner');
    const reflect = () => { if (banner) banner.classList.toggle('hidden', navigator.onLine); };
    window.addEventListener('online', reflect); window.addEventListener('offline', reflect); reflect();
    // service worker
    if ('serviceWorker' in navigator && document.body.dataset.swUrl) {
      window.addEventListener('load', () => navigator.serviceWorker.register(document.body.dataset.swUrl, { scope: '/' }).catch(() => { /* non-fatal */ }));
    }
  });
})();
