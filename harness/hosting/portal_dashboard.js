function analyticsURL(path){
  const url=new URL(path,location.origin), parts=url.pathname.split('/');
  const query=new URLSearchParams(url.search);
  query.set('resource',parts[4]==='avatar'?'avatar':'activity');
  query.set('agent',parts[3]);
  return '/api/analytics?'+query;
}
async function portalSocketURL(){
  try{
    const response=await fetch('/api/dashboard-connect',{
      method:'POST',headers:{'Content-Type':'application/json'},body:'{}'
    });
    if(!response.ok)throw new Error();
    return (await response.json()).url;
  }catch{return null;}
}
new ResizeObserver(()=>parent.postMessage({
  type:'swarm-analytics-height',height:document.body.scrollHeight
},location.origin)).observe(document.body);

document.addEventListener('click',event=>{
  const link=event.target.closest('a');if(!link?.href||!latest?.services)return;
  const url=new URL(link.href);
  for(const service of ['mattermost','gitea','opencode']){
    const base=latest.services[service];if(!base||new URL(base).origin!==url.origin)continue;
    event.preventDefault();parent.postMessage({type:'swarm-open-service',service,
      path:url.pathname+url.search+url.hash},location.origin);break;
  }
});
