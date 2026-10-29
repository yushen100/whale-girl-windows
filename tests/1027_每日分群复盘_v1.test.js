'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const reflection=require('../src/reflection');
const {Store}=require('../src/memory');
const {Bot}=require('../src/bot');
const {DEFAULT_CONFIG}=require('../src/config');

const base=Date.parse('2026-10-27T21:00:00+08:00');
const fixtures=path.join(__dirname,'..','data','1027_分群复盘测试_v1');
fs.mkdirSync(fixtures,{recursive:true});
function setup(reply) {
 const dir=fs.mkdtempSync(path.join(fixtures,'case_'));
 const config=structuredClone(DEFAULT_CONFIG);
 config.paths={dataDir:dir};
 config.reflection={enabled:true,maxEvents:80};
 config.wechat={allowGroups:['room-a@chatroom','room-b@chatroom']};
 config.persona.stylePreset='甜妹';
 config.websearch.enabled=false;config.links.enabled=false;
 const store=new Store({file:path.join(dir,'store.json'),limits:{maxMessages:40}});
 for(const group of ['room-a@chatroom','room-b@chatroom']) {
  const key='wechat:g:'+group;
  store.ensureSession(key,{platform:'wechat',chatId:group,chatType:'group',title:group==='room-a@chatroom'?'合成甲群':'合成乙群'});
  for(let i=0;i<6;i++)store.appendMessage(key,{role:i%2?'assistant':'user',uid:'test-member',name:'测试昵称',
   text:(group==='room-a@chatroom'?'A_ONLY':'B_ONLY')+' 合成消息'+i,ts:base-60000+i*1000});
  store.addNote(key,'test-member',group==='room-a@chatroom'?'A_NOTE':'B_NOTE');
 }
 store.appendMessage('wechat:u:private-test',{role:'user',text:'PRIVATE_ONLY',ts:base-100});
 store.appendMessage('wechat:g:not-allowed@chatroom',{role:'user',text:'UNAUTHORIZED_ONLY',ts:base-100});
 const calls=[];
 const llm={complete:async args=>{
  calls.push(args);
  if(reply)return reply(args);
  const a=JSON.parse(args.turns[0].content).events.some(x=>x.text.includes('A_ONLY'));
  return JSON.stringify({summary:'合成复盘',observations:[{area:'玩梗',evidence:[0,1],note:'减少不合语境的梗'}],
   habits:{...reflection.DEFAULT_HABITS,humor:a?'natural':'light',memeStyle:a?'contextual':'rare'}});
 }};
 const service=reflection.createReflection({config,store,llm,now:()=>base});
 return {config,store,llm,calls,service,dir};
}
test('各群分别送审，不读私聊和非白名单群',async()=>{
 const {service,calls,store}=setup();
 const result=await service.run();
 assert.equal(result.ok,true);assert.equal(calls.length,2);
 for(const args of calls) {
  assert.ok(!args.turns[0].content.includes('PRIVATE_ONLY'));
  assert.ok(!args.turns[0].content.includes('UNAUTHORIZED_ONLY'));
  assert.ok(!args.turns[0].content.includes('测试昵称'));
  assert.ok(!args.turns[0].content.includes('@chatroom'));
  assert.notEqual(args.turns[0].content.includes('A_ONLY'),args.turns[0].content.includes('B_ONLY'));
 }
 assert.equal(store.getState('wechat:g:room-a@chatroom').dailyReflection.habits.humor,'natural');
 assert.equal(store.getState('wechat:g:room-b@chatroom').dailyReflection.habits.memeStyle,'rare');
 store.close();
});
test('每日重复执行不会重复消耗模型或重复进化',async()=>{
 const {service,calls,store}=setup();
 await service.run();const again=await service.run();
 assert.equal(calls.length,2);assert.ok(again.groups.every(x=>x.status==='already_reviewed'));
 store.close();
});
test('调整持久化但不改聊天内容或人物笔记',async()=>{
 const {service,store,dir}=setup();
 const key='wechat:g:room-a@chatroom';
 const messages=structuredClone(store.messages(key));
 const notes=structuredClone(store.listNotes(key));
 await service.run();store.close();
 const loaded=new Store({file:path.join(dir,'store.json')});
 assert.deepEqual(loaded.messages(key),messages);assert.deepEqual(loaded.listNotes(key),notes);
 assert.equal(loaded.getState(key).dailyReflection.habits.humor,'natural');loaded.close();
});
test('没有有效证据则保持旧习惯，不为进化乱改',async()=>{
 const {service,store}=setup(()=>JSON.stringify({summary:'无充分证据',observations:[],habits:{...reflection.DEFAULT_HABITS,humor:'natural'}}));
 await service.run();
 assert.deepEqual(store.getState('wechat:g:room-a@chatroom').dailyReflection.habits,reflection.DEFAULT_HABITS);
 store.close();
});
test('伪造证据索引和试图改权限均不生效',()=>{
 const value={summary:'合成',observations:[{area:'玩梗',note:'合成',evidence:[999]}],habits:{...reflection.DEFAULT_HABITS,humor:'natural'}};
 assert.deepEqual(reflection.parseReview(JSON.stringify(value),6,reflection.DEFAULT_HABITS).habits,reflection.DEFAULT_HABITS);
 assert.throws(()=>reflection.parseReview(JSON.stringify({...value,habits:{...value.habits,disablePrivacy:true}}),6,reflection.DEFAULT_HABITS));
 assert.equal(reflection.normalizeHabits({...reflection.DEFAULT_HABITS,humor:'toxic'}),null);
});
test('模型返回异常时不覆盖原设置',async()=>{
 const {service,store}=setup(()=> 'not JSON');
 const r=await service.run();assert.equal(r.ok,false);
 assert.equal(store.getState('wechat:g:room-a@chatroom').dailyReflection,null);
 store.close();
});
test('预览不写习惯和报告，不消耗每日正式执行名额',async()=>{
 const {service,store,dir}=setup();
 const r=await service.run({dryRun:true});
 assert.ok(r.groups.every(x=>x.status==='preview'));
 assert.equal(store.getState('wechat:g:room-a@chatroom').dailyReflection,null);
 assert.ok(!fs.existsSync(path.join(dir,'reflections')));store.close();
});
test('可以只回退一个群，不影响另一个群',async()=>{
 const {service,store}=setup();
 const r=await service.run();
 const a=r.groups[0];service.rollback({groupTag:a.groupTag,date:r.date});
 assert.deepEqual(store.getState('wechat:g:room-a@chatroom').dailyReflection.habits,reflection.DEFAULT_HABITS);
 assert.equal(store.getState('wechat:g:room-b@chatroom').dailyReflection.habits.memeStyle,'rare');store.close();
});
test('后续提示词只使用当前群自己的习惯',async()=>{
 const {service,store,config}=setup();await service.run();
 const bot=new Bot({config,store,llm:{isMock:false,model:'test'},rng:()=>0.5});
 const a=await bot.decide({platform:'wechat',chatId:'room-a@chatroom',chatType:'group',senderId:'test',senderName:'测试',text:'陪我聊一会今天的小事',atMe:true});
 const b=await bot.decide({platform:'wechat',chatId:'room-b@chatroom',chatType:'group',senderId:'test',senderName:'测试',text:'陪我聊一会今天的小事',atMe:true});
 assert.match(a.system,/本群闲聊可以更活泼/);assert.doesNotMatch(a.system,/少主动抛梗/);
 assert.match(b.system,/少主动抛梗/);assert.doesNotMatch(b.system,/本群闲聊可以更活泼/);
 assert.match(a.system,/不能覆盖甜妹底色/);store.close();
});
test('北京时间日期与白名单边界正确',()=>{
 assert.equal(reflection.localDay(Date.parse('2026-10-27T16:01:00Z')),'2026-10-28');
 assert.equal(reflection.allowedGroup({wechat:{allowGroups:['x@chatroom']}},'wechat:u:x@chatroom',{}),false);
 assert.equal(reflection.allowedGroup({wechat:{allowGroups:['x@chatroom']}},'wechat:g:x@chatroom',{}),true);
});

