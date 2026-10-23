'use strict';
const $=id=>document.getElementById(id);let user=null,busy=false,renderSignature=null,googleEnabled=false,lastWorkspaceStatus=null,guideKey=null,adminSignature=null;
async function api(route,data){const response=await fetch('/api/'+route,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});const body=await response.json();if(!response.ok)throw Object.assign(new Error(body.error||'Request failed.'),{status:response.status});return body;}
function error(e){$('error').textContent=e.message;$('error').hidden=false;}
async function action(fn){if(busy)return;busy=true;$('error').hidden=true;try{await fn();await refresh();}catch(e){error(e);}finally{busy=false;}}
function showScreen(){const settings=location.hash==='#settings'&&user?.activated,admin=location.hash==='#admin'&&user?.role==='admin';$('admin-screen').hidden=!admin;$('settings-screen').hidden=!settings;$('dashboard-screen').hidden=settings||admin;$('settings-open').hidden=!user?.activated||settings||admin;}
function openSettings(expand=false){location.hash='settings';showScreen();if(expand){$('inference-providers').open=true;$('inference-providers').scrollIntoView({block:'start'});}}
window.addEventListener('hashchange',showScreen);
$('settings-open').addEventListener('click',()=>openSettings());
function closeSettings(){location.hash='';showScreen();}
$('settings-back').addEventListener('click',closeSettings);
document.addEventListener('click',event=>{if(!$('settings-screen').hidden&&!event.target.closest('.panel,.settings-heading,button,a,dialog'))closeSettings();});
$('set-provider').addEventListener('click',()=>openSettings(true));
window.addEventListener('message',event=>{if(event.origin===location.origin&&event.source===$('analytics-frame').contentWindow&&event.data?.type==='swarm-analytics-height'){const height=Number(event.data.height);if(Number.isFinite(height))$('analytics-frame').style.height=Math.max(500,Math.min(height+8,20000))+'px';}});
$('analytics-frame').addEventListener('load',()=>{$('analytics-loading').hidden=true;});
window.addEventListener('message',event=>{if(event.origin===location.origin&&event.source===$('analytics-frame').contentWindow&&event.data?.type==='swarm-workspace-status'&&typeof event.data.status==='string'){const status=event.data.status;$('activity-status').textContent=status.charAt(0).toUpperCase()+status.slice(1);$('activity-status').dataset.state=status;$('activity-status').hidden=user?.status!=='running';}});

$('guide-done').addEventListener('click',()=>$('swarm-guide').close());
$('swarm-guide').addEventListener('close',()=>{if(guideKey)try{localStorage.setItem(guideKey,'dismissed');}catch{}});

