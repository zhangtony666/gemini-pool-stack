<#
.SYNOPSIS
    把本地网关注册到 Cherry Studio(走官方 provider 导入协议)。

.EXAMPLE
    pwsh -File connect-cherry.ps1
    pwsh -File connect-cherry.ps1 -Name '我的号池'
#>

[CmdletBinding()]
param(
    [string]$Name = 'Gemini 号池网关（本地）',
    [string]$BaseUrl,      # 留空自动按配置端口生成
    [string]$ApiKey,       # 留空自动从网关数据库读取
    [string]$Market = 'openai'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

$cfg = Get-StackConfig
$gwExe = Get-GatewayExe -Config $cfg

if (-not $BaseUrl) { $BaseUrl = "http://127.0.0.1:$($cfg.GatewayPort)/v1" }
if (-not $ApiKey)  { $ApiKey = Get-GatewayApiKey -Config $cfg -GatewayExe $gwExe }
if (-not $ApiKey) {
    throw "拿不到网关 API key。确认网关已启动过至少一次: pwsh -File stack.ps1 start -Only gateway"
}

Write-Header '注册到 Cherry Studio'
Write-Status '名称' $Name
Write-Status '地址' $BaseUrl
Write-Status '密钥' ($ApiKey.Substring(0, [Math]::Min(14, $ApiKey.Length)) + '...')

$provider = @{
    id      = 'gemini-pool-local'
    name    = $Name
    type    = $Market
    baseUrl = $BaseUrl
    apiKey  = $ApiKey
} | ConvertTo-Json -Compress

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($provider))
$uri = 'cherrystudio://providers/api-keys?v=1&data=' + [Uri]::EscapeDataString($encoded)

Start-Process -FilePath $uri

Write-Host ''
Write-Host '已唤起 Cherry Studio 的服务商导入页。接下来在界面里:' -ForegroundColor Gray
Write-Host '  1. 确认并保存该服务商' -ForegroundColor DarkGray
Write-Host '  2. 进入「模型」页,点「同步模型」' -ForegroundColor DarkGray
Write-Host '  3. 添加 gemini-3.6-flash' -ForegroundColor DarkGray
Write-Host '  4. 编辑该模型 → 输入模态 → 视觉 = 开' -ForegroundColor DarkGray
