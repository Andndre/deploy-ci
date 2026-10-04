# Shared generator stage; dot-sourced after project inspection and SSH prompts.
function ConvertTo-YamlString {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Get-MatchingKnownHosts {
    param(
        [string]$Path,
        [string]$HostName,
        [int]$Port
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $content = Get-Content -LiteralPath $Path -Raw
    if ([string]::IsNullOrWhiteSpace($content)) { return '' }

    $searchHost = if ($Port -and $Port -ne 22) { "[$HostName]:$Port" } else { $HostName }
    $extracted = ''

    if (Get-Command ssh-keygen -ErrorAction SilentlyContinue) {
        try {
            $out = & ssh-keygen -F $searchHost -f $Path 2>$null
            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($out)) {
                $lines = @($out -split "`r?`n" | Where-Object { $_ -and -not $_.StartsWith('#') })
                if ($lines.Count -gt 0) {
                    $extracted = ($lines -join "`n") + "`n"
                }
            }
        } catch { }
    }

    if (-not $extracted) {
        $escapedHost = [regex]::Escape($HostName)
        $pattern = if ($Port -and $Port -ne 22) {
            "(\[${escapedHost}\]:${Port}|^${escapedHost}\b)"
        } else {
            "(^${escapedHost}\b|\[${escapedHost}\])"
        }
        $matching = @($content -split "`r?`n" | Where-Object { $_ -match $pattern -and -not $_.StartsWith('#') })
        if ($matching.Count -gt 0) {
            $extracted = ($matching -join "`n") + "`n"
        }
    }

    if ($extracted) {
        return $extracted
    }

    $nonCommentLines = @($content -split "`r?`n" | Where-Object { $_ -and -not $_.StartsWith('#') })
    if ($nonCommentLines.Count -le 3) {
        return $content
    }

    Write-Warning "Could not isolate specific fingerprint for $searchHost from $Path. Uploading full file."
    return $content
}

function Assert-RelativePath {
    param([string]$Value)
    if ($Value -notmatch '^[a-zA-Z0-9_./-]+$' -or $Value.StartsWith('/') -or
        $Value.Contains('..') -or $Value.Contains('//') -or $Value.EndsWith('/') -or $Value -eq '.') {
        throw "Unsafe relative directory: $Value"
    }
}

function Show-ContentDiff {
    param([string]$Path, [string]$Before, [string]$After)
    $oldLines = @($Before.Replace("`r`n", "`n").TrimEnd("`n") -split "`n")
    $newLines = @($After.Replace("`r`n", "`n").TrimEnd("`n") -split "`n")
    $start = 0
    while ($start -lt $oldLines.Count -and $start -lt $newLines.Count -and $oldLines[$start] -ceq $newLines[$start]) { $start++ }
    $oldEnd = $oldLines.Count
    $newEnd = $newLines.Count
    while ($oldEnd -gt $start -and $newEnd -gt $start -and $oldLines[$oldEnd - 1] -ceq $newLines[$newEnd - 1]) { $oldEnd--; $newEnd-- }
    Write-Host "--- existing/$Path"
    Write-Host "+++ proposed/$Path"
    Write-Host "@@ -$($start + 1),$($oldEnd - $start) +$($start + 1),$($newEnd - $start) @@"
    for ($i = $start; $i -lt $oldEnd; $i++) { Write-Host "-$($oldLines[$i])" -ForegroundColor Red }
    for ($i = $start; $i -lt $newEnd; $i++) { Write-Host "+$($newLines[$i])" -ForegroundColor Green }
}

$pkgJson = Get-Content -LiteralPath 'package.json' -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($NodeVersion)) { $NodeVersion = $detectedNode }
if ([string]::IsNullOrWhiteSpace($PhpVersion)) { $PhpVersion = $detectedPhp }
if ($NodeVersion -notmatch '^(v?[0-9]+(\.[0-9]+){0,2}|lts/\*)$') { throw 'Invalid Node version.' }
if ($PhpVersion -notmatch '^[0-9]+\.[0-9]+$') { throw 'Invalid PHP version.' }
if ($Branch -notmatch '^[a-zA-Z0-9_./-]+$') { throw 'Invalid deployment branch.' }
git check-ref-format --branch $Branch | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Invalid deployment branch.' }
if ($Environment -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Invalid deployment environment.' }
if (-not [string]::IsNullOrWhiteSpace($TargetDir) -and
    ($TargetDir -notmatch '^/[^/]+/[^/]+/[^/].*' -or $TargetDir -match '[\r\n\x00]' -or
     $TargetDir.Contains('/../') -or $TargetDir.EndsWith('/') -or $TargetDir.Contains('//'))) {
    throw 'TargetDir must be an absolute application path, not a home or root directory.'
}

if ([string]::IsNullOrWhiteSpace($DeployUrl) -and (Test-Path '.env')) {
    $appUrl = Get-Content -LiteralPath '.env' | Where-Object { $_ -match '^APP_URL=' } | Select-Object -First 1
    if ($appUrl) { $DeployUrl = $appUrl.Substring(8).Trim().Trim('"', "'") }
}
if ([string]::IsNullOrWhiteSpace($DeployUrl) -and -not $NonInteractive) {
    $DeployUrl = Read-InputWithDefault -Message 'Public URL to verify after deployment' -DefaultValue '' -Required $true
}
$parsedUrl = $null
if (-not [Uri]::TryCreate($DeployUrl, [UriKind]::Absolute, [ref]$parsedUrl) -or
    $parsedUrl.Scheme -notin @('http', 'https') -or $parsedUrl.UserInfo) {
    throw 'DeployUrl is required and must be an HTTP(S) URL without credentials.'
}

$defaults = switch ($Profile) {
    'laravel-vite' { @{ Output = '.'; Immutable = @('public/build/assets'); WebRoot = 'public'; Ignore = 'public/build' } }
    'static-vite' { @{ Output = 'dist'; Immutable = @('assets'); WebRoot = '.'; Ignore = 'dist' } }
    'sveltekit-static' {
        $svelteConfigFiles = @('vite.config.js', 'vite.config.ts', 'vite.config.mjs', 'vite.config.mts', 'svelte.config.js', 'svelte.config.ts') | Where-Object { Test-Path $_ }
        if (-not $svelteConfigFiles) { throw 'SvelteKit requires a Vite or Svelte configuration file.' }
        $svelteConfig = ($svelteConfigFiles | ForEach-Object { Get-Content -LiteralPath $_ -Raw }) -join "`n"
        if ($svelteConfig -notmatch '@sveltejs/adapter-static') { throw 'Configure @sveltejs/adapter-static before using sveltekit-static. SSR Node is not supported by this profile.' }
        @{ Output = 'build'; Immutable = @('_app/immutable'); WebRoot = '.'; Ignore = 'build' }
    }
    'static' {
        if (-not $OutputDir -or $ImmutableDirs.Count -eq 0) { throw 'The static profile requires OutputDir and ImmutableDirs.' }
        @{ Output = $OutputDir; Immutable = $ImmutableDirs; WebRoot = '.'; Ignore = $OutputDir }
    }
}
if (-not $OutputDir) { $OutputDir = $defaults.Output }
if ($OutputDir -ne '.') { Assert-RelativePath $OutputDir }
if ($Profile -ne 'laravel-vite' -and $OutputDir -eq '.') { throw 'Static output must be a dedicated directory.' }
if ($Profile -eq 'laravel-vite' -and $OutputDir -ne '.') { throw 'Laravel deployment must include the application root.' }
if ($ImmutableDirs.Count -eq 0) { $ImmutableDirs = $defaults.Immutable }
foreach ($directory in $ImmutableDirs) { Assert-RelativePath $directory }
if ($Profile -eq 'laravel-vite' -and @($ImmutableDirs | Where-Object { -not $_.StartsWith('public/') }).Count) {
    throw 'Laravel immutable directories must be inside public/.'
}
for ($i = 0; $i -lt $ImmutableDirs.Count; $i++) {
    for ($j = $i + 1; $j -lt $ImmutableDirs.Count; $j++) {
        if ($ImmutableDirs[$i] -eq $ImmutableDirs[$j] -or $ImmutableDirs[$i].StartsWith($ImmutableDirs[$j] + '/') -or $ImmutableDirs[$j].StartsWith($ImmutableDirs[$i] + '/')) {
            throw 'Immutable directories must not overlap.'
        }
    }
}
$ignoreOutput = if ($Profile -eq 'laravel-vite') { 'public/build' } else { $OutputDir }
$protected = @('.env*', '.git', '.github', 'node_modules', '.hostinger-ci', 'deploy-diagnostics')
if ($Profile -eq 'laravel-vite') { $protected += @('storage', 'database/*.sqlite*', 'tests', 'public/storage') }
else { $protected += @('uploads', '.well-known') }
foreach ($path in $ProtectedPaths) {
    if ($path -notmatch '^[a-zA-Z0-9_.*?/-]+$' -or $path.StartsWith('/') -or $path.Contains('..')) { throw "Invalid protected path: $path" }
    $protected += $path
}

$managerVersion = ''
if ($pkgJson.packageManager -match '^(npm|pnpm|yarn|bun)@([0-9]+(?:\.[0-9]+){0,2}(?:-[a-zA-Z0-9.-]+)?)(?:\+.*)?$') {
    if (-not $PackageManager) { $PackageManager = $Matches[1] }
    if ($PackageManager -eq $Matches[1]) { $managerVersion = $Matches[2] }
}
if (-not $PackageManager) {
    $lockManagers = @()
    if (Test-Path 'package-lock.json') { $lockManagers += 'npm' }
    if (Test-Path 'pnpm-lock.yaml') { $lockManagers += 'pnpm' }
    if (Test-Path 'yarn.lock') { $lockManagers += 'yarn' }
    if ((Test-Path 'bun.lock') -or (Test-Path 'bun.lockb')) { $lockManagers += 'bun' }
    if ($lockManagers.Count -ne 1) { throw 'Select PackageManager explicitly when lockfiles are missing or ambiguous.' }
    $PackageManager = $lockManagers[0]
}
$lockfile = switch ($PackageManager) { 'npm' { 'package-lock.json' }; 'pnpm' { 'pnpm-lock.yaml' }; 'yarn' { 'yarn.lock' }; 'bun' { if (Test-Path 'bun.lock') { 'bun.lock' } else { 'bun.lockb' } } }
if (-not (Test-Path $lockfile)) { throw "Missing $PackageManager lockfile: $lockfile" }
if ($PackageManager -in @('pnpm', 'yarn', 'bun') -and -not $managerVersion) { throw "Set packageManager to a pinned $PackageManager version in package.json." }
$installCommand = switch ($PackageManager) {
    'npm' { 'npm ci' }
    'pnpm' { 'pnpm install --frozen-lockfile' }
    'yarn' { if ($managerVersion.StartsWith('1.')) { 'yarn install --frozen-lockfile' } else { 'yarn install --immutable' } }
    'bun' { 'bun install --frozen-lockfile' }
}
if (-not $BuildCommand) {
    if (-not $pkgJson.scripts.build) { throw 'Define scripts.build or pass BuildCommand.' }
    $BuildCommand = "$PackageManager run build"
}
if ($BuildCommand -match '[\r\n]') { throw 'BuildCommand must be a single line.' }
$lintCmd = $lintCmd -replace '^npm ', "$PackageManager "

$phpSetup = ''
$composerInstall = ''
if ($Profile -eq 'laravel-vite') {
    $phpSetup = @"
      - name: Setup PHP
        uses: shivammathur/setup-php@v2
        with:
          php-version: $(ConvertTo-YamlString $PhpVersion)
          extensions: mbstring, xml, ctype, iconv, intl, pdo_mysql, pdo_sqlite, bcmath, curl, zip, gd, exif
          coverage: none

"@
    $composerInstall = "          composer install --no-dev --prefer-dist --optimize-autoloader --no-interaction`n"
}
$nodeSetup = @"
      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: $(ConvertTo-YamlString $NodeVersion)

"@
if ($PackageManager -in @('pnpm', 'yarn')) {
    $nodeSetup += @"
      - name: Activate package manager
        run: |
          if ! command -v corepack >/dev/null; then npm install --global corepack; fi
          corepack enable
          corepack prepare $PackageManager@$managerVersion --activate

"@
} elseif ($PackageManager -eq 'bun') {
    $nodeSetup += @"
      - name: Setup Bun
        uses: oven-sh/setup-bun@v2
        with:
          bun-version: $(ConvertTo-YamlString $managerVersion)

"@
} elseif ($managerVersion) {
    $nodeSetup += "      - name: Activate npm`n        run: npm install --global npm@$managerVersion`n`n"
}

$buildSteps = @"
$phpSetup$nodeSetup      - name: Install dependencies and build
        run: |
$composerInstall          $installCommand
          $BuildCommand

"@
$verifyJob = ''
$deployNeeds = ''
if ($useGated) {
    $qualitySteps = ''
    if ($Profile -eq 'laravel-vite' -and $hasPint) { $qualitySteps += "      - name: Check PHP style`n        run: vendor/bin/pint --test`n`n" }
    if ($hasEslint) { $qualitySteps += "      - name: Check frontend lint`n        run: $lintCmd`n`n" }
    if ($Profile -eq 'laravel-vite' -and ($hasPest -or $hasPhpUnit -or $WithTests)) {
        $testCommand = if ($hasPest) { 'vendor/bin/pest' } else { 'php artisan test' }
        $qualitySteps += "      - name: Run PHP tests`n        run: $testCommand`n`n"
    } elseif ($Profile -ne 'laravel-vite') {
        $testScript = if ($pkgJson.scripts.'test:ci') { 'test:ci' } elseif ($pkgJson.scripts.test) { 'test' } else { '' }
        if ($testScript) { $qualitySteps += "      - name: Run frontend tests`n        run: $PackageManager run $testScript`n`n" }
    }
    $verifyBuild = $buildSteps.Replace('composer install --no-dev ', 'composer install ')
    if ($Profile -eq 'laravel-vite') {
        $verifyBuild = $verifyBuild.Replace("          $BuildCommand", "          cp .env.example .env`n          php artisan key:generate`n          $BuildCommand")
    }
    $verifyJob = @"
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
$verifyBuild$qualitySteps
"@
    $deployNeeds = "    needs: [verify]`n"
}

$workflowTemplate = @'
name: Deploy CI

on:
  push:
    branches: [@@branch@@]
@@pr@@  workflow_dispatch:

permissions:
  contents: read

env:
  CI: 'true'

jobs:
@@verify@@  deploy:
    runs-on: ubuntu-latest
@@needs@@    if: github.ref == 'refs/heads/@@raw_branch@@' && (github.event_name == 'push' || github.event_name == 'workflow_dispatch')
    environment: @@environment@@
    concurrency:
      group: hostinger-@@raw_environment@@
      cancel-in-progress: false
    env:
      HOSTINGER_SSH_HOST: ${{ secrets.HOSTINGER_SSH_HOST }}
      HOSTINGER_SSH_USER: ${{ secrets.HOSTINGER_SSH_USER }}
      HOSTINGER_SSH_PORT: ${{ secrets.HOSTINGER_SSH_PORT }}
      HOSTINGER_SSH_KEY: ${{ secrets.HOSTINGER_SSH_KEY }}
      HOSTINGER_SSH_KNOWN_HOSTS: ${{ secrets.HOSTINGER_SSH_KNOWN_HOSTS }}
      HOSTINGER_TARGET_DIR: ${{ secrets.HOSTINGER_TARGET_DIR }}
    steps:
      - uses: actions/checkout@v4
@@build@@      - name: Upload immutable assets, then publish application
        id: transfer
        run: python3 .github/hostinger/deploy.py transfer

      - name: Verify deployed HTML and JS/CSS
        id: verify
        run: python3 .github/hostinger/check-deploy.py

      - name: Clean expired deployment assets
        id: cleanup
        if: ${{ !cancelled() && steps.transfer.outcome == 'success' }}
        env:
          HOSTINGER_HTTP_VERIFIED: ${{ steps.verify.outcome == 'success' }}
        run: python3 .github/hostinger/deploy.py cleanup

      - name: Summarize deployment stages
        if: always()
        env:
          TRANSFER_OUTCOME: ${{ steps.transfer.outcome }}
          VERIFY_OUTCOME: ${{ steps.verify.outcome }}
          CLEANUP_OUTCOME: ${{ steps.cleanup.outcome }}
        run: |
          printf '## Deployment results\n\n| Stage | Result |\n|---|---|\n| Transfer and optimization | %s |\n| Public HTTP verification | %s |\n| Asset cleanup | %s |\n\n' "$TRANSFER_OUTCOME" "$VERIFY_OUTCOME" "$CLEANUP_OUTCOME" >> "$GITHUB_STEP_SUMMARY"
          printf 'A failed HTTP check does not establish whether application publication failed. Review its response evidence; no automatic rollback was performed.\n' >> "$GITHUB_STEP_SUMMARY"

      - name: Save HTTP diagnostics
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: deploy-diagnostics-${{ github.run_id }}-${{ github.run_attempt }}
          path: deploy-diagnostics/
          if-no-files-found: ignore
          retention-days: 14
'@
$prTrigger = if ($useGated) { "  pull_request:`n    branches: [$(ConvertTo-YamlString $Branch)]`n" } else { '' }
$workflowContent = $workflowTemplate.Replace('@@branch@@', (ConvertTo-YamlString $Branch)).Replace('@@pr@@', $prTrigger).
    Replace('@@verify@@', $verifyJob).Replace('@@needs@@', $deployNeeds).Replace('@@raw_branch@@', $Branch).
    Replace('@@environment@@', (ConvertTo-YamlString $Environment)).Replace('@@raw_environment@@', $Environment).Replace('@@build@@', $buildSteps)

$profileConfig = [ordered]@{
    profile = $Profile
    output_dir = $OutputDir
    web_root = $defaults.WebRoot
    immutable_dirs = @($ImmutableDirs)
    protected_paths = @($protected)
    keep_releases = $KeepReleases
    retention_days = $RetentionDays
    max_retained_files = $MaxRetainedFiles
    max_retained_bytes = ([long]$MaxRetainedMiB * 1048576)
    php_version = $PhpVersion
    migrate = [bool]$IncludeMigration
    maintenance = [bool]$MaintenanceMode
    deploy_url = $DeployUrl
    page_contains = $PageContains
    max_assets = 6
    package_manager = $PackageManager
    build_command = $BuildCommand
}
$files = [ordered]@{ '.github/workflows/deploy.yml' = $workflowContent; '.github/hostinger/profile.json' = ($profileConfig | ConvertTo-Json -Depth 5) }
foreach ($helper in @('deploy.py', 'remote-deploy.sh', 'check-deploy.py')) {
    $files[".github/hostinger/$helper"] = Get-Content -LiteralPath (Join-Path $PSScriptRoot $helper) -Raw
}

# Preview the entire replacement before any repository/cache/secret mutation.
$replacement = $false
foreach ($path in $files.Keys) {
    if (Test-Path -LiteralPath $path) {
        $before = Get-Content -LiteralPath $path -Raw
        if ($before.Replace("`r`n", "`n").TrimEnd() -cne $files[$path].Replace("`r`n", "`n").TrimEnd()) {
            Show-ContentDiff $path $before $files[$path]
            $replacement = $true
        }
    } else { Write-Host "  -> New generated file: $path" }
}
if ($DryRun) {
    if (-not (Test-Path '.github/workflows/deploy.yml')) { Write-Host $workflowContent }
    Write-Host '[DryRun] No workflow, Git index, ignore file, cache, or secrets were changed.' -ForegroundColor Cyan
    return
}
if ($replacement -and -not $Force) {
    if ($NonInteractive) { throw 'Generated files already exist with changes. Review the diff and rerun with -Force to replace them.' }
    if (-not (Confirm-Choice -Message 'Replace the generated files shown in the diff?' -DefaultYes $false)) { return }
}

$uploadSecrets = -not $SkipSecrets -and [bool](Get-Command gh -ErrorAction SilentlyContinue)
if (-not $SkipSecrets -and -not $uploadSecrets) { Write-Warning 'GitHub CLI is unavailable; configure the six SSH secrets manually.' }
if ($uploadSecrets) {
    if (-not $SshHost -or -not $SshUser -or -not $TargetDir) { throw 'SshHost, SshUser and TargetDir are required for secret setup.' }
    if (-not $SshKnownHostsPath -and -not $NonInteractive) {
        $SshKnownHostsPath = Read-InputWithDefault -Message 'Path to verified SSH known_hosts entries for Hostinger' -DefaultValue (Join-Path $HOME '.ssh\known_hosts') -Required $true
    }
    if (-not (Test-Path -LiteralPath $SshKeyPath -PathType Leaf)) { throw "SSH key not found: $SshKeyPath" }
    if (-not $SshKnownHostsPath -or -not (Test-Path -LiteralPath $SshKnownHostsPath -PathType Leaf)) { throw 'Supply SshKnownHostsPath with independently verified Hostinger host keys.' }
}

$utf8 = New-Object Text.UTF8Encoding $false
foreach ($path in $files.Keys) {
    $fullPath = Join-Path (Get-Location).Path $path
    $parent = Split-Path -Parent $fullPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporaryFile = Join-Path $parent ('.deploy-ci-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporaryFile, $files[$path].Replace("`r`n", "`n") + "`n", $utf8)
        Move-Item -LiteralPath $temporaryFile -Destination $fullPath -Force
    } finally {
        if (Test-Path -LiteralPath $temporaryFile) { Remove-Item -LiteralPath $temporaryFile }
    }
}
$gitignoreFile = Join-Path (Get-Location).Path '.gitignore'
$gitignoreContent = if (Test-Path $gitignoreFile) { [IO.File]::ReadAllText($gitignoreFile) } else { '' }
$ignorePattern = '(^|\r?\n)/?' + [regex]::Escape($ignoreOutput) + '/?($|\r?\n)'
if ($gitignoreContent -notmatch $ignorePattern) {
    [IO.File]::WriteAllText($gitignoreFile, $gitignoreContent.TrimEnd() + "`n/$ignoreOutput`n", $utf8)
}
$trackedOutput = @(git ls-files -- $ignoreOutput)
if ($LASTEXITCODE -ne 0) { throw 'Failed to inspect the Git index.' }
if ($trackedOutput.Count -gt 0) {
    git rm -r --cached -- $ignoreOutput | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to untrack generated output.' }
}
try {
    @{ SshHost = $SshHost; SshUser = $SshUser; SshPort = $SshPort; TargetDir = $TargetDir } |
        ConvertTo-Json | Set-Content -LiteralPath $configCacheFile -Encoding UTF8
} catch { Write-Warning 'Unable to update the optional Hostinger configuration cache.' }

if ($uploadSecrets) {
    $knownHostsContent = Get-MatchingKnownHosts -Path $SshKnownHostsPath -HostName $SshHost -Port $SshPort
    if ([string]::IsNullOrWhiteSpace($knownHostsContent)) {
        $knownHostsContent = (Get-Content -LiteralPath $SshKnownHostsPath -Raw)
    }
    $secrets = [ordered]@{
        HOSTINGER_SSH_HOST = $SshHost.Trim()
        HOSTINGER_SSH_USER = $SshUser.Trim()
        HOSTINGER_SSH_PORT = [string]$SshPort
        HOSTINGER_TARGET_DIR = $TargetDir.Trim().TrimEnd('/').TrimEnd('\')
        HOSTINGER_SSH_KNOWN_HOSTS = $knownHostsContent.Trim()
        HOSTINGER_SSH_KEY = (Get-Content -LiteralPath $SshKeyPath -Raw).Trim()
    }
    foreach ($name in $secrets.Keys) {
        $secrets[$name] | & gh secret set $name
        if ($LASTEXITCODE -ne 0) { throw "GitHub secret setup failed for $name (exit $LASTEXITCODE). Some earlier secrets may already have been set." }
    }
    Write-Host 'All six SSH secrets configured successfully.' -ForegroundColor Green
}
Write-Host "Generated $Profile deployment with Node $NodeVersion and $PackageManager." -ForegroundColor Green
Write-Host 'Review and commit .gitignore, .github/workflows/deploy.yml and .github/hostinger/ to activate deployment.'
