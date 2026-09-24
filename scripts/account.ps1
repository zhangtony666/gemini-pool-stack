<#
.SYNOPSIS
    账号管理:导入 / 查看 / 检测 / 删除 Cookie 池中的 Google 账号。

.DESCRIPTION
    从各种来源读 cookie 并导入网关。支持:
      - Firefox 的 cookies.sqlite(直接读,推荐)
      - Cookie-Editor 导出的 JSON 文件
      - 一行式文本  SID=xxx; HSID=xxx; ...

    端口和 token 从 config\stack.psd1 读,脚本内无硬编码。

.EXAMPLE
    pwsh -File account.ps1 list
    pwsh -File account.ps1 import-firefox                 # 自动找 Firefox profile
    pwsh -File account.ps1 import-file -Path 'D:\c.json' -Label 'acc02'
    pwsh -File account.ps1 check-all
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('list', 'import-firefox', 'import-file', 'import-string', 'check-all', 'enable', 'disable', 'remove', 'profiles')]
    [string]$Action = 'list',

    [string]$Path,           # import-file 用
    [string]$Cookie,         # import-string 用
    [string]$Label = '',
    [string]$Note = '',
    [int]$Id = 0,            # enable/disable/remove 用
    [string]$Profile = '',   # import-firefox 用,留空自动挑选
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$cfg = Get-StackConfig
$gwPort = $cfg.GatewayPort
$base = "http://127.0.0.1:$gwPort"
$token = Get-AdminToken -Config $cfg

$REQUIRED = @('SID', 'HSID', 'SSID', 'APISID', 'SAPISID', '__Secure-1PSID', '__Secure-1PSIDTS')

# ── 会话 ──────────────────────────────────────────────────────────────

function Connect-Admin {
    if (-not (Test-PortListening -Port $gwPort)) {
        throw "网关没在运行(:$gwPort)。先跑: pwsh -File stack.ps1 start -Only gateway"
    }
    $null = Invoke-RestMethod "$base/admin/api/login" -Method Post `
        -ContentType 'application/json' `
        -Body (@{ token = $token } | ConvertTo-Json) -SessionVariable sess -TimeoutSec 15
    return $sess
}

# ── cookie 解析 ───────────────────────────────────────────────────────

function ConvertTo-CookieMap {
    param([string]$Raw)

    $map = @{}
    $asJson = $null
    try { $asJson = $Raw | ConvertFrom-Json } catch { $asJson = $null }

    if ($asJson) {
        $items = @()
        if ($asJson -is [System.Array]) { $items = $asJson }
        elseif ($asJson.PSObject.Properties.Name -contains 'cookies') { $items = $asJson.cookies }
        else { $items = @($asJson) }
        foreach ($item in $items) {
            if ($item.PSObject.Properties.Name -contains 'name' -and
                $item.PSObject.Properties.Name -contains 'value') {
                $map[$item.name] = $item.value
            }
        }
    } else {
        foreach ($piece in ($Raw -split '[;\r\n]+')) {
            $piece = $piece.Trim()
            if (-not $piece) { continue }
            $idx = $piece.IndexOf('=')
            if ($idx -gt 0) {
                $map[$piece.Substring(0, $idx).Trim()] = $piece.Substring($idx + 1).Trim()
            }
        }
    }
    return $map
}

function Assert-CookieComplete {
    param([hashtable]$Map)
    $missing = @($REQUIRED | Where-Object { -not $Map.ContainsKey($_) -or -not $Map[$_] })
    if ($missing.Count -gt 0) {
        Write-Host ("  缺少字段: " + ($missing -join ', ')) -ForegroundColor Red
        Write-Host ('  该来源里找到的 cookie 名:') -ForegroundColor DarkGray
        $Map.Keys | Sort-Object | ForEach-Object { Write-Host "     $_" -ForegroundColor DarkGray }
        throw 'cookie 不完整'
    }
    return ($REQUIRED | ForEach-Object { "$_=$($Map[$_])" }) -join '; '
}

# ── Firefox ───────────────────────────────────────────────────────────

