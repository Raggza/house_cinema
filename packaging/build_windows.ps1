# Build the Windows installer: dist\HouseCinema-<version>-Windows-x64-Setup.exe
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -FfmpegDir D:\my-ffmpeg\bin
#
# Needs: uv (https://docs.astral.sh/uv/), Inno Setup 6 (winget install JRSoftware.InnoSetup), Internet
# on the first run (Python packages, ffmpeg). Steps:
#   1. ffmpeg + ffprobe: gyan.dev "release essentials" build (GPL), checked against its SHA-256,
#      kept in build\ffmpeg-windows for the next builds (or -FfmpegDir to use your own build)
#   2. icons (build\icons)
#   3. PyInstaller -> dist\HouseCinema\  (the app folder; HouseCinema.exe runs without installing)
#   4. Inno Setup  -> dist\HouseCinema-<version>-Windows-x64-Setup.exe
# -SkipInstaller stops after step 3.

param(
    [string]$FfmpegDir = "",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is very slow with the progress bar
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    foreach ($c in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) {
        if (Test-Path -LiteralPath $c) { $uv = $c; break }
    }
}
if (-not $uv) { throw "uv not found. Install it: winget install --id=astral-sh.uv -e" }

$match = Select-String -LiteralPath (Join-Path $root "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $match) { throw "cannot read the version from pyproject.toml" }
$version = $match.Matches[0].Groups[1].Value
Write-Host "House Cinema $version"

# ------------------------------------------------------------------------- 1. ffmpeg
Step "ffmpeg"
if ($FfmpegDir) {
    $ffmpeg = (Resolve-Path -LiteralPath $FfmpegDir).Path
} else {
    $ffmpeg = Join-Path $root "build\ffmpeg-windows"
    if (-not (Test-Path -LiteralPath (Join-Path $ffmpeg "ffmpeg.exe"))) {
        $base = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        $download = Join-Path $root "build\download"
        New-Item -ItemType Directory -Force $download | Out-Null
        $zip = Join-Path $download "ffmpeg-release-essentials.zip"
        Write-Host "Downloading $base ..."
        Invoke-WebRequest -UseBasicParsing -Uri $base -OutFile $zip
        $expected = ((Invoke-WebRequest -UseBasicParsing -Uri "$base.sha256").Content | ForEach-Object {
            if ($_ -is [byte[]]) { [Text.Encoding]::ASCII.GetString($_) } else { $_ } }).Trim().Split(" ")[0].ToLower()
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLower()
        if ($expected -ne $actual) { throw "ffmpeg download damaged: SHA-256 $actual, expected $expected" }
        Write-Host "SHA-256 OK"
        $unpacked = Join-Path $download "ffmpeg-unpacked"
        if (Test-Path -LiteralPath $unpacked) { Remove-Item -Recurse -Force -LiteralPath $unpacked }
        Expand-Archive -LiteralPath $zip -DestinationPath $unpacked
        $top = Get-ChildItem -LiteralPath $unpacked -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force $ffmpeg | Out-Null
        Copy-Item -LiteralPath (Join-Path $top.FullName "bin\ffmpeg.exe") -Destination $ffmpeg
        Copy-Item -LiteralPath (Join-Path $top.FullName "bin\ffprobe.exe") -Destination $ffmpeg
        # GPL: ship the licence and the build's readme (it says where the sources are).
        Copy-Item -LiteralPath (Join-Path $top.FullName "LICENSE") -Destination (Join-Path $ffmpeg "LICENSE.txt")
        Copy-Item -LiteralPath (Join-Path $top.FullName "README.txt") -Destination (Join-Path $ffmpeg "README.txt")
        Set-Content -LiteralPath (Join-Path $ffmpeg "SOURCE.txt") -Encoding ascii -Value @(
            "ffmpeg and ffprobe in this folder: $($top.Name), built by Gyan Doshi (https://www.gyan.dev/ffmpeg/builds/).",
            "Licence: GNU GPL v3 (LICENSE.txt). Source code: https://ffmpeg.org/download.html and the",
            "library list with source links in README.txt."
        )
        Remove-Item -Recurse -Force -LiteralPath $unpacked
    }
}
& (Join-Path $ffmpeg "ffmpeg.exe") -hide_banner -version | Select-Object -First 1

# ------------------------------------------------------------------------- 2. icons
Step "Icons"
& $uv run --group desktop python packaging\make_icons.py build\icons
if ($LASTEXITCODE) { throw "icons failed" }

# ------------------------------------------------------------------------- 3. PyInstaller
Step "PyInstaller"
$env:HC_FFMPEG_DIR = $ffmpeg
$env:HC_ICON_DIR = Join-Path $root "build\icons"
& $uv run --group desktop --group packaging pyinstaller --noconfirm --clean `
    --distpath dist --workpath build\pyinstaller packaging\house_cinema.spec
if ($LASTEXITCODE) { throw "PyInstaller failed" }
$appDir = Join-Path $root "dist\HouseCinema"
Write-Host "App folder: $appDir"
if ($SkipInstaller) { exit 0 }

# ------------------------------------------------------------------------- 4. Inno Setup
Step "Inno Setup"
$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                     "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path -LiteralPath $c) { $iscc = $c; break }
    }
}
if (-not $iscc) { throw "Inno Setup 6 not found. Install it: winget install JRSoftware.InnoSetup  (the app folder is ready: $appDir)" }
& $iscc "/DAppVersion=$version" "/DSourceDir=$appDir" "/DOutputDir=$(Join-Path $root 'dist')" `
    "/DIconFile=$(Join-Path $root 'build\icons\icon.ico')" packaging\windows\installer.iss
if ($LASTEXITCODE) { throw "Inno Setup failed" }
Get-ChildItem -LiteralPath (Join-Path $root "dist") -Filter "HouseCinema-*-Setup.exe" |
    Select-Object Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } }
