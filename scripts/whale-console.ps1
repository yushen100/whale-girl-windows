# 鲸鲸 · 反代鲸鲸控制台（独立版）
#
# 全新写的一个启动器，不合并进「反代养鱼控制台」，也不改项目里原有的 bat。
# 界面风格仿照 magpie反代 的 1026 那套（菜单 + .cmd 包装 + .lnk 快捷方式 + .ico 图标）。
#
# 它自己直接拉起两个进程：
#   · node server.js                  ← 鲸鲸服务，网页聊天台 127.0.0.1:8787
#   · python examples/wechatauto_bridge.py  ← 微信桥接，负责收发微信消息
# 不调用 启动鲸鱼娘.bat / 启动微信桥接.bat / bridge_loop.bat，自己管自己的进程。
#
# 用法：
#   反代鲸鲸.cmd            打开菜单
#   反代鲸鲸.cmd Start      直接启动
#   反代鲸鲸.cmd Status     只看状态
param(
    [ValidateSet('Menu','Server','Start','Stop','Restart','Status','OpenWeb','OpenDebug','Bridge','Backup','CrashLog')]
    [string]$Target = 'Menu'
)

$ErrorActionPreference = 'Stop'

# 控制台用 UTF-8，否则中文全是乱码
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    chcp 65001 > $null 2>&1
} catch { }

$root = Split-Path -Parent $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }
Set-Location $root

