'use strict';

/**
 * 「看图卡在另存为对话框」+「群里喊示例身份14」的回归测试。
 *
 * ① 看图点保存 → 弹模态框 → 整条发送通道瘫掉
 *    日志实锤（2026-10-27 20:42）：
 *      20:42:44 看图窗没关掉：hwnd=6162124
 *      20:42:44 原微信窗口已失效或账号归属变化，停止恢复/最小化
 *      20:42:49 群消息未发送：微信主窗被弹窗禁用
 *    根因：download_image_original() 等不到 _h.dat 时会退一步点预览窗的
 *    「保存」，弹 Windows 通用另存为对话框（模态）。模态框把微信主窗禁用了，
 *    IsWindowEnabled 变假 → 账号归属校验失败 → 连窗口还原都跳过 → 发送全挂。
 *    修法：把 _save_via_button 打成空操作，拿不到就用本机缓存那份。
 *
 * ② 主动搭话在群里喊「示例身份14」
 *    日志实锤：20:27:47 主动搭话 → 示例身份6 示例身份14，这么晚还在忙吗
 *    根因：careForChat 把 persona.ownerName（当时的值是「示例身份14」）当成
 *    「对方」的名字传给提示词。ownerName 是「主人怎么称呼自己」，
 *    只对已认证主人生效；主动搭话的目标却是最近活跃的白名单会话。
 *
 * ③ 被问「你主人是谁」要能报出大号昵称
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const bridge = fs.readFileSync(path.join(root, 'examples', 'wechatauto_bridge.py'), 'utf8');
const nosave = fs.readFileSync(path.join(root, 'examples', '1027_看图不保存_v1.py'), 'utf8');
const care = fs.readFileSync(path.join(root, 'src', 'care.js'), 'utf8');
const server = fs.readFileSync(path.join(root, 'server.js'), 'utf8');
const persona = fs.readFileSync(path.join(root, 'src', 'persona.js'), 'utf8');

/* ---------------- ① 不再点「保存」 ---------------- */

test('补丁模块存在，且把 _save_via_button 换成返回 None 的空实现', () => {
  assert.match(nosave, /def install_no_save_patch/, '要有安装入口');
  assert.match(nosave, /MediaDownloader\._save_via_button = _save_via_button/,
    '要替换掉保存按钮那条兜底');
  // 空实现必须返回 None，上层才会走「用本机缓存」的退路
  const body = nosave.match(/def _save_via_button\([\s\S]*?return None/);
  assert.ok(body, '空实现必须 return None');
  assert.match(nosave, /_whale_no_save/, '要留一个幂等标记，重复安装不重复打');
});

test('桥接启动时真的装上这个补丁（不是写了不用）', () => {
  assert.match(bridge, /1027_看图不保存_v1\.py/, '要按路径加载补丁文件');
  assert.match(bridge, /install_no_save_patch\(log=log\)/, '要把桥接的 log 传进去');
  // 用 spec_from_file_location 而不是 import_module：文件名以数字开头，按名字找不稳
  assert.match(bridge, /spec_from_file_location/, '要按文件路径加载');
});

test('关窗时先干掉另存为对话框（模态框不关，预览窗的 PostMessage 处理不了）', () => {
  assert.match(bridge, /def _save_dialog_hwnds/, '要能找另存为对话框');
  assert.match(bridge, /#32770/, 'Windows 通用对话框的类名是 #32770');
  const closeFn = bridge.slice(bridge.indexOf('def close_image_previews'));
  const dlgIdx = closeFn.indexOf('_save_dialog_hwnds(pids)');
  const loopIdx = closeFn.indexOf('for h in _preview_hwnds():');
  assert.ok(dlgIdx > 0 && loopIdx > 0, '两段都要在 close_image_previews 里');
  assert.ok(dlgIdx < loopIdx, '必须先关模态对话框，再关预览窗');
});

/* ---------------- ② 别在群里喊示例身份14 ---------------- */

test('主动搭话不拿 ownerName 当陌生会话的称呼', () => {
  // 以前是 `const master = String(persona.ownerName || '主人')` 然后当对方名字用
  assert.match(care, /isMaster/,
    '要区分「对方是不是主人」，只有主人才能用主人专属称呼');
  assert.match(care, /别自己编一个称呼/, '不知道对方名字时不许编');
  assert.match(care, /不要用「主人」「示例身份14」「哥哥」这类称呼/,
    '群聊里必须显式禁止这类称呼');
});

test('careForChat 按 chatId 判断是不是主人会话，群聊不点名', () => {
  assert.match(server, /masterWxids/, '要拿主人 wxid 名单比对');
  assert.match(server, /isMasterChat/, '要把判断结果传下去');
  assert.match(server, /isMaster: isMasterChat/, '要传给 buildCarePrompt');
  // 群聊里这句话是发给整个群的，不该点某个人的名字
  assert.match(server, /endsWith\('@chatroom'\)/, '要识别群聊');
});

test('ownerName 不再被当成 care 的默认对方名', () => {
  // 老写法：const name = String(personaCfg.ownerName || '主人');
  assert.doesNotMatch(server, /const name = String\(personaCfg\.ownerName/,
    '不能再把 ownerName 当对方名字');
});

/* ---------------- ③ 被问主人是谁 ---------------- */

test('被问「你主人是谁」报主人昵称，而不是私下称呼', () => {
  // 两条提示词路径（甜妹 / 自定义）都要有
  const hits = persona.match(/有人问「你主人是谁」/g) || [];
  assert.ok(hits.length >= 2, '甜妹和自定义两条路径都要覆盖，实际命中 ' + hits.length);
  assert.match(persona, /masterCfg\.names/, '要用 master.names（昵称）');
  // ownerName 是私下称呼（可能是「示例身份14」），不能拿来回答「主人是谁」
  assert.match(persona, /不要用「主人」「示例身份14」这类私下的称呼去代替名字回答/,
    '要明确禁止用私下称呼代替名字');
});
