 'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),path=require('node:path');
const persona=require('../src/persona');const {DEFAULT_CONFIG}=require('../src/config');
const {Bot}=require('../src/bot');const {Store}=require('../src/memory');
const ids=['9000000004@chatroom','9000000001@chatroom'];
function config(){const c=structuredClone(DEFAULT_CONFIG);Object.assign(c.persona,{name:'大肥鱼',stylePreset:'甜妹',customPrompt:'旧身份：网上游荡的大肥鱼'});
 c.wechat={groupPersonas:Object.fromEntries(ids.map(id=>[id,{name:'示例身份9',identity:'正在学习网络安全的大胖鱼',task:'主要任务是回答本群提问，网络安全和技术问题认真解释，不确定先脱敏查证',maxChars:600}]))};
 c.links.enabled=false;c.websearch.enabled=false;return c;}
function msg(id=ids[0]){return {platform:'wechat',chatType:'group',chatId:id,senderId:'fake-user',senderName:'合成群友',text:'示例身份9，解释TLS',msgId:'synthetic-'+id};}
test('两个目标群都采用局部名字身份和答疑任务',()=>{
 const c=config(),before=JSON.stringify(c);
 for(const id of ids){const system=persona.buildSystem({config:c,msg:msg(id)});
 assert.match(system,/名字：示例身份9/);assert.match(system,/正在学习网络安全/);assert.match(system,/主要任务是回答本群提问/);assert.match(system,/600字以内/);
 assert.doesNotMatch(system,/旧身份：网上游荡/);}
 assert.equal(JSON.stringify(c),before);
});
test('其他群、私聊及其他平台不使用该身份',()=>{
 const c=config();for(const m of [msg('other@chatroom'),{...msg(),chatType:'private'},{...msg(),platform:'web'}]){
 const s=persona.buildSystem({config:c,msg:m});assert.match(s,/名字：大肥鱼/);assert.doesNotMatch(s,/名字：示例身份9/);}
});
test('新名字只在目标群可点名唤醒',()=>{
 const c=config();assert.equal(persona.cleanIncoming('示例身份9，解释TLS',c,msg()).atMe,true);
 assert.equal(persona.cleanIncoming('示例身份9，解释TLS',c,msg('other@chatroom')).atMe,false);
});
test('新消息、模型历史标签及保存的机器人名跟随本群身份',async()=>{
 const c=config(),store=new Store({file:path.join(__dirname,'missing-group-persona-fixture.json')});store.markDirty=()=>{};
 const bot=new Bot({config:c,store,llm:{isMock:false,model:'test-model'},rng:()=>0.99});
 const d=await bot.decide(msg());assert.equal(d.action,'reply');assert.match(d.system,/名字：示例身份9/);
 const turns=bot.buildTurns(msg(),[{role:'assistant',text:'合成回答',ts:Date.now()}]);assert.match(JSON.stringify(turns),/示例身份9（机器人）/);
 bot.finalize(d,'TLS保护传输中的数据');assert.equal(store.messages(d.key).at(-1).name,'示例身份9');store.close();
});
