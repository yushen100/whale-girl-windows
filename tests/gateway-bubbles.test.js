'use strict';

/**
 * 「回复已交给本地桥接发送（N 条）」里的 N 必须是真的气泡数。
 *
 * 2026-10-28 00:51 的一次误判：日志写「1 条」，桥接却连着记了两次
 * 「群消息发送成功」，看着像重复上屏，翻群消息库才发现是两条不同的话
 * （「在」+「怎么啦」）。
 *
 * 根因是 N 取自 lines —— sendBubbles 关着时 lines 恒等于 [text]，
 * 于是永远显示 1 条；而桥接实际发的是 payload 里的 bubbles，两边口径对不上。
 */
const test = require('node:test');
const assert = require('node:assert/strict');

const { Gateway } = require('../src/adapters/gateway');

function makeGateway() {
  const lines = [];
  const logger = {
    info: (m) => lines.push(m),
    warn: (m) => lines.push(m),
    error: (m) => lines.push(m),
  };
  // 不配 outboundUrl = 本地桥接模式：回复通过 HTTP 响应交回给桥接去发
  const gateway = new Gateway({ platform: 'wechat', options: {}, logger });
  return { gateway, lines };
}

test('两个气泡就要记「2 条」，不能因为 sendBubbles 关着就恒为 1', async () => {
  const { gateway, lines } = makeGateway();
  await gateway.send({
    chatId: '9000000002@chatroom',
    chatType: 'group',
    text: '在\n怎么啦',
    bubbles: ['在', '怎么啦'],
    local: true,
  });
  assert.equal(lines.length, 1);
  assert.match(lines[0], /回复已交给本地桥接发送（2 条）/, 'N 要是气泡数，不是 lines 的长度');
});

test('单个气泡记「1 条」', async () => {
  const { gateway, lines } = makeGateway();
  await gateway.send({
    chatId: '9000000002@chatroom',
    chatType: 'group',
    text: '在',
    bubbles: ['在'],
    local: true,
  });
  assert.match(lines[0], /（1 条）/);
});

test('空行不计入（桥接会跳过空行，日志也不该算）', async () => {
  const { gateway, lines } = makeGateway();
  await gateway.send({
    chatId: '9000000002@chatroom',
    chatType: 'group',
    text: '在\n\n怎么啦',
    bubbles: ['在', '   ', '怎么啦'],
    local: true,
  });
  assert.match(lines[0], /（2 条）/);
});