async function refresh(){try{
 user=await api('me');$('welcome').hidden=true;$('account').hidden=!user.activated;$('activation').hidden=user.activated;$('settings-open').hidden=!user.activated;document.querySelector('main').classList.toggle('is-account',user.activated);if(!user.activated)return;
 const running=user.status==='running',viewAnalytics=user.analytics_available||running,transitioning=['queued','provisioning','pausing','stopping'].includes(user.status),hasProviders=Object.values(user.providers).some(Boolean);
 for(const section of document.querySelectorAll('.workspace-section'))section.classList.toggle('is-inactive',!running&&!(viewAnalytics&&section.querySelector('#analytics-frame')));
 $('name').textContent=user.name+"’s swarm";$('status').textContent=({sleeping:'Paused',new:'Ready',queued:'Starting',provisioning:'Provisioning',stopping:'Stopping'})[user.status]||user.status.charAt(0).toUpperCase()+user.status.slice(1);$('status').dataset.state=user.status;$('status').setAttribute('aria-busy',String(transitioning));
 $('admin-dashboard-link').hidden=user.role!=='admin';$('admin').hidden=user.role!=='admin';$('google-account').hidden=!googleEnabled||user.google_connected;$('google-connect').disabled=Boolean(user.google_connected);$('google-connect').textContent=user.google_connected?'Google connected':'Connect Google';
 $('start').hidden=running;$('start').disabled=transitioning||!hasProviders;$('pause').hidden=!running;$('stop').disabled=transitioning||['new','stopped'].includes(user.status);
 $('provider-warning').hidden=hasProviders;
 for(const button of document.querySelectorAll('[data-service]'))button.disabled=!running;
 $('notice').hidden=!user.error;$('notice').textContent=user.error||'';
 $('workspace-empty').hidden=viewAnalytics;$('workspace-empty').textContent=transitioning?'':user.status==='stopped'?'Press Play to start a fresh swarm.':user.status==='sleeping'?'Press Play to continue your swarm.':'Press Play to start your swarm.';
 const frame=$('analytics-frame');frame.hidden=!viewAnalytics;if(!running)$('activity-status').hidden=true;if(viewAnalytics&&(!frame.getAttribute('src')||frame.dataset.mode!==user.status)){$('analytics-loading').hidden=false;frame.dataset.mode=user.status;frame.src='/api/analytics?resource=page&mode='+user.status;}else if(!viewAnalytics&&frame.getAttribute('src')){frame.removeAttribute('src');$('analytics-loading').hidden=true;}
 renderProviders();renderAdminKeys();showScreen();
 if(location.hash==='#admin'&&user.role==='admin')await renderUsage();
 if(running){guideKey='swarm-guide:'+user.id+':'+(user.instance_id||user.id);let seen=false;try{seen=localStorage.getItem(guideKey)==='dismissed';}catch{}if((!seen||['queued','provisioning'].includes(lastWorkspaceStatus))&&!$('swarm-guide').open)$('swarm-guide').showModal();}
 lastWorkspaceStatus=user.status;
 await followSharedLink();
 }catch(e){if(e.status===401){user=null;lastWorkspaceStatus=null;$('swarm-guide').close();$('pause-notice').close();$('welcome').hidden=false;$('account').hidden=true;$('activation').hidden=true;$('settings-open').hidden=true;$('analytics-frame').removeAttribute('src');$('support-view').close();$('support-content').replaceChildren();$('usage-users').replaceChildren();$('usage-runs').replaceChildren();document.querySelector('main').classList.remove('is-account');}else error(e);}}

const providerNames={openai:'OpenAI',anthropic:'Anthropic',deepseek:'DeepSeek'};
function renderProviders(){
  const paused=!['running','queued','provisioning','pausing','stopping'].includes(user.status);
  const signature=JSON.stringify([user.provider_keys,user.providers,paused]);
  if(renderSignature===signature)return;renderSignature=signature;
  $('provider-list').replaceChildren();
  for(const [provider,enabled] of Object.entries(user.providers)){
    if(!enabled)continue;
    const row=document.createElement('div');row.className='provider-row';
    const heading=document.createElement('div');heading.className='provider-heading';
    const name=document.createElement('strong');name.textContent=providerNames[provider];
    const key=document.createElement('code');key.className='provider-key';key.textContent=user.provider_keys[provider];
    const remove=document.createElement('button');remove.type='button';remove.className='delete-provider';remove.textContent='Delete';remove.setAttribute('aria-label','Delete '+providerNames[provider]+' key');remove.disabled=!paused;
    remove.addEventListener('click',()=>action(async()=>{await api('providers',{provider,key:''});}));
    heading.append(name,key,remove);row.append(heading);
    $('provider-list').append(row);
  }
  const available=Object.keys(providerNames).filter(provider=>!user.providers[provider]);
  const current=$('provider').value;$('provider').replaceChildren();
  for(const provider of available){const option=document.createElement('option');option.value=provider;option.textContent=providerNames[provider];$('provider').append(option);}
  if(available.includes(current))$('provider').value=current;
  $('provider-empty').hidden=Object.values(user.providers).some(Boolean);
  $('add-provider').hidden=!available.length;$('add-provider').disabled=!paused;
  if(!paused)$('provider-form').hidden=true;
}
$('add-provider').addEventListener('click',()=>{$('provider-form').hidden=false;$('provider').focus();});
$('cancel-provider').addEventListener('click',()=>{$('provider-form').hidden=true;$('provider-key').value='';});