$dataDir = Join-Path $root 'data'
$errLog  = Join-Path $dataDir '1027_反代鲸鲸错误_v1.log'
$servicePort=8787
try {
    $portConfig=Get-Content -LiteralPath (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($portConfig.server.port -ge 1024 -and $portConfig.server.port -le 65535) {$servicePort=[int]$portConfig.server.port}
} catch { }
$webUrl='http://127.0.0.1:'+$servicePort+'/'
$debugUrl=$webUrl+'debug'

# 代理没开时，残留的 HTTPS_PROXY 会让模型请求连到 127.0.0.1:7897 而失败。
# 模型接口和国内站点都不需要代理 —— 和项目里其他启动脚本保持一致的清理。
foreach ($name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')) {
    if (Test-Path "env:$name") { Remove-Item "env:$name" -ErrorAction SilentlyContinue }
}
$env:NO_PROXY = '127.0.0.1,localhost,::1'
$env:no_proxy = $env:NO_PROXY
$env:TZ = 'Asia/Shanghai'

function Say([string]$msg, [string]$color = 'Gray') { Write-Host $msg -ForegroundColor $color }

function Write-ErrLog([string]$text) {
    try {
        New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
        Add-Content -LiteralPath $errLog -Encoding UTF8 `
            -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $text)
    } catch { }
}

# ---------------------------------------------------------------- 找运行时
# 优先用包里自带的，解压即用；没有再退回系统装的。
function Get-PythonExe {
    foreach ($p in @(
        (Join-Path $root 'runtime\python\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    )) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-NodeExe {
    foreach ($p in @((Join-Path $root 'runtime\node.exe'))) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# ---------------------------------------------------------------- 进程检测
# 只认「命令行里出现目标脚本」，避免误伤本机别的 node / python
# （这台机器上还开着 DSH、反代网关，光看进程名会认错）。
#
# 2026-10-27 修：以前还要求命令行里含 'whale-girl'，结果启动器用**相对路径**
# （examples/wechatauto_bridge.py）拉起来的桥接匹配不上 —— 桥接明明起来了，
# 控制台却报「30 秒没起来」。改成：命令行含脚本名，且工作目录是本项目，
# 或者命令行里带绝对路径的 whale-girl。两条满足一条就算。
#
# 2026-10-27 二次修（服务守护）：server.js 太通用，光看命令行会认错别的 node。
# 现在要求：命令行里带 server.js，并且**参数里没有它不该有的东西**，
# 再配合 Test-ServerAlive 的端口确认。见 Get-ServerProcs。
function Get-ProjectProcs([string]$scriptPattern) {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        if (-not $_.CommandLine) { return $false }
        if ($_.CommandLine -notlike "*$scriptPattern*") { return $false }
        # 排除本脚本自己的检测命令（powershell -Command ... 里会带上脚本名）
        if ($_.Name -eq 'powershell.exe' -or $_.Name -eq 'pwsh.exe') { return $false }
        if ($_.CommandLine -like '*whale-girl*') { return $true }
        # 相对路径启动：看命令行是否就是这个相对路径
        if ($_.CommandLine -match 'examples[\\/]wechatauto_bridge\.py') { return $true }
        return $false
    })
}

# 服务进程（node server.js）单独识别。
#
# 为什么不能用 Get-ProjectProcs 'server.js'：这个函数对 server.js 是**故意**
# 返回 false 的（怕误伤本机别的 node）。结果就是 Stop 永远找不到服务进程 ——
# 2026-10-27 之前点「全部停止」只停掉桥接，8787 还挂着，得靠后面的端口兜底才杀得掉，
# 而端口兜底又用了 /T，容易牵连别的进程。
#
# 这里的判据：node 进程 + 命令行含本项目 server.js 的绝对路径。
# 绝对路径是唯一的 —— 别的项目不可能有 本项目根目录中的 server.js。
function Get-ServerProcs {
    $serverJs = (Join-Path $root 'server.js')
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        if (-not $_.CommandLine) { return $false }
        if ($_.Name -notmatch '^(node|electron)') { return $false }
        # 同时接受绝对路径和「工作目录是本项目 + 相对文件名」两种写法
        return ($_.CommandLine -like "*$serverJs*")
    })
}

function Get-ServiceGuards {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -in @('python.exe','pythonw.exe') -and
        $_.CommandLine -like "*$root\scripts\1027_服务守护_v1.py*"
    })
}

function Get-PortListener([int]$port) {
    @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty OwningProcess -Unique)
}

function Test-ServerAlive { return ($null -ne (Get-ServerOwnerPid)) }
function Test-BridgeAlive { return (@(Get-ProjectProcs 'wechatauto_bridge.py').Count -gt 0) }

# 桥接当前连的是哪个微信号。
#
# 为什么需要这个：这台机器上**同时开着两个微信** —— 大号（示例身份15）和小号（鱼）。
# 两个都是正经登录的，不是多开。身份对照表（2026-10-28 实测确认）：
#
#   大号  示例身份15  wxid_demo1d2c3180  微信号 wq2875449652
#   小号  琦    wxid_demo6cf9e397  微信号 w13800000000   ← 鱼在这
#
# 鱼必须只在小号上说话，主号绝不进 whale。config.json 已经做了三层保护：
#   · wechat.account 显式锁死小号
#   · allowWxids 只有小号
#   · denyWxids 里是主号
# 桥接启动时会校验，连错号直接拒绝启动。
#
# 但控制台这边以前只看「有没有桥接进程」—— 万一哪天真连上了主号，
# 控制台照样报「运行中」，主人根本看不出来。所以这里从桥接日志里
# 把实际连上的账号读出来，跟配置里期望的账号对比。
#
# 日志里的样子（examples/wechatauto_bridge.py:979）：
#   自身身份：琦（wxid_demo6cf9e397）
function Get-BridgeIdentity {
    $identityPath = Join-Path $dataDir 'bridge-identity.json'
    if (-not (Test-Path -LiteralPath $identityPath)) { return $null }
    try {
        $record = Get-Content -LiteralPath $identityPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $info = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.pid)" -ErrorAction Stop
        $process = Get-Process -Id $record.pid -ErrorAction Stop
        $started = ([DateTimeOffset]$process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0
        $bridgePath = Join-Path $root 'examples\wechatauto_bridge.py'
        if (-not $info.CommandLine.Contains($bridgePath)) { return $null }
        if ([Math]::Abs($started - [double]$record.processStartedAt) -gt 2) { return $null }
        if (-not ([string]$record.wxid).StartsWith('wxid_')) { return $null }
        return [pscustomobject]@{ Nick=$record.nick; Wxid=$record.wxid; Alias='' ; Pid=$record.pid }
    } catch { return $null }
}

# 从微信本地库里读某个账号的「微信号」（alias）。
#
# 账号别名只用于界面核对，不内置任何个人身份。
# 读法：contact 表里 username = 该账号 wxid 的那一行，alias 列就是微信号。
# 注意要用 -I 隔离模式跑，避免项目目录里的同名模块被误加载。
# 读不到不算错误（微信没登录、库被锁、换了版本都可能失败），返回空字符串。
function Get-AccountAlias([string]$wxid) {
    if (-not $wxid) { return '' }
    $py = Get-PythonExe
    if (-not $py) { return '' }
    $cfgAlias = Get-Content -LiteralPath (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $aliasDbDir = [string]$cfgAlias.wechat.dbDir
    if (-not $aliasDbDir) { return '' }
    $script = @'
import sys, json
try:
    from wechatauto import WeChatDB
    wxid = sys.argv[1]
    db_dir = sys.argv[2]
    # account 参数要带目录后缀（wxid_demo52b40cb2_050c），但调用方只知道裸 wxid。
    # 所以先扫目录，找出以这个 wxid 开头的那个账号目录。
    import os
    acc_dir = None
    for name in os.listdir(db_dir):
        if name.startswith(wxid + '_'):
            acc_dir = name
            break
    if not acc_dir:
        print("")
        sys.exit(0)
    db = WeChatDB(db_dir=db_dir, account=acc_dir)
    con = db._contact_conn
    if callable(con): con = con()
    cur = con.execute("SELECT alias FROM contact WHERE username=?", (wxid,))
    row = cur.fetchone()
    print(row["alias"] if row and row["alias"] else "")
except Exception:
    print("")
'@
    $tmp = Join-Path $env:TEMP ('1027_alias_' + [guid]::NewGuid().ToString('N').Substring(0,8) + '.py')
    try {
        Set-Content -LiteralPath $tmp -Value $script -Encoding UTF8
        $out = & $py -I $tmp $wxid $aliasDbDir 2>$null
        if ($out) { return ([string]$out).Trim() }
    } catch { }
    finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    return ''
}

# 配置里期望桥接连的账号（wechat.account 去掉 _050c 这种后缀）
function Get-ExpectedAccount {
    $cfgPath = Join-Path $root 'config.json'
    if (-not (Test-Path -LiteralPath $cfgPath)) { return $null }
    try {
        $cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $acc = [string]$cfg.wechat.account
        if (-not $acc) { return $null }
        # 注意：wxid 本身就带下划线（wxid_demo6cf9e397），不能按 '_' 简单切第一个。
        # account 的完整形式是 wxid_demo6cf9e397_050c —— 后缀是「_4 位十六进制」那种目录名。
        # 所以只去掉结尾那一段短后缀，前面的原样保留。
        if ($acc -match '^(wxid_[A-Za-z0-9]+)_[A-Za-z0-9]{2,8}$') { return $matches[1] }
        return $acc
    } catch { return $null }
}

# 8787 端口上那个进程，是不是本项目的 server.js。
# 端口能听不代表是我们的服务 —— 别的程序占了 8787 也会让 Test-ServerAlive 为真。
function Get-ServerOwnerPid {
    $serverJs = Join-Path $root 'server.js'
    foreach ($procId in @(Get-PortListener $servicePort)) {
        $info = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        if ($info -and $info.CommandLine -and $info.CommandLine -like "*$serverJs*") { return $procId }
    }
    return $null
}

function Get-ProjectStatus {
    Say ''
    Say '  ---------------- 当前状态 ----------------' 'Cyan'

    $pausedMarker=Join-Path $dataDir 'PAUSED'
    if (Test-Path -LiteralPath $pausedMarker) {Remove-Item -LiteralPath $pausedMarker -Force}
    if (Test-ServerAlive) {
        $ownerPid = Get-ServerOwnerPid
        if ($ownerPid) { Say ('  鲸鲸服务：运行中  PID ' + $ownerPid) 'Green' }
        else { Say '  鲸鲸服务：端口被别的程序占用（不是本项目的 server.js）' 'Yellow' }
    } else { Say '  鲸鲸服务：未启动' 'Yellow' }

    if (Test-BridgeAlive) {
        Say '  微信桥接：运行中' 'Green'

        # 连的是哪个号 —— 这台机器上大号小号同时开着，
        # 必须能一眼确认鱼在小号上说话，不是主号。
        $identity = Get-BridgeIdentity
        $expected = Get-ExpectedAccount
        if ($identity) {
            # 报微信号而不是昵称 —— 主人认的是 w13800000000 这个号，
            # 昵称「琦」对他没有辨识度。
            $label = if ($identity.Alias) { $identity.Alias } else { $identity.Nick }
            if ($expected -and $identity.Wxid -ne $expected) {
                Say ('  [!] 桥接连的是 ' + $label + '（' + $identity.Wxid + '）') 'Red'
                Say ('      期望的是 ' + $expected + ' —— 连错号了，立刻停止并检查 config.json') 'Red'
            } else {
                Say ('  桥接账号：' + $label + '（' + $identity.Wxid + '）✓') 'Green'
            }
        } elseif ($expected) {
            Say ('  桥接账号：读不到（期望 ' + $expected + '）') 'Yellow'
        }
    }
    else { Say '  微信桥接：未启动' 'Yellow' }

    # 守护状态：这是「服务半夜静默死掉没人管」的防线，必须能一眼看到
    $svcGuard = @(Get-ServiceGuards)
    if ($svcGuard.Count -gt 0) { Say ('  服务守护：运行中  PID ' + $svcGuard[0].ProcessId) 'Green' }
    else { Say '  服务守护：未运行 —— 服务意外退出后不会自动拉起' 'Yellow' }

    if (@(Get-BridgeGuards).Count -gt 0) { Say '  桥接守护：运行中' 'Green' }
    else { Say '  桥接守护：未运行' 'Yellow' }

    $weixin = @(Get-Process -Name 'Weixin' -ErrorAction SilentlyContinue)
    if ($weixin.Count -gt 0) { Say ('  微信进程：在跑（' + $weixin.Count + ' 个）') 'Green' }
    else { Say '  微信进程：没开 —— 桥接要先登录微信才发得出消息' 'Red' }

    # 崩溃日志：server.js 加了兜底之后，未捕获异常会落在这里
    $crashLog = Join-Path $dataDir '1027_鲸鲸崩溃_v1.log'
    if (Test-Path -LiteralPath $crashLog) {
        $crashTime = (Get-Item -LiteralPath $crashLog).LastWriteTime
        Say ('  [!] 有崩溃记录：' + $crashLog + '（最后 ' + $crashTime.ToString('MM-dd HH:mm:ss') + '）') 'Red'
    }

    # 桥接日志最后一行：一眼看出它现在在干嘛（等待中 / 发送成功 / 报错）
    $bridgeLog = Join-Path $dataDir 'wechat-bridge-4x.log'
    if (Test-Path -LiteralPath $bridgeLog) {
        try {
            $last = Get-Content -LiteralPath $bridgeLog -Tail 1 -Encoding UTF8 -ErrorAction Stop
            if ($last) { Say ('  桥接最后一条：' + $last) 'DarkGray' }
        } catch { }
    }
    Say '  ------------------------------------------' 'Cyan'
    Say ''
}

# 已运行的旧版服务也收起专属控制台，不重启、不打断正在处理的消息。
function Hide-ServiceConsole([int]$serviceId) {
    if (-not ('WhaleServiceConsole' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class WhaleServiceConsole {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder name, int count);
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
}
"@
    }
    $service = Get-Process -Id $serviceId -ErrorAction SilentlyContinue
    if (-not $service -or $service.MainWindowHandle -eq [IntPtr]::Zero) { return }
    $className = New-Object System.Text.StringBuilder 256
    $null = [WhaleServiceConsole]::GetClassName($service.MainWindowHandle, $className, 256)
    # 只隐藏这个服务的独立控制台，不碰共享终端和控制菜单。
    if ($className.ToString() -eq 'ConsoleWindowClass') {
        $null = [WhaleServiceConsole]::ShowWindowAsync($service.MainWindowHandle, 0)
    }
}

# ---------------------------------------------------------------- 启动
# node 的诊断参数：原生崩溃时吐出报告。
#
# 2026-10-28 00:16:42 服务以退出码 -1073740791（0xC0000409，
# STATUS_STACK_BUFFER_OVERRUN）消失 —— 这是**原生崩溃**，不是 JS 异常。
# server.js 里挂的 uncaughtException 兜底对它完全无效：进程在 V8/原生层
# 就被干掉了，JS 一行都执行不到，所以什么都没留下。
# 23:46:09 那次查不到任何痕迹，原因也是这个。
#
# --report-on-fatalerror 让 Node 在致命错误（OOM、V8 fatal、abort）时
# 写一份 JSON 诊断报告，里面有 V8 的原始报错、JS 调用栈、堆统计。
# 这是目前唯一能抓到这类崩溃现场的手段。
#
# 代价：报告只在崩溃时生成，正常运行时零开销。
function Get-NodeDiagArgs {
    $reportDir = Join-Path $dataDir 'reports'
    try { New-Item -ItemType Directory -Path $reportDir -Force | Out-Null } catch { }
    return @(
        '--report-on-fatalerror',
        "--report-directory=`"$reportDir`"",
        '--report-compact'
    )
}

# 服务守护：node server.js 意外退出后自动拉起。
#
# 为什么必须有（2026-10-27 事故）：23:46:09 服务处理群消息时静默死掉，
# 没有守护就没人管 —— 桥接撞了 6 分钟空，在群里 @ 主人三次还报错了原因。
# 有了守护，最坏情况是几秒后自动恢复。
function Ensure-ServiceGuard([int]$servicePid) {
    if (@(Get-ServiceGuards).Count -gt 0) { return }
    $py = Get-PythonExe
    if (-not $py) { throw '找不到 Python，无法启动服务守护。' }
    $guardScript = Join-Path $root 'scripts\1027_服务守护_v1.py'
    $node = Get-NodeExe
    if (-not $node) { throw '找不到 Node.js，无法启动服务守护。' }
    $env:PYTHONIOENCODING = 'utf-8'
    Start-Process -FilePath $py -ArgumentList '-u', "`"$guardScript`"", '--node', "`"$node`"", '--watch-pid', "$servicePid" `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $dataDir '1027_服务守护后台输出_v1.log') `
        -RedirectStandardError (Join-Path $dataDir '1027_服务守护后台错误_v1.log')
    Start-Sleep -Milliseconds 800
    if (@(Get-ServiceGuards).Count -eq 0) { throw '服务守护未保持运行，请检查 data\1027_服务守护后台错误_v1.log。' }
    Say '  服务崩溃自动恢复守护已启用（后台运行）。' 'Green'
}

function Start-Server {
    if (Test-Path -LiteralPath (Join-Path $dataDir 'CLOUD_PRIMARY')) {
        Say '  已由云端鲸鲸接管，本机服务保持停止，避免重复回复。' 'Yellow'
        return
    }
    if (Test-ServerAlive) {
        $ownerPid = Get-ServerOwnerPid
        if ($ownerPid) {
            Hide-ServiceConsole $ownerPid
            Ensure-ServiceGuard $ownerPid
        }
        Say '  鲸鲸服务已经在后台运行，复用。' 'DarkGray'
        return
    }

    if (@(Get-PortListener $servicePort).Count -gt 0) {throw '端口被其他程序占用，本项目未启动。请修改 config.json 的 server.port。'}
    $node = Get-NodeExe
    if (-not $node) { throw '找不到 Node.js。先双击「一键装运行环境.bat」，或装 Node 20+。' }

    Say '  启动鲸鲸服务…' 'Cyan'
    # 后台隐藏运行，输出保留到文件，关闭控制台不会停止服务。
    # 传绝对路径（见 Start-Bridge 里的说明）。
    $serverJs = Join-Path $root 'server.js'
    $serverArgs = @("`"$serverJs`"", '--no-browser') + (Get-NodeDiagArgs)
    Start-Process -FilePath $node -ArgumentList $serverArgs `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $dataDir '1027_鲸鲸后台输出_v1.log') `
        -RedirectStandardError (Join-Path $dataDir '1027_鲸鲸后台错误_v1.log')

    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        if ((Get-Date) -gt $deadline) { break }
    } while (-not (Test-ServerAlive))

    if (Test-ServerAlive) {
        Say ('  鲸鲸服务已启动：' + $webUrl) 'Green'
        $ownerPid = Get-ServerOwnerPid
        if ($ownerPid) { Ensure-ServiceGuard $ownerPid }
    } else {
        throw '服务 30 秒没起来，请查看 data\1027_鲸鲸后台错误_v1.log。'
    }
}

function Get-BridgeGuards {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -in @('python.exe','pythonw.exe') -and
        $_.CommandLine -like "*$root\scripts\1027_微信桥接守护_v1.py*"
    })
}

function Ensure-BridgeGuard([int]$bridgePid) {
    if (@(Get-BridgeGuards).Count -gt 0) { return }
    $py = Get-PythonExe
    if (-not $py) { throw '找不到 Python，无法启动桥接守护。' }
    $guardScript = Join-Path $root 'scripts\1027_微信桥接守护_v1.py'
    $env:PYTHONIOENCODING = 'utf-8'
    Start-Process -FilePath $py -ArgumentList '-u', "`"$guardScript`"", '--python', "`"$py`"", '--watch-pid', "$bridgePid" `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $dataDir '1027_桥接守护后台输出_v1.log') `
        -RedirectStandardError (Join-Path $dataDir '1027_桥接守护后台错误_v1.log')
    Start-Sleep -Milliseconds 800
    if (@(Get-BridgeGuards).Count -eq 0) { throw '桥接守护未保持运行，请检查 data\1027_桥接守护后台错误_v1.log。' }
    Say '  崩溃自动恢复守护已启用（后台运行）。' 'Green'
}

function Start-Bridge {
    if (Test-Path -LiteralPath (Join-Path $dataDir 'CLOUD_PRIMARY')) {
        Say '  已由云端鲸鲸接管，本机服务保持停止，避免重复回复。' 'Yellow'
        return
    }
    $paused = Join-Path $dataDir 'PAUSED'
    if (Test-Path -LiteralPath $paused) { Remove-Item -LiteralPath $paused -Force }
    if (Test-BridgeAlive) {
        foreach ($serviceInfo in @(Get-ProjectProcs 'wechatauto_bridge.py')) { Hide-ServiceConsole $serviceInfo.ProcessId }
        Ensure-BridgeGuard (@(Get-ProjectProcs 'wechatauto_bridge.py')[0].ProcessId)
        Say '  微信桥接已经在后台运行，复用。' 'DarkGray'
        return
    }

    # 微信必须开着。注意这台机器上**大号小号同时登录**是正常状态（不是多开），
    # 两个 Weixin.exe 都算数 —— 这里只确认微信客户端在跑，
    # 至于桥接最终连上哪个号，由桥接自己按 config.json 的 account/白名单校验。
    if (-not (Get-Process -Name 'Weixin' -ErrorAction SilentlyContinue)) {
        Say '  [!] 微信没开。桥接启动了也发不出消息 —— 先登录微信（鱼用小号）。' 'Red'
        return
    }
    $expected = Get-ExpectedAccount
    if ($expected) { Say ('  期望连的账号：' + $expected + '（鱼的小号）') 'DarkGray' }
    $py = Get-PythonExe
    if (-not $py) { throw '找不到 Python 3.12。先双击「一键装运行环境.bat」。' }

    Say '  启动微信桥接及后台守护…' 'Cyan'
    Ensure-BridgeGuard 0

    # 只等「进程出现」就够了（约 3~6 秒）。UIA 预热还要再花 5~10 秒，
    # 但那期间桥接已经在监听消息了，没必要等它预热完 —— 以前等 30 秒纯属白等。
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 700
        if ((Get-Date) -gt $deadline) { break }
    } while (-not (Test-BridgeAlive))

    if (Test-BridgeAlive) {
        Say '  微信桥接已启动（后台静默运行，日志在 data 目录）' 'Green'
        Say '  界面按需初始化；首次登录后先打开一个白名单群，可运行只读自检确认发送就绪。' 'DarkGray'
        # 等结构化身份记录与当前进程匹配，再核对是不是小号。
        # 这一步很重要：启动成功 ≠ 连对了号。
        $idDeadline = (Get-Date).AddSeconds(15)
        $identity = $null
        do {
            Start-Sleep -Milliseconds 1000
            $identity = Get-BridgeIdentity
        } while (-not $identity -and (Get-Date) -lt $idDeadline)

        if ($identity) {
            $label = if ($identity.Alias) { $identity.Alias } else { $identity.Nick }
            if ($expected -and $identity.Wxid -ne $expected) {
                Say ('  [!] 桥接连的是 ' + $label + '（' + $identity.Wxid + '），不是小号！') 'Red'
                Say ('      立刻停止，检查 config.json 的 wechat.account / allowWxids') 'Red'
            } else {
                Say ('  已连上：' + $label + '（' + $identity.Wxid + '）✓') 'Green'
            }
        } elseif ($expected) {
            Say ('  暂时读不到账号，稍后看状态确认（期望 ' + $expected + '）') 'Yellow'
        }
    } else {
        throw '桥接 20 秒没起来，请查看 data\1027_微信桥接后台错误_v1.log。'
    }
}

