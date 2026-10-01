// Automated-execution panel: bot heartbeat, execution tape and the kill switch.
// The server and bot.py decide everything; this file only renders and posts kill-switch actions.
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const badge = (el, text, level) => { el.textContent = text; el.className = 'badge ' + level; };
const fmt = (n, d = 2) => n == null ? '—' : Number(n).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
const token = document.querySelector('meta[name="action-token"]')?.content || '';
const statusLevel = s => s === 'DONE' ? 'ok' : ['MISS', 'ABORT', 'KILLED'].includes(s) ? 'warn' : 'bad';
let releasePhrase = 'RELEASE KILL SWITCH';

const post = async (path, body) => {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Action-Token': token},
                                      body: JSON.stringify(body), signal: AbortSignal.timeout(10000)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
  return data;
};

function render(v) {
  releasePhrase = v.release_phrase || releasePhrase;
  const bot = v.bot;
  if (!bot) badge($('botState'), 'NOT RUNNING · no heartbeat', 'bad');
  else if (bot.error && !bot.ts) badge($('botState'), bot.error, 'bad');
  else badge($('botState'), `${bot.running ? 'RUNNING' : 'STOPPED'} · ${esc(bot.mode)} · ${bot.age_s}s ago`, bot.running ? (bot.error ? 'warn' : 'ok') : 'bad');
  if (bot && bot.ts) {
    badge($('botLive'), bot.live ? 'LIVE ORDERS' : 'DRY RUN', bot.live ? 'ok' : 'warn');
    $('botGross').textContent = `${fmt(bot.gross)} / ${fmt(bot.max_gross, 0)}`;
    $('botCaps').textContent = `${fmt(bot.max_per_race, 0)} · ${fmt(bot.min_edge, 3)}`;
    const c = bot.counts || {};
    $('botCounts').textContent = `${c.signals ?? 0} / ${c.executed ?? 0} / ${c.skipped ?? 0}`;
    const reasons = [...(bot.live_requested && !bot.live ? bot.live_blockers || [] : []), ...(bot.error ? ['last tick failed: ' + bot.error] : [])];
    $('botBlockers').hidden = !reasons.length;
    $('botBlockers').textContent = reasons.length ? 'Live blocked: ' + reasons.join(' · ') : '';
  } else {
    badge($('botLive'), '—', 'warn');
    $('botBlockers').hidden = true;
  }
  badge($('execKill'), v.kill_switch.engaged ? 'ENGAGED' : 'OFF', v.kill_switch.engaged ? 'bad' : 'ok');
  $('killRelease').disabled = !v.kill_switch.engaged;
  $('execCount').textContent = `· ${v.executions.length} most recent`;
  $('execRows').innerHTML = v.executions.length ? v.executions.map(e => {
    const res = e.result || {};
    const legs = (res.legs || []).map(l => l.error ? `#${esc(l.market)} ${esc(l.error)}` : `#${esc(l.market)} ${esc(l.side)} ${fmt(l.filled, 0)}/${fmt(l.req, 0)} @ ${fmt(l.limit, 3)}${l.dryRun ? ' (dry)' : ''}`).join('<br>');
    return `<tr><td>${esc(e.ts)}</td><td><strong>${esc(e.race)}</strong>${res.action ? `<small>${esc(res.action)}</small>` : ''}</td><td>${esc(e.dir)}</td><td>${esc(e.mode)} · ${e.live ? 'live' : 'dry'}</td><td><span class="badge ${statusLevel(res.status)}">${esc(res.status || '?')}</span></td><td>${fmt(res.qty ?? e.qty, 0)}</td><td>${fmt(e.pnl)}</td><td><small>${legs}</small></td></tr>`;
  }).join('') : '<tr><td colspan="8" class="empty">No executions journaled yet.</td></tr>';
}

async function poll() {
  try {
    const response = await fetch('/api/execution', {signal: AbortSignal.timeout(5000)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Execution state unavailable');
    render(data);
  } catch (error) {
    badge($('botState'), 'STATE UNAVAILABLE', 'bad');
    $('botBlockers').hidden = false;
    $('botBlockers').textContent = error.message + ' Values shown may be stale.';
  }
}

$('killEngage').addEventListener('click', async () => {
  const reason = window.prompt('Reason for engaging the kill switch (optional):', 'operator stop') ?? 'operator stop';
  try { await post('/api/kill-switch/engage', {reason}); } catch (error) { window.alert(error.message); }
  poll();
});
$('killRelease').addEventListener('click', async () => {
  const typed = window.prompt(`Releasing lets bot.py send orders again. Type "${releasePhrase}" to confirm:`);
  if (typed == null) return;
  try { await post('/api/kill-switch/release', {confirmation: typed}); } catch (error) { window.alert(error.message); }
  poll();
});
poll();
window.setInterval(poll, 2000);
