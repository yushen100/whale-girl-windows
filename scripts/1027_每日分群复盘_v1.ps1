# 每日分群复盘入口：只调用已运行的鲸鲸，不启动微信、不向群里发消息。
param([switch]$Preview)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$listeners = @(Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
if ($listeners.Count -ne 1) { throw '鲸鲸服务未运行或监听异常，本次未执行复盘。' }
$serverInfo = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $listeners[0])
$expected = Join-Path $root 'server.js'
if ($serverInfo.Name -ne 'node.exe' -or $serverInfo.CommandLine -notlike ("*" + $expected + "*")) {
    throw '8787 不是本项目鲸鲸服务，已停止调用。'
}
$body = @{dryRun=[bool]$Preview} | ConvertTo-Json -Compress
$result = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/reflection/run' -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 90
$directory = Join-Path $root 'data\reflections\task-runs'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$file = Join-Path $directory ((Get-Date -Format 'yyyyMMdd_HHmmss') + '_每日复盘执行_v1.json')
$result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $file -Encoding UTF8
$result | ConvertTo-Json -Depth 6 -Compress
if (-not $result.ok) { throw ('部分群复盘失败，原习惯保留。执行记录：' + $file) }
