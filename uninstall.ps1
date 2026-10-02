<#
.SYNOPSIS
    Clean uninstaller for setup-hostinger-ci.
#>

$ErrorActionPreference = "Stop"
$installDir = Join-Path $HOME ".setup-hostinger-ci"
$binDir = Join-Path $installDir "bin"

Write-Host "`n========================================================" -ForegroundColor Yellow
Write-Host "  Uninstalling setup-hostinger-ci" -ForegroundColor Yellow
Write-Host "========================================================`n" -ForegroundColor Yellow

# 1. Hapus dari User PATH
$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathParts = $currentPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and $_ -ne $binDir }
$newPath = $pathParts -join ";"
[Environment]::SetEnvironmentVariable("Path", $newPath, "User")
Write-Host "[1/2] Menghapus $binDir dari User PATH..." -ForegroundColor Green

# 2. Hapus folder instalasi
if (Test-Path $installDir) {
    Remove-Item -Path $installDir -Recurse -Force
    Write-Host "[2/2] Menghapus folder instalasi $installDir..." -ForegroundColor Green
} else {
    Write-Host "[2/2] Direktori instalasi tidak ditemukan." -ForegroundColor DarkGray
}

Write-Host "`nsetup-hostinger-ci berhasil dihapus dari sistem.`n" -ForegroundColor Green
