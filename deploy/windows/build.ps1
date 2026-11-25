param(
    [string]$Python = "py",
    [string]$PythonVersion = "-3.8",
    [string]$FbClient = "",
    [string]$Antiword = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

Write-Host "Python: $Python $PythonVersion"
& $Python $PythonVersion -m pip install -r "deploy/windows/requirements-win.txt"

$pyArgs = @(
    "--noconfirm", "--clean", "--onedir",
    "--name", "MailWorkerService",
    "--add-data", "config.ini.example;.",
    "--add-data", "classifiers;classifiers",
    "--hidden-import", "win32timezone",
    "--hidden-import", "win32serviceutil",
    "--hidden-import", "win32service",
    "--hidden-import", "win32event",
    "--hidden-import", "servicemanager",
    "--collect-all", "pymupdf",
    "main.py"
)

if ($FbClient -and (Test-Path $FbClient)) {
    $pyArgs += @("--add-binary", "$FbClient;.")
} else {
    Write-Warning "fbclient.dll not specified; put it next to the exe and set [database] fb_client_library"
}

if ($Antiword -and (Test-Path $Antiword)) {
    $pyArgs += @("--add-binary", "$Antiword;tools")
} else {
    Write-Warning "antiword.exe not specified; .doc will be marked degraded"
}

& $Python $PythonVersion -m PyInstaller @pyArgs

Write-Host ""
Write-Host "Done: $Root\dist\MailWorkerService\MailWorkerService.exe"