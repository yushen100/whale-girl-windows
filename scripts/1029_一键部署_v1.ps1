param([switch]$Check,[switch]$WebOnly,[switch]$NoBrowser)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$node=Join-Path $root 'runtime\node.exe'
$python=Join-Path $root 'runtime\python\python.exe'
function Check-Runtime {
    if (-not (Test-Path -LiteralPath $node) -or -not (Test-Path -LiteralPath $python)) { throw '运行时不完整，请下载 Release 中的 Windows 完整包。' }
    & $node --version
    if ($LASTEXITCODE -ne 0) { throw 'Node 运行时无法执行。' }
    & $python -I -c "import wechatauto,psutil,win32com,uiautomation,PIL,requests;print('Python 与微信桥接依赖正常')"
    if ($LASTEXITCODE -ne 0) { throw 'Python 桥接依赖不完整。' }
    if (-not (Test-Path -LiteralPath (Join-Path $root 'runtime\ffmpeg\bin\ffmpeg.exe'))) { throw '转码组件缺失，请重新解压完整包。' }
}
if (-not (Test-Path -LiteralPath $node) -or -not (Test-Path -LiteralPath $python)) { & (Join-Path $PSScriptRoot 'setup-runtime.ps1') }
Check-Runtime
if ($Check) { Write-Host '部署自检通过。微信登录与真实收发需由使用者在自己的设备上验收。'; exit 0 }
$configPath=Join-Path $root 'config.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form=New-Object Windows.Forms.Form
    $form.Text='鲸鲸 · 首次部署'; $form.Size=New-Object Drawing.Size(620,610);$form.StartPosition='CenterScreen'
    $fields=@{}
    $rows=@(@('base','模型接口地址','https://api.deepseek.com'),@('model','模型名称','deepseek-chat'),@('key','模型 API Key（本地接口可留空）',''),@('name','机器人称呼','鲸鲸'),@('db','微信数据目录（留空自动识别）',''),@('groups','允许回复的群完整名称，每行一个',''))
    $y=15
    foreach ($row in $rows) {
        $label=New-Object Windows.Forms.Label;$label.Text=$row[1];$label.Location=New-Object Drawing.Point(15,$y);$label.Width=560;$form.Controls.Add($label)
        $y+=23;$box=New-Object Windows.Forms.TextBox;$box.Text=$row[2];$box.Location=New-Object Drawing.Point(15,$y);$box.Width=565
        if ($row[0] -eq 'key') {$box.UseSystemPasswordChar=$true}
        if ($row[0] -eq 'groups') {$box.Multiline=$true;$box.Height=78;$y+=56}
        $form.Controls.Add($box);$fields[$row[0]]=$box;$y+=34
    }
    $bridge=New-Object Windows.Forms.CheckBox;$bridge.Text='启用微信桥接（请先在桌面微信中登录）';$bridge.Checked=$true;$bridge.Location=New-Object Drawing.Point(15,$y);$bridge.Width=560;$form.Controls.Add($bridge)
    $y+=32;$save=New-Object Windows.Forms.Button;$save.Text='保存并启动';$save.Location=New-Object Drawing.Point(430,$y);$save.Width=150;$form.Controls.Add($save)
    $save.Add_Click({
        try {
            $uri=$null
            if (-not [Uri]::TryCreate($fields.base.Text.Trim(),[UriKind]::Absolute,[ref]$uri) -or $uri.Scheme -notin @('http','https')) { throw '请填写有效的 HTTP/HTTPS 模型地址。' }
            if (-not $fields.model.Text.Trim()) { throw '请填写模型名称。' }
            $groups=@($fields.groups.Lines | ForEach-Object {$_.Trim()} | Where-Object {$_} | Select-Object -Unique)
            if ($bridge.Checked -and $groups.Count -eq 0) { throw '启用微信桥接前，至少填写一个允许回复的群名。' }
            $cfg=Get-Content -LiteralPath (Join-Path $root 'config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
            $cfg.provider='custom'
            $cfg | Add-Member -NotePropertyName llm -NotePropertyValue @{provider='custom';baseUrl=$fields.base.Text.Trim();model=$fields.model.Text.Trim();apiKey=$fields.key.Text.Trim()} -Force
            $cfg.persona.name=$fields.name.Text.Trim()
            $cfg.persona.aliases=@(@($fields.name.Text.Trim(),'鲸鲸') | Select-Object -Unique)
            $cfg.server.openBrowser=$false
            $cfg.group.requireAt=$true;$cfg.group.freeChat=$false;$cfg.care.enabled=$false;$cfg.share.enabled=$false
            $cfg.wechat | Add-Member -NotePropertyName allowGroups -NotePropertyValue $groups -Force
            $cfg.wechat | Add-Member -NotePropertyName allowPrivate -NotePropertyValue $false -Force
            $cfg.wechat.dbDir=$fields.db.Text.Trim()
            $cfg.gateway.wechat.enabled=[bool]$bridge.Checked
            $cfg.gateway.wechat.inboundToken=[Guid]::NewGuid().ToString('N')
            $cfg | Add-Member -NotePropertyName deployment -NotePropertyValue @{wechatEnabled=[bool]$bridge.Checked} -Force
            [IO.File]::WriteAllText($configPath,($cfg | ConvertTo-Json -Depth 40),(New-Object Text.UTF8Encoding($false)))
            $form.DialogResult=[Windows.Forms.DialogResult]::OK;$form.Close()
        } catch {[Windows.Forms.MessageBox]::Show($_.Exception.Message,'请检查配置') | Out-Null}
    })
    if ($form.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) {exit 0}
}
$cfg=Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($WebOnly -or $cfg.deployment.wechatEnabled -eq $false) {
    & (Join-Path $PSScriptRoot 'whale-console.ps1') -Target Server
} else {
    & (Join-Path $PSScriptRoot 'whale-console.ps1') -Target Start
}
