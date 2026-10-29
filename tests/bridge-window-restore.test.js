'use strict';

/**
 * 回归：桥接「发完把窗口还回去」一直失败，日志刷 NameError("name 'ctypes' is not defined")。
 * 现象是每发一条消息，微信窗口就被抬到前台不再最小化——用户以为程序在抢屏。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const src = fs.readFileSync(path.join(root, 'examples', 'wechatauto_bridge.py'), 'utf8');

/** 取出某个函数体（靠缩进判断，够用） */
function funcBody(name) {
  const lines = src.split('\n');
  const start = lines.findIndex((l) => new RegExp(`^\\s*def ${name}\\(`).test(l));
  assert.ok(start >= 0, `找不到函数 ${name}`);
  const indent = lines[start].match(/^\s*/)[0].length;
  const body = [];
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.trim() && (line.match(/^\s*/)[0].length) <= indent) break;
    body.push(line);
  }
  return body.join('\n');
}

test('restore_win_state 委托有账号核验的异步清理模块', () => {
  const body=funcBody('restore_win_state');assert.match(body,/from window_cleanup import cleanup_window/);
  assert.match(body,/cleanup_window\(snap, account_owns_hwnd/);
  const helper=fs.readFileSync(path.join(root,'examples','window_cleanup.py'),'utf8');
  assert.match(helper,/ShowWindowAsync/);assert.match(helper,/owns_window\(hwnd\)/);
});

test('还原窗口状态失败不该刷成常规噪音（有专门的日志函数）', () => {
  assert.match(src, /def log_window_problem/, '窗口类问题应该走节流日志');
});
