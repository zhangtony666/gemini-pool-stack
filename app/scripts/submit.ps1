param(
    [Parameter(Mandatory=$true)][string[]]$Images,
    [string]$BaseUrl = 'http://127.0.0.1:8083',
    [string]$ApiKey = $env:IV_API_KEY,
    [string]$IdempotencyKey = [guid]::NewGuid().ToString('N'),
    [int]$DeadlineSeconds = 300
)
$ErrorActionPreference = 'Stop'
$headers = @{ 'Idempotency-Key' = $IdempotencyKey }
if ($ApiKey) { $headers['Authorization'] = "Bearer $ApiKey" }
$files = @($Images | ForEach-Object { Get-Item -LiteralPath $_ -ErrorAction Stop })
$response = Invoke-RestMethod -Method Post -Uri "$BaseUrl/v1/batches" -Headers $headers -Form @{
    files = $files
    deadline_seconds = $DeadlineSeconds
}
$response | ConvertTo-Json -Depth 5
Write-Host "查询结果: $BaseUrl$($response.status_url)"
