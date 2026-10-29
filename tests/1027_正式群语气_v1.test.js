'use strict';

/** 正式群语气回归：只使用公开虚构夹具。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const persona = fs.readFileSync(path.join(root, 'src', 'persona.js'), 'utf8');
const server = fs.readFileSync(path.join(root, 'server.js'), 'utf8');
const bridge = fs.readFileSync(path.join(root, 'examples', 'wechatauto_bridge.py'), 'utf8');

/* ---------------- 配置标记 ---------------- */

test('群人设支持 formal 标记，并且会传进提示词上下文', () => {
  assert.match(persona, /formal:\s*value\.formal === true/, 'groupPersona 要读出 formal');
  assert.match(persona, /formalGroup:\s*local\.formal === true/, 'scopedConfig 要传下去');
});

test('正式群的配置项确实打上了（不是只写了代码）', () => {
  const cfg = JSON.parse(fs.readFileSync(path.join(root, 'tests', 'fixtures', 'formal-groups.json'), 'utf8').replace(/^﻿/, ''));
  const gp = (cfg.wechat || {}).groupPersonas || {};
  const formal = Object.entries(gp).filter(([, v]) => v && v.formal === true);
  assert.ok(formal.length >= 3, '至少要覆盖 3 个正式群，实际 ' + formal.length);
  for (const [id, v] of formal) {
    assert.ok(String(id).endsWith('@chatroom'), '群 ID 应当是 @chatroom 结尾：' + id);
    assert.ok(v.name, '不能把群名改没了');
  }
});

/* ---------------- 提示词收紧 ---------------- */

test('正式群提示词禁掉亲昵和家人称呼', () => {
  const hits = persona.match(/【这是正式群，语气要收敛】/g) || [];
  assert.ok(hits.length >= 2, '甜妹和自定义两条路径都要有，实际 ' + hits.length);
  // 必须逐一点名这些词，只说「不要亲昵」模型会自己发挥
  assert.match(persona, /示例身份14、妈妈、爹、娘、哥哥、姐姐、亲爱、宝贝/,
    '要把禁止的称呼列出来');
  assert.match(persona, /不要用「主人」这个称呼/, '正式群不该叫主人');
  assert.match(persona, /「哦鲸鲸」这类私人口头禅在这个群不要用/,
    '口头禅要在正式群关掉');
});

test('正式群规则排在后半段，能压过前面「会撒娇/会损人」的通用风格', () => {
  // 两条提示词路径各有一处。这里查自定义人设那条（最后一次出现），
  // 因为「会突然撒娇」那段说话风格只在自定义路径里。
  const sweetIdx = persona.indexOf('会突然撒娇');
  const formalIdx = persona.lastIndexOf('【这是正式群，语气要收敛】');
  assert.ok(sweetIdx > 0, '要能找到通用说话风格里的「会突然撒娇」');
  assert.ok(formalIdx > 0, '要能找到正式群规则');
  assert.ok(formalIdx > sweetIdx,
    '自定义人设路径里的正式群规则必须排在说话风格之后，实际 %d vs %d' % (formalIdx, sweetIdx));
  // 明确写出来「上面那些说话习惯在这个群按……来」
  assert.match(persona, /上面「会撒娇 \/ 会损人 \/ 会起话头」那些说话习惯，在这个群一律按/,
    '要显式声明覆盖关系');
  // 甜妹路径里那段也要在（位置在甜妹自己的行数组里，靠前是对的）
  const sweetCount = (persona.match(/【这是正式群，语气要收敛】/g) || []).length;
  assert.ok(sweetCount >= 2, '两条路径都要覆盖，实际 ' + sweetCount);
});

/* ---------------- 不主动搭话 ---------------- */

test('正式群跳过主动搭话（服务端拒绝生成）', () => {
  assert.match(server, /gp\.formal === true[\s\S]{0,120}?skipped: 'formal_group'/,
    'careForChat 要直接拒绝正式群');
});

test('桥接挑目标时也跳过正式群（不靠服务端兜底）', () => {
  assert.match(bridge, /def _is_formal_group/, '要有判定函数');
  assert.match(bridge, /if _is_formal_group\(u\):\s*\n\s*continue/,
    '挑主动搭话目标时要跳过');
  // 每次现读配置，调试台改完不用重启
  assert.match(bridge, /def _is_formal_group[\s\S]{0,400}?read_config\(\)/,
    '要现读配置而不是缓存');
});

/* ---------------- 不误伤 ---------------- */

test('formal 只按群 ID 生效，不污染其他群和私聊', () => {
  // scopedConfig 只在 groupPersona 命中时才会改 persona
  assert.match(persona, /const local = groupPersona\(config, msg\);\s*\n\s*if \(!local\) return config;/,
    '没有群人设时要原样返回配置');
  // 私聊没有 chatId 对应的群人设，自然拿不到 formalGroup
  assert.match(persona, /if \(msg\.platform !== 'wechat' \|\| msg\.chatType !== 'group'\) return null;/,
    '群人设只对微信群生效');
});
