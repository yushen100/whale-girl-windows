# 从本项目公开 Release 获取完整包，只安装经 GitHub 摘要校验的运行时。
$ErrorActionPreference='Stop'
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
$root=Split-Path -Parent $PSScriptRoot
$node=Join-Path $root 'runtime\node.exe'
$py=Join-Path $root 'runtime\python\python.exe'
if ((Test-Path -LiteralPath $node) -and (Test-Path -LiteralPath $py)) {Write-Host '便携运行时已存在。';exit 0}
$version=(Get-Content -LiteralPath (Join-Path $root 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
$repo='yushen100/whale-girl-windows'
$assetName="whale-girl-windows-$version-full.zip"
$cache=Join-Path $root 'release\runtime-download'
New-Item -ItemType Directory -Force -Path $cache | Out-Null
try {
    $headers=@{'User-Agent'='WhaleGirl-Windows-Installer'}
    $release=Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/v$version" -Headers $headers
    $asset=@($release.assets | Where-Object {$_.name -eq $assetName})
    if ($asset.Count -ne 1 -or $asset[0].digest -notmatch '^sha256:[a-fA-F0-9]{64}$') {throw 'Release 附件或官方 SHA-256 缺失。请等发布附件上传完整。'}
    $zipPath=Join-Path $cache $assetName
    $expected=$asset[0].digest.Substring(7)
    $ready=(Test-Path -LiteralPath $zipPath) -and ((Get-Item -LiteralPath $zipPath).Length -eq $asset[0].size)
    if ($ready) {$ready=(Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash -eq $expected}
    if (-not $ready) {
        Write-Host '正在下载完整运行环境，请等待。'
        $part="$zipPath.part"
        & curl.exe -L --fail --retry 2 --connect-timeout 20 --output $part $asset[0].browser_download_url
        if ($LASTEXITCODE -ne 0) {throw '下载失败，保留 .part 文件用于排查；可改为直接下载 Release 完整包。'}
        if ((Get-Item -LiteralPath $part).Length -ne $asset[0].size -or (Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash -ne $expected) {throw '附件大小或 SHA-256 不匹配，不安装。'}
        Move-Item -LiteralPath $part -Destination $zipPath -Force
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip=[IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $prefix="whale-girl-windows-$version/runtime/"
        $runtimeRoot=[IO.Path]::GetFullPath((Join-Path $root 'runtime'))+[IO.Path]::DirectorySeparatorChar
        foreach ($entry in $zip.Entries) {
            $entryName=$entry.FullName.Replace('\','/')
            if (-not $entryName.StartsWith($prefix)) {continue}
            $rel=$entryName.Substring($prefix.Length)
            if (-not $rel -or $entryName.EndsWith('/')) {continue}
            $target=[IO.Path]::GetFullPath((Join-Path $runtimeRoot $rel))
            if (-not $target.StartsWith($runtimeRoot,[StringComparison]::OrdinalIgnoreCase)) {throw '压缩包包含不安全路径。'}
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,$target,$true)
        }
    } finally {$zip.Dispose()}
    & $node --version
    & $py -I -c "import wechatauto,psutil,win32com,uiautomation;print('运行环境安装完成')"
    if ($LASTEXITCODE -ne 0) {throw '运行时导入自检失败。'}
} catch {
    Write-Error $_.Exception.Message
    throw '请下载完整 Release 包重试，并保留上方错误信息。'
}
