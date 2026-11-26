// Drives dashboard/static/app.js against the real server-rendered articles page inside jsdom.
// usage: node ui.test.js <articles.html> <app.js> <dashboard.html>
const fs = require('fs');
const { JSDOM } = require('jsdom');
const [htmlPath, jsPath, dashPath] = process.argv.slice(2);
const results = [];
const check = (name, cond, extra = '') => { results.push({ name, ok: !!cond, extra }); };

function boot(path, url) {
  const dom = new JSDOM(fs.readFileSync(path, 'utf8'), { url, runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window;
  w.__fetches = [];
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  w.htmx = { trigger: (el, ev) => { w.__triggered = (w.__triggered || []).concat([ev]); } };
  w.URL.createObjectURL = () => 'blob:fake';
  w.URL.revokeObjectURL = () => {};
  w.HTMLAnchorElement.prototype.click = function () { w.__downloads = (w.__downloads || []).concat([this.download]); };
  w.fetch = async (url, opts = {}) => {
    const entries = opts.body ? [...opts.body.entries()] : [];
    w.__fetches.push({ url, headers: opts.headers, entries });
    if (url.startsWith('/articles/export') || url.startsWith('/articles/zip')) {
      return { ok: true, status: 200, headers: { get: () => 'attachment; filename="scholarbot_articles.csv"' }, blob: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => ({ ok: true, message: 'done-message', reload: true }) };
  };
  w.eval(fs.readFileSync(jsPath, 'utf8'));
  return w;
}
const click = (w, el) => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
const flush = () => new Promise(r => setTimeout(r, 20));
const val = (entries, k) => entries.filter(([a]) => a === k).map(([, v]) => String(v));

(async () => {
  const w = boot(htmlPath, 'http://localhost/articles?per_page=10');
  const d = w.document;
  await flush();   // let DOMContentLoaded handlers run
  const rows = [...d.querySelectorAll('.row-sel')];
  check('page has 10 selectable rows', rows.length === 10, rows.length);
  check('timestamps are localized (Persian calendar)', [...d.querySelectorAll('time.ts')].every(t => t.textContent.trim().length > 0 && /[۰-۹]/.test(t.textContent)));

  rows[0].checked = true; rows[1].checked = true;
  rows[1].dispatchEvent(new w.Event('change', { bubbles: true }));
  check('selection counter updates', /2/.test(d.getElementById('sel-count').textContent), d.getElementById('sel-count').textContent);
  check('selected rows highlighted', d.querySelectorAll('tr.sel').length === 2);

  click(w, d.querySelector('[data-bulk=save]')); await flush();
  let f = w.__fetches.pop();
  check('save posts to /articles/bulk with CSRF header', f.url === '/articles/bulk' && f.headers['X-CSRF-Token'].length > 20);
  check('save sends action + selected ids', val(f.entries, 'action')[0] === 'save' && val(f.entries, 'ids').length === 2 && val(f.entries, 'ids').join() === [rows[0].value, rows[1].value].join());
  check('table refresh requested after success', (w.__triggered || []).includes('submit'));
  check('toast shown with server message', [...d.querySelectorAll('#toasts .toast')].some(t => t.textContent === 'done-message'));

  // select whole page -> "select all N filtered" banner -> bulk action uses server-side filter instead of ids
  const page = d.getElementById('sel-page'); page.checked = true; page.dispatchEvent(new w.Event('change', { bubbles: true }));
  const banner = d.getElementById('sel-banner');
  check('banner offers all filtered results', !banner.hidden && /12/.test(banner.textContent), banner.textContent);
  click(w, d.getElementById('sel-all-btn'));
  check('all-filtered mode announced', /12/.test(d.getElementById('sel-banner').textContent) && /12/.test(d.getElementById('sel-count').textContent));
  click(w, d.querySelector('[data-bulk=save]')); await flush();
  f = w.__fetches.pop();
  check('all-filtered request sends flag and no ids', val(f.entries, 'all_filtered')[0] === '1' && val(f.entries, 'ids').length === 0);

  // exports and zip download through blob
  click(w, d.querySelector('[data-export=csv]')); await flush();
  f = w.__fetches.pop();
  check('export posts format', f.url === '/articles/export' && val(f.entries, 'fmt')[0] === 'csv');
  check('browser download triggered with server file name', (w.__downloads || []).includes('scholarbot_articles.csv'));
  click(w, d.querySelector('[data-bulk=zip]')); await flush();
  check('zip posts to /articles/zip', w.__fetches.pop().url === '/articles/zip');

  // deletion: dialog first, nothing is sent until confirmed; mode radio is honoured
  const before = w.__fetches.length;
  click(w, d.querySelector('[data-delete-id]'));
  const dlg = d.getElementById('delete-dialog');
  check('delete opens confirmation dialog without sending', dlg.hasAttribute('open') && w.__fetches.length === before);
  click(w, d.getElementById('dd-cancel')); await flush();
  check('cancel closes dialog, still nothing sent', !dlg.hasAttribute('open') && w.__fetches.length === before);
  click(w, d.querySelector('[data-delete-id]'));
  d.querySelector('input[name=dmode][value=info_and_file]').checked = true;
  click(w, d.getElementById('dd-confirm')); await flush();
  f = w.__fetches.pop();
  check('confirmed delete sends confirm + mode + the single id',
    val(f.entries, 'action')[0] === 'delete' && val(f.entries, 'confirm')[0] === 'yes' && val(f.entries, 'mode')[0] === 'info_and_file' && val(f.entries, 'ids').length === 1);

  // nothing selected -> error toast, no request
  rows.forEach(r => { r.checked = false; }); d.getElementById('sel-page').checked = false;
  rows[0].dispatchEvent(new w.Event('change', { bubbles: true }));
  const n = w.__fetches.length;
  click(w, d.querySelector('[data-bulk=save]')); await flush();
  check('empty selection is rejected client-side', w.__fetches.length === n && [...d.querySelectorAll('#toasts .toast.err')].length >= 1);

  // dashboard: charts are created from the JSON endpoint
  const dw = new JSDOM(fs.readFileSync(dashPath, 'utf8'), { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true }).window;
  const made = [];
  dw.Chart = function (el, cfg) { made.push([el.id, cfg.type, cfg.data.datasets.length]); };
  dw.Chart.defaults = { font: {}, plugins: {} };
  dw.fetch = async () => ({ json: async () => ({ days: ['2026-11-01', '2026-11-02'], requests: [1, 2], received: [1, 1], success_rate: [100, null], weeks: ['2026-11-02'], active_users: [3], sources: { labels: ['unpaywall'], values: [4] } }) });
  dw.eval(fs.readFileSync(jsPath, 'utf8')); await flush();
  const ids = made.map(m => m[0]).sort().join();
  check('four dashboard charts are drawn', ids === 'ch-active,ch-rate,ch-requests,ch-sources', ids);
  check('request chart has requests + delivered series', made.find(m => m[0] === 'ch-requests')[2] === 2);

  const failed = results.filter(r => !r.ok);
  console.log(JSON.stringify({ total: results.length, failed }, null, 1));
  process.exit(failed.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
