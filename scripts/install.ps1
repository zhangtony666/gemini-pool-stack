<#
.SYNOPSIS
    一键安装:检查环境、下载网关、装 Python 依赖、初始化配置。

.DESCRIPTION
    幂等 —— 可以重复运行,已完成的步骤会自动跳过。

    做的事:
      1. 检查 PowerShell 7 / uv
      2. 下载 gemini-web2api-go 到仓库根目录
      3. 用 uv 安装 Python 依赖
      4. 生成 config\stack.psd1
      5. 跑一次自检

.EXAMPLE
    pwsh -File scripts/install.ps1
    pwsh -File scripts/install.ps1 -SkipGateway     # 已经有网关程序
    pwsh -File scripts/install.ps1 -GatewayVersion v4.20.1
#>

[CmdletBinding()]
param(
    [switch]$SkipGateway,
    [string]$GatewayVersion = '',
    [switch]$SkipPython
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Host ''
Write-Host '  image-verifier-stack  安装' -ForegroundColor White
Write-Host '  ══════════════════════════════════════════════' -ForegroundColor DarkGray

$failed = @()

# ── 1. PowerShell ─────────────────────────────────────────────────────

Write-Header '1/5  运行环境'

if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Status 'PowerShell' "版本 $($PSVersionTable.PSVersion) 过低" 'Red'
    Write-Host '    需要 PowerShell 7+。安装:' -ForegroundColor DarkGray
    Write-Host '      winget install Microsoft.PowerShell' -ForegroundColor DarkGray
    $failed += 'PowerShell 7'
} else {
    Write-Status 'PowerShell' "v$($PSVersionTable.PSVersion) OK" 'Green'
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    Write-Status 'uv' "$($uv.Source)" 'Green'
} else {
    Write-Status 'uv' '未安装' 'DarkYellow'
    Write-Host ''
    Write-Host '    正在安装 uv ...' -ForegroundColor Gray
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($uv) {
            Write-Status 'uv' '安装完成(可能需要重开终端)' 'Green'
        } else {
            Write-Status 'uv' '安装后仍未找到,请重开终端再跑一次' 'DarkYellow'
            $failed += 'uv'
        }
    } catch {
        Write-Status 'uv' "安装失败: $($_.Exception.Message)" 'Red'
        Write-Host '    手动安装: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"' -ForegroundColor DarkGray
        $failed += 'uv'
    }
}

# ── 2. 配置 ───────────────────────────────────────────────────────────

Write-Header '2/5  配置文件'
Initialize-Config
if (Test-Path -LiteralPath $script:ConfigPath) {
    Write-Status '配置文件' $script:ConfigPath 'Green'
}
$cfg = Get-StackConfig
Write-Status '网关端口' "$($cfg.GatewayPort)"
Write-Status '后端端口' "$($cfg.VerifierPort)"

# ── 3. 网关 ───────────────────────────────────────────────────────────

Write-Header '3/5  网关程序'

if ($SkipGateway) {
    Write-Status '跳过' '按参数要求跳过' 'DarkGray'
} else {
    $existing = $null
    try { $existing = Get-GatewayExe -Config $cfg } catch { }

    if ($existing) {
        $size = [math]::Round((Get-Item -LiteralPath $existing).Length / 1MB, 1)
        Write-Status '已存在' "$existing ($size MB)" 'Green'
    } else {
        Write-Host '    未找到,开始下载 ...' -ForegroundColor Gray
        $fetchArgs = @()
        if ($GatewayVersion) { $fetchArgs += @('-Version', $GatewayVersion) }
        try {
            & (Join-Path $PSScriptRoot 'fetch-gateway.ps1') @fetchArgs
            $cfg = Get-StackConfig
            $exe = Get-GatewayExe -Config $cfg
            Write-Status '下载完成' $exe 'Green'
        } catch {
            Write-Status '下载失败' $_.Exception.Message 'Red'
            Write-Host '    手动下载后放到仓库根目录,或改 config\stack.psd1 的 GatewayExe' -ForegroundColor DarkGray
            $failed += '网关程序'
        }
    }
}

