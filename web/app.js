'use strict';
const $=id=>document.getElementById(id);let user=null,busy=false,catalog=null,catalogSignature=null,renderSignature=null;
async function api(route,data){const response=await fetch('/api/'+route,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});const body=await response.json();if(!response.ok)throw Object.assign(new Error(body.error||'Request failed.'),{status:response.status});return body;}
function error(e){$('error').textContent=e.message;$('error').hidden=false;}
async function action(fn){if(busy)return;busy=true;$('error').hidden=true;try{await fn();await refresh();}catch(e){error(e);}finally{busy=false;}}
async function refresh(){try{user=await api('me');$('welcome').hidden=true;$('account').hidden=false;$('logout').hidden=false;$('name').textContent=user.name+"’s swarm";$('status').textContent=user.status;$('admin').hidden=user.role!=='admin';$('services').hidden=user.status!=='running';$('credentials-section').hidden=user.status!=='running';$('pause').hidden=user.status!=='running';$('start').textContent=user.status==='running'?'Extend session':user.status==='sleeping'?'Resume swarm':'Start swarm';$('start').disabled=['queued','provisioning','pausing'].includes(user.status)||!Object.values(user.providers).some(Boolean);$('machine-title').textContent=({new:'Ready when you are',queued:'Your swarm is queued',provisioning:'Preparing your swarm',running:'Your swarm is working',sleeping:'Your swarm is sleeping',pausing:'Saving your work',failed:'Your workspace needs attention'})[user.status]||user.status;$('machine-copy').textContent=user.stage||(user.status==='running'?'Session ends '+new Date(user.run_until*1000).toLocaleString():user.status==='sleeping'?'Everything is preserved. Resume when you’re ready.':'Add a provider key, then start a session.');$('notice').hidden=!user.error;$('notice').textContent=user.error||'';await refreshProviders();}catch(e){if(e.status===401){user=null;$('welcome').hidden=false;$('account').hidden=true;$('logout').hidden=true;}else error(e);}}

const providerNames={openai:'OpenAI',anthropic:'Anthropic',deepseek:'DeepSeek'};
function renderProviders(){
  const paused=!['running','queued','provisioning','pausing'].includes(user.status);
  const signature=JSON.stringify([user.provider_keys,user.providers,paused,catalog]);
  if(renderSignature===signature)return;renderSignature=signature;
  $('provider-list').replaceChildren();
  for(const [provider,enabled] of Object.entries(user.providers)){
    if(!enabled)continue;
    const row=document.createElement('div');row.className='provider-row';
    const heading=document.createElement('div');heading.className='provider-heading';
    const name=document.createElement('strong');name.textContent=providerNames[provider];
    const key=document.createElement('code');key.className='provider-key';key.textContent=user.provider_keys[provider];
    const remove=document.createElement('button');remove.type='button';remove.className='delete-provider';remove.textContent='Delete';remove.setAttribute('aria-label','Delete '+providerNames[provider]+' key');remove.disabled=!paused;
    remove.addEventListener('click',()=>action(async()=>{await api('providers',{provider,key:''});catalogSignature=null;}));
    heading.append(name,key,remove);row.append(heading);
    const models=document.createElement('div');models.className='model-list';
    for(const model of catalog?.models||[]){if(model.provider!==provider)continue;const label=document.createElement('span');label.textContent=model.api_id;models.append(label);}
    if(!models.childElementCount){const note=document.createElement('span');note.className='model-note';note.textContent=catalog===null?'Loading models…':catalog.providers[provider]?.status==='unavailable'?'Model list unavailable':'No models returned';models.append(note);}
    row.append(models);$('provider-list').append(row);
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
async function refreshProviders(){
  const signature=JSON.stringify(user.provider_keys);
  if(catalogSignature!==signature){
    catalogSignature=signature;catalog=null;renderProviders();
    try{const data=await api('models');if(catalogSignature===signature)catalog=data;}
    catch{if(catalogSignature===signature)catalog={models:[],providers:Object.fromEntries(Object.keys(user.provider_keys).map(p=>[p,{status:'unavailable'}]))};}
  }
  renderProviders();
}
$('add-provider').addEventListener('click',()=>{$('provider-form').hidden=false;$('provider').focus();});
$('cancel-provider').addEventListener('click',()=>{$('provider-form').hidden=true;$('provider-key').value='';});

$('login-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{await api('login',{code:$('invite-code').value.trim()});$('invite-code').value='';});});
$('logout').addEventListener('click',()=>action(async()=>{await api('logout',{});catalog=null;catalogSignature=null;renderSignature=null;$('credential-values').replaceChildren();$('invite-result').replaceChildren();}));
$('provider-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{await api('providers',{provider:$('provider').value,key:$('provider-key').value});$('provider-key').value='';$('provider-form').hidden=true;catalogSignature=null;});});
$('start').addEventListener('click',()=>action(()=>api('start',{hours:Number($('duration').value)})));
$('pause').addEventListener('click',()=>action(()=>api('pause',{})));
for(const button of document.querySelectorAll('[data-service]'))button.addEventListener('click',()=>{const tab=window.open('about:blank','_blank');action(async()=>{try{const data=await api('open',{service:button.dataset.service});const url=new URL(data.url);if(url.protocol!=='https:'||!url.hostname.endsWith('.agent37.app'))throw new Error('Invalid service URL.');if(tab){tab.opener=null;tab.location.href=url.href;}else location.href=url.href;}catch(e){tab?.close();throw e;}});});
$('credentials').addEventListener('click',()=>action(async()=>{const data=await api('credentials',{});$('credential-values').replaceChildren();for(const [key,value]of Object.entries(data)){const p=document.createElement('p');p.textContent=key.replaceAll('_',' ')+': '+value;$('credential-values').append(p);}}));
$('invite-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{const data=await api('invite',{name:$('friend-name').value});$('invite-result').replaceChildren();const input=document.createElement('input');input.readOnly=true;input.value=data.url;input.addEventListener('click',()=>input.select());const note=document.createElement('p');note.className='small';note.textContent='This link grants access to their workspace. Share it privately.';$('invite-result').append(input,note);});});
const params=new URLSearchParams(location.hash.slice(1)),code=params.get('invite');if(code){history.replaceState(null,'',location.pathname);$('invite-code').value=code;action(()=>api('login',{code}));}else refresh();setInterval(()=>{if(user&&!busy)refresh();},5000);
