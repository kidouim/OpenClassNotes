[CmdletBinding()]
param(
    [ValidateSet("auto", "cpu", "cuda")]
    [string]$Device,
    [ValidateSet("auto", "en", "zh")]
    [string]$Language
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Environment is not installed. Run .\setup.ps1 first."
}

if ($PSBoundParameters.ContainsKey("Device")) {
    $env:CLASSNOTES_DEVICE = $Device
} else {
    Remove-Item Env:CLASSNOTES_DEVICE -ErrorAction SilentlyContinue
}
if ($PSBoundParameters.ContainsKey("Language")) {
    $env:CLASSNOTES_LANGUAGE = $Language
} else {
    Remove-Item Env:CLASSNOTES_LANGUAGE -ErrorAction SilentlyContinue
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
& $VenvPython cli.py
exit $LASTEXITCODE