function Get-FirefoxProfiles {
    $ini = Join-Path $env:APPDATA 'Mozilla\Firefox\profiles.ini'
    if (-not (Test-Path -LiteralPath $ini)) { return @() }

    $root = Join-Path $env:APPDATA 'Mozilla\Firefox'
    $profiles = @()
    $current = $null
    foreach ($line in (Get-Content -LiteralPath $ini)) {
        $line = $line.Trim()
        if ($line -match '^\[(.+)\]$') {
            if ($current -and $current.Path) { $profiles += [pscustomobject]$current }
            $current = @{ Name = $Matches[1]; Path = $null }
        } elseif ($line -match '^Path=(.+)$' -and $current) {
            $current.Path = $Matches[1]
        } elseif ($line -match '^Name=(.+)$' -and $current) {
            $current.Name = $Matches[1]
        }
    }
    if ($current -and $current.Path) { $profiles += [pscustomobject]$current }

    $result = @()
    foreach ($p in $profiles) {
        $dir = Join-Path $root ($p.Path -replace '/', '\')
        $db = Join-Path $dir 'cookies.sqlite'
        if (Test-Path -LiteralPath $db) {
            $result += [pscustomobject]@{
                Name    = $p.Name
                Dir     = $dir
                Db      = $db
                Updated = (Get-Item -LiteralPath $db).LastWriteTime
            }
        }
    }
    return $result | Sort-Object Updated -Descending
}

function Read-FirefoxCookies {
    param([string]$ProfileDir)

    $src = Join-Path $ProfileDir 'cookies.sqlite'
    $tmpDir = Join-Path $env:TEMP ('ffck-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

    try {
        foreach ($f in @('cookies.sqlite', 'cookies.sqlite-wal', 'cookies.sqlite-shm')) {
            $s = Join-Path $ProfileDir $f
            if (Test-Path -LiteralPath $s) { Copy-Item -LiteralPath $s -Destination (Join-Path $tmpDir $f) -Force }
        }
        $copy = (Join-Path $tmpDir 'cookies.sqlite').Replace('\', '\\')

        $code = @"
import sqlite3
con = sqlite3.connect(r'$copy')
rows = con.execute("SELECT name, value FROM moz_cookies WHERE host LIKE '%google.com'").fetchall()
want = ['SID','HSID','SSID','APISID','SAPISID','__Secure-1PSID','__Secure-1PSIDTS']
got = {}
for n, v in rows:
    if n in want and n not in got:
        got[n] = v
for k in want:
    print(f'{k}={got.get(k, "")}')
"@
        $py = Get-PythonCommand
        $script = Join-Path $tmpDir 'read.py'
        Set-Content -LiteralPath $script -Value $code -Encoding UTF8
        $out = & $py.Exe @($py.Args) $script 2>$null
    } finally {
        Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    $map = @{}
    foreach ($line in $out) {
        $line = "$line".Trim()
        if (-not $line) { continue }
        $i = $line.IndexOf('=')
        if ($i -gt 0) { $map[$line.Substring(0, $i)] = $line.Substring($i + 1) }
    }
    return $map
}

# Get-PythonCommand 由 lib\common.ps1 提供,此处不再重复定义。

# ── 动作 ──────────────────────────────────────────────────────────────

switch ($Action) {

    'profiles' {
        Write-Header 'Firefox 配置文件'
        $profiles = Get-FirefoxProfiles
        if (-not $profiles) {
            Write-Host '  没找到任何带 cookies.sqlite 的 Firefox profile' -ForegroundColor DarkYellow
            break
        }
        foreach ($p in $profiles) {
            Write-Host ("  " + $p.Name) -ForegroundColor White
            Write-Host ("     " + $p.Dir) -ForegroundColor DarkGray
            Write-Host ("     最后更新 " + $p.Updated) -ForegroundColor DarkGray
        }
    }

    'list' {
        $sess = Connect-Admin
        $pool = Invoke-RestMethod "$base/admin/api/cookies" -WebSession $sess -TimeoutSec 15
        Write-Header 'Cookie 池'
        Write-Host ("  共 " + $pool.total + " 个,启用 " + $pool.enabled) -ForegroundColor Gray
        if ($pool.total -eq 0) {
            Write-Host '  池子是空的。用 import-firefox 从 Firefox 导入。' -ForegroundColor DarkYellow
            break
        }
        Write-Host ''
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        foreach ($item in $pool.items) {
            $isEnabled = ($item.status -eq 'enabled')
            $color = if ($isEnabled) { 'Green' } else { 'DarkGray' }
            Write-Host ("  #" + $item.id + "  " + $item.label + "   [" + $item.status + "]") -ForegroundColor $color

            $okAgo = if ($item.last_ok_at) {
                $mins = [int](($now - $item.last_ok_at) / 60)
                if ($mins -lt 60) { "$mins 分钟前" } else { "$([int]($mins/60)) 小时前" }
            } else { '从未' }

            Write-Host ("       健康=" + $item.health + "  失败=" + $item.fail_count +
                        "  最近成功=" + $okAgo +
                        "  出口=" + $(if ($item.proxy_name) { $item.proxy_name } else { '未绑定' })) -ForegroundColor DarkGray
            Write-Host ("       cookie " + $item.cookie_count + " 条  1PSIDTS=" +
                        $(if ($item.has_1psidts) { '有' } else { '无' }) +
                        "  SAPISID末4位=" + $item.sapisid_tail) -ForegroundColor DarkGray
            if ($item.last_error) {
                Write-Host ("       错误: " + $item.last_error) -ForegroundColor DarkYellow
            }
        }
    }

    'import-firefox' {
        $profiles = Get-FirefoxProfiles
        if (-not $profiles) { throw '没找到 Firefox profile' }

        $target = $null
        if ($Profile) {
            $target = $profiles | Where-Object { $_.Name -eq $Profile -or $_.Dir -like "*$Profile*" } |
                Select-Object -First 1
            if (-not $target) { throw "找不到 profile: $Profile(用 profiles 动作查看可用列表)" }
        } else {
            $target = $profiles | Select-Object -First 1
        }

        Write-Header '从 Firefox 导入'
        Write-Status 'profile' $target.Name
        Write-Status '数据库' $target.Db
        Write-Status '最后更新' "$($target.Updated)"

        $map = Read-FirefoxCookies -ProfileDir $target.Dir

        $cookieString = Assert-CookieComplete -Map $map

        if (-not $Label) { $Label = 'firefox-' + (Get-Date -Format 'MMdd-HHmm') }
        Write-Status '标签' $Label
        Write-Status 'cookie' "$($cookieString.Length) 字符"

        if ($DryRun) { Write-Host '  -DryRun:未写入' -ForegroundColor Yellow; break }

        $sess = Connect-Admin
        $payload = @{ cookie = $cookieString; label = $Label; note = $Note } | ConvertTo-Json
        $created = Invoke-RestMethod "$base/admin/api/cookies" -Method Post -WebSession $sess `
            -ContentType 'application/json' -Body $payload -TimeoutSec 60
        Write-Status '导入' "id=$($created.id)" 'Green'

        Write-Host '  检测中...' -ForegroundColor DarkGray
        try {
            $chk = Invoke-RestMethod "$base/admin/api/cookies/$($created.id)/check" `
                -Method Post -WebSession $sess -TimeoutSec 120
            Write-Status '检测' $(if ($chk.ok) { "有效 ($($chk.took_ms)ms)" } else { "无效: $($chk.detail)" }) `
                $(if ($chk.ok) { 'Green' } else { 'Red' })
        } catch {
            Write-Status '检测' "失败: $($_.Exception.Message)" 'DarkYellow'
        }
    }

    'import-file' {
        if (-not $Path) { throw '需要 -Path 指定文件' }
        if (-not (Test-Path -LiteralPath $Path)) { throw "文件不存在: $Path" }

        Write-Header '从文件导入'
        $raw = Get-Content -LiteralPath $Path -Raw
        $map = ConvertTo-CookieMap -Raw $raw
        $cookieString = Assert-CookieComplete -Map $map

        if (-not $Label) { $Label = [IO.Path]::GetFileNameWithoutExtension($Path) }
        Write-Status '文件' $Path
        Write-Status '标签' $Label
        Write-Status 'cookie' "$($cookieString.Length) 字符"

        if ($DryRun) { Write-Host '  -DryRun:未写入' -ForegroundColor Yellow; break }

        $sess = Connect-Admin
        $payload = @{ cookie = $cookieString; label = $Label; note = $Note } | ConvertTo-Json
        $created = Invoke-RestMethod "$base/admin/api/cookies" -Method Post -WebSession $sess `
            -ContentType 'application/json' -Body $payload -TimeoutSec 60
        Write-Status '导入' "id=$($created.id)" 'Green'

        try {
            $chk = Invoke-RestMethod "$base/admin/api/cookies/$($created.id)/check" `
                -Method Post -WebSession $sess -TimeoutSec 120
            Write-Status '检测' $(if ($chk.ok) { '有效' } else { "无效: $($chk.detail)" }) `
                $(if ($chk.ok) { 'Green' } else { 'Red' })
        } catch { }
    }

    'import-string' {
        if (-not $Cookie) { throw '需要 -Cookie 提供 cookie 内容' }
        Write-Header '从字符串导入'
        $map = ConvertTo-CookieMap -Raw $Cookie
        $cookieString = Assert-CookieComplete -Map $map
        if (-not $Label) { $Label = 'manual-' + (Get-Date -Format 'MMdd-HHmm') }

        if ($DryRun) { Write-Host '  -DryRun:未写入' -ForegroundColor Yellow; Write-Status '长度' "$($cookieString.Length)"; break }

        $sess = Connect-Admin
        $payload = @{ cookie = $cookieString; label = $Label; note = $Note } | ConvertTo-Json
        $created = Invoke-RestMethod "$base/admin/api/cookies" -Method Post -WebSession $sess `
            -ContentType 'application/json' -Body $payload -TimeoutSec 60
        Write-Status '导入' "id=$($created.id)" 'Green'
    }

    'check-all' {
        $sess = Connect-Admin
        $pool = Invoke-RestMethod "$base/admin/api/cookies" -WebSession $sess -TimeoutSec 15
        Write-Header '全池检测'
        foreach ($item in $pool.items) {
            Write-Host ("  #" + $item.id + " " + $item.label + " ... ") -NoNewline
            try {
                $chk = Invoke-RestMethod "$base/admin/api/cookies/$($item.id)/check" `
                    -Method Post -WebSession $sess -TimeoutSec 120
                if ($chk.ok) {
                    Write-Host "有效 ($($chk.took_ms)ms)" -ForegroundColor Green
                } else {
                    Write-Host "无效 — $($chk.detail)" -ForegroundColor Red
                }
            } catch {
                Write-Host "检测失败" -ForegroundColor DarkYellow
            }
        }
    }

    'enable' {
        if ($Id -le 0) { throw '需要 -Id' }
        $sess = Connect-Admin
        $null = Invoke-RestMethod "$base/admin/api/cookies/$Id/toggle" -Method Post -WebSession $sess -TimeoutSec 15
        Write-Status "账号 #$Id" '已切换启用状态' 'Green'
    }

    'disable' {
        if ($Id -le 0) { throw '需要 -Id' }
        $sess = Connect-Admin
        $null = Invoke-RestMethod "$base/admin/api/cookies/$Id/toggle" -Method Post -WebSession $sess -TimeoutSec 15
        Write-Status "账号 #$Id" '已切换启用状态' 'Green'
    }

    'remove' {
        if ($Id -le 0) { throw '需要 -Id' }
        $sess = Connect-Admin
        $null = Invoke-RestMethod "$base/admin/api/cookies/$Id" -Method Delete -WebSession $sess -TimeoutSec 15
        Write-Status "账号 #$Id" '已删除' 'Green'
    }
}
