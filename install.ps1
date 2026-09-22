param(
    [switch]$StartWithWindows,
    [switch]$Live
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$appData = Join-Path $env:APPDATA 'CodexNativeRetry'
New-Item -ItemType Directory -Force -Path $appData | Out-Null
$config = Join-Path $appData 'config.json'
if (-not (Test-Path -LiteralPath $config)) {
    @'
{
  "enabled": true,
  "dry_run": true,
  "retry_mode": "dry_run",
  "retry_delays_seconds": [0, 3, 5, 10, 15, 30, 60],
  "max_delay_seconds": 60,
  "max_attempts": 0,
  "jitter_ratio": 0.2,
  "poll_seconds": 5
}
'@ | Set-Content -LiteralPath $config -Encoding UTF8
}

if ($StartWithWindows) {
    $startup = [Environment]::GetFolderPath('Startup')
    $cmd = Join-Path $startup 'CodexNativeRetry.cmd'
    $pythonCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue }
    if (-not $pythonCommand) { throw 'python.exe was not found on PATH' }
    $python = $pythonCommand.Source
    $mode = if ($Live) { 'start' } else { 'start --dry-run' }
    $line = '@echo off' + [Environment]::NewLine + 'start "" /min "' + $python + '" "' + $project + '\src\codex_native_retry.py" ' + $mode + [Environment]::NewLine
    $line | Set-Content -LiteralPath $cmd -Encoding ASCII
    Write-Host "Created $cmd ($mode)."
}

Write-Host "Installed Codex Native Retry in $project"
Write-Host "Config: $config"
Write-Host "Run: python `"$project\src\codex_native_retry.py`" diagnose"
Write-Host "Start all services: `"$project\start.ps1`""
