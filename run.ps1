# House Cinema: start the hub (plan step 6.1). Usually started through run.cmd (double-click it).
# - Needs uv only: uv downloads the right Python and the libraries on the first run.
# - Library folders are chosen on the admin page (http://localhost:8000/admin), not here.
# - ffmpeg is optional: the hub says at start which features are off without it.
# - Extra arguments go to the hub, e.g.  .\run.cmd --port 8080
# - --no-dev: test tools are not installed; ones already in .venv (a developer's) are kept.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$uv = $null
$found = Get-Command uv -ErrorAction SilentlyContinue
if ($found) {
    $uv = $found.Source
} else {
    # uv's installer puts it here; a window opened before the install does not have it on PATH yet.
    foreach ($candidate in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) {
        if (Test-Path -LiteralPath $candidate) { $uv = $candidate; break }
    }
}
if (-not $uv) {
    Write-Host ""
    Write-Host "Không tìm thấy uv (công cụ cài Python và thư viện cho hub)." -ForegroundColor Red
    Write-Host "Cài bằng một trong hai lệnh sau trong PowerShell, mở cửa sổ mới rồi chạy lại run.cmd:"
    Write-Host "  winget install --id=astral-sh.uv -e"
    Write-Host '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    exit 1
}

if (-not (Test-Path -LiteralPath ".venv")) {
    Write-Host "Lần chạy đầu: uv tải Python và thư viện (cần Internet, mất khoảng 1–2 phút)..."
}
& $uv run --no-dev python -m source.pipeline @args
exit $LASTEXITCODE
