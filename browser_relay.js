/* Run in the SIG market page console while signed in.
 * It keeps the local dashboard fed from the browser session's own API access.
 */
(async () => {
  const T = 'bda92870-621e-47b0-bc3c-3602c5c26f55';
  const base = location.origin;
  // Accept the legacy {levels:[...]} shape and the live {books:[{exchangeId,bids,asks}]} shape.
  const toLevels = j => Array.isArray(j.levels) ? j.levels : Array.isArray(j.books)
    ? j.books.flatMap(b => [
        ...(b.bids || []).map(l => ({exchangeId: b.exchangeId, side: 'BUY', isYes: true, price: l.price, quantity: l.quantity})),
        ...(b.asks || []).map(l => ({exchangeId: b.exchangeId, side: 'SELL', isYes: true, price: l.price, quantity: l.quantity}))])
    : null;
  async function snapshot() {
    let markets = [], offset = 0;
    while (offset != null) {
      const j = await (await fetch(`${base}/api/markets/page-data?offset=${offset}`)).json();
      if (!Array.isArray(j.markets)) throw new Error('Market page response has no markets array');
      markets.push(...j.markets);
      offset = j.markets.length ? j.nextOffset : null;
    }
    const levels = {};
    for (let i = 0; i < markets.length; i += 10) {
      await Promise.all(markets.slice(i, i + 10).map(async m => {
        const j = await (await fetch(`${base}/api/markets/${m.id}/orders?marketId=${m.id}&tournamentId=${T}`)).json();
        const L = toLevels(j);
        if (!L) throw new Error(`Order response has no levels or books for ${m.id}`);
        levels[m.id] = L;
      }));
    }
    const payload = {ts: new Date().toISOString(), markets: markets.map(m => ({id:m.id, title:m.title})), levels};
    const sent = await fetch('http://127.0.0.1:8876/api/browser_snapshot', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    });
    const result = await sent.json();
    if (!sent.ok) throw new Error(result.error || `Dashboard returned ${sent.status}`);
    console.log('SIG dashboard relay', result);
  }
  await snapshot();
  window.sigDashboardRelay = setInterval(snapshot, 15000);
  console.log('SIG dashboard relay running; stop with clearInterval(window.sigDashboardRelay)');
})();