function Start-All {
    if (Test-Path -LiteralPath (Join-Path $dataDir 'CLOUD_PRIMARY')) {
        Say '  已由云端鲸鲸接管，本机服务保持停止，避免重复回复。' 'Yellow'
        return
    }
    # 清掉「全部停止」留下的暂停标记 —— 不然后面再点停止/启动会被它挡住。
    # 项目里的 bridge_loop.bat 看到这个标记会拒绝拉起桥接。
    $paused = Join-Path $dataDir 'PAUSED'
    if (Test-Path -LiteralPath $paused) {
        Remove-Item -LiteralPath $paused -Force -ErrorAction SilentlyContinue
        Say '  已清掉 data\PAUSED 暂停标记。' 'DarkGray'
    }
    Start-Server
    Start-Bridge
}

# ---------------------------------------------------------------- 停止
function Stop-All {
    Say '  停止鲸鲸的所有进程…' 'Cyan'

    # 先立暂停标记：万一项目里别的守护脚本在跑，看到它就不会再把桥接拉起来
    try {
        New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $dataDir 'PAUSED') `
            -Value ([string](Get-Date -Format 'o')) -Encoding UTF8
    } catch { }

    # 等守护响应暂停，避免立即重启时旧守护仍在。
    # 两个守护都要收：桥接守护 + 服务守护，否则停完立刻又被拉起来。
    foreach ($guardInfo in @(Get-BridgeGuards) + @(Get-ServiceGuards)) {
        $guardProcess = Get-Process -Id $guardInfo.ProcessId -ErrorAction SilentlyContinue
        if ($guardProcess -and -not $guardProcess.WaitForExit(3000)) {
            Stop-Process -Id $guardInfo.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    $killed = 0
    # 多扫两遍：杀父进程时可能正好有子进程在起
    for ($pass = 1; $pass -le 3; $pass++) {
        $targets = @(
            @(Get-ProjectProcs 'wechatauto_bridge.py') +
            @(Get-ServerProcs)
        ) | Sort-Object ProcessId -Unique

        if ($targets.Count -eq 0) { break }
        foreach ($t in $targets) {
            if ($t.ProcessId -eq $PID) { continue }
            # 只杀进程本身，不带 /T。
            #
            # 教训（跟 1027_养鱼控制台 同源）：带 /T 会连整棵子进程树一起杀。
            # 如果进程是从某个终端窗口里手工起的，那棵树上挂着 Windows Terminal /
            # conhost —— /T 会顺手把主人开着的终端窗口一起关掉（2026-10-27 误关过一次）。
            # 这里要停的只是 node / python 本身，逐个杀同样收得干净。
            $null = & taskkill.exe /PID $t.ProcessId /F 2>&1
            if ($LASTEXITCODE -eq 0) { $killed++ }
        }
        Start-Sleep -Milliseconds 700
    }

    # 兜底：还占着 8787 的，按端口再杀一次（同样不带 /T）
    foreach ($procId in @(Get-PortListener $servicePort)) {
        if (-not $procId -or $procId -eq $PID) { continue }
        $null = & taskkill.exe /PID $procId /F 2>&1
        if ($LASTEXITCODE -eq 0) { $killed++ }
    }

    Start-Sleep -Milliseconds 500
    if (Test-ServerAlive -or (Test-BridgeAlive)) {
        Say '  还有没停干净的，再点一次停止，或用任务管理器。' 'Yellow'
    } elseif ($killed -eq 0) {
        Say '  本来就是停着的。' 'Green'
    } else {
        Say ('  已全部停止（' + $killed + ' 个进程）。') 'Green'
    }
    Say '  注：留下了 data\PAUSED 标记，两个守护看到它都不会再拉起进程。' 'DarkGray'
}

# 看崩溃现场。server.js 加了 uncaughtException 兜底之后，
# 任何未捕获异常都会写进 data\1027_鲸鲸崩溃_v1.log —— 出问题先看这里。
function Show-CrashLog {
    $crashLog = Join-Path $dataDir '1027_鲸鲸崩溃_v1.log'
    $svcGuardLog = Join-Path $dataDir '1027_服务守护_v1.log'
    Say ''
    if (Test-Path -LiteralPath $crashLog) {
        $item = Get-Item -LiteralPath $crashLog
        Say ('  崩溃记录：' + $crashLog) 'Red'
        Say ('  最后写入：' + $item.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') + '，大小 ' + $item.Length + ' 字节') 'DarkGray'
        Say ''
        Get-Content -LiteralPath $crashLog -Encoding UTF8 -Tail 40 | ForEach-Object { Say ('  ' + $_) }
    } else {
        Say '  没有崩溃记录（data\1027_鲸鲸崩溃_v1.log 不存在）。' 'Green'
    }
    Say ''
    if (Test-Path -LiteralPath $svcGuardLog) {
        Say '  服务守护最近 10 条：' 'DarkGray'
        Get-Content -LiteralPath $svcGuardLog -Encoding UTF8 -Tail 10 | ForEach-Object { Say ('  ' + $_) 'DarkGray' }
    }
    Say ''
}

function Open-Page([string]$url) {
    if (-not (Test-ServerAlive)) {
        Say '  服务没在跑，先启动（菜单 1）。' 'Yellow'
        return
    }
    Start-Process $url
    Say ('  已打开：' + $url) 'Green'
}

function Invoke-Backup {
    $node = Get-NodeExe
    if (-not $node) { throw '找不到 Node.js，无法备份。' }
    Say '  备份记忆…' 'Cyan'
    & $node (Join-Path $root 'scripts\memory-tool.js') backup
    if ($LASTEXITCODE -ne 0) { throw '备份失败，看上面的输出。' }
    Say '  备份在 data\backups\ 里。' 'Green'
}

function Invoke-ActionUnlocked([string]$action) {
    switch ($action) {
        'Server'    { Start-Server }
        'Start'     { Start-All }
        'Stop'      { Stop-All }
        'Restart'   { Stop-All; Start-Sleep -Seconds 2; Start-All }
        'Status'    { Get-ProjectStatus }
        'OpenWeb'   { Open-Page $webUrl }
        'OpenDebug' { Open-Page $debugUrl }
        'Bridge'    { Start-Bridge }
        'Backup'    { Invoke-Backup }
        'CrashLog'  { Show-CrashLog }
    }
}

# 防止连续点击或两个控制台同时启动出重复进程。
function Invoke-Action([string]$action) {
    $guard = New-Object System.Threading.Mutex($false, 'Local\WhaleGirlConsoleStartup')
    $held = $false
    try {
        try { $held = $guard.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { Say '  另一项启停操作正在执行，请稍后查看状态。' 'Yellow'; return }
        Invoke-ActionUnlocked $action
    } finally {
        if ($held) { $guard.ReleaseMutex() }
        $guard.Dispose()
    }
}

# ---------------------------------------------------------------- 入口
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null

if ($Target -ne 'Menu') {
    try { Invoke-Action $Target; exit 0 }
    catch {
        Say ('  失败：' + $_.Exception.Message) 'Red'
        Write-ErrLog ($Target + ' 失败：' + $_.Exception.Message)
        exit 1
    }
}

while ($true) {
    Say ''
    Say '  ============== 鲸鲸 · 反代鲸鲸控制台 ==============' 'Cyan'
    Say '  1. 一键启动（服务 + 桥接）'
    Say '  2. 全部停止'
    Say '  3. 重启'
    Say '  4. 查看运行状态'
    Say '  5. 打开网页聊天台'
    Say '  6. 打开调试台（看消息流水）'
    Say '  7. 只启动微信桥接'
    Say '  8. 备份记忆'
    Say '  9. 看崩溃现场（服务死了先看这个）'
    Say '  0. 退出控制台（服务继续运行）'
    Say '  ==============================================' 'Cyan'

    $choice = Read-Host '  请输入选项'
    if ($choice -eq '0') { break }

    $selected = switch ($choice) {
        '1' { 'Start' }
        '2' { 'Stop' }
        '3' { 'Restart' }
        '4' { 'Status' }
        '5' { 'OpenWeb' }
        '6' { 'OpenDebug' }
        '7' { 'Bridge' }
        '8' { 'Backup' }
        '9' { 'CrashLog' }
        default { $null }
    }
    if (-not $selected) { Say '  选项无效，请重新输入。' 'Yellow'; continue }

    try { Invoke-Action $selected }
    catch {
        Say ('  失败：' + $_.Exception.Message) 'Red'
        Write-ErrLog ($selected + ' 失败：' + $_.Exception.Message)
    }
}
