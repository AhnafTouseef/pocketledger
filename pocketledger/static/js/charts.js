/* Charts (Chart.js). Every series comes straight from server-side ledger queries embedded as JSON. */
(function () {
  'use strict';
  if (!window.Chart) return;
  const PL = window.PL; const Chart = window.Chart;
  const C = { green: '#10b981', greenSoft: 'rgba(16,185,129,.18)', red: '#f87171', blue: '#60a5fa', amber: '#fbbf24', text: '#8d9892', grid: '#272e2b', surface: '#151917' };
  const PALETTE = ['#10b981', '#60a5fa', '#fbbf24', '#f472b6', '#a78bfa', '#fb923c', '#22d3ee', '#94a3b8', '#84cc16'];
  const read = (id) => { const el = document.getElementById(id); if (!el) return null; try { return JSON.parse(el.textContent); } catch (e) { return null; } };

  Chart.defaults.color = C.text;
  Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans Bengali", sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.animation = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches ? false : { duration: 350 };
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.boxWidth = 8;
  Chart.defaults.plugins.tooltip.backgroundColor = '#232926';
  Chart.defaults.plugins.tooltip.borderColor = '#35403b';
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.titleColor = '#e9eeeb';
  Chart.defaults.plugins.tooltip.bodyColor = '#e9eeeb';
  Chart.defaults.plugins.tooltip.padding = 10;

  const moneyTip = { callbacks: { label: (ctx) => `${ctx.dataset.label ? ctx.dataset.label + ': ' : ''}${PL.fmt(ctx.parsed.y != null ? ctx.parsed.y : ctx.parsed)}` } };
  const yAxis = { grid: { color: C.grid }, border: { display: false }, ticks: { callback: (v) => PL.fmtCompact(v), maxTicksLimit: 6 } };
  const xAxis = { grid: { display: false }, border: { color: C.grid }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 } };
  const draw = (id, config) => { const el = document.getElementById(id); if (el) new Chart(el, config); };
  const hasData = (arr) => arr.some((v) => v !== 0);

  function lineChart(id, labels, values, color) {
    draw(id, {
      type: 'line',
      data: { labels, datasets: [{ label: 'Net worth', data: values, borderColor: color, backgroundColor: C.greenSoft, fill: true, tension: 0.25, pointRadius: values.length > 40 ? 0 : 3, pointHoverRadius: 5, borderWidth: 2 }] },
      options: { interaction: { mode: 'index', intersect: false }, plugins: { legend: { display: false }, tooltip: moneyTip }, scales: { x: xAxis, y: yAxis } },
    });
  }
  function barChart(id, labels, sets, opts) {
    opts = opts || {};
    draw(id, {
      type: 'bar',
      data: { labels, datasets: sets.map((s) => ({ label: s.label, data: s.data, backgroundColor: s.color, borderRadius: 4, maxBarThickness: 28 })) },
      options: { interaction: { mode: 'index', intersect: false }, plugins: { legend: { display: sets.length > 1, position: 'bottom' }, tooltip: moneyTip }, scales: { x: Object.assign({}, xAxis, { stacked: !!opts.stacked }), y: Object.assign({}, yAxis, { stacked: !!opts.stacked, beginAtZero: true }) } },
    });
  }

  /* dashboard: 30-day net worth */
  const nw = read('nw-data'); const cur = read('cur-data'); if (cur) PL.setCurrency(cur);
  if (nw && document.getElementById('nw-chart')) lineChart('nw-chart', nw.labels, nw.values, C.green);

  /* analytics */
  const a = read('analytics-data');
  if (a) {
    PL.setCurrency(a.currency);
    const bs = a.balance_sheet; const fl = a.flows; const mo = a.monthly;
    lineChart('c-networth', bs.labels, bs.rows.map((r) => r.net_worth), C.green);
    barChart('c-assets', bs.labels, [{ label: 'Assets', data: bs.rows.map((r) => r.assets), color: C.green }, { label: 'Liabilities', data: bs.rows.map((r) => r.liabilities), color: C.red }]);
    barChart('c-flow', fl.labels, [{ label: 'Income', data: fl.rows.map((r) => r.income), color: C.green }, { label: 'Spending', data: fl.rows.map((r) => r.expense), color: C.red }]);
    barChart('c-monthly', mo.labels, [{ label: 'Income', data: mo.rows.map((r) => r.income), color: C.green }, { label: 'Spending', data: mo.rows.map((r) => r.expense), color: C.red }]);
    barChart('c-lending', fl.labels, [
      { label: 'Lent', data: fl.rows.map((r) => r.lent), color: C.amber },
      { label: 'Repaid to me', data: fl.rows.map((r) => r.repayment_received), color: C.green },
      { label: 'Borrowed', data: fl.rows.map((r) => r.borrowed), color: C.blue },
      { label: 'I repaid', data: fl.rows.map((r) => r.repayment_made), color: C.red },
    ]);
    const accts = a.account_distribution.filter((x) => x.balance > 0);
    if (accts.length && document.getElementById('c-accounts')) {
      draw('c-accounts', {
        type: 'doughnut',
        data: { labels: accts.map((x) => x.name), datasets: [{ data: accts.map((x) => x.balance), backgroundColor: accts.map((_, i) => PALETTE[i % PALETTE.length]), borderColor: C.surface, borderWidth: 2 }] },
        options: { cutout: '62%', plugins: { legend: { position: 'right', labels: { padding: 12 } }, tooltip: { callbacks: { label: (ctx) => ` ${ctx.label}: ${PL.fmt(ctx.parsed)}` } } } },
      });
    }
    if (a.expense_categories.length && document.getElementById('c-cats')) {
      draw('c-cats', {
        type: 'bar',
        data: { labels: a.expense_categories.map((x) => x.name), datasets: [{ data: a.expense_categories.map((x) => x.total), backgroundColor: C.red, borderRadius: 4, maxBarThickness: 22 }] },
        options: { indexAxis: 'y', plugins: { legend: { display: false }, tooltip: { callbacks: { label: (ctx) => ` ${PL.fmt(ctx.parsed.x)}` } } }, scales: { x: Object.assign({}, yAxis, { beginAtZero: true }), y: { grid: { display: false }, border: { display: false } } } },
      });
    }
  }
})();
