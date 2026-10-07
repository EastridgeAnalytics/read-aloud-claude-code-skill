<#
Installs the read-aloud reader for Claude Code on Windows.

Run from the folder you cloned:   .\install.ps1

It creates an isolated Python environment in this folder, installs pinned packages, downloads the
Kokoro voice model, checks its checksum, and registers the /read-aloud command with Claude Code.
Nothing is installed outside this folder except two small files:
  <ClaudeDir>\commands\read-aloud.md   the slash command
  <BinDir>\read-aloud.cmd              a shortcut so "read-aloud pause" works in any terminal
#>
param(
    [string]$ClaudeDir = (Join-Path $HOME '.claude'),
    [string]$BinDir = (Join-Path $HOME '.local\bin'),
    [switch]$NoSoundTest
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $here '.venv\Scripts\python.exe'
$script = Join-Path $here 'read_aloud.py'
function Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }

Step 'Checking for uv'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'uv is not installed. Install it, open a new terminal, and run this script again:'
    Write-Host '    winget install --id=astral-sh.uv -e'
    exit 1
}

Step 'Creating an isolated Python 3.13 environment in .venv'
uv venv --python 3.13 (Join-Path $here '.venv')
if ($LASTEXITCODE -ne 0) { exit 1 }

Step 'Installing the pinned packages (hashes required, prebuilt wheels only)'
uv pip sync (Join-Path $here 'requirements.txt') --require-hashes --only-binary :all: --python $python
if ($LASTEXITCODE -ne 0) { exit 1 }

Step 'Getting the Kokoro voice model (about 340 MB, one time)'
$models = Join-Path $here 'models'
$source = 'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1'
foreach ($line in Get-Content (Join-Path $models 'SHA256SUMS')) {
    $parts = $line.Trim() -split '\s+'
    if ($parts.Count -lt 2) { continue }
    $want = $parts[0].ToUpper()
    $name = $parts[1].TrimStart('*')
    $file = Join-Path $models $name
    if ((Test-Path $file) -and ((Get-FileHash $file -Algorithm SHA256).Hash -eq $want)) {
        Write-Host "  $name is already here and matches its checksum"
        continue
    }
    Write-Host "  downloading $name"
    curl.exe -L --fail --progress-bar -o $file "$source/$name"
    if ($LASTEXITCODE -ne 0) { throw "Download of $name failed." }
    if ((Get-FileHash $file -Algorithm SHA256).Hash -ne $want) {
        Remove-Item -LiteralPath $file -Force
        throw "$name did not match its published checksum, so it was deleted. Run the script again."
    }
    Write-Host "  $name matches its checksum"
}

Step 'Registering the /read-aloud command with Claude Code'
$noBom = New-Object System.Text.UTF8Encoding $false
$template = [IO.File]::ReadAllText((Join-Path $here 'commands\read-aloud.md'))
$command = $template.Replace('{{PYTHON}}', $python.Replace('\', '/')).Replace('{{SCRIPT}}', $script.Replace('\', '/')).Replace('{{FOLDER}}', $here)
$commandDir = Join-Path $ClaudeDir 'commands'
New-Item -ItemType Directory -Force $commandDir | Out-Null
[IO.File]::WriteAllText((Join-Path $commandDir 'read-aloud.md'), $command, $noBom)
Write-Host "  wrote $(Join-Path $commandDir 'read-aloud.md')"

Step 'Adding the read-aloud terminal shortcut'
New-Item -ItemType Directory -Force $BinDir | Out-Null
$shim = "@echo off`r`n`"$python`" `"$script`" %*`r`n"
[IO.File]::WriteAllText((Join-Path $BinDir 'read-aloud.cmd'), $shim, $noBom)
Write-Host "  wrote $(Join-Path $BinDir 'read-aloud.cmd')"
$onPath = ($env:PATH -split ';') | Where-Object { $_.TrimEnd('\') -ieq $BinDir.TrimEnd('\') }
if (-not $onPath) {
    Write-Host "  $BinDir is not on your PATH, so 'read-aloud' will not work as a bare command yet."
    Write-Host "  Add that folder to your PATH, or use the /read-aloud command and the hotkeys instead."
}

if (-not $NoSoundTest) {
    Step 'Sound test: you should hear one sentence'
    & $python $script --text 'Read aloud is installed and working.' --foreground
    if ($LASTEXITCODE -ne 0) { throw 'The sound test failed. See the message above.' }
}

Step 'Done'
Write-Host 'Restart Claude Code, then type  /read-aloud  after any reply, or  /read-aloud <file>.'
Write-Host 'While it reads: Ctrl+Alt+P pauses or resumes, Ctrl+Alt+Q stops.'