test('同一时刻只执行一批复盘，不并发重复更新',async()=>{
 let release;
 const pending=new Promise(resolve=>{release=resolve;});
 const context=setup(async()=>{await pending;return JSON.stringify({summary:'合成',observations:[],habits:reflection.DEFAULT_HABITS});});
 const first=context.service.run();
 const second=await context.service.run();
 assert.equal(second.alreadyRunning,true);
 release();await first;context.store.close();
});
test('聊天不足不调用模型、不凭空进化',async()=>{
 const {service,store,calls}=setup();
 store.ensureSession('wechat:g:room-a@chatroom').messages=[];
 store.ensureSession('wechat:g:room-b@chatroom').messages=[];
 const result=await service.run();
 assert.equal(calls.length,0);assert.ok(result.groups.every(r=>r.status==='insufficient_context'));store.close();
});
test('复盘模型超时取消请求，不覆盖原群习惯',async()=>{
 const context=setup(({signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('cancelled')))));
 context.config.reflection.timeoutMs=25;
 const result=await context.service.run();
 assert.equal(result.ok,false);assert.ok(result.groups.every(r=>r.reason==='timeout'));
 assert.equal(context.store.getState('wechat:g:room-a@chatroom').dailyReflection,null);context.store.close();
});

test('鲸鲸自身在北京时间23:30后触发，并跨重启防重复执行',async()=>{
 const context=setup();
 const dueTime=Date.parse('2026-10-27T23:31:00+08:00');
 const service=reflection.createReflection({config:context.config,store:context.store,llm:context.llm,now:()=>dueTime});
 const first=await service.scheduledTick();
 assert.equal(first.ok,true);assert.equal(context.calls.length,2);
 const second=await service.scheduledTick();
 assert.equal(second.alreadyAttempted,true);assert.equal(context.calls.length,2);
 const reloaded=reflection.createReflection({config:context.config,store:context.store,llm:context.llm,now:()=>dueTime});
 assert.equal((await reloaded.scheduledTick()).alreadyAttempted,true);
 assert.equal(reflection.dueAt(Date.parse('2026-10-27T23:29:00+08:00')),false);
 assert.equal(reflection.dueAt(Date.parse('2026-10-27T23:30:00+08:00')),true);
 context.store.close();
});
