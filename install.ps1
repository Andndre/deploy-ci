<#
.SYNOPSIS
    One-liner automated installer for setup-hostinger-ci on Windows.

.USAGE
    irm https://raw.githubusercontent.com/Andndre/setup-hostinger-ci/main/install.ps1 | iex
#>

$ErrorActionPreference = "Stop"

$repoOwner = "Andndre"
$repoName  = "setup-hostinger-ci"
$branch    = "main"
$baseUrl   = "https://raw.githubusercontent.com/$repoOwner/$repoName/$branch"

$installDir = Join-Path $HOME ".setup-hostinger-ci\bin"

Write-Host "`n========================================================" -ForegroundColor Cyan
Write-Host "  Installing setup-hostinger-ci CLI Tool" -ForegroundColor Cyan
Write-Host "========================================================`n" -ForegroundColor Cyan

# 1. Buat folder instalasi jika belum ada
if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
    Write-Host "[1/4] Membuat direktori instalasi di: $installDir" -ForegroundColor Gray
} else {
    Write-Host "[1/4] Menggunakan direktori: $installDir" -ForegroundColor Gray
}

# 2. Unduh file binary / script
Write-Host "[2/4] Mengunduh skrip CLI dari GitHub..." -ForegroundColor Gray
$files = @(
    "setup-hostinger-ci.cmd",
    "setup-hostinger-ci.ps1"
)

foreach ($file in $files) {
    $fileUrl = "$baseUrl/bin/$file"
    $destPath = Join-Path $installDir $file
    try {
        Invoke-RestMethod -Uri $fileUrl -OutFile $destPath
        Write-Host "  -> $file berhasil diunduh" -ForegroundColor Green
    } catch {
        Write-Error "Gagal mengunduh $file dari $fileUrl. Periksa koneksi internet Anda."
        exit 1
    }
}

# 3. Daftarkan ke User PATH jika belum ada
Write-Host "[3/4] Memeriksa User Environment PATH..." -ForegroundColor Gray
$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathParts = $currentPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

if ($pathParts -notcontains $installDir) {
    $newPath = ($pathParts + $installDir) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    $env:Path = "$installDir;" + $env:Path
    Write-Host "  -> $installDir berhasil ditambahkan ke User PATH!" -ForegroundColor Green
} else {
    Write-Host "  -> Direktori sudah terdaftar di User PATH." -ForegroundColor DarkGray
}

# 4. Validasi Prasyarat Toolchain
Write-Host "[4/4] Memeriksa perkakas pendukung..." -ForegroundColor Gray
$hasGit = Get-Command git -ErrorAction SilentlyContinue
$hasGh  = Get-Command gh -ErrorAction SilentlyContinue

if ($hasGit) {
    Write-Host "  -> Git terdeteksi" -ForegroundColor Green
} else {
    Write-Warning "Git belum terdeteksi. Silakan pasang Git: https://git-scm.com/"
}

if ($hasGh) {
    Write-Host "  -> GitHub CLI (gh) terdeteksi" -ForegroundColor Green
} else {
    Write-Warning "GitHub CLI (gh) belum terdeteksi. Disarankan memasangnya via: winget install GitHub.cli"
}

Write-Host "`n========================================================" -ForegroundColor Cyan
Write-Host "  Instalasi Berhasil!" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "Untuk mulai menggunakannya:" -ForegroundColor White
Write-Host "1. Buka terminal baru (PowerShell, CMD, atau Git Bash)." -ForegroundColor Gray
Write-Host "2. Masuk ke root direktori proyek Laravel Anda." -ForegroundColor Gray
Write-Host "3. Jalankan perintah:`n" -ForegroundColor Gray
Write-Host "     setup-hostinger-ci`n" -ForegroundColor Yellow
Write-Host "Repositori: https://github.com/$repoOwner/$repoName`n" -ForegroundColor DarkGray
