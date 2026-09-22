param(
    [switch]$RemoveConfig
)

$startup = [Environment]::GetFolderPath('Startup')
$cmd = Join-Path $startup 'CodexNativeRetry.cmd'
if (Test-Path -LiteralPath $cmd) { Remove-Item -LiteralPath $cmd -Force }
if ($RemoveConfig) {
    $appData = Join-Path $env:APPDATA 'CodexNativeRetry'
    if (Test-Path -LiteralPath $appData) { Remove-Item -LiteralPath $appData -Recurse -Force }
}
Write-Host 'Codex Native Retry startup entry removed.'

