<#
.SYNOPSIS
    统一控制:启动 / 停止 / 查看状态 / 自检。

.DESCRIPTION
    管理两个服务:
      - gemini-web2api-go  资源网关     默认 :8084
      - image-verifier     业务后端     默认 :8083

    所有路径、端口、密钥来自 config\stack.psd1,脚本内无硬编码。

.EXAMPLE
    pwsh -File scripts/stack.ps1 start
    pwsh -File scripts/stack.ps1 start -Only gateway
    pwsh -File scripts/stack.ps1 stop
    pwsh -File scripts/stack.ps1 status
    pwsh -File scripts/stack.ps1 doctor
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'doctor', 'logs', 'config')]
    [string]$Action = 'status',

    [ValidateSet('all', 'gateway', 'verifier')]
    [string]$Only = 'all',

    [switch]$MockVerifier,   # 后端用 mock 启动,不连网关
    [switch]$Foreground      # 网关前台运行,看实时日志
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$cfg = Get-StackConfig
$gwPort = $cfg.GatewayPort
$ivPort = $cfg.VerifierPort
$gwUrl = "http://127.0.0.1:$gwPort"
$ivUrl = "http://127.0.0.1:$ivPort"

# ── 启动 ──────────────────────────────────────────────────────────────

function Start-Gateway {
    param([hashtable]$Cfg, [string]$Exe, [int]$Port, [string]$Token, [bool]$Hide)

    $dataDir = Get-GatewayDataDir -Exe $Exe
    $logDir = Get-GatewayLogDir -Exe $Exe
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

    $existing = Get-PortOwner -Port $Port
    if ($existing) {
        Write-Host "  端口 $Port 被占用,先停旧实例 ..." -ForegroundColor DarkYellow
        Stop-Gateway -Port $Port | Out-Null
        Start-Sleep -Seconds 2
    }

    $args = @('--port', "$Port", '--admin-token', $Token, '--db', (Join-Path $dataDir 'gemini.db'))
    if ($Cfg.ProxySeed) { $args += @('--proxy', $Cfg.ProxySeed) }

    $outLog = Join-Path $logDir 'gateway.out.log'
    $errLog = Join-Path $logDir 'gateway.err.log'

    if ($Hide) {
        $proc = Start-Process -FilePath $Exe -ArgumentList $args -WorkingDirectory $logDir `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog
        Start-Sleep -Seconds 5
        if (Wait-HttpOk -Url "$gwUrl/" -TimeoutSeconds 20) {
            Write-Status '网关' "已启动 (pid $($proc.Id)) :$Port" 'Green'
            return $true
        }
        Write-Status '网关' '启动失败' 'Red'
        Get-Content -LiteralPath $errLog -ErrorAction SilentlyContinue |
            Select-Object -Last 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        return $false
    }

    Write-Host '  前台运行,按 Ctrl+C 停止' -ForegroundColor DarkGray
    & $Exe @args
    return $true
}

function Start-Verifier {
    param([hashtable]$Cfg, [int]$Port, [bool]$Mock)

    $project = Get-ProjectRoot

    $existing = Get-PortOwner -Port $Port
    if ($existing) {
        Write-Host "  端口 $Port 被占用,先停旧实例 ..." -ForegroundColor DarkYellow
        Stop-Verifier -Port $Port | Out-Null
        Start-Sleep -Seconds 1
    }

    $envFile = Join-Path $project '.env'

    if ($Mock) {
        # 用环境变量覆盖,不动 .env
        $env:IV_BACKEND = 'mock'
        $env:IV_PORT = "$Port"
        $env:IV_DB_PATH = (Join-Path $project 'work\mock.sqlite3')
    } else {
        if (-not (Test-Path -LiteralPath $envFile)) {
            Write-Status '业务后端' '缺少 .env,未启动' 'DarkYellow'
            Write-Host '    见 README 的「配置后端」;或加 -MockVerifier 先跑通流程' -ForegroundColor DarkGray
            return $false
        }
        # 补齐关键项,避免配置遗漏导致连错端口
        $text = Get-Content -LiteralPath $envFile -Raw
        $add = @()
        if ($text -notmatch '(?m)^\s*IV_PORT\s*=') { $add += "IV_PORT=$Port" }
        if ($text -notmatch '(?m)^\s*IV_WEB_BASE_URL\s*=') { $add += "IV_WEB_BASE_URL=$gwUrl/v1" }
        if ($add.Count) { Add-Content -LiteralPath $envFile -Value $add }
    }

    $runArgs = @('run', '--frozen')
    if ((Test-Path -LiteralPath $envFile) -and -not $Mock) { $runArgs += @('--env-file', '.env') }
    $runArgs += @('image-verifier', 'serve')

    $outLog = Join-Path $project 'verifier.out.log'
    $errLog = Join-Path $project 'verifier.err.log'

    $proc = Start-Process -FilePath 'uv' -ArgumentList $runArgs -WorkingDirectory $project `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    Start-Sleep -Seconds 3

    if (Wait-HttpOk -Url "$ivUrl/health/live" -TimeoutSeconds 40) {
        Write-Status '业务后端' "已启动 (pid $($proc.Id)) :$Port" 'Green'
        return $true
    }
    Write-Status '业务后端' '启动失败' 'Red'
    Get-Content -LiteralPath $errLog -ErrorAction SilentlyContinue |
        Select-Object -Last 12 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
    return $false
}

# ── 分发 ──────────────────────────────────────────────────────────────

switch ($Action) {

    'config' {
        Write-Header '当前配置'
        Write-Status '配置文件' $script:ConfigPath
        try { Write-Status '网关程序' (Get-GatewayExe -Config $cfg) 'Green' }
        catch { Write-Status '网关程序' '未找到' 'Red' }
        Write-Status '项目目录' $script:RepoRoot
        Write-Status '网关端口' "$gwPort"
        Write-Status '后端端口' "$ivPort"
        Write-Status '管理 token' $(if ($cfg.AdminToken) { '(已设置)' } else { '(首次启动生成)' })
        Write-Status '代理播种' $(if ($cfg.ProxySeed) { $cfg.ProxySeed } else { '(不播种)' })
    }

    'doctor' {
        Write-Header '环境自检'
        $problems = @()

        # 工具链
        if ($PSVersionTable.PSVersion.Major -ge 7) {
            Write-Status 'PowerShell' "v$($PSVersionTable.PSVersion) OK" 'Green'
        } else {
            Write-Status 'PowerShell' "v$($PSVersionTable.PSVersion) 需要 7+" 'Red'
            $problems += 'PowerShell'
        }
        foreach ($tool in @('uv', 'curl', 'git')) {
            $c = Get-Command $tool -ErrorAction SilentlyContinue
            Write-Status $tool $(if ($c) { 'OK' } else { '未安装' }) $(if ($c) { 'Gray' } else { 'DarkYellow' })
        }

        # 网关程序
        try {
            $exe = Get-GatewayExe -Config $cfg
            $size = [math]::Round((Get-Item -LiteralPath $exe).Length / 1MB, 1)
            Write-Status '网关程序' "$exe ($size MB)" 'Green'
        } catch {
            Write-Status '网关程序' '未找到' 'Red'
            $problems += '网关程序'
        }

        # Python 环境
        $project = $null
        try { $project = Get-ProjectRoot } catch { }
        if ($project) {
            $venv = Join-Path $project '.venv\Scripts\python.exe'
            if (Test-Path -LiteralPath $venv) {
                Write-Status 'Python 环境' '已就绪' 'Green'
            } else {
                Write-Status 'Python 环境' '未创建' 'DarkYellow'
                $problems += 'Python 环境'
            }
        } else {
            Write-Status 'Python 环境' '找不到项目目录' 'Red'
            $problems += 'Python 环境'
        }

        # 配置
        $envPath = if ($project) { Join-Path $project '.env' } else { $null }
        Write-Status '.env' $(if ($envPath -and (Test-Path -LiteralPath $envPath)) { '存在' } else { '不存在(只能跑 mock)' }) 'Gray'

        # 端口
        foreach ($p in @(@($gwPort, '网关端口'), @($ivPort, '后端端口'))) {
            $owner = Get-PortOwner -Port $p[0]
            if ($owner) {
                Write-Status $p[1] "被 $($owner.ProcessName) 占用" 'DarkYellow'
            } else {
                Write-Status $p[1] '空闲' 'Gray'
            }
        }

        # 代理连通性
        $proxy = $cfg.ProxySeed
        if ($proxy) {
            Write-Host '  测试代理连通性 ...' -ForegroundColor DarkGray
            $code = & curl.exe -x $proxy -sS -o NUL -w '%{http_code}' --connect-timeout 15 --max-time 40 `
                'https://gemini.google.com/' 2>$null
            if ($code -eq '200') {
                Write-Status '代理 / Google' '可达 (200)' 'Green'
            } else {
                Write-Status '代理 / Google' "返回 $code — 检查代理是否在运行" 'DarkYellow'
            }
        }

        Write-Host ''
        if ($problems.Count -eq 0) {
            Write-Host '  一切就绪。运行 start 启动。' -ForegroundColor Green
        } else {
            Write-Host ("  待处理: " + ($problems -join ', ')) -ForegroundColor DarkYellow
            if ($problems -contains '网关程序') {
                Write-Host '    → pwsh -File scripts/fetch-gateway.ps1' -ForegroundColor DarkGray
            }
            if ($problems -contains 'Python 环境') {
                Write-Host '    → pwsh -File scripts/install.ps1' -ForegroundColor DarkGray
            }
        }
    }

    'start' {
        Write-Header '启动'
        if ($Only -in @('all', 'gateway')) {
            $exe = Get-GatewayExe -Config $cfg
            $token = Get-AdminToken -Config $cfg
            $hide = if ($Foreground) { $false } else { [bool]$cfg.GatewayHidden }
            Start-Gateway -Cfg $cfg -Exe $exe -Port $gwPort -Token $token -Hide $hide | Out-Null
        }
        if ($Only -in @('all', 'verifier')) {
            Start-Verifier -Cfg $cfg -Port $ivPort -Mock:$MockVerifier | Out-Null
        }
        Write-Host ''
        Write-Host "  管理面板   $gwUrl/admin" -ForegroundColor DarkGray
        Write-Host "  网关 API   $gwUrl/v1" -ForegroundColor DarkGray
        if ($cfg.OpenAdminOnStart -and $Only -in @('all', 'gateway')) { Start-Process "$gwUrl/admin" }
    }

    'stop' {
        Write-Header '停止'
        if ($Only -in @('all', 'gateway')) {
            $n = Stop-Gateway -Port $gwPort
            Write-Status '网关' $(if ($n) { "已停止 ($n)" } else { '未运行' }) 'Gray'
        }
        if ($Only -in @('all', 'verifier')) {
            $n = Stop-Verifier -Port $ivPort
            Write-Status '业务后端' $(if ($n) { '已停止' } else { '未运行' }) 'Gray'
        }
    }

    'restart' {
        Write-Header '重启'
        if ($Only -in @('all', 'gateway')) { Stop-Gateway -Port $gwPort | Out-Null }
        if ($Only -in @('all', 'verifier')) { Stop-Verifier -Port $ivPort | Out-Null }
        Start-Sleep -Seconds 2
        & $PSCommandPath start -Only $Only -MockVerifier:$MockVerifier -Foreground:$Foreground
    }

    'status' {
        Write-Header '运行状态'

        if (Test-PortListening -Port $gwPort) {
            try {
                $info = Invoke-RestMethod "$gwUrl/" -TimeoutSec 5
                Write-Status '网关' "运行中 v$($info.version) :$gwPort" 'Green'
            } catch {
                Write-Status '网关' "无响应 :$gwPort" 'DarkYellow'
            }
            if ($cfg.AdminToken) {
                try {
                    $null = Invoke-RestMethod "$gwUrl/admin/api/login" -Method Post `
                        -ContentType 'application/json' `
                        -Body (@{ token = $cfg.AdminToken } | ConvertTo-Json) `
                        -SessionVariable sess -TimeoutSec 10
                    $ck = Invoke-RestMethod "$gwUrl/admin/api/cookies" -WebSession $sess -TimeoutSec 10
                    $px = Invoke-RestMethod "$gwUrl/admin/api/proxies" -WebSession $sess -TimeoutSec 10
                    Write-Host ''
                    Write-Status 'Cookie 池' "$($ck.enabled) 启用 / 共 $($ck.total)" `
                        $(if ($ck.enabled -gt 0) { 'Green' } else { 'DarkYellow' })
                    Write-Status '代理池' "$($px.items.Count) 个" 'Gray'
                } catch { }
            }
        } else {
            Write-Status '网关' "未运行 :$gwPort" 'DarkYellow'
        }

        if (Test-PortListening -Port $ivPort) {
            try {
                $h = Invoke-RestMethod "$ivUrl/health/live" -TimeoutSec 5
                $mode = "$($h.backend)"
                if ($h.simulated) { $mode += ' (模拟)' }
                Write-Status '业务后端' "运行中 $mode :$ivPort" $(if ($h.simulated) { 'DarkYellow' } else { 'Green' })
            } catch {
                Write-Status '业务后端' "无响应 :$ivPort" 'DarkYellow'
            }
        } else {
            Write-Status '业务后端' "未运行 :$ivPort" 'DarkYellow'
        }
    }

    'logs' {
        $exe = Get-GatewayExe -Config $cfg
        $log = Join-Path (Get-GatewayLogDir -Exe $exe) 'gateway.err.log'
        Write-Header '网关日志(最后 30 行)'
        if (Test-Path -LiteralPath $log) {
            Get-Content -LiteralPath $log -Tail 30
        } else {
            Write-Host '  日志文件不存在' -ForegroundColor DarkGray
        }
    }
}
