// Paste into DevTools console on https://sig.thesuper.market (logged in).
// Depth-walking (VWAP / max-executable) complement-arb scanner. Read-only.
// NOTE: BUY_ALL rows are only real arbs if the race is exhaustive (see EXHAUSTIVE list in bot.py).
const T='bda92870-621e-47b0-bc3c-3602c5c26f55', MIN_EDGE=0, re=/^Will the (Republican|Democratic|Independent) Party win the (.+)\?$/;
let all=[],off=0; while(off!=null){const j=await (await fetch('/api/markets/page-data?offset='+off)).json(); all.push(...j.markets); off=j.markets.length?j.nextOffset:null;}
const G={}; await Promise.all(all.map(async m=>{const L=(await (await fetch(`/api/markets/${m.id}/orders?marketId=${m.id}&tournamentId=${T}`)).json()).levels.map(l=>l.isYes?l:{...l,price:1-l.price,side:l.side==='BUY'?'SELL':'BUY'});
  const [,p,race]=m.title.match(re)||[]; (G[race]??={})[p[0]]={id:m.id,bids:L.filter(l=>l.side==='BUY').sort((a,b)=>b.price-a.price).map(l=>[l.price,l.quantity]),asks:L.filter(l=>l.side==='SELL').sort((a,b)=>a.price-b.price).map(l=>[l.price,l.quantity])};}));
function walk(legs,dir){const lad=legs.map(l=>dir==='SELL'?l.bids:l.asks); if(lad.some(x=>!x.length))return null; const n=lad.length,idx=Array(n).fill(0),rem=lad.map(x=>x[0][1]); let Q=0,pnl=0,cap=0; const f=legs.map(()=>[]);
  while(true){const ps=idx.map((k,i)=>lad[i][k][0]); const s=ps.reduce((a,b)=>a+b,0); const e=dir==='SELL'?s-1:1-s; if(e<=0||e<MIN_EDGE)break; const q=Math.min(...rem); ps.forEach((p,i)=>{f[i].push([p,q]);rem[i]-=q}); Q+=q; pnl+=e*q; cap+=(dir==='SELL'?n-s:s)*q; let stop=false; rem.forEach((r,i)=>{if(r<=0){idx[i]++; if(idx[i]>=lad[i].length)stop=true; else rem[i]=lad[i][idx[i]][1];}}); if(stop)break;}
  return Q?{Q,pnl:+pnl.toFixed(2),cap:+cap.toFixed(2),vwap:f.map(x=>+(x.reduce((a,[p,q])=>a+p*q,0)/Q).toFixed(4)).join('/'),limit:f.map(x=>x.at(-1)[0]).join('/')}:null;}
const out=[]; for(const [race,o] of Object.entries(G)){const legs=Object.values(o); for(const d of ['SELL','BUY']){const r=walk(legs,d); if(r) out.push({race,dir:d+'_ALL',ids:legs.map(l=>l.id).join('/'),...r});}}
out.sort((a,b)=>b.pnl-a.pnl); console.table(out);
