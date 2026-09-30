import {getOpportunities, getOpportunity} from './api.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const renderDrawer = drawer => data => {
  drawer.hidden = false;
  if (data.loading) { drawer.innerHTML = '<p>Loading visible books and history...</p>'; return; }
  if (data.error) { drawer.innerHTML = `<p class="error">${esc(data.error.message || data.error)}</p>`; return; }
  drawer.innerHTML = `<div class="section-title"><h3>${esc(data.title)}</h3><button type="button" data-close>Close</button></div><p>${esc(data.state)} · requested ${esc(data.requested_qty)} · executable ${esc(data.executable_qty)}</p><p>${esc((data.block_reasons || []).join(' · ') || 'All evaluated gates passed')} · fees ${esc(data.fees?.status || 'UNKNOWN')}</p><div class="detail-grid"><div><strong>Depth walk</strong>${(data.depth_walk || []).map(item => `<p>${esc(item.market_id)} · ${esc(item.executable_qty)} available · VWAP ${esc(item.vwap || '—')} · worst ${esc(item.worst_level || '—')} · slippage ${esc(item.slippage || '—')}</p>`).join('') || '<p>no depth</p>'}<strong>Partial fill</strong>${(data.partial_fill_scenarios || []).map(item => `<p>${esc(item.filled_qty)} filled · ${esc(item.residual_qty)} residual · ${esc(item.action)}</p>`).join('') || '<p>—</p>'}</div><div><strong>Evidence / freshness</strong><p>Mapping: ${esc(data.mapping_evidence?.status || 'UNKNOWN')}</p><p>Settlement: ${esc(data.settlement_evidence?.status || 'UNKNOWN')} · ${esc(data.settlement_evidence?.evidence || 'unavailable')}</p><p>Break-even: ${esc(data.break_even?.status || 'UNKNOWN')}</p>${(data.freshness || []).map(item => `<p>${esc(item.market_id)} · ${esc(item.source || 'UNKNOWN')} · ${esc(item.age_s ?? '—')}s</p>`).join('') || '<p>freshness unavailable</p>'}<strong>Books / history</strong>${(data.books || []).map(book => `<p>${esc(book.venue)} ${esc(book.market_id)} · ${book.missing ? 'no depth' : `${book.outcomes?.length || 0} outcome(s)`}</p>`).join('') || '<p>no depth</p>'}${(data.history || []).map(item => `<p>${item.missing ? 'history unavailable' : `${item.rows.length} capture point(s), gaps explicit`}</p>`).join('')}</div></div>`;
  drawer.querySelector('[data-close]')?.addEventListener('click', () => { drawer.hidden = true; });
};

export const startOpportunityPolling = (render, intervalMs = 5000) => {
  let stopped = false;
  const poll = async () => {
    try { if (!stopped) render(await getOpportunities()); } catch (error) { if (!stopped) render({error}); }
    if (!stopped) window.setTimeout(poll, intervalMs);
  };
  poll();
  return () => { stopped = true; };
};

const rows = document.getElementById('canonicalRows');
const drawer = document.getElementById('opportunityDrawer');
if (rows && drawer) startOpportunityPolling(data => {
  if (data.error) { rows.innerHTML = `<tr><td colspan="10" class="empty">${esc(data.error.message || data.error)}</td></tr>`; return; }
  rows.innerHTML = (data.opportunities || []).map(item => `<tr><td>${esc(item.strategy)}</td><td><strong>${esc(item.event || item.title)}</strong></td><td>${esc(item.direction || '—')}</td><td>${item.gross_edge == null ? '—' : esc(item.gross_edge)}</td><td>${item.capital == null ? '—' : esc(item.capital)}</td><td>${item.exec_qty == null ? '—' : esc(item.exec_qty)}</td><td>${esc(item.mapping || 'UNKNOWN')}</td><td>${esc(item.settlement || 'UNKNOWN')}</td><td><span class="badge ${item.state === 'blocked' ? 'bad' : item.state === 'exec_ready' ? 'good' : 'warn'}">${esc(item.state)}</span></td><td><button type="button" data-inspect="${esc(item.id)}">Inspect</button></td></tr>`).join('') || '<tr><td colspan="10" class="empty">No normalized opportunities.</td></tr>';
  rows.querySelectorAll('[data-inspect]').forEach(button => button.addEventListener('click', async () => {
    renderDrawer(drawer)({loading: true});
    try { renderDrawer(drawer)(await getOpportunity(button.dataset.inspect)); }
    catch (error) { renderDrawer(drawer)({error}); }
  }));
}, 5000);
