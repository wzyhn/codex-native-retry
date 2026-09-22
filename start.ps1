param(
    [switch]$DryRun,
    [string]$Runtime,
    [string]$SessionsDir
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand) { $pythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue }
if (-not $pythonCommand) { throw 'python.exe or py.exe was not found on PATH' }

$python = $pythonCommand.Source
$script = Join-Path $project 'src\codex_native_retry.py'
$arguments = @($script, 'start')
if ($DryRun) { $arguments += '--dry-run' }
if ($Runtime) { $arguments += @('--runtime', $Runtime) }
if ($SessionsDir) { $arguments += @('--sessions-dir', $SessionsDir) }

& $python @arguments
exit $LASTEXITCODE
