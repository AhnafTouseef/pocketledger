/* Verify page: show the difference as the counted amount is typed. Display only; the server recomputes it. */
(function () {
  'use strict';
  const PL = window.PL; const input = document.getElementById('f-counted');
  if (!input) return;
  const out = document.getElementById('diff-live'); const hint = document.getElementById('diff-hint');
  const expected = parseInt(input.dataset.expected, 10);
  function update() {
    const minor = PL.parseAmount(input.value);
    out.className = 'value num';
    if (minor === null || input.value.trim() === '') { out.textContent = '–'; out.classList.add('muted'); hint.textContent = 'Enter what you counted'; return; }
    const diff = minor - expected;
    out.textContent = PL.fmt(diff);
    if (diff === 0) { out.classList.add('pos'); hint.textContent = 'Matches your records'; }
    else if (diff < 0) { out.classList.add('neg'); hint.textContent = 'You hold less than your records say'; }
    else { out.classList.add('pos'); hint.textContent = 'You hold more than your records say'; }
  }
  input.addEventListener('input', update); update();
})();
