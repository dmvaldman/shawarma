const routes=new Set(['admin-usage','admin-invite','admin-content','support-access','admin-keys','analytics','dashboard-connect','stop','redeem','login','logout','me','providers','start','pause','invite','open','credentials','models','auth-config','google-start','google-callback']);
const sessionCookie=value=>'__Host-swarm_session='+value+'; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000';
const clearOAuth='__Host-swarm_oauth=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0';
export default async function handler(req,res){
  res.setHeader('Cache-Control','no-store');
  res.setHeader('Referrer-Policy','no-referrer');
  const route=req.query.route;
  if(!routes.has(route))return res.status(404).json({error:'Not found.'});
  if(req.method!==(route==='support-access'?req.method:['admin-usage','analytics','me','models','auth-config','google-callback'].includes(route)?'GET':'POST'))return res.status(405).json({error:'Method not allowed.'});
  if(!['GET','POST'].includes(req.method))return res.status(405).json({error:'Method not allowed.'});
  if(req.method==='POST'&&req.headers.origin!=='https://'+req.headers.host)return res.status(403).json({error:'Origin rejected.'});
  if(!process.env.CONTROL_PLANE_URL||!process.env.CONTROL_PLANE_KEY)return res.status(503).json({error:'The beta is being prepared. Please check back shortly.'});
  const cookies={};
  for(const part of (req.headers.cookie||'').split(';')){const [name,...value]=part.trim().split('=');cookies[name]=value.join('=');}
  const headers={'Content-Type':'application/json','X-Swarm-Service-Key':process.env.CONTROL_PLANE_KEY,'X-Swarm-Session':cookies['__Host-swarm_session']||'','X-Swarm-Client':req.headers['x-real-ip']||'unknown'};
  try{
    if(route==='google-callback'){
      res.setHeader('Set-Cookie',clearOAuth);
      if(req.query.error||!cookies['__Host-swarm_oauth'])return res.redirect(302,'/?signin_error=google');
      const response=await fetch(process.env.CONTROL_PLANE_URL+'/api/google-complete',{method:'POST',headers,body:JSON.stringify({state:req.query.state,code:req.query.code,browser_token:cookies['__Host-swarm_oauth']}),signal:AbortSignal.timeout(55000)});
      if(!response.ok)return res.redirect(302,'/?signin_error='+(response.status===403?'invite':'google'));
      const data=await response.json();
      res.setHeader('Set-Cookie',[sessionCookie(data.session),clearOAuth]);
      return res.redirect(302,'/');
    }
    const query=new URLSearchParams();if(route==='analytics')for(const key of ['resource','agent','limit','before'])if(typeof req.query[key]==='string')query.set(key,req.query[key]);
    const response=await fetch(process.env.CONTROL_PLANE_URL+'/api/'+route+(query.size?'?'+query:''),{method:req.method,headers,body:req.method==='POST'?JSON.stringify(req.body||{}):undefined,signal:AbortSignal.timeout(55000)});
    if(response.ok&&route==='analytics'){
      res.setHeader('Content-Type',response.headers.get('Content-Type'));
      const content=Buffer.from(await response.arrayBuffer());
      if(response.headers.get('Content-Type')?.startsWith('text/html')){
        const nonce=content.toString('utf8').match(/<script nonce="([A-Za-z0-9_-]{24,80})">/)?.[1];
        if(!nonce)return res.status(502).json({error:'Analytics is temporarily unavailable.'});
        const control=new URL(process.env.CONTROL_PLANE_URL);
        const socketOrigin='wss://'+control.host;
        res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'nonce-"+nonce+"'; style-src 'self' 'unsafe-inline'; connect-src 'self' "+socketOrigin+"; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'");
      }
      return res.status(200).send(content);
    }
    const data=await response.json();
    if(response.ok&&route==='login'){
      res.setHeader('Set-Cookie',sessionCookie(data.session));delete data.session;
    }
    if(response.ok&&route==='google-start'){
      res.setHeader('Set-Cookie','__Host-swarm_oauth='+data.browser_token+'; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=600');delete data.browser_token;
    }
    if(route==='logout')res.setHeader('Set-Cookie','__Host-swarm_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0');
    return res.status(response.status).json(data);
  }catch{
    if(route==='google-callback')return res.redirect(302,'/?signin_error=google');
    return res.status(503).json({error:'The hosting service is temporarily unavailable.'});
  }
}
