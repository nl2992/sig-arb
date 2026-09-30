// Polls /api/status and renders the ops strip and the 13-gate panel.
// The server decides every gate; any value other than pass === true is shown as not passing.
(()=>{
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function badge(el,text,level){el.textContent=text;el.className='badge '+level;}
function gateBadge(g){return g.pass===true?['PASS','ok']:g.pass===null?['PER OPPORTUNITY','warn']:['FAIL','bad'];}
function render(s){
 badge($('opsHealth'),s.health.toUpperCase()+(s.data_mode==='replay'?' · REPLAY':''),s.health==='ok'?'ok':s.health==='degraded'?'warn':'bad');
 $('opsSnapshot').textContent=s.last_good_snapshot?new Date(s.last_good_snapshot).toLocaleTimeString()+` · ${s.feeds[0].age_s}s ago`:'none yet';
 badge($('opsDb'),s.db.ok?`OK · ${s.db.path}`:`${s.db.error||'unavailable'} · ${s.db.path}`,s.db.ok?'ok':'bad');
 const session=s.sig_auth.session;
 badge($('opsAuth'),session?`SESSION ${session}`:'SESSION NOT CHECKED',['NORMALIZED','UNVERIFIED_SCHEMA'].includes(session)?'ok':'bad');
 badge($('opsPayload'),s.sig_auth.payload_verified?'PAYLOAD VERIFIED':'PAYLOAD UNVERIFIED',s.sig_auth.payload_verified?'ok':'bad');
 badge($('opsKill'),s.kill_switch.engaged?'ENGAGED · live blocked':'OFF',s.kill_switch.engaged?'bad':'ok');
 badge($('opsMode'),s.mode.replace('_',' ').toUpperCase(),'warn');
 const g=s.gate_summary;
 badge($('opsGateCount'),`${g.pass} / ${g.total} PASS`,g.pass===g.total?'ok':'bad');
 $('opsGateSummary').textContent=`· ${g.pass} pass · ${g.fail} fail · ${g.per_candidate} per opportunity`;
 $('opsReasons').hidden=!s.health_reasons.length;
 $('opsReasons').textContent=s.health_reasons.join(' · ');
 $('opsGates').innerHTML=s.gates.map(x=>{const[t,l]=gateBadge(x);return `<div class="gate-row"><span>${esc(x.name)}</span><b class="badge ${l}">${t}</b><small>${esc(x.detail)}</small></div>`;}).join('');
 $('opsFeeds').innerHTML=s.feeds.map(f=>`<div class="gate-row"><span>Feed · ${esc(f.name)}</span><b class="badge ${f.stale?'bad':'ok'}">${f.stale?'STALE':'FRESH'}${f.age_s===null?'':' · '+esc(f.age_s)+'s'}</b><small>${esc(f.detail)}</small></div>`).join('');
}
async function poll(){
 try{const r=await fetch('/api/status',{signal:AbortSignal.timeout(5000)});const s=await r.json();if(!r.ok)throw Error(s.error||'Status unavailable');render(s);}
 catch(e){badge($('opsHealth'),'STATUS UNAVAILABLE','bad');$('opsReasons').hidden=false;$('opsReasons').textContent=e.message+' Values shown may be stale.';}
}
poll();setInterval(poll,2000);
})();
