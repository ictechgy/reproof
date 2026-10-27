import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { PlaybackReview } from './playback.js';
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject}; };
function setup() {
  let time=0; const calls=[];
  const context={id:'s',key:'s:1:recording:geometry',controllerId:'c',epoch:1,recordingId:'r',eligible:true,frame:{id:'f1',geometryVersion:1}};
  const response={plan:{token:'secret',actions:[{action:'text',payload:{text:'<private>'}}],verified:false,startingState:'unknown',expiresInSeconds:30}};
  let handler=async()=>response;
  const review=new PlaybackReview({context:()=>context,now:()=>time,send:(...args)=>{calls.push(args);return handler(...args);}});
  return {review,context,calls,response,setHandler:f=>handler=f,setTime:t=>time=t};
}
test('preview does not execute; explicit acknowledgment and current displayed frame required',async()=>{
 const x=setup(); await x.review.prepare({name:'private'}); assert.equal(x.calls.length,1); assert.equal(x.review.plan.actions[0].payload.text,'<private>');
 await x.review.confirm(false);assert.equal(x.calls.length,1);
 x.context.frame.id='f2'; await x.review.confirm(true);assert.equal(x.calls[1][1],'/playbacks/confirm');assert.equal(x.calls[1][2].frameId,'f2');assert.equal(x.calls[1][2].acknowledgeUnknownStart,true);
 await x.review.confirm(true);assert.equal(x.calls.length,2);
});
test('repeated preview clicks and duplicate confirmation send one request each',async()=>{
 const x=setup(),d=deferred();x.setHandler(()=>d.promise);
 const p=x.review.prepare({});await x.review.prepare({});assert.equal(x.calls.length,1);d.resolve(x.response);await p;
 const c=deferred();x.setHandler(()=>c.promise);const pending=x.review.confirm(true);await x.review.confirm(true);assert.equal(x.calls.length,2);c.resolve({});await pending;
});
for(const change of ['selection','session','epoch','sequence','geometry']) test(`stale ${change} invalidates preview and late response`,async()=>{
 const x=setup(),d=deferred();x.setHandler(()=>d.promise);const p=x.review.prepare({});x.context.key+=change;d.resolve(x.response);await p;assert.equal(x.review.plan,null);
 x.setHandler(async()=>x.response);await x.review.prepare({});x.context.key+='changed';await x.review.confirm(true);assert.equal(x.calls.length,2);
});
test('expired preview cannot execute; ordinary new frames do not invalidate',async()=>{
 const x=setup();await x.review.prepare({});x.context.frame.id='new';x.review.reconcile();assert.ok(x.review.plan);x.setTime(30000);await x.review.confirm(true);assert.equal(x.calls.length,1);assert.equal(x.review.plan,null);
});
test('explicit cancel consumes preview once, passive navigation sends no cancel write',async()=>{
 const x=setup();await x.review.prepare({});await x.review.cancel();await x.review.cancel();assert.equal(x.calls.length,2);assert.equal(x.calls[1][1],'/playbacks/cancel');
 await x.review.prepare({});x.review.invalidate();assert.equal(x.calls.length,3);await x.review.confirm(true);assert.equal(x.calls.length,3);
});
test('uncertain confirmation is not retried and old failures cannot affect new selection',async()=>{
 const x=setup();await x.review.prepare({});x.setHandler(async()=>{throw Error('uncertain');});await x.review.confirm(true);await x.review.confirm(true);assert.equal(x.calls.length,2);assert.match(x.review.error,/No retry/);
 const y=setup(),d=deferred();y.setHandler(()=>d.promise);const p=y.review.prepare({});y.context.key='other';d.reject(Error('old'));await p;assert.equal(y.review.error,'');
});
test('malformed or purported verified preview rejected',async()=>{
 const x=setup();x.response.plan.verified=true;await x.review.prepare({});assert.equal(x.review.plan,null);assert.match(x.review.error,/Invalid/);
});
test('UI wiring keeps separate controls, exact safe rendering, and explicit approval',()=>{
 const js=readFileSync(new URL('./app.js',import.meta.url),'utf8');const html=readFileSync(new URL('./index.html',import.meta.url),'utf8');
 assert.match(js, /playback-actions"\)\.textContent = p \? JSON.stringify/);
 assert.match(js,/confirm\(\$\("#playback-ack"\)\.checked, \$\("#playback-source-ack"\)\.checked\)/);
 for(const id of ['replay-start','playback-preview','playback-confirm','playback-ack','record-without-reset']) assert.ok(html.includes(`id="${id}"`));
 assert.match(html,/NOT verified.*QA/);assert.match(html,/starting state and result are unverified/);
});
test('cancel while confirmation is pending sends cancellation immediately and ignores late response', async()=>{
 const x=setup();await x.review.prepare({});const d=deferred();
 x.setHandler((id,path)=>path.endsWith('/confirm') ? d.promise : Promise.resolve({session:{id:'s'}}));
 const confirming=x.review.confirm(true);const cancel=x.review.cancel();await x.review.cancel();
 assert.equal(x.calls.length,3);assert.equal(x.calls[2][1],'/playbacks/cancel');await cancel;
 d.resolve({session:{id:'s',mode:'playback'}});assert.equal(await confirming,undefined);
});
test('late cancellation cannot adopt stale control or session state', async()=>{
 const x=setup();await x.review.prepare({});const d=deferred();x.setHandler(()=>d.promise);
 const pending=x.review.cancel();x.context.key='new-epoch';d.resolve({session:{id:'s',epoch:1}});assert.equal(await pending,undefined);
});

function reviewedResponse(x) {
 Object.assign(x.response.plan, {kind:'reviewed-untrusted-action-plan',planId:'new-plan',sourceTrust:'unverified',currentTarget:{deviceId:'synthetic',applicationIdentity:{bundle:'synthetic.demo'}}});
}
test('saved/imported data uses separate endpoint and requires additional untrusted-source acknowledgment',async()=>{
 const x=setup();reviewedResponse(x);await x.review.prepareReviewed({});
 assert.equal(x.calls[0][1],'/playbacks/reviewed-plan');assert.equal(x.calls.length,1);
 await x.review.confirm(true);assert.equal(x.calls.length,1);assert.ok(x.review.plan);
 await x.review.confirm(true,true);assert.equal(x.calls.length,2);
 assert.equal(x.calls[1][2].acknowledgeUntrustedSource,true);assert.equal(x.calls[1][2].acknowledgeUnknownStart,true);
 await x.review.confirm(true,true);assert.equal(x.calls.length,2);
});
test('current capture route cannot silently accept imported-source preview',async()=>{
 const x=setup();reviewedResponse(x);await x.review.prepare({});assert.equal(x.review.plan,null);assert.match(x.review.error,/separate saved-data path/);
});
for(const field of ['kind','planId','sourceTrust','currentTarget']) test(`reviewed-data preview missing ${field} is refused`,async()=>{
 const x=setup();reviewedResponse(x);delete x.response.plan[field];await x.review.prepareReviewed({});assert.equal(x.review.plan,null);assert.match(x.review.error,/Invalid untrusted-source preview/);
});
test('reviewed source selection/digest drift and expiry discard authorization',async()=>{
 const x=setup();reviewedResponse(x);await x.review.prepareReviewed({});x.context.key+=':changed-source';await x.review.confirm(true,true);assert.equal(x.calls.length,1);assert.equal(x.review.plan,null);
 await x.review.prepareReviewed({});x.setTime(30000);await x.review.confirm(true,true);assert.equal(x.calls.length,2);
});
test('new reviewed-source UI has explicit warning, separate entry and safe target rendering',()=>{
 const js=readFileSync(new URL('./app.js',import.meta.url),'utf8');const html=readFileSync(new URL('./index.html',import.meta.url),'utf8');
 for(const id of ['playback-reviewed-preview','playback-source-ack','playback-source-review','playback-target']) assert.ok(html.includes(`id="${id}"`));
 assert.match(js,/playback-target"\)\.textContent/);assert.match(js,/playbackReview.prepareReviewed\(variables\)/);
 assert.match(html,/source is untrusted/);assert.match(html,/NEW actions/);assert.match(html,/real app\/server data/);
});
