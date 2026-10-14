<#
.SYNOPSIS
Extracts conversation data from all supported AI coding assistants.

.DESCRIPTION
Windows/PowerShell equivalent of extract_all.sh. It runs each extractor,
writes per-tool logs to extracted_data, reports which tools produced
conversations, and creates one combined JSONL file.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$OutputEncoding = [System.Text.UTF8Encoding]::new()
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    [Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
}
catch {
    # Some redirected/non-interactive hosts do not expose console encoding.
}

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ScriptRoot) {
    $ScriptRoot = (Get-Location).Path
}
Set-Location $ScriptRoot

function Get-PythonRunner {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return [pscustomobject]@{
            Command = $python.Source
            PrefixArgs = @()
        }
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return [pscustomobject]@{
            Command = $py.Source
            PrefixArgs = @('-3')
        }
    }

    throw 'Python was not found. Install Python 3 or add python.exe/py.exe to PATH.'
}

function Format-FileSize {
    param([long]$Bytes)

    if ($Bytes -ge 1GB) {
        return '{0:N2} GB' -f ($Bytes / 1GB)
    }
    if ($Bytes -ge 1MB) {
        return '{0:N2} MB' -f ($Bytes / 1MB)
    }
    if ($Bytes -ge 1KB) {
        return '{0:N2} KB' -f ($Bytes / 1KB)
    }
    return "$Bytes B"
}

function Invoke-Extraction {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Script,

        [Parameter(Mandatory = $true)]
        [string]$LogFile,

        [Parameter(Mandatory = $true)]
        [string]$SuccessPattern,

        [Parameter(Mandatory = $true)]
        [pscustomobject]$PythonRunner
    )

    Write-Host "Extracting from $Name..."

    $arguments = @()
    $arguments += $PythonRunner.PrefixArgs
    $arguments += $Script

    $output = & $PythonRunner.Command @arguments 2>&1 | Tee-Object -FilePath $LogFile
    $exitCode = $LASTEXITCODE
    $text = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine

    Write-Host ''

    return [pscustomobject]@{
        Success = ($text -match $SuccessPattern)
        Failed = ($exitCode -ne 0)
        ExitCode = $exitCode
    }
}

Write-Host '================================================================================'
Write-Host 'AI CODING ASSISTANT DATA EXTRACTION - ALL TOOLS'
Write-Host '================================================================================'
Write-Host ''

$outputDir = Join-Path $ScriptRoot 'extracted_data'
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

$previousPythonIoEncoding = $env:PYTHONIOENCODING
$previousPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$pythonRunner = Get-PythonRunner
$foundTools = New-Object System.Collections.Generic.List[string]
$notFound = New-Object System.Collections.Generic.List[string]
$failedTools = New-Object System.Collections.Generic.List[string]

$extractors = @(
    @{ Name = 'Claude Code'; Script = 'extract_claude_code.py'; Log = 'claude_extraction.log'; Pattern = 'Total conversations: [1-9]' },
    @{ Name = 'Cursor'; Script = 'extract_cursor.py'; Log = 'cursor_extraction.log'; Pattern = 'Total conversations: [1-9]' },
    @{ Name = 'Codex'; Script = 'extract_codex.py'; Log = 'codex_extraction.log'; Pattern = 'Total conversations: [1-9]' },
    @{ Name = 'Trae'; Script = 'extract_trae.py'; Log = 'trae_extraction.log'; Pattern = 'Total conversations: [1-9]' },
    @{ Name = 'Windsurf'; Script = 'extract_windsurf.py'; Log = 'windsurf_extraction.log'; Pattern = 'Total conversations: [1-9]' },
    @{ Name = 'Continue'; Script = 'extract_continue.py'; Log = 'continue_extraction.log'; Pattern = 'Found [1-9]' },
    @{ Name = 'Gemini CLI'; Script = 'extract_gemini.py'; Log = 'gemini_extraction.log'; Pattern = 'Total conversations: [1-9]' },
    @{ Name = 'OpenCode'; Script = 'extract_opencode.py'; Log = 'opencode_extraction.log'; Pattern = 'Total conversations extracted: [1-9]' }
)

foreach ($extractor in $extractors) {
    $scriptPath = Join-Path $ScriptRoot $extractor.Script
    $logPath = Join-Path $outputDir $extractor.Log

    if (-not (Test-Path -LiteralPath $scriptPath)) {
        Write-Host "Skipping $($extractor.Name): $($extractor.Script) was not found."
        Write-Host ''
        $notFound.Add($extractor.Name)
        continue
    }

    $result = Invoke-Extraction `
        -Name $extractor.Name `
        -Script $scriptPath `
        -LogFile $logPath `
        -SuccessPattern $extractor.Pattern `
        -PythonRunner $pythonRunner

    if ($result.Success) {
        $foundTools.Add($extractor.Name)
    }
    elseif ($result.Failed) {
        $failedTools.Add("$($extractor.Name) (exit code $($result.ExitCode), log: $logPath)")
    }
    else {
        $notFound.Add($extractor.Name)
    }
}

Write-Host '================================================================================'
Write-Host 'EXTRACTION SUMMARY'
Write-Host '================================================================================'
Write-Host ''

if ($foundTools.Count -gt 0) {
    Write-Host 'Successfully extracted from:'
    foreach ($tool in $foundTools) {
        Write-Host "   - $tool"
    }
    Write-Host ''
}

if ($notFound.Count -gt 0) {
    Write-Host 'No data found for:'
    foreach ($tool in $notFound) {
        Write-Host "   - $tool"
    }
    Write-Host ''
}

if ($failedTools.Count -gt 0) {
    Write-Host 'Extraction errors:'
    foreach ($tool in $failedTools) {
        Write-Host "   - $tool"
    }
    Write-Host ''
}

$jsonlFiles = @(Get-ChildItem -Path $outputDir -Filter '*.jsonl' -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike 'ALL_CONVERSATIONS_*.jsonl' })

$totalLines = 0
foreach ($file in $jsonlFiles) {
    $totalLines += (Get-Content -LiteralPath $file.FullName -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
}

Write-Host "Total conversations extracted: $totalLines"
Write-Host ''

Write-Host 'Output files:'
foreach ($file in $jsonlFiles) {
    Write-Host "   $($file.FullName) ($(Format-FileSize $file.Length))"
}
Write-Host ''

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$combinedFile = Join-Path $outputDir "ALL_CONVERSATIONS_$timestamp.jsonl"

if ($jsonlFiles.Count -gt 0) {
    Get-Content -LiteralPath $jsonlFiles.FullName -Encoding UTF8 |
        Set-Content -LiteralPath $combinedFile -Encoding UTF8
}
else {
    New-Item -ItemType File -Path $combinedFile -Force | Out-Null
}

$combinedItem = Get-Item -LiteralPath $combinedFile
Write-Host 'Combined file created:'
Write-Host "   $combinedFile ($(Format-FileSize $combinedItem.Length))"
Write-Host ''

Write-Host '================================================================================'
Write-Host 'COMPLETE!'
Write-Host '================================================================================'

$env:PYTHONIOENCODING = $previousPythonIoEncoding
$env:PYTHONUTF8 = $previousPythonUtf8
