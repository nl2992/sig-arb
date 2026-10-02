// Positions & P&L panel. Reads /api/positions (written by bot.py every minute from the
// account snapshot it already takes); never calls SIG itself.
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = (n, d = 0) => n == null ? '—' : Number(n).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
const signed = (n, d = 0) => n == null ? '—' : `<span class="${n >= 0 ? 'pos-pos' : 'pos-neg'}">${n >= 0 ? '+' : ''}${num(n, d)}</span>`;
const NAMES = {arb: 'Arbs (locked)', fv: 'Fair value', mm: 'Market making', ll: 'Lead-lag'};

function tile(label, value, sub) {
  return `<div><span>${esc(label)}</span><b>${value}</b>${sub ? `<small class="tile-sub">${sub}</small>` : ''}</div>`;
}

function render(p) {
  const a = p.account;
  $('posAsOf').textContent = `as of ${new Date(p.as_of).toLocaleTimeString()} (${num(p.age_s)}s ago) · ${p.open_orders} resting order(s)`;
  $('posTiles').innerHTML = [
    tile('Account value', num(a.account_value), signed(a.pnl_vs_start) + ' vs 100,000 (SIG marks)'),
    tile('Settlement value', signed(a.settle_ev), 'expected at Kalshi/Polymarket prices'),
    tile('Deployed', num(a.open_cost), `cash ${num(a.cash)}`),
    tile('Mark P&L (open)', signed(a.mark_value - a.open_cost), 'SIG currentPrice'),
    tile('Realized', signed(a.realized, 2), 'closed trades'),
    tile('Daily P&L (SIG)', signed(a.daily_pnl)),
  ].join('');
  $('posStrategies').innerHTML = Object.entries(p.strategies).map(([k, s]) =>
    `<tr><td>${esc(NAMES[k] || k)}</td><td>${num(s.markets)}</td><td>${num(s.cost)}</td><td>${signed(s.mark - s.cost)}</td><td>${signed(s.ev)}</td></tr>`
  ).join('') || '<tr><td colspan="5" class="empty">No positions.</td></tr>';
  const r = p.risk;
  $('posRisk').innerHTML = `<div><span>If Democrats win every race</span><b>${signed(r.if_democrats_sweep)}</b></div>
    <div><span>If Republicans win every race</span><b>${signed(r.if_republicans_sweep)}</b></div>
    <div><span>Every race at its worst outcome</span><b>${signed(r.sum_of_race_worst_cases)}</b></div>
    <div><span>Races / not hedged</span><b>${num(r.races)} / ${num(r.unhedged_races)}</b></div>`;
  $('posRaceCount').textContent = `· ${p.races.length} races`;
  $('posRaces').innerHTML = p.races.map(x =>
    `<tr><td><strong>${esc(x.race)}</strong></td><td>${num(x.cost)}</td><td>${signed(x.if_dem)}</td><td>${signed(x.if_rep)}</td><td>${signed(x.worst)}</td><td>${x.hedged ? '<span class="badge ok">hedged</span>' : '<span class="badge warn">directional</span>'}</td></tr>`
  ).join('');
  $('posCount').textContent = `· ${p.positions.length}`;
  $('posRows').innerHTML = p.positions.map(x =>
    `<tr><td>${esc(NAMES[x.strategy] || x.strategy)}</td><td>${esc(x.race)}</td><td>${esc(x.party)}</td><td>${esc(x.side)}</td><td>${num(x.qty)}</td><td>${num(x.avg, 3)}</td><td>${num(x.mark, 3)}</td><td>${num(x.fair, 3)}</td><td>${num(x.cost)}</td><td>${signed(x.mark_pnl)}</td><td>${signed(x.ev)}</td></tr>`
  ).join('');
}

async function poll() {
  try {
    const r = await fetch('/api/positions', {signal: AbortSignal.timeout(5000)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Positions unavailable');
    render(d);
  } catch (e) {
    $('posAsOf').textContent = e.message;
  }
}
poll();
window.setInterval(poll, 15000);
