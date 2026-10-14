const routes=new Set(['login','logout','me','providers','start','pause','invite','open','credentials']);
export default async function handler(req,res){
  res.setHeader('Cache-Control','no-store');
  const route=req.query.route;
  if(!routes.has(route))return res.status(404).json({error:'Not found.'});
  if(req.method!==(route==='me'?'GET':'POST'))return res.status(405).json({error:'Method not allowed.'});
  if(req.method==='POST'&&req.headers.origin!=='https://'+req.headers.host)return res.status(403).json({error:'Origin rejected.'});
  if(!process.env.CONTROL_PLANE_URL||!process.env.CONTROL_PLANE_KEY)return res.status(503).json({error:'The beta is being prepared. Please check back shortly.'});
  let session='';
  for(const part of (req.headers.cookie||'').split(';')){const [name,...value]=part.trim().split('=');if(name==='__Host-swarm_session')session=value.join('=');}
  const headers={'Content-Type':'application/json','X-Swarm-Service-Key':process.env.CONTROL_PLANE_KEY,'X-Swarm-Session':session,'X-Swarm-Client':req.headers['x-real-ip']||'unknown'};
  try{
    const response=await fetch(process.env.CONTROL_PLANE_URL+'/api/'+route,{method:req.method,headers,body:req.method==='POST'?JSON.stringify(req.body||{}):undefined,signal:AbortSignal.timeout(55000)});
    const data=await response.json();
    if(response.ok&&route==='login'){
      res.setHeader('Set-Cookie','__Host-swarm_session='+data.session+'; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000');delete data.session;
    }
    if(route==='logout')res.setHeader('Set-Cookie','__Host-swarm_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0');
    return res.status(response.status).json(data);
  }catch{return res.status(503).json({error:'The hosting service is temporarily unavailable.'});}
}
