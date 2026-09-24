param(
    [string]$BaseUrl = 'http://127.0.0.1:8083',
    [string]$ApiKey = $env:IV_API_KEY
)
$ErrorActionPreference = 'Stop'
# Official provider-import protocol. This does not edit Cherry's internal database.
# Use a non-secret placeholder when the local demo does not require authentication.
if (-not $ApiKey) { $ApiKey = 'local-demo' }
$provider = @{
    id = 'image-verifier-local'
    name = '图片真实性核验（本地）'
    type = 'openai'
    baseUrl = $BaseUrl
    apiKey = $ApiKey
} | ConvertTo-Json -Compress
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($provider))
$uri = 'cherrystudio://providers/api-keys?v=1&data=' + [Uri]::EscapeDataString($encoded)
# User-facing GUI launch requested by this script's caller.
Start-Process -FilePath $uri
Write-Host '已请求 Cherry Studio 打开服务商导入页。'
Write-Host '确认 Chat Completions 端点，获取模型列表，添加 image-verifier-vision，并开启图片输入。'
