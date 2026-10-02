<#
.SYNOPSIS
    Interactive & Adaptive CI/CD Generator for Laravel on Hostinger (GitHub Actions).

.DESCRIPTION
    Skrip interaktif dan adaptif untuk menyiapkan GitHub Actions CI/CD ke Hostinger:
    - Cukup jalankan 'setup-hostinger-ci' tanpa argumen untuk wizard interaktif.
    - Otomatis mendeteksi versi PHP, Node, branch, dan tools (Pest, PHPUnit, Pint, ESLint, Wayfinder).
    - Menawarkan pilihan Lean Deploy vs Gated Quality Pipeline (CI Test/Lint -> CD Deploy).
    - Mengingat konfigurasi SSH Hostinger terakhir (~/.hostinger-ci.json) agar tidak perlu input ulang.
    - Mengamankan file database SQLite (*.sqlite*) dan user uploads (storage/**) dari penghapusan rsync.
    - Otomatis un-track public/build dari Git dan mengunggah Secrets ke GitHub via 'gh' CLI.
#>

[CmdletBinding()]
param (
    [Parameter(Position = 0)]
    [string]$TargetDir = "",

    [Parameter(Position = 1)]
    [string]$SshHost = "",

    [Parameter(Position = 2)]
    [string]$SshUser = "",

    [int]$SshPort = 0,

    [string]$SshKeyPath = "",

    [string]$Branch = "",

    [string]$PhpVersion = "",

    [string]$NodeVersion = "",

    [switch]$WithTests,

    [switch]$WithoutTests,

    [switch]$IncludeMigration,

    [switch]$SkipSecrets,

    [switch]$DryRun,

    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"

Write-Host "`n========================================================" -ForegroundColor Cyan
Write-Host "  Hostinger Laravel CI/CD Wizard (GitHub Actions v2)" -ForegroundColor Cyan
Write-Host "========================================================`n" -ForegroundColor Cyan

# ----------------------------------------------------
# 1. Validasi Prasyarat Dasar
# ----------------------------------------------------
if (-not (Test-Path "artisan")) {
    Write-Error "ERROR: Perintah ini HARUS dijalankan dari root direktori project Laravel (file 'artisan' tidak ditemukan)."
    exit 1
}

$isGit = git rev-parse --is-inside-work-tree 2>$null
if ($LASTEXITCODE -ne 0 -or $isGit -ne "true") {
    Write-Error "ERROR: Direktori saat ini bukan git repository. Jalankan 'git init' dan hubungkan ke GitHub terlebih dahulu."
    exit 1
}

# ----------------------------------------------------
# 2. Cache Hostinger Config Helper (~/.hostinger-ci.json)
# ----------------------------------------------------
$configCacheFile = Join-Path $HOME ".hostinger-ci.json"
$cachedConfig = @{}
if (Test-Path $configCacheFile) {
    try {
        $cachedConfig = Get-Content $configCacheFile -Raw | ConvertFrom-Json
    } catch {
        $cachedConfig = @{}
    }
}

# ----------------------------------------------------
# 3. Deteksi Lingkungan & Dependensi Otomatis
# ----------------------------------------------------
Write-Host "[1/6] Mendeteksi Konfigurasi Proyek..." -ForegroundColor Gray

# A. Branch
$detectedBranch = "main"
$currentGitBranch = git branch --show-current 2>$null
if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($currentGitBranch)) {
    $detectedBranch = $currentGitBranch.Trim()
}

# B. Versi PHP dari composer.json
$detectedPhp = "8.3"
$hasPest = $false
$hasPhpUnit = $false
$hasPint = $false

if (Test-Path "composer.json") {
    try {
        $composerJson = Get-Content "composer.json" -Raw | ConvertFrom-Json
        if ($composerJson.require -and $composerJson.require.php) {
            $reqPhp = [string]$composerJson.require.php
            if ($reqPhp -match "8\.[1-4]") {
                $detectedPhp = $Matches[0]
            }
        }
        
        $devDeps = @()
        if ($composerJson.'require-dev') {
            $devDeps = $composerJson.'require-dev'.PSObject.Properties.Name
        }
        if ($devDeps -contains "pestphp/pest") { $hasPest = $true }
        if ($devDeps -contains "phpunit/phpunit") { $hasPhpUnit = $true }
        if ($devDeps -contains "laravel/pint") { $hasPint = $true }
    } catch {
        Write-Warning "Gagal mem-parse composer.json untuk deteksi otomatis."
    }
}

# C. Node & Frontend Linters dari package.json
$detectedNode = "22"
if (Test-Path ".nvmrc") {
    $nvmVersion = (Get-Content ".nvmrc" -Raw).Trim()
    if ($nvmVersion -match "^v?([0-9]+)") {
        $detectedNode = $Matches[1]
    }
}

$hasEslint = $false
$lintCmd = ""
$hasWayfinder = $false

if (Test-Path "package.json") {
    try {
        $pkgJson = Get-Content "package.json" -Raw | ConvertFrom-Json
        $scripts = @()
        if ($pkgJson.scripts) {
            $scripts = $pkgJson.scripts.PSObject.Properties.Name
        }
        if ($scripts -contains "lint:check") {
            $hasEslint = $true
            $lintCmd = "npm run lint:check"
        } elseif ($scripts -contains "lint") {
            $hasEslint = $true
            $lintCmd = "npm run lint"
        }

        $allDeps = @()
        if ($pkgJson.dependencies) { $allDeps += $pkgJson.dependencies.PSObject.Properties.Name }
        if ($pkgJson.devDependencies) { $allDeps += $pkgJson.devDependencies.PSObject.Properties.Name }

        if ($allDeps -contains "@laravel/wayfinder" -or $allDeps -contains "tightenco/ziggy") {
            $hasWayfinder = $true
        }
    } catch {
        Write-Warning "Gagal mem-parse package.json."
    }
}

$toolsFound = @()
if ($hasPint) { $toolsFound += "Laravel Pint" }
if ($hasEslint) { $toolsFound += "ESLint" }
if ($hasPest) { $toolsFound += "Pest Tests" } elseif ($hasPhpUnit) { $toolsFound += "PHPUnit Tests" }
if ($hasWayfinder) { $toolsFound += "Wayfinder/Ziggy Generator" }

Write-Host "  -> Git Branch       : $detectedBranch" -ForegroundColor White
Write-Host "  -> Versi PHP        : $detectedPhp" -ForegroundColor White
Write-Host "  -> Versi Node       : $detectedNode" -ForegroundColor White
if ($toolsFound.Count -gt 0) {
    Write-Host "  -> Tools Terdeteksi : $($toolsFound -join ', ')" -ForegroundColor Green
} else {
    Write-Host "  -> Tools Terdeteksi : Tidak ada test runner / linter otomatis" -ForegroundColor DarkGray
}

# ----------------------------------------------------
# 4. Wizard Interaktif (Jika parameter tidak diisi)
# ----------------------------------------------------
function Read-InputWithDefault {
    param (
        [string]$Message,
        [string]$DefaultValue,
        [bool]$Required = $false
    )

    while ($true) {
        $promptText = if (-not [string]::IsNullOrWhiteSpace($DefaultValue)) { "$Message [$DefaultValue]" } else { $Message }
        $inputVal = Read-Host -Prompt $promptText
        
        if ([string]::IsNullOrWhiteSpace($inputVal)) {
            if (-not [string]::IsNullOrWhiteSpace($DefaultValue)) {
                return $DefaultValue
            }
            if (-not $Required) {
                return ""
            }
            Write-Host "  [!] Input tidak boleh kosong." -ForegroundColor Red
        } else {
            return $inputVal.Trim()
        }
    }
}

function Confirm-Choice {
    param (
        [string]$Message,
        [bool]$DefaultYes = $true
    )

    $choices = if ($DefaultYes) { "[Y/n]" } else { "[y/N]" }
    $ans = Read-Host -Prompt "$Message $choices"
    if ([string]::IsNullOrWhiteSpace($ans)) {
        return $DefaultYes
    }
    return ($ans.Trim().ToLower() -in @("y", "yes", "ya"))
}

if (-not $NonInteractive) {
    Write-Host "`n[2/6] Wizard Konfigurasi Deployment..." -ForegroundColor Gray

    # Branch
    if ([string]::IsNullOrWhiteSpace($Branch)) {
        $Branch = Read-InputWithDefault -Message "Target Git Branch untuk deploy otomatis" -DefaultValue $detectedBranch
    }

    # PHP Version
    if ([string]::IsNullOrWhiteSpace($PhpVersion)) {
        $PhpVersion = Read-InputWithDefault -Message "Versi PHP di Hostinger" -DefaultValue $detectedPhp
    }

    # Pipeline Type: Gated vs Lean
    $useGated = $false
    if ($WithTests) {
        $useGated = $true
    } elseif ($WithoutTests) {
        $useGated = $false
    } else {
        $suggestGated = ($hasPest -or $hasPhpUnit -or $hasPint -or $hasEslint)
        $useGated = Confirm-Choice -Message "Aktifkan Gated Pipeline (jalankan Linting & Automated Test sebelum deploy)?" -DefaultYes $suggestGated
    }

    # Migration
    if (-not $PSBoundParameters.ContainsKey('IncludeMigration')) {
        $IncludeMigration = Confirm-Choice -Message "Otomatis jalankan 'php artisan migrate --force' setelah deployment?" -DefaultYes $false
    }

    # Hostinger SSH Host / IP
    if ([string]::IsNullOrWhiteSpace($SshHost)) {
        $defaultHost = if ($cachedConfig.SshHost) { $cachedConfig.SshHost } else { "" }
        $SshHost = Read-InputWithDefault -Message "Hostinger SSH Host / IP" -DefaultValue $defaultHost -Required $true
    }

    # Hostinger SSH User
    if ([string]::IsNullOrWhiteSpace($SshUser)) {
        $defaultUser = if ($cachedConfig.SshUser) { $cachedConfig.SshUser } else { "" }
        $SshUser = Read-InputWithDefault -Message "Hostinger SSH User" -DefaultValue $defaultUser -Required $true
    }

    # Hostinger SSH Port
    if ($SshPort -le 0) {
        $defaultPort = if ($cachedConfig.SshPort) { [string]$cachedConfig.SshPort } else { "65002" }
        $enteredPort = Read-InputWithDefault -Message "Hostinger SSH Port" -DefaultValue $defaultPort
        $SshPort = [int]$enteredPort
    }

    # SSH Private Key Path
    if ([string]::IsNullOrWhiteSpace($SshKeyPath)) {
        $defaultKey = Join-Path $HOME ".ssh\id_ed25519"
        if (-not (Test-Path $defaultKey)) {
            $rsaKey = Join-Path $HOME ".ssh\id_rsa"
            if (Test-Path $rsaKey) { $defaultKey = $rsaKey }
        }
        $SshKeyPath = Read-InputWithDefault -Message "Path SSH Private Key lokal" -DefaultValue $defaultKey -Required $true
    }

    # Target Directory di Hostinger (Auto-infer domain folder)
    if ([string]::IsNullOrWhiteSpace($TargetDir)) {
        $currentDirName = (Get-Item .).Name
        $defaultTarget = if (-not [string]::IsNullOrWhiteSpace($SshUser)) {
            "/home/$SshUser/domains/$currentDirName/app"
        } elseif ($cachedConfig.TargetDir) {
            $cachedConfig.TargetDir
        } else {
            ""
        }
        $TargetDir = Read-InputWithDefault -Message "Path TARGET_DIR di Hostinger" -DefaultValue $defaultTarget -Required $true
    }
} else {
    # Non-interactive fallback
    if ([string]::IsNullOrWhiteSpace($Branch)) { $Branch = $detectedBranch }
    if ([string]::IsNullOrWhiteSpace($PhpVersion)) { $PhpVersion = $detectedPhp }
    if ($SshPort -le 0) { $SshPort = 65002 }
    if ([string]::IsNullOrWhiteSpace($SshKeyPath)) { $SshKeyPath = Join-Path $HOME ".ssh\id_ed25519" }
    $useGated = ($WithTests.IsPresent -or ($hasPest -and -not $WithoutTests.IsPresent))
}

# Simpan host/user/port ke cache lokal untuk kenyamanan proyek berikutnya
try {
    $saveCache = @{
        SshHost   = $SshHost
        SshUser   = $SshUser
        SshPort   = $SshPort
        TargetDir = $TargetDir
    }
    $saveCache | ConvertTo-Json | Set-Content -Path $configCacheFile -Encoding UTF8
} catch {}

# ----------------------------------------------------
# 5. Membersihkan pelacakan Git public/build
# ----------------------------------------------------
Write-Host "`n[3/6] Memeriksa .gitignore & Git Index..." -ForegroundColor Gray
$gitignoreFile = ".gitignore"
if (Test-Path $gitignoreFile) {
    $gitignoreContent = Get-Content $gitignoreFile -Raw
    if ($gitignoreContent -notmatch "(^|\r?\n)/?public/build/?($|\r?\n)") {
        if (-not $DryRun) {
            Add-Content -Path $gitignoreFile -Value "`n/public/build"
        }
        Write-Host "  -> '/public/build' ditambahkan ke .gitignore" -ForegroundColor Green
    } else {
        Write-Host "  -> '/public/build' sudah ada di .gitignore" -ForegroundColor DarkGray
    }
} else {
    if (-not $DryRun) {
        Set-Content -Path $gitignoreFile -Value "/public/build"
    }
    Write-Host "  -> File .gitignore dibuat dengan '/public/build'" -ForegroundColor Green
}

git ls-files --error-unmatch public/build 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  -> Menghapus public/build dari cache Git..." -ForegroundColor Yellow
    if (-not $DryRun) {
        git rm -r --cached public/build 2>$null | Out-Null
    }
    Write-Host "  -> public/build berhasil di-untrack dari Git (file fisik tetap ada)." -ForegroundColor Green
}

# ----------------------------------------------------
# 6. Menghasilkan Workflow GitHub Actions
# ----------------------------------------------------
Write-Host "`n[4/6] Menyiapkan Workflow GitHub Actions..." -ForegroundColor Gray

$migrationCommand = ""
if ($IncludeMigration) {
    $migrationCommand = "`n            php artisan migrate --force"
}

$workflowContent = ""

if ($useGated) {
    Write-Host "  -> Mode: GATED PIPELINE (Verify Quality/Tests -> Deploy to Hostinger)" -ForegroundColor Cyan
    
    $testCommand = if ($hasPest) { "./vendor/bin/pest" } else { "php artisan test" }
    
    $verifySteps = @"
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Setup PHP
        uses: shivammathur/setup-php@v2
        with:
          php-version: '$PhpVersion'
          extensions: mbstring, xml, ctype, iconv, intl, pdo_mysql, pdo_sqlite, bcmath, curl, zip
          coverage: none

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: $detectedNode
          cache: 'npm'

      - name: Install Dependencies
        run: |
          composer install --no-interaction --prefer-dist --optimize-autoloader
          npm ci

      - name: Prepare Environment & Frontend Types
        run: |
          cp .env.example .env 2>/dev/null || true
          php artisan key:generate || true
          npm run build
"@

    if ($hasPint) {
        $verifySteps += @"

      - name: Check PHP Code Style (Pint)
        run: vendor/bin/pint --test
"@
    }

    if ($hasEslint -and -not [string]::IsNullOrWhiteSpace($lintCmd)) {
        $verifySteps += @"

      - name: Check Frontend Lint (ESLint)
        run: $lintCmd
"@
    }

    $verifySteps += @"

      - name: Run Automated Tests
        run: $testCommand
"@

    $workflowContent = @"
name: CI/CD Pipeline

on:
  push:
    branches: [ $Branch ]
  pull_request:
    branches: [ $Branch ]
  workflow_dispatch:

jobs:
  verify:
    name: Code Quality & Tests
    runs-on: ubuntu-latest

    steps:
$verifySteps

  deploy:
    name: Deploy to Hostinger
    runs-on: ubuntu-latest
    needs: [verify]
    if: github.ref == 'refs/heads/$Branch' && github.event_name == 'push'

    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Setup PHP
        uses: shivammathur/setup-php@v2
        with:
          php-version: '$PhpVersion'
          extensions: mbstring, xml, ctype, iconv, intl, pdo_mysql, pdo_sqlite, bcmath, curl, zip
          coverage: none

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: $detectedNode
          cache: 'npm'

      - name: Install Composer Dependencies
        run: |
          composer install --no-dev --prefer-dist --optimize-autoloader --no-interaction

      - name: Build Frontend Assets
        run: |
          npm ci
          npm run build

      - name: Deploy via Rsync over SSH
        uses: easingthemes/ssh-deploy@main
        env:
          SSH_PRIVATE_KEY: `${{ secrets.HOSTINGER_SSH_KEY }}
          ARGS: "-rlgoDzvcO --delete --exclude=.env --exclude=node_modules --exclude=.git --exclude=.github --exclude=storage/** --exclude=database/*.sqlite* --exclude=tests"
          REMOTE_HOST: `${{ secrets.HOSTINGER_SSH_HOST }}
          REMOTE_PORT: `${{ secrets.HOSTINGER_SSH_PORT }}
          REMOTE_USER: `${{ secrets.HOSTINGER_SSH_USER }}
          TARGET: `${{ secrets.HOSTINGER_TARGET_DIR }}

      - name: Post-Deploy Optimization
        uses: appleboy/ssh-action@v1.0.3
        with:
          host: `${{ secrets.HOSTINGER_SSH_HOST }}
          port: `${{ secrets.HOSTINGER_SSH_PORT }}
          username: `${{ secrets.HOSTINGER_SSH_USER }}
          key: `${{ secrets.HOSTINGER_SSH_KEY }}
          script: |
            cd `${{ secrets.HOSTINGER_TARGET_DIR }}
            php artisan optimize:clear
            php artisan config:cache
            php artisan route:cache
            php artisan view:cache$migrationCommand
"@
} else {
    Write-Host "  -> Mode: LEAN DEPLOY (Build & Deploy to Hostinger)" -ForegroundColor Cyan
    
    $workflowContent = @"
name: Deploy to Hostinger

on:
  push:
    branches: [ $Branch ]
  workflow_dispatch:

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Setup PHP
        uses: shivammathur/setup-php@v2
        with:
          php-version: '$PhpVersion'
          extensions: mbstring, xml, ctype, iconv, intl, pdo_mysql, pdo_sqlite, bcmath, curl, zip
          coverage: none

      - name: Install Composer Dependencies
        run: |
          composer install --no-dev --prefer-dist --optimize-autoloader --no-interaction

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: $detectedNode
          cache: 'npm'

      - name: Build Frontend Assets
        run: |
          npm ci
          npm run build

      - name: Deploy via Rsync over SSH
        uses: easingthemes/ssh-deploy@main
        env:
          SSH_PRIVATE_KEY: `${{ secrets.HOSTINGER_SSH_KEY }}
          ARGS: "-rlgoDzvcO --delete --exclude=.env --exclude=node_modules --exclude=.git --exclude=.github --exclude=storage/** --exclude=database/*.sqlite* --exclude=tests"
          REMOTE_HOST: `${{ secrets.HOSTINGER_SSH_HOST }}
          REMOTE_PORT: `${{ secrets.HOSTINGER_SSH_PORT }}
          REMOTE_USER: `${{ secrets.HOSTINGER_SSH_USER }}
          TARGET: `${{ secrets.HOSTINGER_TARGET_DIR }}

      - name: Post-Deploy Optimization
        uses: appleboy/ssh-action@v1.0.3
        with:
          host: `${{ secrets.HOSTINGER_SSH_HOST }}
          port: `${{ secrets.HOSTINGER_SSH_PORT }}
          username: `${{ secrets.HOSTINGER_SSH_USER }}
          key: `${{ secrets.HOSTINGER_SSH_KEY }}
          script: |
            cd `${{ secrets.HOSTINGER_TARGET_DIR }}
            php artisan optimize:clear
            php artisan config:cache
            php artisan route:cache
            php artisan view:cache$migrationCommand
"@
}

$workflowDir = ".github/workflows"
$workflowFile = "$workflowDir/deploy.yml"

if (-not $DryRun) {
    if (-not (Test-Path $workflowDir)) {
        New-Item -ItemType Directory -Path $workflowDir -Force | Out-Null
    }
    Set-Content -Path $workflowFile -Value $workflowContent -Encoding UTF8
    Write-Host "  -> File workflow tersimpan di: $workflowFile" -ForegroundColor Green
} else {
    Write-Host "  -> [DryRun] Workflow akan disimpan di: $workflowFile" -ForegroundColor Cyan
}

# ----------------------------------------------------
# 7. Konfigurasi GitHub Secrets via 'gh' CLI
# ----------------------------------------------------
Write-Host "`n[5/6] Mengonfigurasi GitHub Secrets..." -ForegroundColor Gray
if ($SkipSecrets) {
    Write-Host "  -> [SkipSecrets] Konfigurasi secrets GitHub dilewati." -ForegroundColor Yellow
} else {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Warning "GitHub CLI (gh) tidak terdeteksi. Silakan pasang GitHub Secrets secara manual di repository."
    } else {
        if (-not (Test-Path $SshKeyPath)) {
            Write-Error "ERROR: SSH Private Key tidak ditemukan di: $SshKeyPath"
            exit 1
        }
        $privateKeyContent = Get-Content $SshKeyPath -Raw

        if ($DryRun) {
            Write-Host "  -> [DryRun] Secrets yang akan dikirim via gh CLI:" -ForegroundColor Cyan
            Write-Host "     - HOSTINGER_SSH_HOST: $SshHost"
            Write-Host "     - HOSTINGER_SSH_USER: $SshUser"
            Write-Host "     - HOSTINGER_SSH_PORT: $SshPort"
            Write-Host "     - HOSTINGER_TARGET_DIR: $TargetDir"
            Write-Host "     - HOSTINGER_SSH_KEY: [Private Key from $SshKeyPath]"
        } else {
            Write-Host "  -> Mengirim secrets ke repository GitHub..." -ForegroundColor Cyan
            gh secret set HOSTINGER_SSH_HOST --body "$SshHost"
            gh secret set HOSTINGER_SSH_USER --body "$SshUser"
            gh secret set HOSTINGER_SSH_PORT --body "$SshPort"
            gh secret set HOSTINGER_TARGET_DIR --body "$TargetDir"
            gh secret set HOSTINGER_SSH_KEY --body "$privateKeyContent"
            Write-Host "  -> Semua Secrets berhasil disimpan di GitHub!" -ForegroundColor Green
        }
    }
}

# ----------------------------------------------------
# 8. Selesai
# ----------------------------------------------------
Write-Host "`n[6/6] Selesai!" -ForegroundColor Green
Write-Host "Untuk mengaktifkan deployment, jalankan:" -ForegroundColor Cyan
Write-Host "  git add .gitignore .github/workflows/deploy.yml" -ForegroundColor White
Write-Host "  git commit -m `"ci: setup automated hostinger deployment`"" -ForegroundColor White
Write-Host "  git push origin $Branch`n" -ForegroundColor White
Write-Host "========================================================`n" -ForegroundColor Cyan
