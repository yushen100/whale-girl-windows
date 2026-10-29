'use strict';

/**
 * 「每次重启后重复回话一次」的回归测试。
 *
 * 主人原话：「还有一个问题它每次重启后会重复回话一次」。
 *
 * 日志证据（同一个问题被答了三遍）：
 *   20:43:48  回复 群 示例身份6 → 留雨 ： 先说结论：我不是 DeepSeek harness…
 *   20:59:15  醒来后补一句 → 示例身份6 ： 哦鲸鲸，先纠正一下：我不是 DeepSeek harness…
 *   21:14:15  醒来后补一句 → 示例身份6 ： 不是 DeepSeek harness。我是 group/whale-wx…
 *
 * 根因两处：
 *   ① 补读只看「最后一句像不像在叫她」，不看「这句是不是已经答过了」；
 *   ② 补读的 msgId 是 `catchup:<chat>:<Date.now()>`，每次重启都不同，
 *      存储层的去重完全拦不住。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const server = fs.readFileSync(path.join(root, 'server.js'), 'utf8');

/** 只截 catchup 这一段，避免匹配到别处的同名变量 */
function catchupBlock() {
  const start = server.indexOf("pathname === '/api/catchup'");
  assert.ok(start > 0, '要能找到 /api/catchup 处理段');
  const end = server.indexOf("pathname === '/api/chat'", start);
  assert.ok(end > start, '要能找到 catchup 段的结尾');
  return server.slice(start, end);
}

test('补读的 msgId 必须是稳定指纹，不能用 Date.now()', () => {
  const block = catchupBlock();
  assert.match(block, /const catchupId = \(m\) =>/, '要有生成稳定 id 的函数');
  assert.match(block, /createHash\('sha256'\)/, '按内容做指纹');
  assert.doesNotMatch(
    block,
    /msgId:\s*`catchup:\$\{chatId\}:\$\{Date\.now\(\)\}`/,
    '旧写法每次重启都是新 id，去重永远不生效'
  );
  assert.match(block, /msgId: catchupId\(last\)/, '补话时要用稳定指纹');
});

test('已经答过的最后一句，不再补话', () => {
  const block = catchupBlock();
  assert.match(block, /const answeredAfter =/, '要有「是否已答过」的判断');
  assert.match(
    block,
    /answeredAfter[\s\S]{0,200}?skipped: 'already_answered'/,
    '已答过时要跳过并留下可追踪的原因'
  );
  // 判据要用时间关系（之后有没有助手消息），不能靠正文比对 ——
  // 她同一句可能换个说法，正文比对会漏
  assert.match(block, /e\.role === 'assistant' && e\.deliveryStatus !== 'pending' && Number\(e\.ts \|\| 0\) >= lastTs/,
    '用「最后一条之后是否已有回复」判断，而不是比对正文');
});

test('重复补读的历史不会重复入库', () => {
  const block = catchupBlock();
  assert.match(block, /if \(runtime\.store\.isDuplicate\(key, id\)\) continue;/,
    '同一批历史反复送过来时只入库一次');
  assert.match(block, /runtime\.store\.markSeen\(key, id\)/, '入库后要标记已见');
  assert.match(block, /const alreadyHas = \(m\) =>/, '还要拦住「记忆里本来就有的那条」');
  assert.match(block, /if \(alreadyHas\(m\)\) continue;/, '已在记忆里的不重复写');
});

test('这些闸门排在「判断是否叫她」之后，不改变原有行为', () => {
  const block = catchupBlock();
  const calledIdx = block.indexOf('const called =');
  const answeredIdx = block.indexOf('const answeredAfter =');
  const handleIdx = block.indexOf('await runtime.bot.handle(');
  assert.ok(calledIdx > 0 && answeredIdx > 0 && handleIdx > 0, '三段都要存在');
  assert.ok(calledIdx < answeredIdx, '先判断是否在叫她，再判断是否已答过');
  assert.ok(answeredIdx < handleIdx, '两个判断都要排在真正生成回复之前');
});
