import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// Exercise the production request/refresh functions without real credentials or a browser.
const source=readFileSync(new URL('../../web/app.js',import.meta.url),'utf8');
function portal(){
 const nodes=new Map();
 const node=id=>{if(!nodes.has(id))nodes.set(id,{hidden:true,textContent:'',dataset:{},
  addEventListener(){},classList:{toggle(){}}});return nodes.get(id);};
 const context=vm.createContext({
  document:{getElementById:node,querySelector:()=>node('main'),addEventListener(){}},
  window:{addEventListener(){}},location:{hash:'#settings'},
  fetch:async()=>({ok:true,json:async()=>({activated:false})}),
 });
 vm.runInContext(source.slice(0,source.indexOf('\nconst providerNames=')),context);
 return {context,error:node('error'),run:code=>vm.runInContext(code,context)};
}

test('an offline account poll clears its banner after a successful reconnect',async()=>{
 const p=portal();p.context.fetch=async()=>{throw new TypeError('Failed to fetch');};
 await p.run('refresh()');
 assert.equal(p.error.hidden,false);
 assert.equal(p.error.textContent,'Connection lost. Reconnecting…');
 p.context.fetch=async()=>({ok:true,json:async()=>({activated:false})});
 await p.run('refresh()');
 assert.equal(p.error.hidden,true);
 assert.equal(p.error.textContent,'');
});

test('successful background polls do not erase failed settings actions',async()=>{
 const p=portal();
 p.run("error(new Error('Provider rejected this key.'))");
 await p.run('refresh()');
 assert.equal(p.error.hidden,false);
 assert.equal(p.error.textContent,'Provider rejected this key.');
});

test('offline writes are not retried and retain an actionable error',async()=>{
 const p=portal();let calls=0;
 p.context.fetch=async()=>{calls++;throw new TypeError('Failed to fetch');};
 await p.run("action(()=>api('providers',{provider:'openai',key:'test-only'}))");
 assert.equal(calls,1);
 assert.equal(p.error.textContent,'Unable to connect. Check your internet connection and try again.');
 assert.equal(p.error.dataset.source,'action');
});
