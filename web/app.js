'use strict';
const $=id=>document.getElementById(id);let user=null,busy=false,renderSignature=null,googleEnabled=false,lastWorkspaceStatus=null,guideKey=null,adminSignature=null;
async function api(route,data){const response=await fetch('/api/'+route,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});const body=await response.json();if(!response.ok)throw Object.assign(new Error(body.error||'Request failed.'),{status:response.status});return body;}
function error(e){$('error').textContent=e.message;$('error').hidden=false;}
async function action(fn){if(busy)return;busy=true;$('error').hidden=true;try{await fn();await refresh();}catch(e){error(e);}finally{busy=false;}}
function showScreen(){const settings=location.hash==='#settings'&&user?.activated;$('settings-screen').hidden=!settings;$('dashboard-screen').hidden=settings;}
function openSettings(expand=false){location.hash='settings';showScreen();if(expand){$('inference-providers').open=true;$('inference-providers').scrollIntoView({block:'start'});}}
window.addEventListener('hashchange',showScreen);
$('settings-open').addEventListener('click',()=>openSettings());
$('settings-back').addEventListener('click',()=>{location.hash='';showScreen();});
$('set-provider').addEventListener('click',()=>openSettings(true));
window.addEventListener('message',event=>{if(event.origin===location.origin&&event.source===$('analytics-frame').contentWindow&&event.data?.type==='swarm-analytics-height'){const height=Number(event.data.height);if(Number.isFinite(height))$('analytics-frame').style.height=Math.max(500,Math.min(height+8,20000))+'px';}});
$('analytics-frame').addEventListener('load',()=>{$('analytics-loading').hidden=true;});
window.addEventListener('message',event=>{if(event.origin===location.origin&&event.source===$('analytics-frame').contentWindow&&event.data?.type==='swarm-workspace-status'&&typeof event.data.status==='string'){const status=event.data.status;$('activity-status').textContent=status.charAt(0).toUpperCase()+status.slice(1);$('activity-status').dataset.state=status;$('activity-status').hidden=user?.status!=='running';}});

for(const id of ['guide-close','guide-done'])$(id).addEventListener('click',()=>$('swarm-guide').close());
$('swarm-guide').addEventListener('close',()=>{if(guideKey)try{localStorage.setItem(guideKey,'dismissed');}catch{}});

async function refresh(){try{
 user=await api('me');$('welcome').hidden=true;$('account').hidden=!user.activated;$('activation').hidden=user.activated;$('settings-open').hidden=!user.activated;document.querySelector('main').classList.toggle('is-account',user.activated);if(!user.activated)return;
 const running=user.status==='running',transitioning=['queued','provisioning','pausing','stopping'].includes(user.status),hasProviders=Object.values(user.providers).some(Boolean);
 $('name').textContent=user.name+"’s swarm";$('status').textContent=({sleeping:'Paused',new:'Ready',queued:'Starting',provisioning:'Provisioning',stopping:'Stopping'})[user.status]||user.status.charAt(0).toUpperCase()+user.status.slice(1);$('status').dataset.state=user.status;$('status').setAttribute('aria-busy',String(transitioning));
 $('admin').hidden=user.role!=='admin';$('google-account').hidden=!googleEnabled||user.google_connected;$('google-connect').disabled=Boolean(user.google_connected);$('google-connect').textContent=user.google_connected?'Google connected':'Connect Google';
 $('start').hidden=running;$('start').disabled=transitioning||!hasProviders;$('pause').hidden=!running;$('stop').disabled=transitioning||['new','stopped'].includes(user.status);$('duration').disabled=transitioning;
 $('session-until').hidden=!running;$('session-until').textContent=running?'(Until '+new Intl.DateTimeFormat('en-US',{hour:'numeric',minute:'2-digit',hour12:true}).format(new Date(user.run_until*1000)).replace(':','.')+')':'';$('provider-warning').hidden=hasProviders;
 for(const button of document.querySelectorAll('[data-service]'))button.disabled=!running;
 $('notice').hidden=!user.error;$('notice').textContent=user.error||'';
 $('workspace-empty').hidden=running;$('workspace-empty').textContent=transitioning?'':user.status==='stopped'?'Press Play to start a fresh swarm.':user.status==='sleeping'?'Press Play to continue your swarm.':'Press Play to start your swarm.';
 const frame=$('analytics-frame');frame.hidden=!running;if(!running)$('activity-status').hidden=true;if(running&&!frame.getAttribute('src')){$('analytics-loading').hidden=false;frame.src='/api/analytics?resource=page';}else if(!running&&frame.getAttribute('src')){frame.removeAttribute('src');$('analytics-loading').hidden=true;}
 renderProviders();renderAdminKeys();showScreen();
 if(running){guideKey='swarm-guide:'+user.id+':'+(user.instance_id||user.id);let seen=false;try{seen=localStorage.getItem(guideKey)==='dismissed';}catch{}if((!seen||['queued','provisioning'].includes(lastWorkspaceStatus))&&!$('swarm-guide').open)$('swarm-guide').showModal();}
 lastWorkspaceStatus=user.status;
 }catch(e){if(e.status===401){user=null;lastWorkspaceStatus=null;$('swarm-guide').close();$('welcome').hidden=false;$('account').hidden=true;$('activation').hidden=true;$('settings-open').hidden=true;$('analytics-frame').removeAttribute('src');document.querySelector('main').classList.remove('is-account');}else error(e);}}

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
$('start').addEventListener('click',()=>action(()=>api('start',{hours:Number($('duration').value)})));
$('pause').addEventListener('click',()=>action(()=>api('pause',{})));
$('duration').addEventListener('change',()=>{if(user?.status==='running')action(()=>api('start',{hours:Number($('duration').value)}));});
$('stop').addEventListener('click',()=>$('stop-confirm').showModal());
$('stop-cancel').addEventListener('click',()=>$('stop-confirm').close());
$('stop-confirmed').addEventListener('click',()=>{if(busy)return;$('stop-confirm').close();action(()=>api('stop',{}));});
function openService(service,path=''){if(busy)return;const tab=window.open('about:blank','_blank');action(async()=>{try{const data=await api('open',{service,path});const url=new URL(data.url);if(url.protocol!=='https:'||!url.hostname.endsWith('.agent37.app'))throw new Error('Invalid service URL.');if(tab){tab.opener=null;tab.location.href=url.href;}else location.href=url.href;}catch(e){tab?.close();throw e;}});}
for(const button of document.querySelectorAll('[data-service]'))button.addEventListener('click',()=>openService(button.dataset.service));
window.addEventListener('message',event=>{if(event.origin===location.origin&&event.source===$('analytics-frame').contentWindow&&event.data?.type==='swarm-open-service'&&['mattermost','gitea','opencode'].includes(event.data.service)&&typeof event.data.path==='string')openService(event.data.service,event.data.path);});
$('invite-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{const data=await api('invite',{name:$('friend-name').value});$('invite-result').replaceChildren();const input=document.createElement('input');input.readOnly=true;input.value=data.url;input.addEventListener('click',()=>input.select());const note=document.createElement('p');note.className='small';note.textContent='This link grants access to their workspace. Share it privately.';$('invite-result').append(input,note);});});
const params=new URLSearchParams(location.hash.slice(1)),code=params.get('invite');
if(code){sessionStorage.setItem('swarm-invite',code);history.replaceState(null,'',location.pathname);}
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
