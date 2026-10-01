(async () => {
  const keys = ['orders/place', 'orders/quote', 'orders/cancel', 'priceLimit', 'isLimitOrder',
                'idempotencyKey', 'OrderType', 'LIMIT', 'nextExpiryAt', 'expiresAt', 'tickSize',
                'minQuantity', 'maxQuantity', 'blocksSubmit', 'Trading Locked', 'filledQuantity'];
  const urls = [...new Set(performance.getEntriesByType('resource').map(e => e.name)
    .filter(u => u.endsWith('.js') && u.includes(location.host)))];
  const out = [];
  for (const u of urls) {
    const t = await (await fetch(u)).text();
    for (const k of keys) {
      let i = t.indexOf(k), n = 0;
      while (i >= 0 && n < 3) {
        out.push('### ' + k + ' @ ' + u.split('/').pop() + ':' + i + '\n' + t.slice(Math.max(0, i - 400), i + 600));
        i = t.indexOf(k, i + k.length); n++;
      }
    }
  }
  const text = out.join('\n\n');
  const el = document.createElement('a');
  el.href = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
  el.download = 'sig_order_logic.txt';
  el.click();
  console.log(out.length + ' snippets, ' + text.length + ' chars -> sig_order_logic.txt');
})();
