<#
.SYNOPSIS
  Governed Agent Bridge - installer (build plan section 1.1).

.DESCRIPTION
  Checks that Python 3.12+ is available, copies the bridge stack into a
  folder of your choice, and prints the command that starts the local
  approvals dashboard. Plain script: no scheduled tasks, no services,
  no encoded commands, nothing runs at logon.

  The dashboard binds 127.0.0.1 only. Demo data is 100% synthetic.

.PARAMETER TargetDir
  Destination folder. Default: %USERPROFILE%\GovernedAgentBridge

.PARAMETER DryRun
  Show what would be copied, without writing anything.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -TargetDir D:\Tools\GovernedAgentBridge -DryRun
#>
[CmdletBinding()]
param(
    [string]$TargetDir = (Join-Path $env:USERPROFILE 'GovernedAgentBridge'),
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---------------------------------------------------------------------------
# 1. Python 3.12+ check
# ---------------------------------------------------------------------------
function Find-Python {
    foreach ($candidate in @('py', 'python', 'python3')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            if ($candidate -eq 'py') {
                $versionText = & $cmd -3 --version 2>&1
            } else {
                $versionText = & $cmd --version 2>&1
            }
        } catch { continue }
        if ($versionText -match 'Python (\d+)\.(\d+)\.(\d+)') {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 12)) {
                return @{ Exe = $candidate; Version = $versionText.Trim() }
            }
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host 'ERROR: Python 3.12 or newer was not found on PATH.' -ForegroundColor Red
    Write-Host 'Install it from https://www.python.org/downloads/ (check "Add python.exe to PATH"), then re-run this script.'
    exit 1
}
Write-Host "Python found: $($python.Version) (via '$($python.Exe)')"

# ---------------------------------------------------------------------------
# 2. Copy the stack
# ---------------------------------------------------------------------------
$StackDirs  = @('auditlog', 'approvals', 'classifier', 'dashboard', 'executor', 'planner')
$StackFiles = @('demo_run.py', 'README.md', 'install.ps1')

foreach ($dir in $StackDirs) {
    if (-not (Test-Path (Join-Path $SourceDir $dir))) {
        Write-Host "ERROR: expected folder '$dir' is missing next to install.ps1." -ForegroundColor Red
        exit 1
    }
}

if ($DryRun) {
    Write-Host "[DryRun] Would create: $TargetDir"
} else {
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
}

foreach ($dir in $StackDirs) {
    $src = Join-Path $SourceDir $dir
    $dst = Join-Path $TargetDir $dir
    if ($DryRun) {
        Write-Host "[DryRun] Would copy folder: $src -> $dst"
        continue
    }
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Get-ChildItem -Path $src -Recurse -File |
        Where-Object { $_.FullName -notmatch '__pycache__' -and $_.Extension -ne '.pyc' } |
        ForEach-Object {
            $relative = $_.FullName.Substring($src.Length + 1)
            $target = Join-Path $dst $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        }
    Write-Host "Copied: $dir"
}

foreach ($file in $StackFiles) {
    $src = Join-Path $SourceDir $file
    if (-not (Test-Path $src)) { continue }
    if ($DryRun) {
        Write-Host "[DryRun] Would copy file: $src -> $TargetDir"
    } else {
        Copy-Item -LiteralPath $src -Destination (Join-Path $TargetDir $file) -Force
        Write-Host "Copied: $file"
    }
}

# ---------------------------------------------------------------------------
# 3. Done - how to start the dashboard
# ---------------------------------------------------------------------------
Write-Host ''
if ($DryRun) {
    Write-Host '[DryRun] Nothing was written.'
} else {
    Write-Host "Governed Agent Bridge installed in: $TargetDir"
}
Write-Host ''
Write-Host 'Start the approvals dashboard (local only, 127.0.0.1:8765):'
Write-Host "  cd `"$TargetDir`""
if ($python.Exe -eq 'py') {
    Write-Host '  py -3 -m dashboard.server'
} else {
    Write-Host "  $($python.Exe) -m dashboard.server"
}
Write-Host ''
Write-Host 'Then open http://127.0.0.1:8765/ in your browser.'
Write-Host 'Planner selection: set PLANNER=mock (default, offline) or PLANNER=spark'
Write-Host '(live Muse Spark via the Meta Model API, through this PC - enable it'
Write-Host 'only once the API credits are confirmed applied to the account).'
