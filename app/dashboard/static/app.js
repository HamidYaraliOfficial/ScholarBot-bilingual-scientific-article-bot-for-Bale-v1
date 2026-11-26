(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const body = document.body;
  const lang = body.dataset.lang;
  const i18n = JSON.parse($('#i18n')?.textContent || '{}');
  const csrf = () => body.dataset.csrf;

  /* sidebar */
  const app = $('#app');
  if (localStorage.getItem('sb') === '1') app.classList.add('collapsed');
  $('#collapse-btn')?.addEventListener('click', () => {
    app.classList.toggle('collapsed');
    localStorage.setItem('sb', app.classList.contains('collapsed') ? '1' : '0');
  });
  $('#menu-btn')?.addEventListener('click', () => app.classList.toggle('open'));

  /* toasts */
  function toast(msg, kind = 'ok') {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.setAttribute('role', 'status');
    el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), 7000);
  }
  $$('.toast').forEach(el => setTimeout(() => el.remove(), 7000));
  document.body.addEventListener('toast', e => toast(e.detail.msg, e.detail.kind));

  /* localized timestamps (Persian calendar in fa) */
  const fmt = new Intl.DateTimeFormat(lang === 'fa' ? 'fa-IR-u-ca-persian' : 'en-GB', { dateStyle: 'medium', timeStyle: 'short' });
  function formatTimes(root = document) {
    $$('time.ts', root).forEach(t => {
      if (t.dataset.done) return;
      const d = new Date(t.getAttribute('datetime'));
      if (!isNaN(d)) { t.textContent = fmt.format(d); t.title = d.toISOString(); t.dataset.done = '1'; }
    });
  }

  /* confirm on destructive plain forms */
  document.addEventListener('submit', e => {
    const msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  /* detail dialog */
  document.addEventListener('htmx:afterSwap', e => {
    if (e.detail.target && e.detail.target.id === 'detail-body') {
      formatTimes(e.detail.target);
      const d = $('#detail-dialog');
      if (d && !d.open) d.showModal();
    }
  });
  document.addEventListener('click', e => { if (e.target.id === 'detail-close') $('#detail-dialog').close(); });

  /* ---------- articles table: selection, bulk actions, exports ---------- */
  let allFiltered = false;
  const wrap = () => $('#articles-wrap');
  const selectedIds = () => $$('.row-sel:checked').map(c => c.value);

  function updateSelection() {
    const w = wrap(); if (!w) return;
    const rows = $$('.row-sel', w), ids = selectedIds();
    rows.forEach(c => c.closest('tr').classList.toggle('sel', c.checked));
    const total = parseInt(w.dataset.total || '0', 10);
    const count = allFiltered ? total : ids.length;
    $('#sel-count').textContent = count ? i18n.selected.replace('{n}', count) : '';
    const banner = $('#sel-banner');
    const page = $('#sel-page');
    if (page) page.checked = rows.length > 0 && ids.length === rows.length;
    if (allFiltered) {
      banner.hidden = false; banner.textContent = i18n.all_selected.replace('{n}', total);
    } else if (rows.length && ids.length === rows.length && total > rows.length) {
      banner.hidden = false; banner.textContent = '';
      const btn = document.createElement('button');
      btn.className = 'btn sm'; btn.id = 'sel-all-btn'; btn.type = 'button';
      btn.textContent = i18n.select_all.replace('{n}', total);
      banner.appendChild(btn);
    } else banner.hidden = true;
  }
  document.addEventListener('change', e => {
    if (e.target.matches('.row-sel')) { allFiltered = false; updateSelection(); }
    if (e.target.id === 'sel-page') {
      $$('.row-sel').forEach(c => { c.checked = e.target.checked; });
      allFiltered = false; updateSelection();
    }
  });
  document.addEventListener('htmx:load', e => {
    formatTimes(e.target);
    if (e.target.id === 'articles-wrap') { allFiltered = false; updateSelection(); }
  });

  function payload(ids, useAll, extra = {}) {
    const fd = new FormData(), w = wrap();
    if (useAll && w) {
      fd.set('all_filtered', '1');
      new URLSearchParams(w.dataset.filter).forEach((v, k) => fd.set(k, v));
      if (w.dataset.library === '1') fd.set('library', '1');
    } else ids.forEach(id => fd.append('ids', id));
    Object.entries(extra).forEach(([k, v]) => fd.set(k, v));
    return fd;
  }
  const post = (url, fd) => fetch(url, { method: 'POST', body: fd, headers: { 'X-CSRF-Token': csrf() } });
  const refreshTable = () => { const f = $('#filters'); if (f && window.htmx) htmx.trigger(f, 'submit'); };

  async function download(url, fd) {
    const r = await post(url, fd);
    if (!r.ok) {
      let msg = i18n.error; try { msg = (await r.json()).message || msg; } catch (_) {}
      return toast(msg, 'err');
    }
    const name = /filename="?([^";]+)"?/.exec(r.headers.get('content-disposition') || '')?.[1] || 'download';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await r.blob()); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }
  async function runBulk(fd) {
    const r = await post('/articles/bulk', fd);
    let data = {}; try { data = await r.json(); } catch (_) {}
    toast(data.message || i18n.error, r.ok && data.ok ? 'ok' : 'err');
    if (r.ok && data.ok) refreshTable();
  }

  const dlg = () => $('#delete-dialog');
  let pending = null;
  function askDelete(ids, useAll) {
    pending = { ids, useAll };
    const n = useAll ? parseInt(wrap().dataset.total, 10) : ids.length;
    $('#dd-count').textContent = i18n.delete_n.replace('{n}', n);
    dlg().showModal();
  }

  document.addEventListener('click', async e => {
    if (e.target.id === 'sel-all-btn') { allFiltered = true; updateSelection(); return; }
    if (e.target.id === 'dd-cancel') { dlg().close(); return; }
    if (e.target.id === 'dd-confirm') {
      const mode = $('input[name=dmode]:checked', dlg()).value;
      dlg().close();
      await runBulk(payload(pending.ids, pending.useAll, { action: 'delete', confirm: 'yes', mode }));
      return;
    }
    const b = e.target.closest('[data-bulk],[data-export],[data-delete-id]');
    if (!b) return;
    if (b.dataset.deleteId) return askDelete([b.dataset.deleteId], false);
    const ids = selectedIds();
    if (!ids.length && !allFiltered) return toast(i18n.none, 'err');
    if (b.dataset.bulk === 'delete') return askDelete(ids, allFiltered);
    if (b.dataset.bulk === 'zip') return download('/articles/zip', payload(ids, allFiltered));
    if (b.dataset.export) return download('/articles/export', payload(ids, allFiltered, { fmt: b.dataset.export }));
    if (b.dataset.bulk) return runBulk(payload(ids, allFiltered, { action: b.dataset.bulk }));
  });

  /* ---------- charts ---------- */
  async function charts() {
    const box = $('#charts');
    if (!box || !window.Chart) return;
    const L = JSON.parse(box.dataset.labels);
    const data = await (await fetch('/api/stats/charts')).json();
    const css = getComputedStyle(document.documentElement);
    const c = n => css.getPropertyValue(n).trim();
    Chart.defaults.font.family = 'Vazirmatn, system-ui, sans-serif';
    Chart.defaults.color = c('--muted');
    Chart.defaults.borderColor = c('--line');
    const dayFmt = new Intl.DateTimeFormat(lang === 'fa' ? 'fa-IR-u-ca-persian' : 'en-GB', { month: 'short', day: 'numeric' });
    const day = d => dayFmt.format(new Date(d + 'T00:00:00'));
    const common = { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { boxWidth: 12 } } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } };
    new Chart($('#ch-requests'), { type: 'line', data: { labels: data.days.map(day), datasets: [
      { label: L.requests, data: data.requests, borderColor: c('--info'), backgroundColor: c('--info'), tension: .25, pointRadius: 2 },
      { label: L.received, data: data.received, borderColor: c('--accent'), backgroundColor: c('--accent'), tension: .25, pointRadius: 2 }] }, options: common });
    new Chart($('#ch-active'), { type: 'bar', data: { labels: data.weeks.map(day), datasets: [{ label: L.active, data: data.active_users, backgroundColor: c('--accent') }] }, options: common });
    new Chart($('#ch-sources'), { type: 'bar', data: { labels: data.sources.labels, datasets: [{ label: L.sources, data: data.sources.values, backgroundColor: c('--warn') }] }, options: { ...common, indexAxis: 'y', scales: { x: { beginAtZero: true, ticks: { precision: 0 } } } } });
    new Chart($('#ch-rate'), { type: 'line', data: { labels: data.days.map(day), datasets: [{ label: L.rate, data: data.success_rate, borderColor: c('--ok'), backgroundColor: c('--ok'), spanGaps: true, tension: .25, pointRadius: 2 }] }, options: { ...common, scales: { y: { min: 0, max: 100 } } } });
  }

  const init = () => { formatTimes(); updateSelection(); charts().catch(() => {}); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