# ── 4. Python 依赖 ────────────────────────────────────────────────────

Write-Header '4/5  Python 依赖'

if ($SkipPython) {
    Write-Status '跳过' '按参数要求跳过' 'DarkGray'
} elseif (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Status '跳过' 'uv 不可用' 'DarkYellow'
} else {
    $project = Get-ProjectRoot
    Push-Location $project
    try {
        Write-Host "    uv sync --frozen (在 $project) ..." -ForegroundColor DarkGray
        & uv sync --frozen 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $venv = Join-Path $project '.venv\Scripts\python.exe'
            if (Test-Path -LiteralPath $venv) {
                $pyVersion = (& $venv -c "import sys; print('.'.join(map(str,sys.version_info[:3])))" 2>$null)
                Write-Status '依赖' "已安装 (Python $pyVersion)" 'Green'
            } else {
                Write-Status '依赖' 'sync 完成但虚拟环境缺失' 'DarkYellow'
            }
        } else {
            Write-Status '依赖' 'uv sync 失败' 'Red'
            $failed += 'Python 依赖'
        }
    } finally {
        Pop-Location
    }
}

# ── 5. 自检 ───────────────────────────────────────────────────────────

Write-Header '5/5  自检'

$gwExe = $null
try { $gwExe = Get-GatewayExe -Config $cfg } catch { }
Write-Status '网关程序' $(if ($gwExe) { 'OK' } else { '缺失' }) $(if ($gwExe) { 'Green' } else { 'Red' })

$projectRoot = $null
try { $projectRoot = Get-ProjectRoot } catch { }
if ($projectRoot) {
    $venvOk = Test-Path -LiteralPath (Join-Path $projectRoot '.venv\Scripts\python.exe')
    Write-Status 'Python 环境' $(if ($venvOk) { 'OK' } else { '缺失' }) $(if ($venvOk) { 'Green' } else { 'DarkYellow' })
} else {
    Write-Status 'Python 环境' '找不到项目目录' 'Red'
}

foreach ($p in @(@($cfg.GatewayPort, '网关端口'), @($cfg.VerifierPort, '后端端口'))) {
    $owner = Get-PortOwner -Port $p[0]
    Write-Status $p[1] $(if ($owner) { "被 $($owner.ProcessName) 占用" } else { '空闲' }) `
        $(if ($owner) { 'DarkYellow' } else { 'Gray' })
}

# ── 结束 ──────────────────────────────────────────────────────────────

Write-Host ''
if ($failed.Count -eq 0) {
    Write-Host '  ══════════════════════════════════════════════' -ForegroundColor DarkGray
    Write-Host '  安装完成' -ForegroundColor Green
    Write-Host ''
    Write-Host '  下一步:' -ForegroundColor Gray
    Write-Host '    1. 配置代理(如果默认 7890 不对)' -ForegroundColor DarkGray
    Write-Host '       编辑 config\stack.psd1 里的 ProxySeed' -ForegroundColor DarkGray
    Write-Host '    2. 启动服务' -ForegroundColor DarkGray
    Write-Host '       pwsh -File scripts/stack.ps1 start -Only gateway' -ForegroundColor DarkGray
    Write-Host '    3. 导入一个 Google 账号的 cookie' -ForegroundColor DarkGray
    Write-Host '       pwsh -File scripts/account.ps1 import-firefox' -ForegroundColor DarkGray
    Write-Host '    4. 接上 Cherry Studio' -ForegroundColor DarkGray
    Write-Host '       pwsh -File scripts/connect-cherry.ps1' -ForegroundColor DarkGray
} else {
    Write-Host '  ══════════════════════════════════════════════' -ForegroundColor DarkGray
    Write-Host ("  安装未完全成功,以下步骤失败: " + ($failed -join ', ')) -ForegroundColor DarkYellow
    Write-Host ''
    Write-Host '  修好后重新运行本脚本即可(已完成的步骤会自动跳过)。' -ForegroundColor Gray
    Write-Host '  详细排查: pwsh -File scripts/stack.ps1 doctor' -ForegroundColor DarkGray
}
Write-Host ''
