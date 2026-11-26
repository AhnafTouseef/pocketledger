/* Instant feedback on slow submits: a form marked data-busy="Label…" shows a spinner on its button and
 * blocks double clicks the moment it is submitted. The page navigates away when the server answers; if it
 * never does (dead connection) the button comes back after 20 s so the user can try again. */
(function () {
  'use strict';
  function reset(form) {
    const b = form.querySelector('button.is-busy');
    if (!b) return;
    b.classList.remove('is-busy'); b.disabled = false; b.removeAttribute('aria-busy');
    if (b.dataset.label) b.textContent = b.dataset.label;
    form._busy = false;
  }
  document.addEventListener('submit', function (e) {
    const form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-busy')) return;
    if (form._busy) { e.preventDefault(); return; }                 // already sending
    // run after any other submit handler had its say
    setTimeout(function () {
      if (e.defaultPrevented) return;
      const b = e.submitter || form.querySelector('button[type="submit"]');
      if (!b) return;
      form._busy = true;
      b.dataset.label = b.textContent;
      b.classList.add('is-busy'); b.setAttribute('aria-busy', 'true');
      b.textContent = form.getAttribute('data-busy') || 'Please wait…';
      b.disabled = true;                                           // after the browser collected the form data
      setTimeout(function () { reset(form); }, 20000);
    }, 0);
  });
  // coming back with the Back button must not show a stuck spinner
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) document.querySelectorAll('form[data-busy]').forEach(reset);
  });
})();
