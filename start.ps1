# Start Swifteye (collector + web app) on Windows and open it in the browser.
#   Double-click start.cmd, or run:  powershell -ExecutionPolicy Bypass -File start.ps1
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$url = 'http://127.0.0.1:8787'
$data = Join-Path $repo 'run'

function Test-Up { try { Invoke-RestMethod "$url/books" -TimeoutSec 2 | Out-Null; $true } catch { $false } }

if (Test-Up) {
  Write-Host 'Swifteye is already running.'
  Start-Process $url
  exit 0
}

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
  Write-Host 'Rust is not installed. Get it from https://rustup.rs, then run this again.'
  exit 1
}

Write-Host 'Building...'
Push-Location $repo
cargo build --release
if ($LASTEXITCODE -ne 0) { Pop-Location; exit 1 }
Pop-Location

New-Item -ItemType Directory -Force $data | Out-Null
$exe = Join-Path $repo 'target\release\oddsfeed.exe'
Write-Host "Starting the collector (log: $data\log.txt)..."
Start-Process -FilePath $exe -WorkingDirectory $data -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $data 'log.txt') -RedirectStandardError (Join-Path $data 'log.err.txt')

for ($i = 0; $i -lt 30; $i++) {
  if (Test-Up) { Write-Host "Running at $url"; Start-Process $url; exit 0 }
  Start-Sleep -Seconds 1
}
Write-Host "It did not come up in 30 s. Check $data\log.txt"
exit 1
