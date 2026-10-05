<#
.SYNOPSIS
    Interactive CI/CD Generator for Laravel/Vite and static projects on Hostinger.

.DESCRIPTION
    Interactive CLI wizard for asset-safe CI/CD deployment to Hostinger:
    - Simply run 'deploy-ci' with no arguments for the guided interactive wizard.
    - Automatically detects PHP version, Node, Git branch, and tools (Pest, PHPUnit, Pint, ESLint, Wayfinder).
    - Offers Lean Deploy vs Gated Quality Pipeline (CI Test/Lint -> CD Deploy).
    - Remembers last used Hostinger SSH credentials (~/.hostinger-ci.json) for instant re-use across multiple domains.
    - Uploads immutable assets before application publication, then verifies HTTP responses.
    - Retains previous assets and protects production SQLite databases and uploads.
    - Supports static Vite, SvelteKit adapter-static, and custom static output profiles.
    - Previews replacements, keeps dry runs read-only, and checks GitHub secret command failures.
#>

[CmdletBinding()]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidAssignmentToAutomaticVariable', 'Profile', Justification = 'Profile parameter is part of the public CLI contract')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseDeclaredVarsMoreThanAssignments', 'lintCmd', Justification = 'Consumed by dot-sourced generate-workflow.ps1')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseDeclaredVarsMoreThanAssignments', 'useGated', Justification = 'Consumed by dot-sourced generate-workflow.ps1')]
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

    [ValidateSet('laravel-vite', 'static-vite', 'sveltekit-static', 'static')]
    [string]$Profile = 'laravel-vite',

    [string]$OutputDir = '',

    [string[]]$ImmutableDirs = @(),

    [string[]]$ProtectedPaths = @(),

    [ValidateSet('', 'npm', 'pnpm', 'yarn', 'bun')]
    [string]$PackageManager = '',

    [string]$BuildCommand = '',

    [string]$DeployUrl = '',

    [string]$PageContains = '',

    [ValidateRange(2, 100)]
    [int]$KeepReleases = 3,

    [ValidateRange(1, 3650)]
    [int]$RetentionDays = 7,

    [ValidateRange(10, 1000000)]
    [int]$MaxRetainedFiles = 10000,

    [ValidateRange(1, 1048576)]
    [int]$MaxRetainedMiB = 512,

    [string]$Environment = 'production',

    [string]$SshKnownHostsPath = '',

    [string]$ConfigCachePath = '',

    [switch]$Force,

    [switch]$WithTests,

    [switch]$WithoutTests,

    [switch]$IncludeMigration,

    [switch]$MaintenanceMode,

    [switch]$Preflight,

    [switch]$SkipPreflight,

    [switch]$SkipSecrets,

    [switch]$DryRun,

    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"

Write-Host "`n========================================================" -ForegroundColor Cyan
Write-Host "  Deploy CI Wizard ($Profile)" -ForegroundColor Cyan
Write-Host "========================================================`n" -ForegroundColor Cyan

# ----------------------------------------------------
# 1. Validate Prerequisites
# ----------------------------------------------------
if ($Profile -eq 'laravel-vite' -and -not (Test-Path "artisan")) {
    Write-Error "ERROR: This command MUST be run from the root directory of a Laravel project ('artisan' file not found)."
    exit 1
}
if (-not (Test-Path 'package.json')) {
    throw 'A package.json with a frontend build script is required.'
}
if ($WithTests -and $WithoutTests) { throw 'Choose either WithTests or WithoutTests.' }
if ($Profile -ne 'laravel-vite' -and $IncludeMigration) { throw 'Migrations require the laravel-vite profile.' }

$isGit = git rev-parse --is-inside-work-tree 2>$null
if ($LASTEXITCODE -ne 0 -or $isGit -ne "true") {
    Write-Error "ERROR: Current directory is not a Git repository. Run 'git init' and connect to GitHub first."
    exit 1
}

