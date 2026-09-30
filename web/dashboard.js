const $=id=>document.getElementById(id);
const fmt=(n,d=2)=>Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let data=null, selected=null, busy=false, failed=false;
let opportunityData=null;
let params=new URLSearchParams({fee:'0',edge:'0',profit:'1',budget:'0',capital:'0',cap_pct:'5',roi:'5'});
function filtered(){return (data?.signals||[]).filter(r=>r.race.toLowerCase().includes($('search').value.toLowerCase())).sort((a,b)=>b[$('sort').value]-a[$('sort').value]);}
function render(){
 if(!data)return;
 const chosen=$('market').value;
 $('market').innerHTML=data.markets_list.map(m=>`<option value="${m.id}">${esc(m.title)}</option>`).join('');
 if(chosen)$('market').value=chosen;
 $('count').textContent=data.signals.length;
 $('best').textContent=fmt(Math.max(0,...data.signals.map(r=>r.profit)));
 $('edgeStat').textContent=fmt(Math.max(0,...data.signals.map(r=>r.edge))*100,2)+'¢';
 $('coverage').textContent=data.markets;
 $('races').textContent=data.races+' races';
 const cv=data.crossvenue||{};
 $('kalshiCoverage').textContent=fmt(cv.market_counts?.kalshi||0,0);
 $('polyCoverage').textContent=fmt(cv.market_counts?.polymarket||0,0);
 $('mappingCoverage').textContent=`${cv.mapping_counts?.discovered||0} / ${cv.mapping_counts?.approved||0}`;
 $('movementCount').textContent=fmt(cv.movement?.candidates?.length||0,0);
 $('relativeCount').textContent=fmt((cv.relative_value||[]).filter(r=>r.status==='RESEARCH_CANDIDATE').length,0);
 $('crossvenueStatus').textContent=`${cv.observation_counts?.kalshi||0} Kalshi observations · ${cv.observation_counts?.polymarket||0} Polymarket observations · ${cv.history_points||0} stored history points · research only`;
 const cross=[...(cv.opportunities||[]).map(r=>({...r,kind:'movement'})),...(cv.relative_value||[]).filter(r=>r.status==='RESEARCH_CANDIDATE').map(r=>({...r,kind:'relative value'}))];
 $('crossvenueRows').innerHTML=cross.length?cross.slice(0,12).map(r=>`<div class="near-item"><span><strong>${esc(r.kind)}</strong> · SIG ${esc(r.sig_market_id)}<small>${r.mapping_status?`Mapping ${esc(r.mapping_status)} · News ${esc(r.news_status||'UNKNOWN')}`:'Review-gated relative value'}</small></span><span>${r.kind==='movement'?`${esc(r.direction)} ${fmt(r.movement_pp)}pp / gap ${fmt(r.gap_pp)}pp<small>${r.freshness_seconds===null?'Age unknown':fmt(r.freshness_seconds,0)+'s'} · ${esc((r.execution_risk||[]).join(', '))}</small>`:`z ${fmt(r.z_score)}`}</span></div>`).join(''):'<p>No review-gated cross-venue candidates in this snapshot.</p>';
 $('opportunityStates').innerHTML=opportunityData?.opportunities?.length?opportunityData.opportunities.slice(0,12).map(r=>`<div class="near-item"><span><strong>${esc(r.kind)}</strong> · ${esc(r.title)}<small>${esc((r.block_reasons||[]).join(' · ')||'All evaluated gates passed')}</small></span><span class="badge ${r.state==='exec_ready'?'good':r.state==='blocked'?'bad':'warn'}">${esc(r.state)}</span></div>`).join(''):'<p>No normalized opportunities in this snapshot.</p>';
 $('time').textContent='Snapshot '+new Date(data.ts).toLocaleTimeString();
 $('scope').textContent=`${data.exhaustive} exhaustive races · ${data.exhaustive?'YES + NO positions':'NO positions only'} · ${data.budget?'Cap '+fmt(data.budget,0)+'/punt':'Capital cap unset'}`;
 const rows=filtered();
 $('rows').innerHTML=rows.length?rows.map((r,i)=>`<tr class="${selected===r.race+r.direction?'selected':''}"><td><strong>${esc(r.race)}</strong><small>${r.direction==='SELL_ALL'?'Buy NO across all legs':'Buy YES across all legs'}</small></td><td class="positive">+${fmt(r.profit)}</td><td>${fmt(r.vwap,4)}</td><td class="positive">${fmt(r.edge*100)}¢</td><td>${fmt(r.roi*100)}%</td><td>${fmt(r.qty,0)}</td><td>${fmt(r.capital)}</td><td><button data-row="${i}" aria-label="Inspect ${esc(r.race)}">Details</button></td></tr>`).join(''):'<tr><td colspan="8" class="empty">No executable opportunities match these filters.</td></tr>';
 $('punts').innerHTML=data.punts.length?data.punts.map(r=>`<tr><td><strong>${esc(r.race)}</strong><small>${r.direction==='SELL_ALL'?'Buy NO across all legs':'Buy YES across all legs'}</small></td><td>+${fmt(r.profit)}</td><td>${fmt(r.roi*100)}%</td><td>${fmt(r.capital)}</td><td>${fmt(r.required_roi*100)}%</td><td><button data-punt="${esc(r.race)}">Inspect news / Kelly</button></td></tr>`).join(''):'<tr><td colspan="6" class="empty">No conditional punts in this snapshot.</td></tr>';
 document.querySelectorAll('[data-row]').forEach(b=>b.onclick=()=>{selected=rows[+b.dataset.row].race+rows[+b.dataset.row].direction;render();});
 $('near').innerHTML=data.near.map(r=>`<div class="near-item"><span>${esc(r.race)}</span><span>${fmt(r.edge*100)}¢</span></div>`).join('')||'<p>No near misses available.</p>';
 const r=rows.find(r=>r.race+r.direction===selected);
 $('detail').hidden=!r;
 if(r){$('detail').innerHTML=`<div class="section-title"><h2>${esc(r.race)} / execution depth</h2><span>Marginal edge ${fmt(r.marginal_edge*100)}¢</span></div><div class="detail-meta"><span>Freshness ${fmt(r.freshness_seconds,0)}s</span><span>Mapping ${esc(r.mapping_status)}</span><span>Settlement ${esc(r.settlement_status)}</span><span>Fee ${fmt(r.fee_assumption,4)} / share / leg</span><span>News ${esc(r.news_status)}</span><span>Execution ${r.execution_ready?'READY':'BLOCKED'}</span></div><p class="model-note">Risk gates: ${esc((r.execution_risk||[]).join(', '))}</p><div class="detail-grid"><div class="table-wrap"><table><thead><tr><th>Market</th><th>Position</th><th>VWAP</th><th>Limit</th><th>Qty</th></tr></thead><tbody>${r.legs.map(l=>`<tr><td><a href="https://sig.thesuper.market/markets/${l.id}" target="_blank" rel="noopener noreferrer">${esc(l.title.match(/the (\w+) Party/)?.[1]||l.id)} ↗</a></td><td>${l.side}</td><td>${fmt(l.vwap,4)}</td><td>${fmt(l.limit,4)}</td><td>${fmt(l.qty,0)}</td></tr>`).join('')}</tbody></table></div><div class="chart-box"><div class="chart-label"><span>Cumulative estimated profit</span><span>+${fmt(r.profit)} SUSQies</span></div><canvas id="chart" role="img" aria-label="Cumulative estimated profit rises to ${fmt(r.profit)} over ${r.qty} bundles"></canvas><div class="chart-label"><span>0 bundles</span><span>${fmt(r.qty,0)} bundles</span></div></div></div>`;draw(r);}
 freshness();
}
function draw(r){const c=$('chart');if(!c)return;const rect=c.getBoundingClientRect(),dpr=window.devicePixelRatio||1;c.width=rect.width*dpr;c.height=175*dpr;const x=c.getContext('2d');x.scale(dpr,dpr);const w=rect.width,h=175;x.strokeStyle='#e0e7e2';for(let i=0;i<4;i++){x.beginPath();x.moveTo(0,i*50+15);x.lineTo(w,i*50+15);x.stroke();}const pts=[{cum_qty:0,cum_pnl:0},...r.steps];x.beginPath();pts.forEach((p,i)=>{const a=8+p.cum_qty/r.qty*(w-16),b=h-10-p.cum_pnl/r.profit*(h-25);i?x.lineTo(a,b):x.moveTo(a,b)});x.strokeStyle='#159563';x.lineWidth=2.5;x.stroke();x.lineTo(w-8,h-10);x.lineTo(8,h-10);x.closePath();x.fillStyle='rgba(21,149,99,.08)';x.fill();}
function freshness(){if(!data)return;const age=(Date.now()-Date.parse(data.ts))/1000;$('status').textContent=failed?'Scan failed · stale':data.mode==='replay'?'Replay data':age>60?'Stale snapshot':'Live · read only';$('status').style.color=failed||age>60?'#a2612e':'#19744b';}
async function refresh(){if(busy)return;busy=true;$('refresh').disabled=true;$('status').textContent='Scanning…';try{const [signalResponse, opportunityResponse]=await Promise.all([fetch('/api/signals?'+params,{signal:AbortSignal.timeout(120000)}),fetch('/api/opportunities',{signal:AbortSignal.timeout(120000)})]);const result=await signalResponse.json();const normalized=await opportunityResponse.json();if(!signalResponse.ok)throw Error(result.error||'Scan failed');data=result;opportunityData=opportunityResponse.ok?normalized:null;failed=false;$('error').hidden=true;if(selected===null&&data.signals.length)selected=data.signals[0].race+data.signals[0].direction;render();}catch(e){failed=true;$('error').textContent=e.message+' Last successful results, if present, are retained.';$('error').hidden=false;$('status').textContent='Scan failed';freshness();}finally{busy=false;$('refresh').disabled=false;}}
$('filters').onsubmit=e=>{e.preventDefault();if(busy)return;params=new URLSearchParams(Object.fromEntries(['fee','budget','capital','cap_pct','edge','profit','roi'].map(k=>[k,$(k).value||'0'])));refresh();};
$('refresh').onclick=refresh;$('search').oninput=render;$('sort').onchange=render;
async function refreshPortfolio(){
 try{
  const response=await fetch('/api/portfolio',{signal:AbortSignal.timeout(120000)});
  const result=await response.json();
  $('portfolioStatus').textContent=result.status||'UNKNOWN';
  $('portfolioStatus').style.color=result.kill_switch_required?'#a2612e':'#19744b';
 }catch(e){$('portfolioStatus').textContent='UNAVAILABLE';$('portfolioStatus').style.color='#a2612e';}
}
window.addEventListener('resize',()=>{const r=data?.signals.find(r=>r.race+r.direction===selected);if(r)draw(r)});
setInterval(()=>{if($('auto').checked&&!document.hidden)refresh();},30000);setInterval(()=>{if(!busy)freshness();},5000);refresh();refreshPortfolio();
$('kellyForm').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;const q=new URLSearchParams({market:$('market').value,no:$('position').value,probability:Number($('probability').value)/100,bankroll:$('bankroll').value,fraction:$('fraction').value,fee:params.get('fee')});$('kellyResult').textContent='Calculating…';try{const response=await fetch('/api/kelly?'+q,{signal:AbortSignal.timeout(120000)});const r=await response.json();if(!response.ok)throw Error(r.error);$('kellyResult').textContent=`${fmt(r.qty,0)} shares · ${fmt(r.capital)} capital (${fmt(r.bankroll_fraction*100)}% of bankroll) · ${r.vwap===null?'No positive edge':fmt(r.vwap,4)+' VWAP'} · ${fmt(r.expected_profit)} expected profit · ${fmt(r.growth*100,4)}% expected log growth · snapshot ${new Date(r.ts).toLocaleTimeString()}`;}catch(e){$('kellyResult').textContent=e.message;}finally{button.disabled=false;}};
['market','position','probability','bankroll','fraction'].forEach(id=>$(id).addEventListener('change',()=>{$('kellyResult').textContent='Inputs changed. Awaiting calculation.'}));
let newsRequest=0;
$('market').addEventListener('change',()=>{newsRequest++;$('news').textContent='Market changed. Load related news.';});
$('loadNews').onclick=async()=>{
 const request=++newsRequest, market=$('market').value;
 if(!market)return;
 $('news').textContent='Loading source context…';
 try{
  const response=await fetch('/api/news?market='+encodeURIComponent(market));
  const n=await response.json();if(!response.ok)throw Error(n.error);
  if(request!==newsRequest)return;
  const safe=url=>{try{const u=new URL(url);return ['http:','https:'].includes(u.protocol)?u.href:null}catch{return null}};
  $('news').innerHTML=`<p><strong>${esc(n.tradingStatus)}</strong>${n.tradingStart?' · Opens '+esc(new Date(n.tradingStart).toLocaleString()):''}</p><p>${esc(n.contextSummary||'No context summary available.')}</p><p class="model-note">Platform summaries and probability claims are unverified. Feed refresh: ${esc(n.lastRefresh||'Unknown')}. First seen is our collection time, not publication time.</p>`+n.headlines.map(a=>`<article class="news-item"><h3>${safe(a.url)?`<a href="${esc(safe(a.url))}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>`:esc(a.title)}</h3><small>${esc(a.source)} · Published ${esc(a.publishedDate||'Unknown')} · First seen ${esc(new Date(a.firstSeen).toLocaleString())}</small><p>${esc(a.summary||'')}</p></article>`).join('')+(n.headlines.length?'':'<p>No related news available.</p>');
 }catch(e){if(request===newsRequest)$('news').textContent='News unavailable: '+e.message;}
};
