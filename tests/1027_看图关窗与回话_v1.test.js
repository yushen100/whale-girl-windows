'use strict';

/**
 * 「看完图不回消息」+「看图窗堆满桌面」的回归测试。
 *
 * 两个问题的根因（2026-10-27 实测）：
 *
 * ① 看图窗不关
 *    wechatauto 的 download_image_original() 必须点开图片气泡才能拿原图，
 *    但它从头到尾没有关窗代码 —— 它自己的日志就在抱怨
 *    「屏幕上已有 N 个预览窗：本轮只把新出现的窗口算作点开成功」。
 *    预览窗是独立顶层窗，最小化微信主窗带不走它，于是越堆越多。
 *    修法：桥接在看图前后各点一次名，只关这一轮新冒出来的。
 *
 * ② 看完图不回
 *    桥接把图片消息的正文替换成自己拼的「（对方发了一张图片，图里是：…）」，
 *    里面没有 @。群配置 requireAt=true 时服务端判成「没叫她」，
 *    只剩 freeChatChance（12%）的概率会回 —— 表现就是「有时候看完图不回」。
 *    修法：桥接额外投 saw_image，服务端看到它就像被 @ 一样接话。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const bridge = fs.readFileSync(path.join(root, 'examples', 'wechatauto_bridge.py'), 'utf8');
const bot = fs.readFileSync(path.join(root, 'src', 'bot.js'), 'utf8');
const gateway = fs.readFileSync(path.join(root, 'src', 'adapters', 'gateway.js'), 'utf8');

/* ---------------- ① 看图窗要关 ---------------- */

