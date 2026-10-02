<#
.SYNOPSIS
    Interactive & Adaptive CI/CD Generator for Laravel on Hostinger (GitHub Actions).

.DESCRIPTION
    Zero-config interactive CLI wizard to automate Laravel + Vite CI/CD deployment to Hostinger:
    - Simply run 'setup-hostinger-ci' with no arguments for the guided interactive wizard.
    - Automatically detects PHP version, Node, Git branch, and tools (Pest, PHPUnit, Pint, ESLint, Wayfinder).
    - Offers Lean Deploy vs Gated Quality Pipeline (CI Test/Lint -> CD Deploy).
    - Remembers last used Hostinger SSH credentials (~/.hostinger-ci.json) for instant re-use across multiple domains.
    - Protects production SQLite databases (*.sqlite*) and user uploads (storage/**) from Rsync deletion.
    - Automatically untracks public/build from Git and provisions GitHub Secrets via 'gh' CLI.
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
# 1. Validate Prerequisites
# ----------------------------------------------------
if (-not (Test-Path "artisan")) {
    Write-Error "ERROR: This command MUST be run from the root directory of a Laravel project ('artisan' file not found)."
    exit 1
}

$isGit = git rev-parse --is-inside-work-tree 2>$null
if ($LASTEXITCODE -ne 0 -or $isGit -ne "true") {
    Write-Error "ERROR: Current directory is not a Git repository. Run 'git init' and connect to GitHub first."
    exit 1
}

# ----------------------------------------------------
# 2. Hostinger Config Cache Helper (~/.hostinger-ci.json)
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
# 3. Automatic Project & Environment Inspection
# ----------------------------------------------------
Write-Host "[1/6] Inspecting Project Configuration..." -ForegroundColor Gray

# A. Branch
$detectedBranch = "main"
$currentGitBranch = git branch --show-current 2>$null
if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($currentGitBranch)) {
    $detectedBranch = $currentGitBranch.Trim()
}

# B. PHP Version from composer.json
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
        Write-Warning "Failed to parse composer.json for auto-detection."
    }
}

# C. Node & Frontend Linters from package.json
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
        Write-Warning "Failed to parse package.json."
    }
}

$toolsFound = @()
if ($hasPint) { $toolsFound += "Laravel Pint" }
if ($hasEslint) { $toolsFound += "ESLint" }
if ($hasPest) { $toolsFound += "Pest Tests" } elseif ($hasPhpUnit) { $toolsFound += "PHPUnit Tests" }
if ($hasWayfinder) { $toolsFound += "Wayfinder/Ziggy Generator" }

Write-Host "  -> Git Branch       : $detectedBranch" -ForegroundColor White
Write-Host "  -> PHP Version      : $detectedPhp" -ForegroundColor White
Write-Host "  -> Node Version     : $detectedNode" -ForegroundColor White
if ($toolsFound.Count -gt 0) {
    Write-Host "  -> Detected Tools   : $($toolsFound -join ', ')" -ForegroundColor Green
} else {
    Write-Host "  -> Detected Tools   : None (no automated test runner or linter found)" -ForegroundColor DarkGray
}

# ----------------------------------------------------
# 4. Interactive Wizard (When parameters are not passed)
# ----------------------------------------------------
function Read-InputWithDefault {
    param (
        [string]$Message,
        [string]$DefaultValue,
        [bool]$Required = $false
    )

    $attempts = 0
    while ($attempts -lt 5) {
        $attempts++
        $promptText = if (-not [string]::IsNullOrWhiteSpace($DefaultValue)) { "$Message [$DefaultValue]" } else { $Message }
        $inputVal = Read-Host -Prompt $promptText
        
        if ($null -eq $inputVal) {
            if (-not [string]::IsNullOrWhiteSpace($DefaultValue)) {
                return $DefaultValue
            }
            if (-not $Required) {
                return ""
            }
            Write-Error "ERROR: End of input (EOF) reached while waiting for required input: '$Message'."
            exit 1
        }

        if ([string]::IsNullOrWhiteSpace($inputVal)) {
            if (-not [string]::IsNullOrWhiteSpace($DefaultValue)) {
                return $DefaultValue
            }
            if (-not $Required) {
                return ""
            }
            Write-Host "  [!] Input cannot be empty." -ForegroundColor Red
        } else {
            return $inputVal.Trim()
        }
    }
    Write-Error "ERROR: Too many empty input attempts for: '$Message'."
    exit 1
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
    Write-Host "`n[2/6] Interactive Deployment Wizard..." -ForegroundColor Gray

    # Branch
    if ([string]::IsNullOrWhiteSpace($Branch)) {
        $Branch = Read-InputWithDefault -Message "Target Git branch for automated deployment" -DefaultValue $detectedBranch
    }

    # PHP Version
    if ([string]::IsNullOrWhiteSpace($PhpVersion)) {
        $PhpVersion = Read-InputWithDefault -Message "PHP version on Hostinger" -DefaultValue $detectedPhp
    }

    # Pipeline Type: Gated vs Lean
    $useGated = $false
    if ($WithTests) {
        $useGated = $true
    } elseif ($WithoutTests) {
        $useGated = $false
    } else {
        $suggestGated = ($hasPest -or $hasPhpUnit -or $hasPint -or $hasEslint)
        $useGated = Confirm-Choice -Message "Enable Gated Pipeline (run Linting & Automated Tests prior to deployment)?" -DefaultYes $suggestGated
    }

    # Migration
    if (-not $PSBoundParameters.ContainsKey('IncludeMigration')) {
        $IncludeMigration = Confirm-Choice -Message "Automatically run 'php artisan migrate --force' after deployment?" -DefaultYes $false
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
        $SshKeyPath = Read-InputWithDefault -Message "Path to local SSH Private Key" -DefaultValue $defaultKey -Required $true
    }

    # Target Directory on Hostinger (Auto-infer domain folder)
    if ([string]::IsNullOrWhiteSpace($TargetDir)) {
        $currentDirName = (Get-Item .).Name
        $defaultTarget = if (-not [string]::IsNullOrWhiteSpace($SshUser)) {
            "/home/$SshUser/domains/$currentDirName/app"
        } elseif ($cachedConfig.TargetDir) {
            $cachedConfig.TargetDir
        } else {
            ""
        }
        $TargetDir = Read-InputWithDefault -Message "Path to TARGET_DIR on Hostinger" -DefaultValue $defaultTarget -Required $true
    }
} else {
    # Non-interactive fallback
    if ([string]::IsNullOrWhiteSpace($Branch)) { $Branch = $detectedBranch }
    if ([string]::IsNullOrWhiteSpace($PhpVersion)) { $PhpVersion = $detectedPhp }
    if ($SshPort -le 0) { $SshPort = 65002 }
    if ([string]::IsNullOrWhiteSpace($SshKeyPath)) { $SshKeyPath = Join-Path $HOME ".ssh\id_ed25519" }
    $useGated = ($WithTests.IsPresent -or ($hasPest -and -not $WithoutTests.IsPresent))
}

# Cache host/user/port locally for frictionless setup of subsequent projects
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
# 5. Clean up Git tracking for public/build
# ----------------------------------------------------
Write-Host "`n[3/6] Inspecting .gitignore & Git Index..." -ForegroundColor Gray
$gitignoreFile = ".gitignore"
if (Test-Path $gitignoreFile) {
    $gitignoreContent = Get-Content $gitignoreFile -Raw
    if ($gitignoreContent -notmatch "(^|\r?\n)/?public/build/?($|\r?\n)") {
        if (-not $DryRun) {
            Add-Content -Path $gitignoreFile -Value "`n/public/build"
        }
        Write-Host "  -> '/public/build' added to .gitignore" -ForegroundColor Green
    } else {
        Write-Host "  -> '/public/build' already present in .gitignore" -ForegroundColor DarkGray
    }
} else {
    if (-not $DryRun) {
        Set-Content -Path $gitignoreFile -Value "/public/build"
    }
    Write-Host "  -> Created .gitignore with '/public/build'" -ForegroundColor Green
}

git ls-files --error-unmatch public/build 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  -> Removing public/build from Git tracking cache..." -ForegroundColor Yellow
    if (-not $DryRun) {
        git rm -r --cached public/build 2>$null | Out-Null
    }
    Write-Host "  -> public/build successfully untracked from Git (physical files preserved)." -ForegroundColor Green
}

# ----------------------------------------------------
# 6. Generate GitHub Actions Workflow
# ----------------------------------------------------
Write-Host "`n[4/6] Generating GitHub Actions Workflow..." -ForegroundColor Gray

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
    Write-Host "  -> Workflow file saved at: $workflowFile" -ForegroundColor Green
} else {
    Write-Host "  -> [DryRun] Workflow will be saved at: $workflowFile" -ForegroundColor Cyan
}

# ----------------------------------------------------
# 7. Configure GitHub Secrets via 'gh' CLI
# ----------------------------------------------------
Write-Host "`n[5/6] Configuring GitHub Secrets..." -ForegroundColor Gray
if ($SkipSecrets) {
    Write-Host "  -> [SkipSecrets] GitHub secrets configuration skipped." -ForegroundColor Yellow
} else {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Warning "GitHub CLI (gh) not detected. Please configure GitHub Secrets manually in your repository settings."
    } else {
        if (-not (Test-Path $SshKeyPath)) {
            Write-Error "ERROR: SSH Private Key not found at: $SshKeyPath"
            exit 1
        }
        $privateKeyContent = Get-Content $SshKeyPath -Raw

        if ($DryRun) {
            Write-Host "  -> [DryRun] Secrets to be uploaded via gh CLI:" -ForegroundColor Cyan
            Write-Host "     - HOSTINGER_SSH_HOST: $SshHost"
            Write-Host "     - HOSTINGER_SSH_USER: $SshUser"
            Write-Host "     - HOSTINGER_SSH_PORT: $SshPort"
            Write-Host "     - HOSTINGER_TARGET_DIR: $TargetDir"
            Write-Host "     - HOSTINGER_SSH_KEY: [Private Key from $SshKeyPath]"
        } else {
            Write-Host "  -> Uploading secrets to GitHub repository..." -ForegroundColor Cyan
            gh secret set HOSTINGER_SSH_HOST --body "$SshHost"
            gh secret set HOSTINGER_SSH_USER --body "$SshUser"
            gh secret set HOSTINGER_SSH_PORT --body "$SshPort"
            gh secret set HOSTINGER_TARGET_DIR --body "$TargetDir"
            gh secret set HOSTINGER_SSH_KEY --body "$privateKeyContent"
            Write-Host "  -> All Secrets successfully configured on GitHub!" -ForegroundColor Green
        }
    }
}

# ----------------------------------------------------
# 8. Complete
# ----------------------------------------------------
Write-Host "`n[6/6] Done!" -ForegroundColor Green
Write-Host "To activate automated deployment, run:" -ForegroundColor Cyan
Write-Host "  git add .gitignore .github/workflows/deploy.yml" -ForegroundColor White
Write-Host "  git commit -m `"ci: setup automated hostinger deployment`"" -ForegroundColor White
Write-Host "  git push origin $Branch`n" -ForegroundColor White
Write-Host "========================================================`n" -ForegroundColor Cyan
