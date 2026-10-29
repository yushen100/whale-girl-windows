'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const persona = require('../src/persona');
const {DEFAULT_CONFIG} = require('../src/config');
const {Store} = require('../src/memory');
const {Bot} = require('../src/bot');

function setup() {
  const config=structuredClone(DEFAULT_CONFIG);
  Object.assign(config.persona,{name:'大肥鱼',stylePreset:'甜妹',masterRoast:true,
    customPrompt:'温柔、会玩梗会接话，俏皮但不攻击人',master:{names:['主人测试'],wxids:['test-owner']}});
  config.websearch.enabled=false;config.links.enabled=false;
  const store=new Store({file:path.join(__dirname,'missing-sweet-test-memory.json')});
  store.markDirty=()=>{};
  const bot=new Bot({config,store,llm:{isMock:false,model:'test-model'},rng:()=>0.9});
  return {config,store,bot};
}
function decision(extra={}) {
 return {key:'wechat:g:fake-room@chatroom',msg:{chatType:'group',senderName:'测试群友',senderId:'test-user',text:'普通问题',...extra}};
}

test('甜妹提示词不混入强制顶嘴或对喷规则，保留梗和当前群笔记',()=>{
 const {config}=setup();
 const system=persona.buildSystem({config,msg:{chatType:'group',chatTitle:'107',senderName:'测试群友',fight:true,isMaster:true},notes:['ONLY_THIS_GROUP_NOTE']});
 assert.match(system,/甜妹/);assert.match(system,/会玩梗/);assert.match(system,/ONLY_THIS_GROUP_NOTE/);
 assert.ok(!system.includes(persona.CORE_TEMPER));
 assert.ok(!system.includes(persona.FIGHT_STYLE));
 assert.doesNotMatch(system,/禁止道歉：不许说|必须反过来怼回去|直接冲这个人大骂/);
});
test('被直接冒犯也不启动自动对喷',async()=>{
 const {bot}=setup();
 const d=await bot.decide({platform:'wechat',chatId:'test-group@chatroom',chatType:'group',
  senderId:'test-user',senderName:'测试群友',text:'大肥鱼你怎么这么蠢',atMe:true,msgId:'synthetic-1'});
 assert.equal(d.action,'reply');assert.equal(d.msg.fight,false);assert.equal(d.msg.fightTarget,null);
 assert.notEqual(d.reason,'clapback');
});
test('旧的fight标记和masterRoast开关不能把道歉替换成狠话',()=>{
 const {bot}=setup();const d=decision({isMaster:true,fight:true,text:'你怎么这么笨'});
 bot.finalize(d,'抱歉，刚才理解偏了，我认真改一下');
 assert.match(d.finalText,/理解偏了/);assert.doesNotMatch(d.finalText,/主人你骂我|笨的是|自己来/);
});
test('替主人说话也不会自动追加点名攻击',()=>{
 const {bot}=setup();const d=decision({isMaster:true,fight:true,fightTarget:{name:'被点名的人'}});
 bot.finalize(d,'咱们把事情好好说清楚');
 assert.match(d.finalText,/好好说清楚/);assert.doesNotMatch(d.finalText,/被点名的人|你算/);
});
test('复读兜底不会阴阳怪气',()=>{
 const {bot}=setup();const d=decision();d.heardList=['同一句话'];d.ownList=[];
 bot.finalize(d,'同一句话');
 assert.doesNotMatch(d.finalText,/尴尬|你说呢/);
});
test('人物笔记仍按群隔离，同一人不串群',()=>{
 const {store}=setup();
 store.addNote('wechat:g:group-a@chatroom','same-person','GROUP_A_NOTE');
 store.addNote('wechat:g:group-b@chatroom','same-person','GROUP_B_NOTE');
 assert.deepEqual(store.listNotes('wechat:g:group-a@chatroom')[0].notes,['GROUP_A_NOTE']);
 assert.deepEqual(store.listNotes('wechat:g:group-b@chatroom')[0].notes,['GROUP_B_NOTE']);
});
test('强攻击输出不会被投递为甜妹回复',()=>{
 const {bot}=setup();const d=decision();
 bot.finalize(d,'你算什么东西，废物');
 assert.doesNotMatch(d.finalText,/废物|你算什么东西/);
});