test('桥接提供关看图窗的函数，并认识两种预览窗形状', () => {
  assert.match(bridge, /def _preview_hwnds\(\)/, '要能枚举看图窗');
  // 微信 4.x 实测两种形状：顶层就是 PreviewWindow / Qt 顶层 + 标题「图片和视频」
  assert.match(bridge, /"PreviewWindow" in cls/, '要认 PreviewWindow 类名');
  assert.match(bridge, /_PREVIEW_TITLES/, '要认「图片和视频」标题');
  assert.match(bridge, /def close_image_previews\(/, '要有统一的关闭入口');
});

test('关看图窗只动机器人账号的进程，认不出就不关', () => {
  // 这台机器上可能同时开着主号和小号的微信，关错就是把主人正在看的图叉了。
  assert.match(
    bridge,
    /pids = account_window_pids\(\)[\s\S]{0,400}?if not pids:[\s\S]{0,300}?return 0/,
    '认不出机器人账号的进程时必须直接放弃，不能盲关'
  );
  assert.match(bridge, /if pid not in pids:\s*\n\s*continue/, '不属于机器人账号的窗口要跳过');
});

test('看图前记下已开着的窗，收尾只关这一轮新开的', () => {
  assert.match(
    bridge,
    /_pre_views = set\(_preview_hwnds\(\)\)/,
    '看图之前要先点名，区分「主人自己开的」和「这一轮点开的」'
  );
  assert.match(
    bridge,
    /close_image_previews\(keep=_pre_views\)/,
    '收尾要把已有窗口作为 keep 传进去'
  );
});

test('关看图窗要放在还原主窗之前（否则预览窗变孤儿窗留在桌面）', () => {
  const finallyBlock = bridge.match(
    /finally:\s*\n([\s\S]{0,900}?)restore_win_state\(_snap, force_minimize=bool\(path\)\)/
  );
  assert.ok(finallyBlock, 'describe_image 的 finally 里应当有关窗 + 还原主窗');
  assert.match(finallyBlock[1], /close_image_previews\(keep=_pre_views\)/,
    '先关预览窗，再最小化主窗 —— 顺序反了预览窗会失去父窗口留在桌面上');
});

test('取图失败也要关窗，不能只成功路径才关', () => {
  // 关窗写在 finally 里，不管 path 有没有拿到都会执行。
  // 注意要限定在 describe_image 内部找 —— 前面 with_window_on_top 也有一个 finally。
  const fnStart = bridge.indexOf('def _describe_image_impl(msg):');
  assert.ok(fnStart > 0, '要能找到 describe_image');
  const fnBody = bridge.slice(fnStart, bridge.indexOf('def on_message_inner', fnStart));
  const finallyBlock = fnBody.match(/finally:\s*\n([\s\S]{0,900}?)restore_win_state/);
  assert.ok(finallyBlock, 'describe_image 的 finally 里要有收尾');
  assert.match(finallyBlock[1], /close_image_previews/, 'finally 里必须关窗');
});

/* ---------------- ② 看完图要回 ---------------- */

test('桥接投递 saw_image，且只在真的认出图之后才置真', () => {
  assert.match(bridge, /"saw_image": saw_image/, 'payload 要带 saw_image');
  assert.match(bridge, /saw_image = True/, '识别成功之后才置真');
  // 取不到图时提前 return 了，不该投递
  assert.match(
    bridge,
    /if not desc:[\s\S]{0,200}?return[\s\S]{0,400}?saw_image = True/,
    '取不到图就 return，不能把「没看到」当成「看到了」'
  );
});

test('网关把 saw_image 解析成独立字段，不篡改 at_me', () => {
  assert.match(gateway, /const sawImage = toBool\(firstDefined\(body, \['saw_image'/);
  assert.match(gateway, /^\s+sawImage,$/m, '要放进统一消息对象');
  // at_me 还管着接话窗口和真 @ 的成员选择，混用会把语义搞乱
  const atMeLine = gateway.match(/const atMe = toBool\([^\n]*\)/);
  assert.ok(atMeLine && !/saw_image/.test(atMeLine[0]), 'at_me 的解析里不能混进 saw_image');
});

test('看过图就像被 @ 一样接话，但仍然尊重冷却', () => {
  assert.match(bot, /if \(msg && msg\.sawImage\)/, 'computeTrigger 要看这个字段');
  assert.match(bot, /reason: 'saw_image'/, '要留下可追踪的 reason');
  // 冷却必须继续生效：有人连发十张图不该刷屏
  assert.match(
    bot,
    /if \(msg && msg\.sawImage\) \{\s*\n\s*if \(!tooSoon\) return \{ wake: true, reason: 'saw_image' \};\s*\n\s*return \{ wake: false, reason: 'saw_image_cooldown' \};/,
    '冷却期内应当静默（saw_image_cooldown），而不是硬回'
  );
});

test('每小时回复上限对看图也生效（判断在 saw_image 之前）', () => {
  const capIdx = bot.indexOf("reason: 'hourly_cap'");
  const sawIdx = bot.indexOf("if (msg && msg.sawImage)");
  assert.ok(capIdx > 0 && sawIdx > 0, '两段都要存在');
  assert.ok(capIdx < sawIdx, '每小时上限的判断必须排在 saw_image 之前，否则刷图能绕过上限');
});

test('normalize 不能把 sawImage 丢掉（曾经就是这里漏的）', () => {
  // 真实链路：网关 normalizeInbound 解析出 sawImage → bot.handle 第一件事是
  // this.normalize(rawMsg)，它只挑白名单字段重建对象。
  // 之前只加了 computeTrigger 的判断、没在 normalize 里登记，字段在门口就被扔掉，
  // 于是「看完图就回话」在线上一次都没生效 —— 而只读源码的正则测试发现不了。
  const { loadConfig } = require(path.join(root, 'src', 'config.js'));
  const { Bot } = require(path.join(root, 'src', 'bot.js'));
  const quiet = { info() {}, warn() {}, error() {} };
  const b = new Bot({ config: loadConfig({ root }), logger: quiet });

  const base = {
    platform: 'wechat', chatType: 'group', chatId: 'x@chatroom', chatTitle: '群',
    senderId: 'wxid_demo0c2205b7', senderName: '群友', text: '（对方发了一张图片，图里是：一只猫）',
    atMe: false, msgId: 'm1',
  };
  for (const truthy of [true, 1, '1', 'true']) {
    assert.equal(b.normalize({ ...base, sawImage: truthy }).sawImage, true, '真值 ' + JSON.stringify(truthy) + ' 应当保留');
  }
  for (const falsy of [false, 0, '', undefined, null]) {
    assert.equal(b.normalize({ ...base, sawImage: falsy }).sawImage, false, '假值 ' + JSON.stringify(falsy) + ' 应当是 false');
  }
});
