# ═══════════════════════════════════════════════════════════════════════
#  公共模块:配置、路径、密钥、进程控制
#  被 scripts/ 下的所有脚本 dot-source。不要直接运行本文件。
# ═══════════════════════════════════════════════════════════════════════

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# common.ps1 位于 <仓库根>\scripts\lib\,所以仓库根要往上两级
$script:RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$script:ConfigPath = Join-Path $script:RepoRoot 'config\stack.psd1'
$script:ExampleConfig = Join-Path $script:RepoRoot 'config\stack.example.psd1'

# Python 项目根。仓库布局是 <仓库根>\app\pyproject.toml;
# 若把项目放在仓库根,则自动回退到仓库根。
function Get-ProjectRoot {
    $candidates = @(
        (Join-Path $script:RepoRoot 'app'),
        $script:RepoRoot
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $c 'pyproject.toml')) { return $c }
    }
    throw "找不到 pyproject.toml(查过 app\ 和仓库根)"
}

# ── 配置 ──────────────────────────────────────────────────────────────

function Initialize-Config {
    <# 首次运行:从模板生成实际配置 #>
    if (Test-Path -LiteralPath $script:ConfigPath) { return }
    if (-not (Test-Path -LiteralPath $script:ExampleConfig)) {
        throw "找不到配置模板: $script:ExampleConfig"
    }
    Copy-Item -LiteralPath $script:ExampleConfig -Destination $script:ConfigPath
    Write-Host '  已从模板生成 config\stack.psd1' -ForegroundColor DarkGray
}

function Get-StackConfig {
    Initialize-Config
    return Import-PowerShellDataFile -LiteralPath $script:ConfigPath
}

function Save-StackConfig {
    param([hashtable]$Config)
    # 仅在回写自动生成的密钥时调用。注释会丢失,这是 .psd1 的固有限制。
    $lines = @(
        '# 由脚本自动生成 —— 详细说明见 config\stack.example.psd1',
        '@{',
        "    GatewayExe    = '$($Config.GatewayExe)'",
        "    GatewayPort   = $($Config.GatewayPort)",
        "    VerifierPort  = $($Config.VerifierPort)",
        "    AdminToken    = '$($Config.AdminToken)'",
        "    GatewayApiKey = '$($Config.GatewayApiKey)'",
        "    ProxySeed     = '$($Config.ProxySeed)'",
        "    GatewayHidden = `$$($Config.GatewayHidden.ToString().ToLower())",
        "    OpenAdminOnStart = `$$($Config.OpenAdminOnStart.ToString().ToLower())",
        '}'
    )
    Set-Content -LiteralPath $script:ConfigPath -Value ($lines -join [Environment]::NewLine) -Encoding UTF8
}

# ── 网关程序定位 ──────────────────────────────────────────────────────

function Get-GatewayExe {
    param([hashtable]$Config)

    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($Config.GatewayExe) { $candidates.Add($Config.GatewayExe) }

    $candidates.Add((Join-Path $script:RepoRoot 'gemini-web2api-go.exe'))
    $candidates.Add((Join-Path $script:RepoRoot 'bin\gemini-web2api-go.exe'))
    $candidates.Add((Join-Path $script:RepoRoot 'gemini-gateway\gemini-web2api-go.exe'))

    $sibling = Join-Path (Split-Path -Parent $script:RepoRoot) 'gemini-gateway\gemini-web2api-go.exe'
    $candidates.Add($sibling)

    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }

    $tried = ($candidates | Where-Object { $_ } | ForEach-Object { "    $_" }) -join [Environment]::NewLine
    throw @"
找不到网关程序 gemini-web2api-go.exe。

已查找:
$tried

解决(二选一):
  1. 运行 pwsh -File scripts/fetch-gateway.ps1   自动下载
  2. 在 config\stack.psd1 里把 GatewayExe 填成完整路径
"@
}

# 网关的数据目录 = exe 旁边的 data/,与项目目录无关
function Get-GatewayDataDir {
    param([string]$Exe)
    return (Join-Path (Split-Path -Parent $Exe) 'data')
}

function Get-GatewayLogDir {
    param([string]$Exe)
    return (Split-Path -Parent $Exe)
}