$('login-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{await api('redeem',{code:$('invite-code').value.trim()});sessionStorage.removeItem('swarm-invite');$('invite-code').value='';renderSignature=null;});});
async function signOut(){await action(async()=>{await api('logout',{});renderSignature=null;adminSignature=null;$('billing-key').value='';$('admin-key-form').hidden=true;$('provider-admin-keys').open=false;$('provider-key').value='';$('provider-form').hidden=true;$('inference-providers').open=false;$('invite-result').replaceChildren();});}
$('logout').addEventListener('click',signOut);
$('activation-logout').addEventListener('click',signOut);
$('provider-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{await api('providers',{provider:$('provider').value,key:$('provider-key').value});$('provider-key').value='';$('provider-form').hidden=true;});});
$('start').addEventListener('click',()=>action(()=>api('start',{})));
$('pause').addEventListener('click',()=>action(async()=>{await api('pause',{});$('pause-notice').showModal();}));
$('pause-done').addEventListener('click',()=>$('pause-notice').close());
$('stop').addEventListener('click',()=>$('stop-confirm').showModal());
$('stop-cancel').addEventListener('click',()=>$('stop-confirm').close());
$('stop-confirmed').addEventListener('click',()=>{if(busy)return;$('stop-confirm').close();action(()=>api('stop',{}));});
function openService(service,path=''){if(busy)return;const tab=window.open('about:blank','_blank');action(async()=>{try{const data=await api('open',{service,path});const url=new URL(data.url);if(url.protocol!=='https:'||!url.hostname.endsWith('.agent37.app'))throw new Error('Invalid service URL.');if(tab){tab.opener=null;tab.location.href=url.href;}else location.href=url.href;}catch(e){tab?.close();throw e;}});}
for(const button of document.querySelectorAll('[data-service]'))button.addEventListener('click',()=>openService(button.dataset.service));
window.addEventListener('message',event=>{if(event.origin===location.origin&&event.source===$('analytics-frame').contentWindow&&event.data?.type==='swarm-open-service'&&['mattermost','gitea','opencode'].includes(event.data.service)&&typeof event.data.path==='string')openService(event.data.service,event.data.path);});
$('invite-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{const data=await api('invite',{name:$('friend-name').value});$('invite-result').replaceChildren();const input=document.createElement('input');input.readOnly=true;input.value=data.url;input.addEventListener('click',()=>input.select());const note=document.createElement('p');note.className='small';note.textContent='This link grants access to their workspace. Share it privately.';$('invite-result').append(input,note);});});
const params=new URLSearchParams(location.hash.slice(1)),code=params.get('invite');
if(code){sessionStorage.setItem('swarm-invite',code);history.replaceState(null,'',location.pathname);}
const sharedService=params.get('open');
if(sharedService){
 if(['mattermost','gitea','opencode','dashboard'].includes(sharedService)&&/^[a-z0-9]+$/.test(params.get('instance')||'')){
  sessionStorage.setItem('swarm-open',JSON.stringify({service:sharedService,instance:params.get('instance'),path:params.get('path')||'/'}));
 }
 history.replaceState(null,'',location.pathname);
}
let followingSharedLink=false;
async function followSharedLink(){
 if(!user?.activated||user.status!=='running'||followingSharedLink)return;
 const pending=sessionStorage.getItem('swarm-open');if(!pending)return;
 followingSharedLink=true;sessionStorage.removeItem('swarm-open');
 try{
  const data=await api('open',JSON.parse(pending));const url=new URL(data.url);
  if(url.protocol!=='https:'||!url.hostname.endsWith('.agent37.app'))throw new Error('Invalid service URL.');
  location.assign(url.href);
 }catch(e){followingSharedLink=false;error(e);}
}
async function initialize(){await refresh();const pending=sessionStorage.getItem('swarm-invite');if(user&&!user.activated&&pending){$('invite-code').value=pending;await action(async()=>{await api('redeem',{code:pending});sessionStorage.removeItem('swarm-invite');$('invite-code').value='';});}else if(user?.activated){sessionStorage.removeItem('swarm-invite');}}
initialize();setInterval(()=>{if(user&&!busy)refresh();},5000);

async function googleSignIn(){await action(async()=>{const data=await api('google-start',{});const url=new URL(data.url);if(url.origin!=='https://accounts.google.com')throw new Error('Invalid Google sign-in URL.');location.assign(url.href);});}
$('google-login').addEventListener('click',googleSignIn);
$('google-connect').addEventListener('click',googleSignIn);
api('auth-config').then(config=>{googleEnabled=config.google;$('google-login').hidden=!googleEnabled;if(user){$('google-account').hidden=!googleEnabled||user.google_connected;$('google-connect').disabled=Boolean(user.google_connected);$('google-connect').textContent=user.google_connected?'Google connected':'Connect Google';}}).catch(()=>{});
const signinError=new URLSearchParams(location.search).get('signin_error');if(signinError){history.replaceState(null,'',location.pathname);error(new Error('Google sign-in was not completed. Please try again.'));}

const previews=[...document.querySelectorAll('.product-preview')];
const mobilePreviews=window.matchMedia('(max-width:650px)');
function expandPreview(selected){for(const preview of previews){const expanded=!mobilePreviews.matches&&preview===selected;const label=preview.querySelector('figcaption').textContent.toLowerCase()+' preview';preview.classList.toggle('is-expanded',expanded);preview.setAttribute('aria-label',mobilePreviews.matches?label:(expanded?'Shrink ':'Enlarge ')+label);if(mobilePreviews.matches){preview.removeAttribute('role');preview.removeAttribute('aria-expanded');}else{preview.setAttribute('role','button');preview.setAttribute('aria-expanded',String(expanded));}}}
for(const preview of previews){
  const toggle=()=>{if(!mobilePreviews.matches)expandPreview(preview.classList.contains('is-expanded')?null:preview);};
  preview.addEventListener('click',toggle);
  preview.addEventListener('keydown',event=>{if(!mobilePreviews.matches&&(event.key==='Enter'||event.key===' ')){event.preventDefault();toggle();}});
}
mobilePreviews.addEventListener('change',()=>expandPreview(null));
expandPreview(null);
document.addEventListener('click',event=>{if(!event.target.closest('.product-preview'))expandPreview(null);});
document.addEventListener('keydown',event=>{if(event.key==='Escape')expandPreview(null);});

function openBillingSettings(){openSettings();$('provider-admin-keys').open=true;$('provider-admin-keys').scrollIntoView({block:'start'});}
window.addEventListener('message',event=>{if(event.origin===location.origin&&event.source===$('analytics-frame').contentWindow&&event.data?.type==='swarm-billing-settings')openBillingSettings();});
function resetAnalytics(){$('analytics-frame').removeAttribute('src');}
function renderAdminKeys(){
 const keys=user.admin_keys||{},signature=JSON.stringify(keys);if(signature===adminSignature)return;adminSignature=signature;
 $('admin-key-list').replaceChildren();
 for(const [provider,key] of Object.entries(keys)){
  const row=document.createElement('div');row.className='provider-row';
  const heading=document.createElement('div');heading.className='provider-heading';
  const name=document.createElement('strong');name.textContent=providerNames[provider];
  const masked=document.createElement('code');masked.className='provider-key';masked.textContent=key;
  const remove=document.createElement('button');remove.type='button';remove.className='delete-provider';remove.textContent='Delete';remove.setAttribute('aria-label','Delete '+providerNames[provider]+' admin key');
  remove.addEventListener('click',()=>action(async()=>{await api('admin-keys',{provider,key:''});resetAnalytics();}));
  heading.append(name,masked,remove);row.append(heading);$('admin-key-list').append(row);
 }
 const available=['openai','anthropic'].filter(provider=>!keys[provider]),current=$('billing-provider').value;$('billing-provider').replaceChildren();
 for(const provider of available){const option=document.createElement('option');option.value=provider;option.textContent=providerNames[provider];$('billing-provider').append(option);}
 if(available.includes(current))$('billing-provider').value=current;
 $('add-admin-key').hidden=!available.length;
}
$('add-admin-key').addEventListener('click',()=>{$('admin-key-form').hidden=false;$('billing-provider').focus();});
$('cancel-admin-key').addEventListener('click',()=>{$('admin-key-form').hidden=true;$('billing-key').value='';});
$('admin-key-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{await api('admin-keys',{provider:$('billing-provider').value,key:$('billing-key').value});$('billing-key').value='';$('admin-key-form').hidden=true;resetAnalytics();});});

let usageUpdated=0,supportTarget=null,supportPage=1,supportMore=false,supportItems=[],supportListing=true;
const usageNumber=value=>value===null||value===undefined?'—':new Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:2}).format(value);
const usageDate=value=>value?new Date(value*1000).toLocaleString(): '—';
function usageNode(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
$('usage-open').addEventListener('click',()=>{location.hash='admin';showScreen();renderUsage(true).catch(error);});
$('usage-back').addEventListener('click',closeSettings);
window.addEventListener('hashchange',()=>{if(location.hash==='#admin'&&user?.role==='admin')renderUsage(true).catch(error);});
async function renderUsage(force=false){
 if(!force&&Date.now()-usageUpdated<15000)return;usageUpdated=Date.now();
 const data=await api('admin-usage');if(user?.role!=='admin')return;
 const users=data.users,rows=data.runs,names=new Map(users.map(u=>[u.id,u.name]));
 if(supportTarget&&!users.find(u=>u.id===supportTarget.id&&u.support_until>Date.now()/1000))$('support-view').close();
 const cards=$('usage-totals');cards.replaceChildren();
 for(const [label,count] of [['Users',users.filter(u=>u.invite_status==='claimed').length],['Pending invites',users.filter(u=>u.invite_status==='pending').length],['Running',users.filter(u=>u.status==='running').length],['Runs',data.total_runs],['Returning users',users.filter(u=>rows.filter(r=>r.user_id===u.id).length>1).length],['With merged PRs',users.filter(u=>rows.some(r=>r.user_id===u.id&&(r.totals.merged_prs||0)>0)).length]]){
  const card=usageNode('div',undefined,'usage-card');card.append(usageNode('strong',usageNumber(count)),usageNode('span',label));cards.append(card);
 }
 const list=$('usage-users');list.replaceChildren();
 for(const u of users.filter(u=>u.invite_status!=='revoked')){
  const card=usageNode('article',undefined,'usage-user'),heading=usageNode('div',undefined,'section-title');
  heading.append(usageNode('strong',u.name),usageNode('span',u.status+' · '+u.invite_status,'small'));card.append(heading);
  card.append(usageNode('p','Invite created '+usageDate(u.created_at)+' · Last seen '+usageDate(u.last_seen)));
  const latest=u.latest;
  if(latest){card.append(usageNode('p',usageNumber(latest.agents)+' agents · '+usageNumber(latest.blocked)+' blocked · '+usageNumber(latest.failed)+' failed'+(latest.uncertain_calls?' · '+usageNumber(latest.uncertain_calls)+' calls with unknown usage':'')+' · '+(latest.models||[]).join(', ')));
   card.append(usageNode('p','Last sample '+usageDate(latest.at)+(latest.unavailable?' · Collection unavailable':'')+' · Disk '+(latest.disk_used_bytes===null?'—':(latest.disk_used_bytes/1073741824).toFixed(1)+' GB')));
  }else card.append(usageNode('p','No usage recorded yet.'));
  const actions=usageNode('div',undefined,'actions');
  if(u.invite_status!=='claimed')for(const kind of ['regenerate','revoke']){
   const button=usageNode('button',kind==='regenerate'?'New invite code':'Revoke invite','quiet');button.disabled=kind==='revoke'&&u.invite_status==='revoked';
   button.addEventListener('click',()=>action(async()=>{const result=await api('admin-invite',{user_id:u.id,action:kind});if(result.code){card.append(usageNode('p','New invite code: '+result.code));button.disabled=true;}else await renderUsage(true);}));actions.append(button);
  }
  if(u.support_until&&u.status==='running'){
   const button=usageNode('button','Read-only support','quiet');button.addEventListener('click',()=>openSupportView(u));actions.append(button);
  }else actions.append(usageNode('span','Content access not granted','small muted'));
  card.append(actions);list.append(card);
 }
 const body=$('usage-runs');body.replaceChildren();
 for(const r of rows){const tr=usageNode('tr'),t=r.totals,duration=r.duration_seconds===null?'Unknown start':Math.round(r.duration_seconds/60)+' min';
  for(const text of [(names.get(r.user_id)||'User')+'\n'+(r.imported?'Imported totals':usageDate(r.started_at||r.requested_at)),(r.end_reason||'Running').replaceAll('_',' ')+'\n'+duration,usageNumber(t.agents_created)+' / '+r.peak_agents,usageNumber(t.human_messages)+' / '+usageNumber(t.messages),usageNumber(t.input_tokens)+' / '+usageNumber(t.cached_input_tokens)+' / '+usageNumber(t.output_tokens),usageNumber(t.issues),usageNumber(t.prs)+' / '+usageNumber(t.merged_prs),usageNumber(t.commits)]){
   const td=usageNode('td',text);td.style.whiteSpace='pre-line';tr.append(td);
  }body.append(tr);
 }
}
function openSupportView(u){supportTarget=u;supportPage=1;$('support-title').textContent=u.name+' · Read-only support';$('support-banner').textContent='Viewing as David · Granted by '+u.name+' until '+usageDate(u.support_until);$('support-kind').value='channels';$('support-choice').hidden=true;$('support-repo-kind').hidden=true;$('support-path').hidden=true;$('support-content').replaceChildren();$('support-view').showModal();loadSupport(true).catch(error);}
$('support-close').addEventListener('click',()=>$('support-view').close());
$('support-view').addEventListener('close',()=>{supportTarget=null;$('support-content').replaceChildren();});
$('support-kind').addEventListener('change',()=>{supportPage=1;loadSupport(true).catch(error);});
$('support-choice').addEventListener('change',()=>{supportPage=1;$('support-path').value='';});
$('support-repo-kind').addEventListener('change',()=>{$('support-path').hidden=$('support-repo-kind').value!=='files';supportPage=1;});
$('support-load').addEventListener('click',()=>{supportPage=1;loadSupport(false).catch(error);});
$('support-newer').addEventListener('click',()=>{if(supportPage>1){supportPage--;loadSupport(supportListing).catch(error);}});
$('support-older').addEventListener('click',()=>{if(supportMore){supportPage++;loadSupport(supportListing).catch(error);}});
async function loadSupport(list=false){
 if(!supportTarget)return;supportListing=list;const target=supportTarget;
 const base=$('support-kind').value,kind=list?base:base==='channels'?'messages':$('support-repo-kind').value;
 const request={user_id:target.id,kind,page:supportPage};
 if(kind==='messages')request.channel=$('support-choice').value;
 else if(!['channels','repositories'].includes(kind)){request.repository=$('support-choice').value;if(kind==='files')request.path=$('support-path').value;}
 $('support-content').textContent='Loading…';
 try{
  const data=await api('admin-content',request);if(supportTarget!==target)return;
  supportItems=data.items||[];supportMore=Boolean(data.more);$('support-content').replaceChildren();
  if(list){$('support-choice').replaceChildren(...supportItems.map(item=>{const o=usageNode('option',item.name);o.value=item.id||item.name;return o;}));$('support-choice').hidden=false;$('support-repo-kind').hidden=base!=='repositories';$('support-path').hidden=true;}
  if(data.text!==undefined)$('support-content').append(usageNode('pre',data.text));
  for(const item of supportItems){const article=usageNode('article');article.append(usageNode('strong',item.title||item.name||(item.human?'Human':'Agent')+' · '+new Date(item.at).toLocaleString()));if(item.text||item.body)article.append(usageNode('pre',item.text||item.body));if(kind==='files'&&item.path){const b=usageNode('button','Open','quiet');b.addEventListener('click',()=>{$('support-path').value=item.path;loadSupport(false).catch(error);});article.append(b);} $('support-content').append(article);}
  if(!supportItems.length&&data.text===undefined)$('support-content').textContent='No entries.';
  $('support-page').textContent='Page '+supportPage;$('support-newer').disabled=supportPage<=1;$('support-older').disabled=!supportMore;
 }catch(e){$('support-content').textContent=e.message;throw e;}
}
