# 微信定位失败时，恢复已有微信主窗，不自动最大化；不操作会话、不输入或发送消息。
param([long]$WindowHandle = 0, [switch]$Check)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class WhaleWechatWindow {
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr h);
}
"@
$running = @(Get-Process -Name Weixin -ErrorAction SilentlyContinue)
if ($running.Count -eq 0) { throw '微信未运行，请先手动打开并登录微信。' }
function Find-ExistingWechatWindow {
    if ($WindowHandle -ne 0) {
        $candidate = [IntPtr]$WindowHandle
        [uint32]$ownerId = 0
        $null = [WhaleWechatWindow]::GetWindowThreadProcessId($candidate, [ref]$ownerId)
        if ([WhaleWechatWindow]::IsWindow($candidate) -and $running.Id -contains $ownerId) {
            return $candidate
        }
    }
    return [IntPtr]::Zero
}
$handle = Find-ExistingWechatWindow
if ($Check) {
    [pscustomobject]@{running=$true;windowFound=($handle -ne [IntPtr]::Zero);checkOnly=$true} | ConvertTo-Json -Compress
    exit 0
}
# 只能恢复调用方已经确认的精确窗口，不启动或猜测其他账号。
if ($handle -eq [IntPtr]::Zero) { throw '未提供有效的精确微信主窗句柄，已停止；不按进程顺序选择其他微信。' }
$null = [WhaleWechatWindow]::ShowWindowAsync($handle, 9)
Start-Sleep -Milliseconds 500

[pscustomobject]@{restored=$true;windowHandle=$handle.ToInt64()} | ConvertTo-Json -Compress