# ----------------------------------------------------
# 2. Hostinger Config Cache Helper (~/.hostinger-ci.json)
# ----------------------------------------------------
$configCacheFile = if ($ConfigCachePath) { $ConfigCachePath } else { Join-Path $HOME '.hostinger-ci.json' }
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
            if ($reqPhp -match '[0-9]+\.[0-9]+') {
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
    if ($Profile -eq 'laravel-vite' -and [string]::IsNullOrWhiteSpace($PhpVersion)) {
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
    if ($Profile -eq 'laravel-vite' -and -not $PSBoundParameters.ContainsKey('IncludeMigration')) {
        $IncludeMigration = Confirm-Choice -Message "Automatically run 'php artisan migrate --force' after deployment?" -DefaultYes $false
    }

    # Maintenance Mode (Graceful downtime buffer during transfer)
    if ($Profile -eq 'laravel-vite' -and -not $PSBoundParameters.ContainsKey('MaintenanceMode')) {
        $MaintenanceMode = Confirm-Choice -Message "Enable graceful Maintenance Mode ('php artisan down/up') during deployment?" -DefaultYes $true
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
    if (-not $SkipSecrets -and [string]::IsNullOrWhiteSpace($SshKeyPath)) {
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
        $defaultTarget = if ($cachedConfig.TargetDir) {
            $cachedConfig.TargetDir
        } elseif (-not [string]::IsNullOrWhiteSpace($SshUser)) {
            "/home/$SshUser/domains/$currentDirName/public_html"
        } else {
            ""
        }
        $TargetDir = Read-InputWithDefault -Message "Path to TARGET_DIR on Hostinger" -DefaultValue $defaultTarget -Required $true
        if (-not [string]::IsNullOrWhiteSpace($TargetDir)) {
            $TargetDir = $TargetDir.Trim().TrimEnd('/').TrimEnd('\')
        }
    }

    # Pre-Flight SSH and Remote Environment Check
    $shouldPreflight = $false
    if ($Preflight) {
        $shouldPreflight = $true
    } elseif (-not $SkipPreflight -and -not $SkipSecrets -and -not $DryRun) {
        $shouldPreflight = Confirm-Choice -Message "Test SSH connection and remote environment now?" -DefaultYes $true
    }

    if ($shouldPreflight -and -not [string]::IsNullOrWhiteSpace($SshHost) -and -not [string]::IsNullOrWhiteSpace($SshUser)) {
        if (Test-Path -LiteralPath $SshKeyPath -PathType Leaf) {
            Write-Host "`n  -> Testing SSH connection to Hostinger ($SshHost on port $SshPort)..." -ForegroundColor Cyan
            $testCmd = "if [ -d '$TargetDir' ]; then echo 'DIR_OK'; else echo 'DIR_MISSING'; fi; if [ -f '$TargetDir/.env' ]; then echo 'ENV_OK'; else echo 'ENV_MISSING'; fi"
            $preflightOut = & ssh -p $SshPort -i $SshKeyPath -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "$SshUser@$SshHost" $testCmd 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Pre-flight SSH connection test failed (exit code $LASTEXITCODE):`n$preflightOut"
                if (-not (Confirm-Choice -Message "Continue anyway despite connection failure?" -DefaultYes $false)) {
                    Write-Error "Setup aborted by user due to failed pre-flight connection test."
                    exit 1
                }
            } else {
                Write-Host "  -> SSH connection successful!" -ForegroundColor Green
                if ($preflightOut -match 'DIR_MISSING') {
                    Write-Warning "Target directory does not exist on server: $TargetDir"
                    if (Confirm-Choice -Message "Create destination directory '$TargetDir' on server now?" -DefaultYes $true) {
                        & ssh -p $SshPort -i $SshKeyPath -o BatchMode=yes "$SshUser@$SshHost" "mkdir -p '$TargetDir'"
                        if ($LASTEXITCODE -eq 0) {
                            Write-Host "  -> Destination directory created successfully!" -ForegroundColor Green
                        } else {
                            Write-Warning "Failed to create destination directory automatically. Please create it manually."
                        }
                    }
                }
                if ($preflightOut -match 'ENV_MISSING' -and $Profile -eq 'laravel-vite') {
                    Write-Warning "No .env file found at '$TargetDir/.env' on server."
                    if (Test-Path -LiteralPath '.env.example') {
                        if (Confirm-Choice -Message "Upload .env.example as baseline production .env to server now?" -DefaultYes $true) {
                            $envExampleContent = (Get-Content -LiteralPath '.env.example' -Raw)
                            $envExampleContent = $envExampleContent -replace '(?m)^APP_ENV=.*$', 'APP_ENV=production'
                            $envExampleContent = $envExampleContent -replace '(?m)^APP_DEBUG=.*$', 'APP_DEBUG=false'
                            $envExampleContent = $envExampleContent.Replace("`r`n", "`n")
                            $uploadEnvCmd = "cat << 'HOSTINGER_ENV_EOF' > '$TargetDir/.env'`n$envExampleContent`nHOSTINGER_ENV_EOF"
                            & ssh -p $SshPort -i $SshKeyPath -o BatchMode=yes "$SshUser@$SshHost" $uploadEnvCmd
                            if ($LASTEXITCODE -eq 0) {
                                Write-Host "  -> Baseline .env uploaded successfully! (APP_KEY will be generated during deploy; configure DB in hPanel)" -ForegroundColor Green
                            } else {
                                Write-Warning "Failed to upload .env template automatically. Please create it manually on server."
                            }
                        }
                    } else {
                        Write-Warning "Remember to create production .env on Hostinger before pushing code!"
                    }
                }
            }
        }
    }
} else {
    # Non-interactive fallback
    if ([string]::IsNullOrWhiteSpace($Branch)) { $Branch = $detectedBranch }
    if ([string]::IsNullOrWhiteSpace($PhpVersion)) { $PhpVersion = $detectedPhp }
    if ($SshPort -le 0) { $SshPort = 65002 }
    if ([string]::IsNullOrWhiteSpace($SshKeyPath)) { $SshKeyPath = Join-Path $HOME ".ssh\id_ed25519" }
    $useGated = ($WithTests.IsPresent -or (($hasPest -or $hasPhpUnit -or $hasPint -or $hasEslint) -and -not $WithoutTests.IsPresent))
    if (-not [string]::IsNullOrWhiteSpace($TargetDir)) { $TargetDir = $TargetDir.Trim().TrimEnd('/').TrimEnd('\') }

    if ($Preflight -and -not [string]::IsNullOrWhiteSpace($SshHost) -and -not [string]::IsNullOrWhiteSpace($SshUser) -and (Test-Path -LiteralPath $SshKeyPath -PathType Leaf)) {
        $testCmd = "if [ -d '$TargetDir' ]; then echo 'DIR_OK'; else echo 'DIR_MISSING'; fi; if [ -f '$TargetDir/.env' ]; then echo 'ENV_OK'; else echo 'ENV_MISSING'; fi"
        $preflightOut = & ssh -p $SshPort -i $SshKeyPath -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "$SshUser@$SshHost" $testCmd 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Pre-flight SSH test failed:`n$preflightOut"
            exit 1
        }
    }
}

. (Join-Path $PSScriptRoot 'generate-workflow.ps1')
