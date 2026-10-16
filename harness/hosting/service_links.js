'use strict';
// Keep previously posted cross-service links usable without exposing access tickets.
function rewriteServiceLinks(root){
 const links=root.matches?.('a[href]')?[root]:[...root.querySelectorAll('a[href]')];
 for(const link of links){
  let url;try{url=new URL(link.href,location.href);}catch{continue;}
  if(url.origin===location.origin)continue;
  const service=swarmServices.find(entry=>entry.origin===url.origin);if(!service)continue;
  const params=new URLSearchParams({open:service.name,instance:service.instance,path:url.pathname+url.search+url.hash});
  link.href='https://swarm.forum/#'+params;
 }
}
rewriteServiceLinks(document);
new MutationObserver(records=>{for(const record of records){
 if(record.type==='attributes')rewriteServiceLinks(record.target);
 else for(const node of record.addedNodes)if(node.nodeType===1)rewriteServiceLinks(node);
}}).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['href']});
