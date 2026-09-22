param(
    [switch]$StartWithWindows
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
  "initial_delay_seconds": 5,
  "max_delay_seconds": 60,
  "max_attempts": 5,
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
    $line = '@echo off' + [Environment]::NewLine + 'start "" /min "' + $python + '" "' + $project + '\src\codex_native_retry.py" watch --dry-run' + [Environment]::NewLine
    $line | Set-Content -LiteralPath $cmd -Encoding ASCII
    Write-Host "Created $cmd (dry-run)."
}

Write-Host "Installed Codex Native Retry in $project"
Write-Host "Config: $config"
Write-Host "Run: python `"$project\src\codex_native_retry.py`" diagnose"
