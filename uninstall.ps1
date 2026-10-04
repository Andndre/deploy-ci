<#
.SYNOPSIS
    Clean uninstaller for deploy-ci.
#>

$ErrorActionPreference = "Stop"
$installDir = Join-Path $HOME ".deploy-ci"
$binDir = Join-Path $installDir "bin"

Write-Host "`n========================================================" -ForegroundColor Yellow
Write-Host "  Uninstalling deploy-ci" -ForegroundColor Yellow
Write-Host "========================================================`n" -ForegroundColor Yellow

# 1. Remove from User PATH
$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathParts = $currentPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and $_ -ne $binDir }
$newPath = $pathParts -join ";"
[Environment]::SetEnvironmentVariable("Path", $newPath, "User")
Write-Host "[1/2] Removing $binDir from User PATH..." -ForegroundColor Green

# 2. Delete installation directory
if (Test-Path $installDir) {
    Remove-Item -Path $installDir -Recurse -Force
    Write-Host "[2/2] Deleting installation directory $installDir..." -ForegroundColor Green
} else {
    Write-Host "[2/2] Installation directory not found." -ForegroundColor DarkGray
}

Write-Host "`ndeploy-ci successfully removed from your system.`n" -ForegroundColor Green