# ── 密钥 ──────────────────────────────────────────────────────────────

function New-RandomToken {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return 'adm-' + ([Convert]::ToHexString($bytes).ToLower())
}

function Get-AdminToken {
    param([hashtable]$Config)
    if ($Config.AdminToken) { return $Config.AdminToken }
    $token = New-RandomToken
    $Config.AdminToken = $token
    Save-StackConfig -Config $Config
    Write-Host '  已生成管理 token 并写入 config\stack.psd1' -ForegroundColor DarkGray
    return $token
}

function Get-GatewayApiKey {
    param([hashtable]$Config, [string]$GatewayExe)

    if ($Config.GatewayApiKey) { return $Config.GatewayApiKey }

    $db = Join-Path (Get-GatewayDataDir -Exe $GatewayExe) 'gemini.db'
    if (-not (Test-Path -LiteralPath $db)) { return '' }

    $py = Get-PythonCommand
    $code = @"
import sqlite3
try:
    con = sqlite3.connect(r'$($db.Replace('\','\\'))')
    row = con.execute("SELECT v FROM kv WHERE k='api_key'").fetchone()
    print(row[0] if row else '')
except Exception:
    print('')
"@
    $tmp = Join-Path $env:TEMP ('gwkey-' + [guid]::NewGuid().ToString('N') + '.py')
    Set-Content -LiteralPath $tmp -Value $code -Encoding UTF8
    try {
        $key = (& $py.Exe @($py.Args) $tmp 2>$null | Select-Object -First 1)
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    $key = "$key".Trim()

    if ($key) {
        $Config.GatewayApiKey = $key
        Save-StackConfig -Config $Config
    }
    return $key
}

# ── Python ────────────────────────────────────────────────────────────

function Get-PythonCommand {
    <# 返回 @{Exe=...; Args=@(...)},优先项目自己的 uv 环境 #>
    $project = Get-ProjectRoot
    $venv = Join-Path $project '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return @{ Exe = $venv; Args = @() } }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { return @{ Exe = $uv.Source; Args = @('run', '--frozen', '--project', $project, 'python') } }

    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py) { return @{ Exe = $py.Source; Args = @() } }

    throw @'
找不到可用的 Python。

安装 uv(推荐,会自动管理 Python 版本):
    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
'@
}

# ── 进程控制 ──────────────────────────────────────────────────────────

function Test-PortListening {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-PortOwner {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $conn) { return $null }
    return (Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue)
}

function Stop-Gateway {
    param([int]$Port)
    $stopped = 0
    Get-Process -Name 'gemini-web2api-go' -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        $stopped++
    }
    $owner = Get-PortOwner -Port $Port
    if ($owner -and $owner.ProcessName -notmatch 'gemini-web2api-go') {
        Write-Warning "端口 $Port 被 '$($owner.ProcessName)' (pid $($owner.Id)) 占用,那不是本项目的进程。"
    }
    if ($stopped -gt 0) { Start-Sleep -Seconds 2 }
    return $stopped
}

function Stop-Verifier {
    param([int]$Port)
    $killed = 0
    try {
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='uv.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'image.verifier' } |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                $killed++
            }
    } catch { }

    $owner = Get-PortOwner -Port $Port
    if ($owner -and $owner.ProcessName -match 'python|uv') {
        Stop-Process -Id $owner.Id -Force -ErrorAction SilentlyContinue
        $killed++
    }
    if ($killed -gt 0) { Start-Sleep -Seconds 1 }
    return $killed
}

function Wait-HttpOk {
    param([string]$Url, [int]$TimeoutSeconds = 30, [int]$IntervalMs = 400)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest $Url -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds $IntervalMs
    }
    return $false
}

# ── 展示 ──────────────────────────────────────────────────────────────

function Write-Header {
    param([string]$Text)
    Write-Host ''
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ('─' * [Math]::Max($Text.Length, 8)) -ForegroundColor DarkCyan
}

function Write-Status {
    param([string]$Label, [string]$Value, [string]$Color = 'Gray')
    Write-Host ("  {0,-16}" -f $Label) -NoNewline
    Write-Host $Value -ForegroundColor $Color
}
