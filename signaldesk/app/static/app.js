/* SignalDesk UI: semantics and behavior hooks deliberately separate from styling. */
'use strict';
const state={me:null,team:null,view:'board',tickets:[],ticket:null,ruleId:null,runbookId:null,stream:null,refreshTimer:null,loading:false};
const $=id=>document.getElementById(id);
const e=v=>String(v??'').replace(/[&<>"']/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
const fmtTime=v=>v?new Date(v).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'—';
const age=v=>{if(!v)return '—';const n=Math.max(0,Date.now()-new Date(v).getTime());if(n<60e3)return 'just now';if(n<3600e3)return `${Math.floor(n/60e3)}m ago`;if(n<864e5)return `${Math.floor(n/3600e3)}h ago`;return `${Math.floor(n/864e5)}d ago`};
const initials=s=>String(s||'—').split(/\s+/).map(x=>x[0]).slice(0,2).join('').toUpperCase();
const pretty=s=>String(s||'').replaceAll('_',' ').replace(/\b\w/g,ch=>ch.toUpperCase());
const safeJson=v=>JSON.stringify(v??{},null,2);
const icon={incident:'!',task:'☷',change_request:'◇',ticket:'▤',comment:'✎',approval:'✓',change:'⌘',runbook:'☑',recovery:'↺'};
let toastTimer;
function toast(message,error=false){const el=$('toast');el.textContent=message;el.classList.toggle('error',error);el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,4500)}
async function api(path,method='GET',payload){
  const options={method,credentials:'same-origin',headers:{}};
  if(payload!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(payload)}
  const res=await fetch('/api'+path,options);
  if(res.status===204)return null;
  let body=await res.json().catch(()=>({}));
  if(!res.ok)throw Error(typeof body.detail==='string'?body.detail:(body.detail?JSON.stringify(body.detail):`${res.status} ${res.statusText}`));
  return body;
}
function selectHtml(options,selected){return options.map(([v,label])=>`<option value="${e(v)}" ${String(v)===String(selected)?'selected':''}>${e(label)}</option>`).join('')}
function myTeams(){return state.me?.teams||[]}
function allTeams(){return [...myTeams(),...(state.me?.shared_teams||[])]}
function teamName(id){return allTeams().find(t=>t.id===id)?.name||'Team #'+id}
function getTeam(){return allTeams().find(t=>t.id===state.team)}
function isManager(){return state.me?.role==='M'&&getTeam()?.leader_id===state.me.id}
function findName(id){for(const t of allTeams()){const x=t.members?.find(m=>m.id===id);if(x)return x.name}return state.me?.id===id?state.me.name:('Employee #'+id)}
function severityBadge(t){const s=t.type==='incident'?t.severity:t.type;return `<span class="severity-badge" data-severity="${e(s)}">${e(s==='change_request'?'CHANGE':s==='task'?'TASK':s.toUpperCase())}</span>`}
function flagsBadges(t){let flags=[];if((t.flags||[]).includes('attention'))flags.push('<span class="flag-badge attention">Attention needed</span>');if((t.flags||[]).includes('relapse'))flags.push('<span class="flag-badge">Relapse</span>');if((t.flags||[]).includes('action_approval'))flags.push('<span class="flag-badge">Action approval</span>');if(t.state==='done')flags.push('<span class="flag-badge">Awaiting approval</span>');if((t.flags||[]).includes('recurrence'))flags.push('<span class="flag-badge">Recurring</span>');return flags.join('')}
const fixture={
  payments:{alertname:'HighErrorRate',labels:{service:'payments-api',region:'eu-west',severity:'critical'},annotations:{summary:'Error rate 12% over the last 5 minutes'},startsAt:new Date().toISOString(),status:'firing'},
  workflow:{type:'WORKFLOW_FAILED',workflowId:'WF-DEMO-00042',workflowName:'order-settlement',step:'validate-counterparty',severity:'ERROR',environment:'prod',timestamp:new Date().toISOString(),error:{code:'TIMEOUT',message:'Counterparty service did not respond in 5000 ms'}},
  trade:{meta:{schema:'trade.rejected.v2',producer:'trade-gateway',ts:Date.now()},payload:{tradeId:'T-DEMO-8821',desk:'rates',notional:25000000,currency:'EUR',reason:'LIMIT_BREACH'}},
  recovery:{alertname:'HighErrorRate',labels:{service:'payments-api',region:'eu-west',severity:'critical'},annotations:{summary:'Error rate returned to normal'},startsAt:new Date().toISOString(),status:'resolved'},
  unknown:{envelope:{kind:'CUSTOM_UPSTREAM_FAILURE',facility:{component:'risk-engine',instance:'risk-eu-4'},signal:{level:'CRITICAL',details:{identifier:42,text:'Unknown schema'}}},timestamp:new Date().toISOString()}
};
function showPersonaLogin(){
  $('app-shell').hidden=true;$('login-screen').hidden=false;
  api('/personas').then(personas=>{
    $('personas-list').innerHTML=personas.map(u=>`<button type="button" class="persona-card" data-action="login" data-id="${u.id}"><span class="user-avatar">${e(initials(u.name))}</span><span><strong>${e(u.name)}</strong><small>${e(u.debug_enabled||u.debug?'Demo / debug':({E:'Employee',M:'Team manager',A:'Platform admin'}[u.role]||u.role))}</small></span></button>`).join('');
  }).catch(err=>toast(err.message,true));
}
async function initialize(){
  try{state.me=await api('/me');$('login-screen').hidden=true;$('app-shell').hidden=false;setupIdentity();await chooseTeam(null,false);const role=state.me.role;
    const start=role==='A'?'admin':state.me.debug_enabled?'debug':'board';navigate(start);}
  catch(err){showPersonaLogin()}
}
function setupIdentity(){
  const u=state.me;
  $('current-user').innerHTML=`<span class="user-avatar">${e(initials(u.name))}</span><span class="user-meta">${e(u.name)}<small>${e(u.debug_enabled?'Demo / debug':({E:'Employee',M:'Team manager',A:'Platform admin'}[u.role]||u.role))} · switch</small></span>`;
  $('app-shell').dataset.preset=u.preset||'engineer';
  document.querySelectorAll('[data-nav-role="M"]').forEach(el=>el.hidden=u.role!=='M');
  document.querySelectorAll('[data-nav-role="A"]').forEach(el=>el.hidden=u.role!=='A');
  document.querySelectorAll('[data-nav-role="D"]').forEach(el=>el.hidden=!u.debug_enabled);
  document.querySelectorAll('[data-nav-cap="triage"]').forEach(el=>el.hidden=!(u.role==='A'||u.role==='M'||u.debug_enabled));
  if(u.role==='A'||u.debug_enabled){document.querySelectorAll('.nav-link').forEach(el=>{if(!['settings','organization',u.role==='A'?'admin':'debug','unmatched'].includes(el.dataset.view))el.hidden=true})}
  else document.querySelectorAll('.nav-link').forEach(el=>{if(el.dataset.view==='admin'||el.dataset.view==='debug')el.hidden=true});
}
async function chooseTeam(value,refresh=true){
  const teams=allTeams();if(value===null||value===undefined)value=teams[0]?.id||null;
  state.team=value?Number(value):null;
  let opts=teams.map(t=>[t.id,t.name+((state.me.shared_teams||[]).some(s=>s.id===t.id)?' (read-only)':'')]);
  $('team-selector').innerHTML=selectHtml(opts,state.team);$('filter-team').innerHTML='<option value="">All my teams</option>'+selectHtml(opts,state.team);
  $('filter-team').value=state.team||'';
  document.querySelectorAll('.manager-only').forEach(el=>el.hidden=!isManager());
  if(refresh){connectSSE();await refreshView()}
}
function navigate(view){
  state.view=view;
  for(const section of document.querySelectorAll('.app-view'))section.hidden=section.id!=='view-'+view;
  document.querySelectorAll('.nav-link').forEach(btn=>btn.classList.toggle('is-active',btn.dataset.view===view));
  const label=document.querySelector(`.nav-link[data-view="${view}"] span:last-child`)?.textContent||pretty(view);
  $('breadcrumb-current').textContent=label;
  connectSSE();refreshView();
}
function connectSSE(){
  if(state.stream){state.stream.close();state.stream=null}
  if(!state.me||['admin','debug','settings','organization'].includes(state.view))return;
  let stream=new EventSource('/api/stream'+(state.team?'?team='+state.team:''));state.stream=stream;
  stream.addEventListener('invalidate',()=>scheduleRefresh());
  stream.onopen=()=>{$('live-status').innerHTML='<span class="status-dot"></span> Live'};
  stream.onerror=()=>{$('live-status').textContent='Reconnecting…'};
}
function scheduleRefresh(){clearTimeout(state.refreshTimer);state.refreshTimer=setTimeout(()=>refreshView(true),650)}
async function refreshView(quiet=false){
  if(state.loading)return;state.loading=true;
  try{
    switch(state.view){
      case 'board':await renderBoard();break;case 'activity':await renderActivity();break;case 'tickets':await renderTickets();break;
      case 'archive':await renderArchive();break;case 'unmatched':await renderUnmatched();break;case 'rules':await renderRules();break;
      case 'runbooks':await renderRunbooks();break;case 'proposals':await renderProposals();break;case 'delivery':await renderDelivery();break;
      case 'organization':await renderOrganization();break;case 'access':await renderAccess();break;case 'settings':await renderSettings();break;
      case 'admin':await renderAdmin();break;case 'debug':await renderDebug();break;
    }
    if(state.ticket)await openTicket(state.ticket,true);
  }catch(err){if(!quiet)toast(err.message,true)}
  finally{state.loading=false}
}
function ticketCard(t){
  const display=t.display_id||'TKT-'+t.id;
  const members=(t.members||[]).slice(0,3);
  return `<article id="ticket-card-${t.id}" class="ticket-card" tabindex="0" role="button" data-action="open-ticket" data-id="${t.id}" data-ticket-id="${t.id}" data-state="${e(t.state)}" data-severity="${e(t.severity)}" data-type="${e(t.type)}" data-flag-attention="${(t.flags||[]).includes('attention')}">
   <div class="ticket-card-top"><span class="ticket-card-id">${e(display)}</span>${severityBadge(t)}</div>
   <h4 class="ticket-card-title">${e(t.title)}</h4><p class="ticket-card-description">${e(t.body||'No description provided')}</p>
   <div class="ticket-card-badges">${flagsBadges(t)}</div>
   <div class="ticket-card-meta"><span class="ticket-card-incident-time" title="${e(fmtTime(t.occurred_at))}">◷ ${e(age(t.type==='incident'?t.occurred_at:t.created_at))}</span><span class="ticket-card-count">${t.type==='incident'?'↻ '+Number(t.occurrences).toLocaleString()+' occurrences':'Task'}</span></div>
   <div class="ticket-card-meta"><span class="ticket-card-members">${members.length?members.map(id=>`<span class="initials" title="${e(findName(id))}">${e(initials(findName(id)))}</span>`).join(''):'<span class="muted-inline">Unassigned</span>'}</span><span>${e(t.runbook_steps?.length?`${t.runbook_steps.filter(s=>s.status==='done').length}/${t.runbook_steps.length} steps`:'View details')}</span></div>
   </article>`;
}
async function renderBoard(){
  const data=await api('/board'+new URLSearchParams({...(state.team?{team:state.team}:{}),severity:$('filter-severity').value,search:$('filter-search').value,my_tickets:$('filter-my-tickets').checked?'true':'false',unassigned:$('filter-unassigned').checked?'true':'false'}).toString().replace(/^/,'?'));
  state.tickets=data.tickets||[];
  const filteredState=$('filter-state').value;
  const tickets=state.tickets.filter(t=>!filteredState||t.state===filteredState);
  if(state.me?.preset==='oncall')tickets.sort((a,b)=>['critical','error','warning','info'].indexOf(a.severity)-['critical','error','warning','info'].indexOf(b.severity));
  // Counters count visible tickets, not repeated occurrences; state is a UI filter.
  const counts={new:tickets.filter(t=>t.state==='new'&&t.type==='incident').length,
    critical:tickets.filter(t=>t.type==='incident'&&t.severity==='critical').length,
    total:tickets.length,awaiting_approval:tickets.filter(t=>t.state==='done').length,
    attention:tickets.filter(t=>(t.flags||[]).includes('attention')).length};
  $('counter-new').textContent=counts.new;$('counter-critical').textContent=counts.critical;$('counter-total').textContent=counts.total;
  $('counter-awaiting-approval').textContent=counts.awaiting_approval;$('counter-attention').textContent=counts.attention+' attention flags';
  for(const s of ['new','acknowledged','in_progress','done']){
    const items=tickets.filter(t=>t.state===s);
    document.querySelector(`[data-count="${s}"]`).textContent=items.length;
    document.querySelector(`[data-column="${s}"]`).innerHTML=items.length?items.map(ticketCard).join(''):'<p class="empty-column">No tickets here</p>';
  }
}
async function renderActivity(){
  const items=await api('/activity'+(state.team?'?team='+state.team:'?limit=40'));
  $('recent-activity-count').textContent=items.length+' updates';
  $('recent-activity-list').innerHTML=items.length?items.map(x=>`<li class="activity-row"><span class="activity-glyph">${e(icon[x.kind]||'•')}</span><div><div class="activity-label">${e(x.label)} ${x.ticket_id?`<button data-action="open-ticket" data-id="${x.ticket_id}">#${x.ticket_id}</button>`:''}</div><div class="activity-meta">${e(teamName(x.team_id))} · ${e(pretty(x.kind))}${x.count>1?' · '+x.count+' occurrences (coalesced)':''}</div></div><time class="activity-when">${e(age(x.updated_at))}</time></li>`).join(''):'<li class="empty-state">No recent activity for this team.</li>';
}
function ticketTable(items){
  if(!items.length)return '<p class="empty-state">No tickets to display.</p>';
  return `<table class="data-table"><thead><tr><th>ID</th><th>Title</th><th>Team</th><th>Type</th><th>State</th><th>Updated</th><th></th></tr></thead><tbody>${items.map(t=>`<tr><td>${e(t.display_id)}</td><td><strong>${e(t.title)}</strong></td><td>${e(teamName(t.team_id))}</td><td>${severityBadge(t)}</td><td>${e(pretty(t.state))}</td><td>${e(age(t.updated_at))}</td><td><button data-action="open-ticket" data-id="${t.id}">View details</button></td></tr>`).join('')}</tbody></table>`;
}
async function renderTickets(){const [active,handled]=await Promise.all([api('/tickets'),api('/tickets?handled=true')]);$('tickets-list').innerHTML=ticketTable(active);$('handled-list').innerHTML=ticketTable(handled)}
async function renderArchive(){
  let team=state.team;let managerTeams=myTeams().filter(t=>t.leader_id===state.me.id).map(t=>t.id);if(!managerTeams.includes(team))team=managerTeams[0];
  let qs=new URLSearchParams({include_errors:$('filter-include-errors').checked?'true':'false',search:$('archive-search').value});if(team)qs.set('team',team);
  $('history-list').innerHTML=ticketTable(await api('/archive?'+qs));
}
async function renderUnmatched(){
  const items=await api('/unmatched');
  $('unmatched-list').innerHTML=items.length?items.map(x=>`<article class="panel"><div class="stack-item-head"><h3>Event #${x.id} · No matching rule</h3><span class="soft-pill">UNMATCHED</span></div><p class="field-hint">Received ${e(fmtTime(x.received_at))}; traversed ${(x.route_trace||[]).length} team rulebooks.</p><details><summary>Raw event and full routing trace</summary><pre class="drawer-json">${e(x.raw)}</pre><pre class="drawer-json">${e(safeJson(x.route_trace))}</pre></details></article>`).join(''):'<div class="panel empty-state">All processed events matched. An unknown schema without a rule appears here, never silently discarded.</div>';
}
const sampleRule={name:'New routing rule',description:'Describe when this rule should fire',priority:50,enabled:true,condition:{all:[{field:'canonical.severity',op:'in',value:['error','critical']},{field:'canonical.service',op:'eq',value:'risk-engine'}]},presentation:{title:'Alert · {{canonical.service}}',body:'{{error.message}}'},routing:{recipients:'team',mandatory_channels:[]},dedup_template:'{{canonical.service}}:{{canonical.schema}}',group_template:'',throttle_seconds:0,runbook_id:null};
async function renderRules(){
  if(!state.team){$('rules-list').innerHTML='<p class="empty-state">Select a team first.</p>';return}
  const items=await api('/rules?team='+state.team);state.rules=items;
  $('rules-count').textContent=items.length+' rules';
  $('rules-list').innerHTML=items.length?items.map(r=>`<div class="stack-item"><div class="stack-item-head"><h3>${e(r.name)}</h3><span class="soft-pill">${r.enabled?'Enabled':'Disabled'}</span></div><p>Priority ${r.priority} · v${r.version} · ${e(r.description||'First matching rule wins')}</p><div class="stack-item-actions"><button class="micro-btn" data-action="edit-rule" data-id="${r.id}">Inspect / edit</button>${isManager()?`<button class="micro-btn" data-action="disable-rule" data-id="${r.id}">Disable</button>`:''}</div></div>`).join(''):'<p class="empty-state">No matching rules yet. The manager can create the first one.</p>';
  if(!state.ruleId || !items.some(r=>r.id===state.ruleId)){
    if(!$('rule-json').value)$('rule-json').value=safeJson(sampleRule);
  }else if(document.activeElement!==$('rule-json'))$('rule-json').value=safeJson(items.find(r=>r.id===state.ruleId));
  if(!$('rule-sample').value)$('rule-sample').value=safeJson(fixture.payments);
}
function readRuleEditor(){let data=JSON.parse($('rule-json').value);delete data.id;delete data.team_id;delete data.version;return {...data,team_id:state.team}}
async function renderRunbooks(){
  if(!state.team){$('runbooks-list').innerHTML='<p class="empty-state">Select a team first.</p>';return}
  const items=await api('/runbooks?team='+state.team);state.runbooks=items;
  $('runbooks-list').innerHTML=items.length?items.map(rb=>`<div class="stack-item"><div class="stack-item-head"><h3>${e(rb.name)}</h3><span class="soft-pill">${e(rb.source_format.toUpperCase())}</span></div><p>${rb.steps?.length||1} steps · Version ${rb.version}</p><div class="stack-item-actions"><button class="micro-btn" data-action="edit-runbook" data-id="${rb.id}">Inspect / edit</button></div></div>`).join(''):'<p class="empty-state">No runbooks yet.</p>';
}
async function renderProposals(){
  const items=await api('/proposals');
  $('proposals-list').innerHTML=items.length?items.map(p=>`<article class="panel"><div class="stack-item-head"><div><h3>CHANGE #${p.id} · ${e(p.target_type)} ${p.target_id?'#'+p.target_id:'(new)'}</h3><p class="field-hint">${e(teamName(p.team_id))} · requested by ${e(findName(p.requester_id))} · ${e(fmtTime(p.created_at))}</p></div><span class="soft-pill">${e(p.status.toUpperCase())}</span></div><p class="drawer-note">${e(p.description)}</p>${p.comment?`<p class="drawer-note"><strong>Manager decision:</strong> ${e(p.comment)}</p>`:''}${p.revised_from?`<p class="field-hint">Revision of request #${p.revised_from}</p>`:''}${isManager()&&p.status==='open'?`<div class="form-actions"><button class="btn btn-subtle" data-action="proposal-decision" data-id="${p.id}" data-decision="accepted">Accept</button><button class="btn btn-danger" data-action="proposal-decision" data-id="${p.id}" data-decision="rejected">Reject</button><button class="btn btn-primary" data-action="proposal-decision" data-id="${p.id}" data-decision="implemented">Implemented</button></div>`:''}</article>`).join(''):'<div class="panel empty-state">No change requests yet. Submit an enhancement proposal for your team manager.</div>';
}
async function renderDelivery(){
  const m=state.me;
  $('primary-channel').value=m.primary_channel;$('backup-channel').value=m.backup_channel;
  $('schedule-start').value=m.schedule?.start||'00:00';$('schedule-end').value=m.schedule?.end||'23:59';$('schedule-timezone').value=m.schedule?.timezone||'UTC';$('buffer-seconds').value=m.buffer_seconds||0;
  const pol=state.team?await api('/policies?team='+state.team):[];
  $('delivery-forced-policies').innerHTML=pol.length?pol.map(p=>`<div class="stack-item"><h3>${e(pretty(p.severity))} · ${e(pretty(p.channel))}</h3><p>Applies to ${e(p.target)} · ${e(pretty(p.kind))}. Team manager policy.</p></div>`).join(''):'<p class="empty-state">No additional manager-enforced channels for this team.</p>';
  const logs=await api('/delivery');
  $('delivery-log').innerHTML=logs.length?`<table class="data-table"><thead><tr><th>Ticket</th><th>Channel</th><th>Kind</th><th>Status</th><th>Attempts</th><th>Next / sent</th></tr></thead><tbody>${logs.map(d=>`<tr><td>#${e(d.ticket_id||'—')}</td><td>${e(pretty(d.channel))}</td><td>${e(pretty(d.kind))}</td><td><span class="soft-pill">${e(d.status)}</span></td><td>${e(d.attempts)}</td><td>${e(fmtTime(d.sent_at||d.available_at))}</td></tr>`).join('')}</tbody></table>`:'<p class="empty-state">Deliveries appear here when a rule creates an alert.</p>';
}
async function renderOrganization(){
  const data=await api('/organization');
  const card=p=>p?`<div class="stack-item"><div class="stack-item-head"><h3>${e(p.name)}</h3><span class="soft-pill">${e(p.role)}</span></div><p>${e(p.email)}</p><p>${e(p.direct_reports)} direct reports</p></div>`:'<p class="empty-state">No direct manager in the organization tree.</p>';
  $('org-manager-card').innerHTML=card(data.manager);
  $('org-reports-list').innerHTML=data.reports.length?data.reports.map(card).join(''):'<p class="empty-state">No direct reports.</p>';
}
async function renderAccess(){
  const opts=allTeams().map(t=>t.id);const others=(await api('/teams').catch(()=>[]));
  // Board-request targets are deliberately the other seeded teams; no hierarchy exposed.
  const potential=[['1','Payments & Reliability'],['2','Settlement Operations'],['3','Rates Trading'],['4','Pricing Infrastructure']].filter(([id])=>!opts.includes(Number(id)));
  $('access-team').innerHTML=selectHtml(potential,null);
  const items=await api('/access');
  $('access-requests-pending').innerHTML=items.length?items.map(a=>`<div class="stack-item"><div class="stack-item-head"><h3>Team #${a.team_id} · ${e(a.status)}</h3><span class="soft-pill">${a.user_id===state.me.id?'My request':'From '+e(findName(a.user_id))}</span></div><p>${e(a.reason||'No reason provided')}</p>${a.status==='pending'&&allTeams().find(t=>t.id===a.team_id)?.leader_id===state.me.id?`<div class="form-actions"><button class="micro-btn" data-action="access-decision" data-id="${a.id}" data-decision="1">Approve</button><button class="micro-btn" data-action="access-decision" data-id="${a.id}" data-decision="0">Deny</button></div>`:''}</div>`).join(''):'<p class="empty-state">No access requests.</p>';
  $('access-request-form').hidden=potential.length===0;
}
async function renderSettings(){
  const m=state.me;
  $('settings-preset').value=m.preset||'engineer';$('settings-timezone').value=m.timezone||'Europe/Warsaw';
  $('preset-indicator').textContent='Current preset: '+pretty(m.preset||'engineer')+'. This never alters authorization.';
  $('profile-card').innerHTML=`<div class="panel-head"><h2>Employee profile</h2><span class="soft-pill">${e(m.role)}${m.debug_enabled?' + D':''}</span></div><div class="stack-item"><div class="stack-item-head"><div class="user-avatar">${e(initials(m.name))}</div><h3>${e(m.name)}</h3></div><p>${e(m.email)}</p><p>${e(m.phone||'No phone')}</p></div><div class="data-kv panel-nested"><div><span>PRIMARY CHANNEL</span><strong>${e(pretty(m.primary_channel))}</strong></div><div><span>BACKUP CHANNEL</span><strong>${e(pretty(m.backup_channel))}</strong></div><div><span>TEAM MEMBERSHIP</span><strong>${myTeams().length} teams</strong></div><div><span>UI PRESET</span><strong>${e(pretty(m.preset))}</strong></div></div>`;
}
async function renderAdmin(){
  const data=await api('/admin');
  $('admin-metrics').innerHTML=Object.entries(data.metrics).slice(0,4).map(([name,val])=>`<article class="metric"><span class="metric-label">${e(pretty(name).toUpperCase())}</span><div class="metric-main"><strong>${val}</strong><span class="metric-symbol">⌁</span></div><span class="metric-caption">Persisted platform data</span></article>`).join('');
  $('admin-sources').innerHTML=`<table class="data-table"><thead><tr><th>Source</th><th>Adapter</th><th>Routing chain</th><th>State</th></tr></thead><tbody>${data.sources.map(s=>`<tr><td>${e(s.name)}<br><small>${e(s.slug)}</small></td><td>${e(s.type)}</td><td>${s.routing_chain?.map(id=>e(data.teams.find(t=>t.id===id)?.name||String(id))).join(' → ')||'All teams'}</td><td>${s.enabled?'Enabled':'Disabled'}</td></tr>`).join('')}</tbody></table>`;
}
async function renderDebug(){
  const data=await api('/debug');
  $('debug-channel-failure').innerHTML=data.channels.map(c=>`<div class="stack-item"><div class="stack-item-head"><h3>${e(pretty(c.name))}</h3><span class="soft-pill">${c.failing?'OUTAGE':'HEALTHY'}</span></div><button class="micro-btn" data-action="toggle-channel" data-name="${e(c.name)}" data-failing="${!c.failing}">${c.failing?'Restore provider':'Simulate outage'}</button></div>`).join('');
  $('debug-latency').innerHTML=[['Total received',data.events],['Queued',data.pending],['Unmatched',data.unmatched]].map(([k,v])=>`<div><span>${e(k)}</span><strong>${e(v)}</strong></div>`).join('');
  if(!$('debug-payload').value)$('debug-payload').value=safeJson(fixture.payments);
  const traces=await api('/debug/events');
  $('debug-traces').innerHTML=traces.length?`<table class="data-table"><thead><tr><th>Event</th><th>Status</th><th>Ticket</th><th>Received</th><th>Routing attempts</th></tr></thead><tbody>${traces.map(t=>`<tr><td>#${t.id}</td><td>${e(t.state)}</td><td>${e(t.ticket_id||'—')}</td><td>${e(fmtTime(t.received_at))}</td><td>${t.route_trace?.length||0}</td></tr>`).join('')}</tbody></table>`:'<p class="empty-state">No event traces yet.</p>';
}
function drawerSection(id,title,body){return `<section class="drawer-section" id="${id}"><h3>${e(title)}</h3>${body}</section>`}
async function openTicket(id,quiet=false){
  const t=await api('/tickets/'+id);state.ticket=t.id;
  $('drawer-backdrop').hidden=false;$('ticket-detail').hidden=false;$('ticket-detail').setAttribute('aria-hidden','false');
  $('ticket-summary').textContent=t.title;
  const details=`<dl class="drawer-details"><div><dt>Ticket</dt><dd>${e(t.display_id)} · ${e(pretty(t.type))}</dd></div><div><dt>Team</dt><dd>${e(teamName(t.team_id))}</dd></div><div><dt>Status</dt><dd>${e(pretty(t.state))}</dd></div><div><dt>Severity</dt><dd>${e(pretty(t.severity))}</dd></div><div><dt>Incident date</dt><dd>${e(t.type==='incident'?fmtTime(t.occurred_at):'Not applicable (manual task)')}</dd></div><div><dt>Occurrences</dt><dd>${t.type==='incident'?e(t.occurrences):'N/A'}</dd></div><div><dt>Source / schema</dt><dd>${e(t.source_id||'—')} / ${e(t.schema||'—')}</dd></div><div><dt>Technical state</dt><dd>${e(t.type==='incident'?t.incident_status:'N/A')}</dd></div></dl><p class="drawer-note">${e(t.body)}</p><div class="ticket-card-badges">${flagsBadges(t)}</div>`;
  const raw=`<details><summary>First event · complete original payload</summary><pre class="drawer-json">${e(safeJson(t.first_payload))}</pre></details><details><summary>Latest event · complete original payload</summary><pre class="drawer-json">${e(safeJson(t.latest_payload))}</pre></details>`;
  const occurrences=t.type==='incident'?`${t.occurrences} occurrences · ${e(fmtTime(t.last_occurrence_at))}<details><summary>Recent occurrence samples (up to 20)</summary><pre class="drawer-json">${e(safeJson(t.samples))}</pre></details>`:'Not applicable to manual tasks or change requests.';
  const rb=t.runbook_steps||[];
  const rbbody=rb.length?rb.map((s,i)=>`<div class="step-card" data-status="${e(s.status)}"><div class="step-circle">${s.status==='done'?'✓':i+1}</div><div><div class="step-title">${e(s.text)}</div><div class="step-meta">${e(pretty(s.type))} · ${e(pretty(s.status))}${s.approval==='required'?' · Manager approval required':''}</div><div class="step-actions">${t.permissions.can_work&&s.status!=='done'?`<button class="micro-btn" data-action="run-step" data-id="${t.id}" data-index="${i}">${s.status==='waiting_approval'?'Awaiting approval':'Complete / run'}</button>`:''}${t.permissions.can_manage&&s.status==='waiting_approval'?`<button class="micro-btn" data-action="approve-step" data-id="${t.id}" data-index="${i}">Manager approve</button>`:''}</div></div></div>`).join('')+(t.runbook_source?`<details><summary>Original ${e(t.runbook_format||'')} runbook source</summary><pre class="drawer-json">${e(t.runbook_source)}</pre></details>`:''):'<p class="empty-state">No runbook attached. The team manager can publish one and associate it with a rule.</p>';
  const members=t.members?.length?t.members.map(m=>`<span class="soft-pill">${e(m.name)}</span>`).join(' '):'<span class="muted-inline">Nobody has joined yet</span>';
  let buttons=[];
  if(t.permissions.can_join)buttons.push(`<button class="btn btn-subtle" data-action="join-ticket" data-id="${t.id}">Join ticket</button>`);
  if(t.permissions.can_work&&t.type!=='change_request'){
    if(t.state==='new')buttons.push(`<button class="btn btn-subtle" data-action="move-ticket" data-id="${t.id}" data-state="acknowledged">Acknowledge</button>`);
    if(['new','acknowledged'].includes(t.state))buttons.push(`<button class="btn btn-subtle" data-action="move-ticket" data-id="${t.id}" data-state="in_progress">Start work</button>`);
    if(t.state!=='done'&&t.state!=='archived')buttons.push(`<button class="btn btn-primary" data-action="move-ticket" data-id="${t.id}" data-state="done">Mark Done · request approval</button>`);
    if((t.flags||[]).includes('attention'))buttons.push(`<button class="btn btn-subtle" data-action="ack-attention" data-id="${t.id}">Seen · clear attention flag</button>`);
    buttons.push(`<button class="btn btn-subtle" data-action="add-member" data-id="${t.id}">Add member</button>`);
  }
  if(t.state==='done'&&t.type!=='change_request'&&t.permissions.can_approve){buttons.push(`<button class="btn btn-primary" data-action="approve-ticket" data-id="${t.id}">Approve & archive</button>`,`<button class="btn btn-danger" data-action="reject-ticket" data-id="${t.id}">Send back</button>`)}
  if(t.state==='done'&&t.type!=='change_request'&&t.done_by===state.me.id&&!t.permissions.can_approve&&isManager())buttons.push('<span class="field-hint">Self-approval is prohibited. Another manager/maintainer must approve.</span>');
  const timeline=(t.timeline||[]).slice().reverse().map(a=>`<li class="timeline-item"><strong>${e(pretty(a.action))}</strong> · ${e(a.actor)}<div>${e(Object.entries(a.detail||{}).map(([k,v])=>k+': '+(typeof v==='object'?JSON.stringify(v):v)).join(' · ').slice(0,320))}</div><time>${e(fmtTime(a.created_at))}</time></li>`).join('');
  if(t.state==='archived'&&t.permissions.can_manage)buttons.push(`<button class="btn btn-danger" data-action="purge-ticket" data-id="${t.id}">Purge archived ticket</button>`);
  $('ticket-detail-content').innerHTML=drawerSection('ticket-summary-section','Ticket summary',details)+drawerSection('ticket-alert-data','Original event data',raw)+drawerSection('ticket-occurrences','Occurrence history',occurrences)+drawerSection('ticket-runbook','Runbook · next steps',rbbody)+drawerSection('ticket-members','Ticket members',members)+drawerSection('ticket-actions','Available actions',`<div class="drawer-actions">${buttons.join('')||'<span class="muted-inline">Read-only ticket.</span>'}</div>`)+drawerSection('ticket-timeline','Audit timeline',`<ol class="timeline-list">${timeline||'<li class="empty-state">No updates</li>'}</ol>`)+(t.state==='archived'?'':drawerSection('ticket-comments','Add a comment',`<textarea class="comment-box" id="ticket-comment-text" placeholder="Add context for your team..."></textarea><button class="btn btn-subtle" data-action="comment" data-id="${t.id}">Post comment</button>`))+drawerSection('ticket-proposals','Delivery status',`<p class="field-hint">${(t.deliveries||[]).map(d=>`${e(d.channel)}: ${e(d.status)}`).join(' · ')||'No delivery records yet'}</p>`);
}
function closeTicket(){state.ticket=null;$('drawer-backdrop').hidden=true;$('ticket-detail').hidden=true;$('ticket-detail').setAttribute('aria-hidden','true')}
function dialog(title,fields,confirmText='Confirm'){
  return new Promise(resolve=>{
    const d=$('app-dialog');$('dialog-title').textContent=title;$('dialog-body').innerHTML=`<div class="dialog-fields">${fields}</div>`;$('dialog-confirm').textContent=confirmText;
    const onclose=()=>{d.removeEventListener('close',onclose);resolve(d.returnValue==='confirm'?new FormData(d.querySelector('form')):null)};
    d.addEventListener('close',onclose);d.showModal();
  });
}
async function confirmDialog(title,message){return !!(await dialog(title,`<p class="drawer-note">${e(message)}</p>`))}
async function userAction(el){
  const action=el.dataset.action;const id=Number(el.dataset.id);
  try{
    if(action==='login'){await api('/session','POST',{user_id:id});await initialize();return}
    if(action==='logout'){if(await confirmDialog('Switch persona','Sign out of this demo identity?')){await api('/logout','POST');state.stream?.close();state.me=null;showPersonaLogin()}return}
    if(action==='close-ticket'){closeTicket();return}
    if(action==='open-ticket'){await openTicket(id);return}
    if(action==='new-task'){
      const f=await dialog('Create a new task',`<label>Title <input name="title" required placeholder="What needs to be done?"></label><label>Description <textarea name="body" placeholder="Purpose and next steps"></textarea></label>`,'Create task');
      if(f){await api('/tickets','POST',{title:f.get('title'),body:f.get('body'),team_id:state.team});toast('Task created');await refreshView()}return;
    }
    if(action==='join-ticket'){await api('/tickets/'+id+'/join','POST');toast('Joined ticket');await refreshView();return}
    if(action==='add-member'){
      const options=[...new Map(allTeams().flatMap(t=>t.members||[]).map(m=>[m.id,m])).values()].map(m=>[m.id,m.name]);
      const f=await dialog('Assign ticket member',`<label>Employee <select name="user_id">${selectHtml(options,'')}</select></label>`,'Assign');
      if(f){await api('/tickets/'+id+'/members','POST',{user_id:Number(f.get('user_id'))});toast('Ticket member notified');await refreshView()}return;
    }
    if(action==='move-ticket'){await api('/tickets/'+id+'/move','POST',{state:el.dataset.state});toast('Ticket moved to '+pretty(el.dataset.state));await refreshView();return}
    if(action==='purge-ticket'){
      const phrase=`PURGE TKT-${String(id).padStart(5,'0')}`;
      const form=await dialog('Permanently purge ticket',`<p class="drawer-note">This deliberately removes linked event and notification payloads and leaves an audit tombstone. Type <strong>${phrase}</strong> to continue.</p><label>Confirmation <input name="confirmation" required></label>`,'Purge ticket');
      if(form){await api('/tickets/'+id+'/purge','POST',{confirmation:form.get('confirmation')});toast('Ticket purged; audit tombstone retained');closeTicket();await refreshView()}
      return;
    }
    if(action==='approve-ticket'){
      if(await confirmDialog('Approve ticket resolution','This approval archives the ticket and preserves its full audit history.')){await api('/tickets/'+id+'/approve','POST');toast('Resolution approved · ticket archived');closeTicket();await refreshView()}return;
    }
    if(action==='reject-ticket'){
      const f=await dialog('Return ticket to In progress','<label>Reason for rejection <textarea name="comment" required placeholder="What still needs work?"></textarea></label>','Reject resolution');
      if(f){await api('/tickets/'+id+'/reject','POST',{comment:f.get('comment')});toast('Ticket returned to In progress');await refreshView()}return;
    }
    if(action==='comment'){const value=$('ticket-comment-text').value;await api('/tickets/'+id+'/comment','POST',{comment:value});toast('Comment added');await refreshView();return}
    if(action==='ack-attention'){await api('/tickets/'+id+'/attention','POST');toast('Attention flag cleared');await refreshView();return}
    if(action==='run-step'){await api('/tickets/'+id+'/steps/'+el.dataset.index+'/run','POST');toast('Runbook step updated');await refreshView();return}
    if(action==='approve-step'){await api('/tickets/'+id+'/steps/'+el.dataset.index+'/approve','POST');toast('Risky step approved by manager');await refreshView();return}
    if(action==='new-rule'){state.ruleId=null;$('rule-editor-title').textContent='New routing rule';$('rule-json').value=safeJson(sampleRule);return}
    if(action==='edit-rule'){const rule=state.rules?.find(r=>r.id===id);if(rule){state.ruleId=id;$('rule-editor-title').textContent='Edit · '+rule.name;$('rule-json').value=safeJson(rule)}return}
    if(action==='save-rule'){const rule=readRuleEditor();if(state.ruleId)await api('/rules/'+state.ruleId,'PUT',rule);else await api('/rules','POST',rule);toast('Rule published; next event uses new version');await renderRules();return}
    if(action==='disable-rule'){if(await confirmDialog('Disable rule','The rule will no longer match new events; historic tickets remain intact.')){await api('/rules/'+id,'DELETE');await renderRules()}return}
    if(action==='test-rule'){const result=await api('/rules/test','POST',{team_id:state.team,rule:readRuleEditor(),event:JSON.parse($('rule-sample').value)});$('rule-test-result').textContent=`Matched: ${result.matched?'YES':'NO'}\nSeverity: ${result.severity}\nTitle: ${result.title}\nBody: ${result.body}`;return}
    if(action==='request-rule'||action==='request-runbook'){const desc=await dialog('Request a manager review','<label>Describe the requested change<textarea name="description" required placeholder="What needs to change, and why?"></textarea></label>','Submit request');if(desc){await api('/proposals','POST',{team_id:state.team,target_type:action==='request-rule'?'rule':'runbook',target_id:action==='request-rule'?state.ruleId:state.runbookId,description:desc.get('description')});toast('Change request sent to team manager')}return}
    if(action==='new-runbook'){state.runbookId=null;$('runbook-name').value='';$('runbook-text').value='';$('runbook-format').value='markdown';$('runbook-editor-title').textContent='New runbook';return}
    if(action==='edit-runbook'){const rb=state.runbooks?.find(r=>r.id===id);if(rb){state.runbookId=id;$('runbook-name').value=rb.name;$('runbook-text').value=rb.source_text;$('runbook-format').value=rb.source_format;$('runbook-editor-title').textContent=rb.name;$('runbook-canonical').textContent='Parsed steps: '+rb.steps.map(s=>s.text).join(' • ')}return}
    if(action==='save-runbook'){const data={team_id:state.team,name:$('runbook-name').value,source_format:$('runbook-format').value,source_text:$('runbook-text').value};if(state.runbookId)await api('/runbooks/'+state.runbookId,'PUT',data);else await api('/runbooks','POST',data);toast('Runbook published');await renderRunbooks();return}
    if(action==='new-proposal'){
      const f=await dialog('Request a change',`<label>Target type <select name="target_type"><option value="rule">Routing rule</option><option value="runbook">Runbook</option></select></label><label>Target ID (optional)<input type="number" name="target_id" placeholder="Leave blank for new item"></label><label>Requested change<textarea name="description" required></textarea></label>`,'Submit request');
      if(f){await api('/proposals','POST',{team_id:state.team,target_type:f.get('target_type'),target_id:f.get('target_id')?Number(f.get('target_id')):null,description:f.get('description')});toast('Manager review requested');await renderProposals()}return;
    }
    if(action==='proposal-decision'){
      const decision=el.dataset.decision;let comment='';if(decision==='rejected'){let f=await dialog('Reject change request','<label>Reason for rejection<textarea required name="comment"></textarea></label>','Reject');if(!f)return;comment=f.get('comment')}
      await api('/proposals/'+id+'/decision','POST',{decision,comment});toast('Change request '+decision);await renderProposals();return;
    }
    if(action==='save-preferences'){
      await api('/preferences','PUT',{primary_channel:$('primary-channel').value,backup_channel:$('backup-channel').value,buffer_seconds:Number($('buffer-seconds').value),schedule:{timezone:$('schedule-timezone').value,start:$('schedule-start').value,end:$('schedule-end').value}});
      state.me=await api('/me');toast('Delivery preferences updated');return;
    }
    if(action==='force-policy'){await api('/policies','POST',{team_id:state.team,severity:$('force-severity').value,channel:$('force-channel').value});toast('Mandatory team channel added');await renderDelivery();return}
    if(action==='save-settings'){await api('/preferences','PUT',{preset:$('settings-preset').value,timezone:$('settings-timezone').value});state.me=await api('/me');$('app-shell').dataset.preset=state.me.preset;toast('Settings saved');await renderSettings();return}
    if(action==='request-access'){await api('/access','POST',{team_id:Number($('access-team').value),reason:$('access-reason').value});toast('Access request sent');await renderAccess();return}
    if(action==='access-decision'){await api('/access/'+id+'/decision','POST',{approved:el.dataset.decision==='1'});toast('Board access decision recorded');await renderAccess();return}
    if(action==='create-source'){let data=JSON.parse($('source-config').value);await api('/admin/sources','POST',data);toast('Source registered');await renderAdmin();return}
    if(action==='toggle-channel'){await api('/debug/channel','POST',{name:el.dataset.name,failing:el.dataset.failing==='true'});toast('Test channel state changed');await renderDebug();return}
    if(action==='generate'){const value=JSON.parse($('debug-payload').value);const n=Number($('debug-count').value);const result=await api('/debug/generate','POST',{event:value,count:n});toast(`Queued ${result.accepted} test events in the real ingestion pipeline`);await renderDebug();return}
  }catch(err){toast(err.message,true)}
}
document.addEventListener('click',ev=>{const node=ev.target.closest('[data-action]');if(!node)return;ev.preventDefault();ev.stopPropagation();userAction(node)});
document.addEventListener('keydown',ev=>{if(ev.key==='Enter'&&ev.target.classList.contains('ticket-card')){ev.preventDefault();userAction(ev.target)}if(ev.key==='Escape'&&state.ticket)closeTicket()});
document.querySelectorAll('.nav-link').forEach(el=>el.addEventListener('click',()=>navigate(el.dataset.view)));
$('team-selector').addEventListener('change',ev=>chooseTeam(ev.target.value));
$('filter-team').addEventListener('change',ev=>chooseTeam(ev.target.value));
for(const field of ['filter-state','filter-severity','filter-my-tickets','filter-unassigned','filter-include-errors'])$(field).addEventListener('change',()=>refreshView());
for(const field of ['filter-search','archive-search'])$(field).addEventListener('input',()=>scheduleRefresh());
$('debug-fixture').addEventListener('change',ev=>{$('debug-payload').value=safeJson(fixture[ev.target.value]||fixture.payments)});
initialize();
