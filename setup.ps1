[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "> Checking Python..."
$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Ready = $false

if (Test-Path -LiteralPath $VenvPython) {
    & $VenvPython -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) else 1)"
    $Ready = $LASTEXITCODE -eq 0
    if (-not $Ready) {
        throw "The existing .venv uses an unsupported Python version. Remove .venv and rerun setup.ps1."
    }
    Write-Host "> Reusing existing .venv."
}

if (-not $Ready -and (Get-Command python.exe -ErrorAction SilentlyContinue)) {
    & python -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        & python -m venv .venv
        $Ready = $LASTEXITCODE -eq 0
    }
}
if (-not $Ready -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    foreach ($Version in @("3.11", "3.12", "3.10")) {
        & py "-$Version" -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) {
            & py "-$Version" -m venv .venv
            $Ready = $LASTEXITCODE -eq 0
            if ($Ready) { break }
        }
    }
}
if (-not $Ready) {
    throw "Python 3.10-3.12 was not found. Install Python 3.11 and enable Add Python to PATH."
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Virtual environment creation failed: $VenvPython does not exist."
}

& $VenvPython -c "import sys; assert (3, 10) <= sys.version_info[:2] < (3, 13), 'Python 3.10-3.12 required'"
if ($LASTEXITCODE -ne 0) {
    throw "Unsupported Python version. Install Python 3.10-3.12 (3.11 recommended)."
}

Write-Host "> Installing Python dependencies..."
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt

Write-Host "> Creating local data directories..."
@("mp3", "transcripts", "notes", "graphs", "knowledge", "logs") | ForEach-Object {
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot $_) | Out-Null
}

Write-Host ""
Write-Host "=============================================="
Write-Host " Setup complete. Run:"
Write-Host "    .\run.ps1"
Write-Host "=============================================="
