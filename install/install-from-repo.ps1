<#
.SYNOPSIS
    Install locmodel-image (MCP server + companion Skill) straight from the GitHub
    repository, with no prebuilt exe and no pip step.

.DESCRIPTION
    Two things get deployed:

      MCP    scripts/*.py  -> %LOCALAPPDATA%\locmodel-image\repo\scripts
             (pure standard-library Python; the host launches locmodel_mcp.py over stdio)

      Skill  SKILL.md      -> <host skills dir>\locmodel-image\SKILL.md

    Files are fetched one by one from raw.githubusercontent.com at the pinned ref and
    every file is checked against install/manifest.json (SHA-256). Nothing is written
    outside the two target roots. Host config files are only touched when you pass
    -Register; a .bak-<timestamp> copy is made first.

    This script is additive: it never touches an existing exe install at
    %LOCALAPPDATA%\locmodel-image (that tree is used by locmodel-image-setup.exe).

.PARAMETER Component
    mcp | skill | both   (default: both)

.PARAMETER Host
    auto | claude | marvis | workbuddy | cursor | vscode | generic
    Controls where SKILL.md goes and which config schema -Register writes.
    auto (default) deploys the skill to every host directory that already exists.

.PARAMETER Ref
    Git ref to fetch from. Default: the version pinned in the local manifest.

.PARAMETER InstallRoot
    Where the Python payload goes. Default: %LOCALAPPDATA%\locmodel-image\repo

.PARAMETER Source
    manifest    fetch each file from raw.githubusercontent.com (default)
    zip         fetch the release asset zip once and unpack it
    local       use -FromPath, no network at all

.PARAMETER FromPath
    Repository root to copy from when -Source local.

.PARAMETER Register
    Also write the MCP entry into the host config (claude | cursor | vscode).
    For marvis / workbuddy / generic the JSON is printed for manual pasting instead,
    because those hosts rewrite their own config at runtime.

.PARAMETER DryRun
    Print every planned action, download nothing, write nothing.

.EXAMPLE
    # the normal case: everything, auto-detect hosts, no config editing
    powershell -ExecutionPolicy Bypass -File .\install-from-repo.ps1

.EXAMPLE
    # skill only, into Claude's skills dir, and register the MCP server too
    .\install-from-repo.ps1 -Component both -Host claude -Register claude

.EXAMPLE
    # air-gapped machine: unzip the release asset, then
    .\install-from-repo.ps1 -Source local -FromPath .\locmodel-image-repo-install-v4.1.0
#>

[CmdletBinding()]
param(
    [ValidateSet('mcp', 'skill', 'both')] [string]$Component = 'both',
    [Alias('Host')]
    [ValidateSet('auto', 'claude', 'marvis', 'workbuddy', 'cursor', 'vscode', 'generic')] [string]$HostName = 'auto',
    [string]$Ref = '',
    [string]$InstallRoot = '',
    [ValidateSet('manifest', 'zip', 'local')] [string]$Source = 'manifest',
    [string]$FromPath = '',
    [ValidateSet('', 'claude', 'cursor', 'vscode', 'marvis', 'workbuddy', 'all')] [string]$Register = '',
    [switch]$DryRun,
    [switch]$Force,
    [switch]$CreateHostDirs
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

# ---------------------------------------------------------------- helpers

$script:StepNo = 0
function Step($msg) { $script:StepNo++; Write-Host ("[{0}] {1}" -f $script:StepNo, $msg) -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" -ForegroundColor Gray }
function Ok($msg)   { Write-Host "    OK  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    !!  $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "    ERR $msg" -ForegroundColor Red; exit 1 }

function Expand-HostPath([string]$p) {
    if (-not $p) { return $p }
    $p = $p.Replace('%LOCALAPPDATA%', $env:LOCALAPPDATA).
            Replace('%APPDATA%', $env:APPDATA).
            Replace('%USERPROFILE%', $env:USERPROFILE).
            Replace('%TEMP%', $env:TEMP)
    return [Environment]::ExpandEnvironmentVariables($p)
}

function Get-Sha256([string]$path) {
    return (Get-FileHash -Path $path -Algorithm SHA256).Hash.ToLower()
}

function Invoke-Download([string]$url, [string]$dest) {
    $dir = Split-Path -Parent $dest
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -Headers @{ 'User-Agent' = 'locmodel-image-repo-install' }
}

function Copy-Payload([string]$srcRel, [string]$destAbs) {
    $src = Join-Path $script:LocalRoot $srcRel
    if (-not (Test-Path $src)) { Die "payload file missing in -FromPath: $srcRel" }
    $dir = Split-Path -Parent $destAbs
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Copy-Item -Path $src -Destination $destAbs -Force
}

function Write-TextFile([string]$path, [string]$text) {
    $dir = Split-Path -Parent $path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false)))
}

function Get-HostDefs {
    $hostsJson = Join-Path $script:ScriptRoot 'hosts.json'
    if (-not (Test-Path $hostsJson)) {
        $tmp = Join-Path $env:TEMP 'locmodel-image-hosts.json'
        Invoke-Download "$script:RawBase/install/hosts.json" $tmp
        $hostsJson = $tmp
    }
    return (Get-Content -Path $hostsJson -Raw -Encoding UTF8 | ConvertFrom-Json)
}

# ---------------------------------------------------------------- header

$script:ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:LocalRoot = ''
$script:RawBase = ''
$script:Manifest = $null

Write-Host ''
Write-Host 'locmodel-image  .  install from repository' -ForegroundColor White
Write-Host 'MCP server + companion Skill, standard-library Python, no pip, no exe' -ForegroundColor DarkGray
Write-Host ''

# ---------------------------------------------------------------- step 1: manifest

Step 'Load install manifest'
$manifestPath = Join-Path $script:ScriptRoot 'manifest.json'
if ($Source -eq 'local') {
    if (-not $FromPath) { Die '-Source local needs -FromPath <repo root or unzipped release>' }
    $script:LocalRoot = (Resolve-Path $FromPath).Path
    if (-not (Test-Path (Join-Path $script:LocalRoot 'install\manifest.json'))) {
        # allow -FromPath to point at the install\ folder itself
        if (Test-Path (Join-Path $script:LocalRoot 'manifest.json')) {
            $script:LocalRoot = Split-Path -Parent $script:LocalRoot
        }
    }
    $manifestPath = Join-Path $script:LocalRoot 'install\manifest.json'
    if (-not (Test-Path $manifestPath)) { Die "manifest.json not found under $script:LocalRoot" }
    $script:Manifest = Get-Content -Path $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $script:ScriptRoot = Split-Path -Parent $manifestPath
    $script:RawBase = $script:Manifest.raw_base
    Ok "local source: $script:LocalRoot"
}
else {
    if (Test-Path $manifestPath) {
        $script:Manifest = Get-Content -Path $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Ok "manifest bundled with script (v$($script:Manifest.version))"
    }
    else {
        $fallbackRef = if ($Ref) { $Ref } else { 'v4.1.0' }
        $url = "https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/$fallbackRef/install/manifest.json"
        $tmp = Join-Path $env:TEMP 'locmodel-image-manifest.json'
        Invoke-Download $url $tmp
        $script:Manifest = Get-Content -Path $tmp -Raw -Encoding UTF8 | ConvertFrom-Json
        Ok "manifest fetched from $fallbackRef"
    }
    if ($Ref -and $Ref -ne $script:Manifest.ref) {
        $script:Manifest.raw_base = "https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/$Ref"
        Warn "ref overridden to $Ref (hashes in the bundled manifest may no longer match)"
    }
    $script:RawBase = $script:Manifest.raw_base
}

$files = @($script:Manifest.files)
Info ("payload: {0} files, {1:N0} bytes total" -f $files.Count, (($files | Measure-Object -Property bytes -Sum).Sum))

# ---------------------------------------------------------------- step 2: target roots

Step 'Resolve target directories'
$hosts = Get-HostDefs
if (-not $InstallRoot) { $InstallRoot = Expand-HostPath $hosts.install_root.windows }
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
Info "mcp    -> $InstallRoot"

$wanted = @()
if ($HostName -ne 'auto') { $wanted = @($HostName) }
else {
    foreach ($h in $hosts.hosts.PSObject.Properties) {
        $dir = Expand-HostPath $h.Value.skill_dir.windows
        if (Test-Path (Split-Path -Parent $dir)) { $wanted += $h.Name }
    }
    if ($wanted.Count -eq 0) { $wanted = @('generic') }
}
foreach ($h in $wanted) {
    $def = $hosts.hosts.$h
    if (-not $def) { Warn "unknown host '$h' - skipped"; continue }
    $dir = Expand-HostPath $def.skill_dir.windows
    Info ("skill  -> {0} : {1}" -f $h, $dir)
}
if ($Component -eq 'skill' -and $wanted.Count -eq 0) { Die 'no host selected for the skill' }

if ($DryRun) {
    Warn 'dry run: nothing will be downloaded or written'
    Write-Host ''
    foreach ($f in $files) {
        if ($f.role -eq 'doc') { continue }
        Write-Host ("    would fetch {0}  ({1:N0} B, sha256 {2}...)" -f $f.path, $f.bytes, $f.sha256.Substring(0, 12))
    }
    exit 0
}

# ---------------------------------------------------------------- step 3: fetch + verify

Step "Fetch payload (source: $Source)"
$stage = Join-Path $env:TEMP ("locmodel-image-stage-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Info "staging in $stage"

$zipMode = ($Source -eq 'zip')
if ($zipMode) {
    $zipPath = Join-Path $stage 'payload.zip'
    Info "downloading $($script:Manifest.asset_zip)"
    Invoke-Download $script:Manifest.asset_zip $zipPath
    $unz = Join-Path $stage 'unz'
    Expand-Archive -Path $zipPath -DestinationPath $unz -Force
    $script:LocalRoot = $unz
    Ok ("unpacked {0:N0} bytes" -f (Get-Item $zipPath).Length)
}

$failed = @()
$need = @('mcp', 'meta', 'skill')
if ($Component -eq 'mcp') { $need = @('mcp', 'meta') }
if ($Component -eq 'skill') { $need = @('skill') }

foreach ($f in ($files | Where-Object { $need -contains $_.role })) {
    $dest = Join-Path $stage $f.path.Replace('/', '\')
    try {
        if ($Source -eq 'local' -or $zipMode) {
            $srcCandidate = Join-Path $script:LocalRoot $f.path.Replace('/', '\')
            if (-not (Test-Path $srcCandidate)) { throw "missing in source tree: $($f.path)" }
            New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
            Copy-Item $srcCandidate $dest -Force
        }
        else {
            Invoke-Download "$script:RawBase/$($f.path)" $dest
        }
        $got = Get-Sha256 $dest
        if ($got -ne $f.sha256.ToLower()) {
            if ($Force) { Warn "$($f.path) sha256 mismatch (ignored, -Force)" }
            else {
                if ($f.required) { throw "sha256 mismatch: expected $($f.sha256.Substring(0,12))..., got $($got.Substring(0,12))..." }
                Warn "$($f.path) sha256 mismatch (optional file, skipped)"; Remove-Item $dest -Force; continue
            }
        }
        Ok "$($f.path)"
    }
    catch {
        $failed += $f.path
        Warn "$($f.path): $($_.Exception.Message)"
    }
}
if ($failed.Count -gt 0) {
    $required = @($files | Where-Object { $_.required -and ($failed -contains $_.path) })
    if ($required.Count -gt 0) {
        Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
        Die ("required files failed: " + ($required.path -join ', '))
    }
}

# ---------------------------------------------------------------- step 4: deploy

Step 'Deploy'
$deployed = @()
if ($Component -eq 'mcp' -or $Component -eq 'both') {
    foreach ($f in ($files | Where-Object { $_.role -eq 'mcp' -or $_.role -eq 'meta' })) {
        $src = Join-Path $stage $f.path.Replace('/', '\')
        if (-not (Test-Path $src)) { continue }
        $dst = Join-Path $InstallRoot $f.path.Replace('/', '\')
        $dir = Split-Path -Parent $dst
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Copy-Item $src $dst -Force
        $deployed += $dst
    }
    Ok "MCP runtime -> $InstallRoot ($(@($files | Where-Object { $_.role -eq 'mcp' }).Count) modules)"
}
if ($Component -eq 'skill' -or $Component -eq 'both') {
    $skillSrc = Join-Path $stage 'SKILL.md'
    if (-not (Test-Path $skillSrc)) { Die 'SKILL.md missing from payload' }
    foreach ($h in $wanted) {
        $def = $hosts.hosts.$h
        if (-not $def) { continue }
        $dir = Expand-HostPath $def.skill_dir.windows
        if (-not (Test-Path (Split-Path -Parent $dir)) -and -not $CreateHostDirs) {
            Warn "$h : parent dir does not exist, skipped (pass -CreateHostDirs to force)"
            continue
        }
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Copy-Item $skillSrc (Join-Path $dir 'SKILL.md') -Force
        $deployed += (Join-Path $dir 'SKILL.md')
        Ok "skill -> $dir"
    }
}

# ---------------------------------------------------------------- step 5: install manifest

Step 'Write install-manifest.json'
$pyExe = 'python'
$cands = @()
foreach ($cand in @('python.exe', 'python3.exe')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch '\\WindowsApps\\') { $cands += $cmd.Source }
}
if ($cands.Count -gt 0) { $pyExe = $cands[0] }
else {
    foreach ($guess in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
                         "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
                         "$env:ProgramFiles\Python312\python.exe",
                         "$env:ProgramFiles\Python311\python.exe")) {
        if (Test-Path $guess) { $pyExe = $guess; break }
    }
}
$instManifest = [ordered]@{
    schema          = 'locmodel-image.repo-install-report/1'
    version         = $script:Manifest.version
    ref             = $script:Manifest.ref
    source          = $Source
    installed_at    = (Get-Date).ToString('o')
    mcp_root        = $InstallRoot
    python          = $pyExe
    entrypoint      = (Join-Path $InstallRoot 'scripts\locmodel_mcp.py')
    hosts           = $wanted
    files           = @($deployed)
    invoke          = @{ command = $pyExe; args = @((Join-Path $InstallRoot 'scripts\locmodel_mcp.py')); env = @{ PYTHONIOENCODING = 'utf-8'; PYTHONUTF8 = '1' } }
}
$instPath = Join-Path $InstallRoot 'install-manifest.json'
if (Test-Path $InstallRoot) {
    Write-TextFile $instPath ($instManifest | ConvertTo-Json -Depth 6)
    Ok $instPath
}

# ---------------------------------------------------------------- step 6: host snippet / register

function Get-McpEntry([string]$hostName) {
    $entry = [pscustomobject]@{
        command = $pyExe
        args    = @((Join-Path $InstallRoot 'scripts\locmodel_mcp.py'))
        env     = [pscustomobject]@{ PYTHONIOENCODING = 'utf-8'; PYTHONUTF8 = '1' }
    }
    return $entry
}

Step 'Host registration'
$regTargets = @()
if ($Register) {
    if ($Register -eq 'all') { $regTargets = @('claude', 'cursor', 'vscode') } else { $regTargets = @($Register) }
}
foreach ($h in $regTargets) {
    $def = $hosts.hosts.$h
    if (-not $def) { Warn "unknown host $h"; continue }
    if (-not $def.mcp_config) {
        Warn "$h : config is managed by the app ($($def.mcp_mode)) - paste the JSON below manually"
        continue
    }
    $cfgPath = Expand-HostPath $def.mcp_config.windows
    $key = $def.mcp_key
    $cfg = $null
    if (Test-Path $cfgPath) {
        Copy-Item $cfgPath "$cfgPath.bak-$(Get-Date -Format yyyyMMddHHmmss)" -Force
        try { $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $cfg = $null; Warn "existing $cfgPath is not valid JSON, starting fresh (backup kept)" }
    }
    else {
        $d = Split-Path -Parent $cfgPath
        if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
    if ($null -eq $cfg) { $cfg = [pscustomobject]@{} }
    if (-not ($cfg.PSObject.Properties.Name -contains $key)) {
        $cfg | Add-Member -NotePropertyName $key -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    $section = $cfg.$key
    if ($null -eq $section) { $cfg | Add-Member -NotePropertyName $key -NotePropertyValue ([pscustomobject]@{}) -Force; $section = $cfg.$key }
    $section | Add-Member -NotePropertyName 'locmodel-image' -NotePropertyValue (Get-McpEntry $h) -Force
    Write-TextFile $cfgPath ($cfg | ConvertTo-Json -Depth 8)
    Ok "$h : written $cfgPath (backup kept)"
}

Write-Host ''
Write-Host 'Paste this into any host that manages its own MCP config:' -ForegroundColor White
$snippet = [ordered]@{ mcpServers = [ordered]@{ 'locmodel-image' = (Get-McpEntry 'manual') } }
Write-Host ($snippet | ConvertTo-Json -Depth 6) -ForegroundColor DarkCyan

# ---------------------------------------------------------------- step 7: verify

Step 'Verify'
if (Test-Path (Join-Path $InstallRoot 'scripts\locmodel_mcp.py')) {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $req = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"repo-install-check","version":"1"}}}'
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = ($req | & $pyExe (Join-Path $InstallRoot 'scripts\locmodel_mcp.py') 2>&1 | Out-String)
        if ($out -match 'serverInfo') { Ok 'stdio handshake responded (serverInfo present)' }
        else { Warn ("handshake produced no serverInfo; first 300 chars: " + $out.Substring(0, [Math]::Min(300, $out.Length))) }
    }
    catch { Warn "handshake failed: $($_.Exception.Message)" }
    try {
        & $pyExe (Join-Path $InstallRoot 'scripts\locmodel_image.py') doctor 2>&1 | Select-Object -First 10 | ForEach-Object { Info $_ }
    }
    catch { Warn "doctor failed to run: $($_.Exception.Message)" }
    finally { $ErrorActionPreference = $prevEap; Remove-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue }
}

Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host "  MCP   $InstallRoot\scripts\locmodel_mcp.py"
Write-Host "  Skill $($wanted -join ', ')"
Write-Host ''
Write-Host 'Next: start LocModel (or any sd-server on 127.0.0.1) with a model loaded, then' -ForegroundColor DarkGray
Write-Host "  $pyExe `"$InstallRoot\scripts\locmodel_image.py`" status" -ForegroundColor DarkGray
