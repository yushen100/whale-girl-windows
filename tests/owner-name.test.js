'use strict';

/**
 * 「怎么称呼你」必须真的进提示词。
 * 回归的是：网页设置里把称呼改成「示例身份14」，私聊里她还在叫「主人」。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { loadConfig } = require('../src/config');
const persona = require('../src/persona');

function configWith(patch = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'whale-owner-'));
  fs.writeFileSync(path.join(root, 'config.json'), JSON.stringify(patch));
  const config = loadConfig({ root, env: {} });
  return { config, cleanup: () => fs.rmSync(root, { recursive: true, force: true }) };
}

const fromMaster = (text = '在吗') => ({
  platform: 'wechat',
  chatId: 'wxid_demo93e98d15',
  chatType: 'private',
  senderId: 'wxid_demo93e98d15',
  senderName: 'Banan',
  text,
  isMaster: true,
});

test('设置里叫「示例身份14」，提示词里就出现「示例身份14」', () => {
  const { config, cleanup } = configWith({ persona: { ownerName: '示例身份14' } });
  const system = persona.buildSystem({ config, msg: fromMaster(), profile: {}, notes: [] });
  assert.match(system, /示例身份14/, '提示词里必须有「示例身份14」');
  cleanup();
});

test('默认还是叫「主人」，不乱改', () => {
  const { config, cleanup } = configWith({});
  const system = persona.buildSystem({ config, msg: fromMaster(), profile: {}, notes: [] });
  assert.match(system, /主人/);
  cleanup();
});

test('提示词明确要求只用这个称呼，不许混着叫', () => {
  const { config, cleanup } = configWith({ persona: { ownerName: '示例身份14' } });
  const system = persona.buildSystem({ config, msg: fromMaster(), profile: {}, notes: [] });
  assert.match(system, /就这一个称呼|别自己换/);
  assert.doesNotMatch(system, /主要叫「主人」/, '不能还留着写死的「主要叫主人」');
  cleanup();
});

test('非主人时不会把称呼错加给别人', () => {
  const { config, cleanup } = configWith({ persona: { ownerName: '示例身份14' } });
  const system = persona.buildSystem({
    config,
    msg: { ...fromMaster('在吗'), senderId: 'wxid_demo4c90539d', senderName: '路人', isMaster: false },
    profile: {},
    notes: [],
  });
  assert.doesNotMatch(system, /你叫 TA「示例身份14」/);
  cleanup();
});
