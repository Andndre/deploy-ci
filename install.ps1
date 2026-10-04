<#
.SYNOPSIS
    One-liner automated installer for deploy-ci on Windows.

.USAGE
    irm https://raw.githubusercontent.com/Andndre/deploy-ci/main/install.ps1 | iex
#>

$ErrorActionPreference = "Stop"

$repoOwner = "Andndre"
$repoName  = "deploy-ci"
$branch    = "main"
$baseUrl   = "https://raw.githubusercontent.com/$repoOwner/$repoName/$branch"

$installDir = Join-Path $HOME ".deploy-ci\bin"

Write-Host "`n========================================================" -ForegroundColor Cyan
Write-Host "  Installing deploy-ci CLI Tool" -ForegroundColor Cyan
Write-Host "========================================================`n" -ForegroundColor Cyan

# 1. Create installation directory if it does not exist
if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
    Write-Host "[1/4] Creating installation directory at: $installDir" -ForegroundColor Gray
} else {
    Write-Host "[1/4] Using directory: $installDir" -ForegroundColor Gray
}

# 2. Download executable scripts from GitHub
Write-Host "[2/4] Downloading CLI scripts from GitHub..." -ForegroundColor Gray
$files = @(
    "deploy-ci.cmd",
    "deploy-ci.ps1",
    "generate-workflow.ps1",
    "deploy.py",
    "remote-deploy.sh",
    "check-deploy.py"
)

foreach ($file in $files) {
    $fileUrl = "$baseUrl/bin/$file"
    $destPath = Join-Path $installDir $file
    try {
        Invoke-RestMethod -Uri $fileUrl -OutFile $destPath
        Write-Host "  -> $file successfully downloaded" -ForegroundColor Green
    } catch {
        Write-Error "Failed to download $file from $fileUrl. Please check your internet connection."
        exit 1
    }
}

# 3. Add to User PATH if not already registered
Write-Host "[3/4] Checking User Environment PATH..." -ForegroundColor Gray
$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathParts = $currentPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

if ($pathParts -notcontains $installDir) {
    $newPath = ($pathParts + $installDir) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    $env:Path = "$installDir;" + $env:Path
    Write-Host "  -> $installDir successfully added to User PATH!" -ForegroundColor Green
} else {
    Write-Host "  -> Directory is already registered in User PATH." -ForegroundColor DarkGray
}

# 4. Validate toolchain prerequisites
Write-Host "[4/4] Verifying prerequisites..." -ForegroundColor Gray
$hasGit = Get-Command git -ErrorAction SilentlyContinue
$hasGh  = Get-Command gh -ErrorAction SilentlyContinue

if ($hasGit) {
    Write-Host "  -> Git detected" -ForegroundColor Green
} else {
    Write-Warning "Git was not found. Please install Git: https://git-scm.com/"
}

if ($hasGh) {
    Write-Host "  -> GitHub CLI (gh) detected" -ForegroundColor Green
} else {
    Write-Warning "GitHub CLI (gh) was not found. Recommended to install via: winget install GitHub.cli"
}

Write-Host "`n========================================================" -ForegroundColor Cyan
Write-Host "  Installation Succeeded!" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "To get started:" -ForegroundColor White
Write-Host "1. Open a new terminal window (PowerShell, CMD, or Git Bash)." -ForegroundColor Gray
Write-Host "2. Navigate to your Laravel project root directory." -ForegroundColor Gray
Write-Host "3. Run the command:`n" -ForegroundColor Gray
Write-Host "     deploy-ci`n" -ForegroundColor Yellow
Write-Host "Repository: https://github.com/$repoOwner/$repoName`n" -ForegroundColor DarkGray
