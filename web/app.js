'use strict';
const $=id=>document.getElementById(id);let user=null,busy=false,renderSignature=null,googleEnabled=false;
async function api(route,data){const response=await fetch('/api/'+route,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});const body=await response.json();if(!response.ok)throw Object.assign(new Error(body.error||'Request failed.'),{status:response.status});return body;}
function error(e){$('error').textContent=e.message;$('error').hidden=false;}
async function action(fn){if(busy)return;busy=true;$('error').hidden=true;try{await fn();await refresh();}catch(e){error(e);}finally{busy=false;}}
async function refresh(){try{user=await api('me');$('welcome').hidden=true;$('account').hidden=!user.activated;$('activation').hidden=user.activated;$('logout').hidden=false;if(!user.activated)return;$('name').textContent=user.name+"’s swarm";$('status').textContent=user.status.charAt(0).toUpperCase()+user.status.slice(1);$('status').dataset.state=user.status;$('status').setAttribute('aria-busy',String(['queued','provisioning','pausing'].includes(user.status)));$('admin').hidden=user.role!=='admin';$('google-account').hidden=!googleEnabled||user.google_connected;$('google-connect').disabled=Boolean(user.google_connected);$('google-connect').textContent=user.google_connected?'Google connected':'Connect Google';$('services').hidden=user.status!=='running';$('credentials-section').hidden=user.status!=='running';$('pause').hidden=user.status!=='running';$('start').textContent=user.status==='running'?'Extend session':user.status==='sleeping'?'Resume swarm':'Start swarm';$('start').disabled=['queued','provisioning','pausing'].includes(user.status)||!Object.values(user.providers).some(Boolean);$('machine-title').textContent=({new:'Ready when you are',queued:'Your swarm is queued',provisioning:'Preparing your swarm',running:'Your swarm is working',sleeping:'Your swarm is sleeping',pausing:'Saving your work',failed:'Your workspace needs attention'})[user.status]||user.status;$('machine-copy').textContent=user.stage||(user.status==='running'?'Session ends '+new Date(user.run_until*1000).toLocaleString():user.status==='sleeping'?'Everything is preserved. Resume when you’re ready.':'Add a provider key, then start a session.');$('notice').hidden=!user.error;$('notice').textContent=user.error||'';renderProviders();}catch(e){if(e.status===401){user=null;$('welcome').hidden=false;$('account').hidden=true;$('activation').hidden=true;$('logout').hidden=true;}else error(e);}}

const providerNames={openai:'OpenAI',anthropic:'Anthropic',deepseek:'DeepSeek'};
function renderProviders(){
  const paused=!['running','queued','provisioning','pausing'].includes(user.status);
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
  $('provider-pause-note').hidden=paused;
  if(!paused)$('provider-form').hidden=true;
}
$('add-provider').addEventListener('click',()=>{$('provider-form').hidden=false;$('provider').focus();});
$('cancel-provider').addEventListener('click',()=>{$('provider-form').hidden=true;$('provider-key').value='';});

$('login-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{await api('redeem',{code:$('invite-code').value.trim()});sessionStorage.removeItem('swarm-invite');$('invite-code').value='';renderSignature=null;});});
$('logout').addEventListener('click',()=>action(async()=>{await api('logout',{});renderSignature=null;$('credential-values').replaceChildren();$('invite-result').replaceChildren();}));
$('provider-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{await api('providers',{provider:$('provider').value,key:$('provider-key').value});$('provider-key').value='';$('provider-form').hidden=true;});});
$('start').addEventListener('click',()=>action(()=>api('start',{hours:Number($('duration').value)})));
$('pause').addEventListener('click',()=>action(()=>api('pause',{})));
for(const button of document.querySelectorAll('[data-service]'))button.addEventListener('click',()=>{const tab=window.open('about:blank','_blank');action(async()=>{try{const data=await api('open',{service:button.dataset.service});const url=new URL(data.url);if(url.protocol!=='https:'||!url.hostname.endsWith('.agent37.app'))throw new Error('Invalid service URL.');if(tab){tab.opener=null;tab.location.href=url.href;}else location.href=url.href;}catch(e){tab?.close();throw e;}});});
$('credentials').addEventListener('click',()=>action(async()=>{const data=await api('credentials',{});$('credential-values').replaceChildren();for(const [key,value]of Object.entries(data)){const p=document.createElement('p');p.textContent=key.replaceAll('_',' ')+': '+value;$('credential-values').append(p);}}));
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
