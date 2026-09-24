<#
.SYNOPSIS
    下载 gemini-web2api-go 网关程序,并校验 SHA256。

.DESCRIPTION
    从 GitHub Releases 下载对应平台的单文件二进制,放到仓库根目录。

    默认从 config\stack.psd1 的 ProxySeed 取代理 —— 在多数网络环境下,
    直连 GitHub 的下载 CDN 会超时,走代理只需数秒。

    校验和优先通过 GitHub API 的 asset digest 获取(一次请求同时拿到版本、
    下载地址和摘要)。校验失败会删除文件并非零码退出。

.EXAMPLE
    pwsh -File scripts/fetch-gateway.ps1                    # 用配置里的代理
    pwsh -File scripts/fetch-gateway.ps1 -Proxy ''          # 强制直连
    pwsh -File scripts/fetch-gateway.ps1 -Proxy 'http://127.0.0.1:10809'
    pwsh -File scripts/fetch-gateway.ps1 -Version v4.20.1
#>

[CmdletBinding()]
param(
    [string]$Version = '',       # 留空 = 取最新正式版
    [string]$Destination = '',   # 留空 = 仓库根目录
    [string]$Proxy,              # 省略 = 从配置读;传 '' = 强制直连
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$repo = 'zexadev/gemini-web2api-go'
$apiHeaders = @{
    'User-Agent' = 'image-verifier-stack'
    'Accept'     = 'application/vnd.github+json'
}

# ── 代理解析 ──────────────────────────────────────────────────────────
# 未显式传参时,沿用配置里的 ProxySeed(用户配它说明它可用)。
$proxyExplicit = $PSBoundParameters.ContainsKey('Proxy')
$useProxy = ''
if ($proxyExplicit) {
    $useProxy = "$Proxy"
} else {
    try {
        $cfg = Get-StackConfig
        if ($cfg.ProxySeed) { $useProxy = "$($cfg.ProxySeed)" }
    } catch { }
}

if (-not $Destination) { $Destination = Join-Path $script:RepoRoot 'gemini-web2api-go.exe' }

Write-Header '获取网关程序'
if ($useProxy) {
    Write-Status '代理' $useProxy 'Gray'
} else {
    Write-Status '代理' '直连(未配置代理)' 'DarkGray'
}

if ((Test-Path -LiteralPath $Destination) -and -not $Force) {
    $size = [math]::Round((Get-Item -LiteralPath $Destination).Length / 1MB, 1)
    Write-Status '已存在' "$Destination ($size MB)" 'Green'
    Write-Host '  要重新下载,加 -Force' -ForegroundColor DarkGray
    return
}

# ── 1. 查询 release ───────────────────────────────────────────────────

if ($Version) {
    $tag = if ($Version.StartsWith('v')) { $Version } else { "v$Version" }
    $apiUrl = "https://api.github.com/repos/$repo/releases/tags/$tag"
    Write-Status '指定版本' $tag 'Gray'
} else {
    $apiUrl = "https://api.github.com/repos/$repo/releases/latest"
    Write-Host '  查询最新版本 ...' -ForegroundColor DarkGray
}

$apiArgs = @{ Uri = $apiUrl; Headers = $apiHeaders; TimeoutSec = 60 }
if ($useProxy) { $apiArgs['Proxy'] = $useProxy }

try {
    $release = Invoke-RestMethod @apiArgs
} catch {
    throw @"
无法访问 GitHub API:$($_.Exception.Message)

排查:
  1. 确认代理在运行,且 config\stack.psd1 的 ProxySeed 配置正确
  2. 手动指定代理重试:
       pwsh -File scripts/fetch-gateway.ps1 -Proxy 'http://127.0.0.1:7890'
  3. 或手动下载后放到: $Destination
     下载页: https://github.com/$repo/releases
"@
}

$tag = $release.tag_name
Write-Status '版本' $tag 'Green'

# ── 2. 定位平台资产 ───────────────────────────────────────────────────

$arch = if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { '386' }
$assetName = "gemini-web2api-go_${tag}_windows_${arch}.exe"

$asset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
if (-not $asset) {
    $available = ($release.assets | ForEach-Object { "    " + $_.name }) -join [Environment]::NewLine
    throw "该版本里没有找到 $assetName`n`n可用资产:`n$available"
}

$url = $asset.browser_download_url
Write-Status '目标文件' $assetName 'Gray'

# 优先用 API 提供的 digest,省一次额外请求
$expected = $null
if ($asset.digest -and "$($asset.digest)".StartsWith('sha256:')) {
    $expected = "$($asset.digest)".Substring(7).ToLower()
    Write-Status '期望摘要' '来自 API asset digest' 'Gray'
}

# ── 3. 下载 ───────────────────────────────────────────────────────────

$tmpDir = Join-Path $env:TEMP ('gwfetch-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
$tmpExe = Join-Path $tmpDir $assetName
$tmpSums = Join-Path $tmpDir 'SHA256SUMS.txt'

function Invoke-CurlDownload {
    param([string]$Url, [string]$Out, [string]$Proxy, [int]$MaxMinutes = 15)
    $curlArgs = @('-L', '-sS', '--fail',
                  '--retry', '4', '--retry-delay', '3', '--retry-all-errors',
                  '--connect-timeout', '60', '--max-time', "$($MaxMinutes * 60)",
                  '-o', $Out)
    if ($Proxy) { $curlArgs += @('-x', $Proxy) }
    $curlArgs += $Url
    & curl.exe @curlArgs
    return $LASTEXITCODE
}

try {
    Write-Host '  下载二进制 (约 17 MB) ...' -ForegroundColor DarkGray
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $code = Invoke-CurlDownload -Url $url -Out $tmpExe -Proxy $useProxy
    $sw.Stop()

    # 有代理且失败时,再试一次直连(代理可能是仅对特定流量生效的规则)
    if ($code -ne 0 -and $useProxy) {
        Write-Host '    代理下载失败,尝试直连 ...' -ForegroundColor DarkYellow
        $code = Invoke-CurlDownload -Url $url -Out $tmpExe -Proxy ''
    }

    if ($code -ne 0) {
        throw @"
下载失败(curl 退出码 $code)。

可能原因:网络到 GitHub 的下载 CDN 不通。

解决办法:
  1. 配置代理后重试 —— 编辑 config\stack.psd1 的 ProxySeed,或:
       pwsh -File scripts/fetch-gateway.ps1 -Proxy 'http://127.0.0.1:7890' -Force
  2. 手动下载后放到: $Destination
     下载页: https://github.com/$repo/releases
"@
    }

    $size = [math]::Round((Get-Item -LiteralPath $tmpExe).Length / 1MB, 2)
    Write-Status '下载完成' "$size MB  用时 $([math]::Round($sw.Elapsed.TotalSeconds,1)) 秒" 'Green'

    # ── 4. 校验 ───────────────────────────────────────────────────────

    if (-not $expected) {
        Write-Host '  API 未提供摘要,尝试下载校验文件 ...' -ForegroundColor DarkGray
        $sumsUrl = ($url -replace '[^/]+$', 'SHA256SUMS.txt')
        $sumsCode = Invoke-CurlDownload -Url $sumsUrl -Out $tmpSums -Proxy $useProxy -MaxMinutes 2
        if ($sumsCode -eq 0) {
            foreach ($line in (Get-Content -LiteralPath $tmpSums)) {
                if ($line -match [regex]::Escape($assetName)) {
                    $expected = ($line -split '\s+')[0].ToLower()
                    break
                }
            }
        }
    }

    if (-not $expected) {
        Write-Host ''
        Write-Status 'SHA256' '未取得期望摘要,完整性未核对' 'DarkYellow'
        Write-Host '    文件来自官方 HTTPS 源,但未做完整性验证。手动核对:' -ForegroundColor DarkGray
        Write-Host "      https://github.com/$repo/releases/download/$tag/SHA256SUMS.txt" -ForegroundColor DarkGray
    } else {
        $actual = (Get-FileHash -LiteralPath $tmpExe -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expected) {
            throw @"
SHA256 不匹配 —— 文件可能在传输中被篡改,已中止。

  期望: $expected
  实际: $actual
"@
        }
        Write-Status 'SHA256' '校验通过' 'Green'
    }

    # ── 5. 落位 ───────────────────────────────────────────────────────

    $destDir = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $destDir)) {
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    }
    Move-Item -LiteralPath $tmpExe -Destination $Destination -Force
    Write-Status '已放置' $Destination 'Green'

    if (Test-Path -LiteralPath $tmpSums) {
        Copy-Item -LiteralPath $tmpSums -Destination (Join-Path $destDir 'SHA256SUMS.txt') -Force
    }

    $ver = (& $Destination --version 2>$null | Select-Object -First 1)
    Write-Status '可执行' "$ver" 'Green'

} finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
