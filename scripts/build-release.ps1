param([string]$OutDir='', [string]$RuntimeSource='', [switch]$SkipFull)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$node=Join-Path $root 'runtime\node.exe'
if (-not (Test-Path -LiteralPath $node)) {$node='node'}
if (-not $OutDir) {$OutDir=Join-Path $root 'release\packages'}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Push-Location $root
try {
    & $node --test 'tests/*.test.js'
    if ($LASTEXITCODE -ne 0) {throw '测试失败，不出包。'}
    $version=(Get-Content -LiteralPath (Join-Path $root 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
    $build=Join-Path $OutDir ('build-'+[Guid]::NewGuid().ToString('N'))
    $name="whale-girl-windows-$version"
    $src=Join-Path $build "$name-source"
    New-Item -ItemType Directory -Force -Path $src | Out-Null
    $args=@($root,$src,'/E','/NFL','/NDL','/NJH','/NJS','/NP','/XD','data','runtime','release','drafts','content','.git','node_modules','__pycache__','wechatauto_logs','wechatauto_db','/XF','config.json','.env','*.log','*.pyc','*.pyo','*.part')
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -ge 8) {throw '源码复制失败。'}
    Push-Location $src
    try { & $node 'scripts/release-check.js' --release; $releaseCode=$LASTEXITCODE } finally {Pop-Location}
    if ($releaseCode -ne 0) {throw '公开发布自检失败。'}
    $srcZip=Join-Path $OutDir "$name-source.zip"
    if (Test-Path -LiteralPath $srcZip) {throw '目标 ZIP 已存在，请选择新输出目录，避免覆盖已有发布。'}
    & tar.exe -a -cf $srcZip -C $build "$name-source"
    if ($LASTEXITCODE -ne 0) {throw '源码 ZIP 生成失败。'}
    if (-not $SkipFull) {
        if (-not $RuntimeSource) {$RuntimeSource=Join-Path $root 'runtime'}
        if (-not (Test-Path -LiteralPath (Join-Path $RuntimeSource 'python\python.exe'))) {throw '完整包需要完整便携运行时。'}
        $full=Join-Path $build $name
        & robocopy $src $full /E /NFL /NDL /NJH /NJS /NP | Out-Null
        & robocopy $RuntimeSource (Join-Path $full 'runtime') /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ /XF *.pyc *.log | Out-Null
        if ($LASTEXITCODE -ge 8) {throw '运行时复制失败。'}
        $third=Join-Path $root 'third_party'
        if (Test-Path -LiteralPath $third) {Copy-Item -LiteralPath $third -Destination $full -Recurse}
        $fullZip=Join-Path $OutDir "$name-full.zip"
        if (Test-Path -LiteralPath $fullZip) {throw '目标完整 ZIP 已存在。'}
        & tar.exe -a -cf $fullZip -C $build $name
        if ($LASTEXITCODE -ne 0) {throw '完整 ZIP 生成失败。'}
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    foreach ($archiveFile in Get-ChildItem -LiteralPath $OutDir -Filter "$name-*.zip") {
        $zip=[IO.Compression.ZipFile]::OpenRead($archiveFile.FullName)
        try {
            $entry=@($zip.Entries | Where-Object {$_.FullName.Replace('\','/').EndsWith('/web/index.html')})
            if ($entry.Count -ne 1) {throw 'ZIP 缺少唯一界面入口。'}
            $reader=New-Object IO.StreamReader($entry[0].Open())
            try {$html=$reader.ReadToEnd()} finally {$reader.Dispose()}
            if (-not $html.Contains('visionApiKey')) {throw 'ZIP 中看图配置字段丢失。'}
            foreach ($e in $zip.Entries) {
                if ($e.FullName.Replace('\','/') -match '^[^/]+/(data/|config\.json$|wechatauto_logs/|drafts/)') {throw 'ZIP 混入私有运行数据。'}
            }
        } finally {$zip.Dispose()}
    }
    $lines=@()
    foreach ($file in Get-ChildItem -LiteralPath $OutDir -Filter "$name-*.zip") {
        $hash=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $lines+="$hash  $($file.Name)|bytes=$($file.Length)"
    }
    $lines | Set-Content -LiteralPath (Join-Path $OutDir 'SHA256SUMS.txt') -Encoding UTF8
    Write-Host "完成：$OutDir。构建副本保留用于核对。"
} finally {Pop-Location}
