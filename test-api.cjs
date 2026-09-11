const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/static/hub.js','utf8');
const apiSource=source.slice(source.indexOf('async function api('),source.indexOf('\nfunction auth()'));
async function scenario(responses,expectedError){
 const calls=[];const context=vm.createContext({FormData,JSON,Error,csrf:'old',user:{username:'reader'},fetch:async(path,options)=>{calls.push({path,headers:{...options.headers},body:options.body});const next=responses.shift();if(next instanceof Error)throw next;return{status:next.status,ok:next.status<400,json:async()=>next.body}}});
 vm.runInContext(apiSource,context);const result=context.api('/api/posts',{method:'POST',body:{title:'article'},headers:{'Idempotency-Key':'same-key'}});
 if(expectedError)await assert.rejects(result,expectedError);else await result;return calls;
}
(async()=>{
 let calls=await scenario([{status:403,body:{code:'csrf_expired'}},{status:200,body:{csrf:'fresh',user:{username:'reader'}}},{status:200,body:{id:1}}]);
 assert.equal(calls.length,3);assert.equal(calls[0].body,calls[2].body);assert.equal(calls[2].headers['X-CSRF-Token'],'fresh');assert.equal(calls[2].headers['Idempotency-Key'],'same-key');
 calls=await scenario([{status:403,body:{code:'csrf_expired'}},{status:200,body:{csrf:'fresh',user:{username:'other'}}}],/Аккаунт изменился/);assert.equal(calls.length,2);
 calls=await scenario([{status:403,body:{error:'muted'}}],/muted/);assert.equal(calls.length,1);
 calls=await scenario([new Error('offline')],/Нет соединения/);assert.equal(calls.length,1);
 console.log('4 API client recovery scenarios passed');
})().catch(e=>{console.error(e);process.exitCode=1});
