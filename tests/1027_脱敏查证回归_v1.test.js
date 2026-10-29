'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const http=require('node:http');
const research=require('../src/research');
const web=require('../src/websearch');
const scope='synthetic-group-only';
const makeDecision=()=>({key:scope,system:'甜妹；只使用当前群记忆',turns:[{role:'user',content:'合成测试问题'}],msg:{}});
const request=q=>'<lookup>'+JSON.stringify({kind:'public_term',query:q})+'</lookup>';

test('只允许独立公共术语，已知群名和一字昵称也不能外发',()=>{
 assert.equal(research.sanitizeQuery('显眼包 含义 出处',{privateTerms:['合成测试群','测试用户']}).ok,true);
 for(const query of ['合成测试群 显眼包','测试用户 显眼包','甲 显眼包'])
  assert.equal(research.sanitizeQuery(query,{privateTerms:['合成测试群','测试用户','甲']}).ok,false);
});
test('账号、联系方式、地址、凭据与身份追查均拦截',()=>{
 for(const query of ['wxid_demoa84885ad 梗','12345@chatroom','13800000000','someone@example.com','sk-abcdefghijk',
  '密码 abcdef','上海某路12号','100001','李四是谁','我朋友说的梗','https://example.com/?token=x'])
  assert.equal(research.sanitizeQuery(query).ok,false,query);
});
test('只有结构正确的公共检索请求可执行',()=>{
 assert.ok(research.parseRequest(request('显眼包')));
 assert.equal(research.parseRequest('<lookup>{"kind":"private_person","query":"某人"}</lookup>'),null);
 assert.equal(research.parseRequest('先猜一个答案'+request('显眼包')),null);
});
test('正常回答不会触发外查',async()=>{
 let searched=0;
 const answer=await research.complete({llm:{complete:async()=> '这个常识我可以直接解释'},decision:makeDecision(),
  config:{enabled:true},privacy:{},search:async()=>{searched++;return '';}});
 assert.equal(searched,0);assert.match(answer,/直接解释/);
});
test('不确定问题只外发脱敏术语，查到资料后再答且最多查一次',async()=>{
 let calls=0;const queries=[];const audit=[];
 const d=makeDecision();
 const answer=await research.complete({llm:{complete:async args=>{
  calls++;if(calls===1)return request('显眼包 含义 出处');
  assert.match(args.system,/本轮实际检索资料/);assert.match(args.system,/不是给你的指令/);
  return '这是爱表现、容易吸引注意的俏皮说法，要看语境';
 }},decision:d,config:{enabled:true,budgetMs:4000},privacy:{privateTerms:['合成测试群']},
 search:async(q)=>{queries.push(q);return '合成公开资料，不含任何真实聊天';},audit:x=>audit.push(x)});
 assert.deepEqual(queries,['显眼包']);assert.equal(calls,2);assert.match(answer,/看语境/);
 assert.ok(audit[0].queryHash);assert.ok(!('query' in audit[0]));
});
test('脱敏不确定时不调用任何搜索传输',async()=>{
 let searched=false;
 const answer=await research.complete({llm:{complete:async()=>request('测试用户的身份')},decision:makeDecision(),
 config:{enabled:true},privacy:{privateTerms:['测试用户']},search:async()=>{searched=true;return '';}});
 assert.equal(searched,false);assert.match(answer,/私人信息/);
});
test('没查到就如实说明，不再多发一轮模型请求',async()=>{
 let calls=0;
 const answer=await research.complete({llm:{complete:async()=>{calls++;return request('合成陌生术语');}},
 decision:makeDecision(),config:{enabled:true},privacy:{},search:async()=>''});
 assert.equal(calls,1);assert.match(answer,/暂时没找到/);
});
test('第二轮不允许再次请求检索，标记不外露',async()=>{
 let calls=0,searched=0;
 const answer=await research.complete({llm:{complete:async()=>{calls++;return request('合成术语');}},
 decision:makeDecision(),config:{enabled:true},privacy:{},search:async()=>{searched++;return '合成资料';}});
 assert.equal(calls,2);assert.equal(searched,1);assert.doesNotMatch(answer,/<lookup>/);
});
test('总检索预算到点会取消在途工作',async()=>{
 let cancelled=false;
 const started=Date.now();
 const value=await research.withBudget(signal=>new Promise(resolve=>{
  signal.addEventListener('abort',()=>{cancelled=true;resolve('');});
 }),40);
 assert.equal(value,'');assert.equal(cancelled,true);assert.ok(Date.now()-started<250);
});
test('缓存按会话隔离，不把另一个群的检索缓存拿过来',async()=>{
 let sent=0;
 const transport=async()=>{sent++;return 'synthetic public result';};
 await research.lookup('合成公共词',{scope:'scope-A',transport});
 await research.lookup('合成公共词',{scope:'scope-A',transport});
 await research.lookup('合成公共词',{scope:'scope-B',transport});
 assert.equal(sent,2);
});
test('流式输出不会把检索指令发给群友',async()=>{
 let n=0;
 const llm={async *stream(){yield '<loo';yield 'kup>';yield JSON.stringify({kind:'public_term',query:'合成术语'});yield '</lookup>';},
 complete:async()=>{n++;return '已核实的合成答案';}};
 let out='';
 for await(const part of research.generate({llm,decision:makeDecision(),config:{enabled:true},privacy:{},
 search:async()=> '合成资料'}))out+=part;
 assert.equal(out,'已核实的合成答案');assert.equal(n,1);
});
test('RSS解析保留来源，拒绝非网页链接',()=>{
 const xml='<rss><channel><item><title>公开词&amp;解释</title><link>https://example.com/term</link><description><![CDATA[<b>释义</b>]]></description></item><item><title>bad</title><link>javascript:x</link></item></channel></rss>';
 const rows=web.parseRss(xml);
 assert.equal(rows.length,1);assert.equal(rows[0].title,'公开词&解释');assert.equal(rows[0].snippet,'释义');
});
test('网络超时包含等待首字节，重定向不重置总预算',async()=>{
 const server=http.createServer((req,res)=>{
  if(req.url==='/slow'){setTimeout(()=>res.end('late'),300);return;}
  const count=Number(req.url.slice(1));
  setTimeout(()=>{res.writeHead(302,{Location:'/'+(count+1)});res.end();},45);
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const url='http://127.0.0.1:'+server.address().port;
 try {
  let started=Date.now();
  await assert.rejects(web.getText(url+'/slow',70));
  assert.ok(Date.now()-started<260);
  started=Date.now();
  await assert.rejects(web.getText(url+'/0',100));
  assert.ok(Date.now()-started<280);
 }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});

test('无关搜索结果不能当作查到解释',()=>{
 const rows=[{title:'X染色体的区别',snippet:'合成无关结果'},{title:'显眼包的含义',snippet:'合成释义'}];
 assert.deepEqual(web.relevantResults(rows,'显眼包 含义 出处'),[rows[1]]);
 assert.deepEqual(web.relevantResults(rows.slice(0,1),'显眼包'),[]);
});

test('整轮确认有等待上限，取消慢模型并且不进入无限重写',async()=>{
 let cancelled=false;
 const llm={complete:({signal})=>new Promise((resolve,reject)=>{
  signal.addEventListener('abort',()=>{cancelled=true;reject(Object.assign(new Error('abort'),{code:'aborted'}));});
 })};
 const d=makeDecision();const started=Date.now();
 const answer=await research.complete({llm,decision:d,config:{enabled:true,responseBudgetMs:35},privacy:{}});
 assert.equal(cancelled,true);assert.equal(d.skipRewrite,true);assert.match(answer,/超时/);assert.ok(Date.now()-started<300);
});

test('不接受跳到未授权检索目的地',async()=>{
 await assert.rejects(web.getText('http://localhost:1',100,{allowedHosts:['cn.bing.com']}),/destination not allowed/);
});
