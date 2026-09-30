/* Run in the SIG market page console while signed in.
 * It keeps the local dashboard fed from the browser session's own API access.
 */
(async () => {
  const T = 'bda92870-621e-47b0-bc3c-3602c5c26f55';
  const base = location.origin;
  async function snapshot() {
    let markets = [], offset = 0;
    while (offset != null) {
      const j = await (await fetch(`${base}/api/markets/page-data?offset=${offset}`)).json();
      markets.push(...j.markets);
      offset = j.markets.length ? j.nextOffset : null;
    }
    const levels = {};
    for (let i = 0; i < markets.length; i += 10) {
      await Promise.all(markets.slice(i, i + 10).map(async m => {
        const j = await (await fetch(`${base}/api/markets/${m.id}/orders?marketId=${m.id}&tournamentId=${T}`)).json();
        levels[m.id] = j.levels;
      }));
    }
    const payload = {ts: new Date().toISOString(), markets: markets.map(m => ({id:m.id, title:m.title})), levels};
    const sent = await fetch('http://127.0.0.1:8876/api/browser_snapshot', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    });
    console.log('SIG dashboard relay', await sent.json());
  }
  await snapshot();
  window.sigDashboardRelay = setInterval(snapshot, 15000);
  console.log('SIG dashboard relay running; stop with clearInterval(window.sigDashboardRelay)');
})();
