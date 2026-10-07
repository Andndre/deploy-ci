#!/usr/bin/env bash
# Executed over SSH by deploy.py. All arguments are shell-quoted by the caller.
set -euo pipefail
target=$1
phase=$2
run=$3
immutable=$4
keep=$5
days=$6
profile=$7
migrate=$8
max_files=${9:-10000}
max_bytes=${10:-536870912}
incoming_bytes=${11:-0}
php_expected=${12:-}
http_verified=${13:-false}
maintenance=${14:-false}
php_web_user=${15:-auto}
php_bin="php"
if [[ -n "$php_expected" ]]; then
  version_clean="${php_expected//./}"
  alt_php="/opt/alt/php${version_clean}/usr/bin/php"
  ea_php="/usr/bin/ea-php${version_clean}"
  if [[ -x "$alt_php" ]]; then
    php_bin="$alt_php"
  elif [[ -x "$ea_php" ]]; then
    php_bin="$ea_php"
  fi
fi

incoming=${incoming:-}
active_stage=
stage_started=0
start_stage() {
  active_stage=$1
  stage_started=$(date +%s%N)
}
finish_stage() {
  local elapsed=$(( $(date +%s%N) - stage_started ))
  printf 'HOSTINGER_TIMING {"stage":"%s","seconds":%s.%03d,"outcome":"%s"}\n' "$active_stage" "$((elapsed / 1000000000))" "$((elapsed / 1000000 % 1000))" "${1:-success}"
  active_stage=
}
start_stage remote-preparation

fail() { echo "Hostinger $phase: $*" >&2; exit 1; }
trap 'echo "Hostinger remote phase $phase failed at line $LINENO" >&2' ERR
cleanup_trap() {
  local code=$?
  if [[ -n "$active_stage" ]]; then finish_stage "$([[ $code == 0 ]] && echo success || echo failure)"; fi
  if [[ "$phase" == prepare && "${entered_maintenance:-false}" == true && $code -ne 0 ]]; then
    echo 'Deployment prepare aborted; recovering from maintenance mode' >&2
    "$php_bin" artisan up || echo 'Maintenance recovery failed; operator intervention required' >&2
  fi
}
trap cleanup_trap EXIT

(( BASH_VERSINFO[0] >= 4 )) || fail 'Bash 4 or newer is required'
for tool in find sha256sum cut stat dirname basename cat mkdir printf touch mv sort mktemp date grep rm rsync awk wc id; do
  command -v "$tool" >/dev/null || fail "Required server command is unavailable: $tool"
done

safe_directory() {
  local path=$1 part walk=$target
  [[ "$path" =~ ^[a-zA-Z0-9_./-]+$ && "$path" != /* && "$path" != *..* ]] || fail "Unsafe runtime directory: $path"
  IFS=/ read -r -a path_parts <<< "$path"
  for part in "${path_parts[@]}"; do
    walk="$walk/$part"
    [[ ! -L "$walk" ]] || fail "Symlink in runtime directory: $walk; no permission changes performed"
    [[ ! -e "$walk" || -d "$walk" ]] || fail "Runtime path is not a directory: $walk"
  done
}
check_writable() {
  local path=$1 owner group mode metadata
  metadata=$(stat -c '%u %g %a' -- "$path") || fail "Cannot read permission/ownership metadata: $path"
  read -r owner group mode <<< "$metadata"
  echo "Runtime directory $path: uid=$owner gid=$group mode=$mode; deploy/PHP uid=$(id -u)"
  [[ "$owner" == "$(id -u)" && -w "$path" && -x "$path" && -r "$path" ]] || fail "Ownership/access mismatch at $path (uid=$owner gid=$group mode=$mode). Fix this directory for the audited PHP user; no recursive chmod/chown attempted"
  (( (8#$mode & 0700) == 0700 )) || fail "Owner cannot read/write/traverse $path (mode=$mode); no recursive chmod attempted"
  (( (8#$mode & 0002) == 0 )) || fail "World-writable runtime directory: $path (mode=$mode). Review ownership and access explicitly"
}
check_runtime_permissions() {
  local uid worker_users path cli_uid
  uid=$(id -u)
  cli_uid=$("$php_bin" -r 'if (!function_exists("posix_geteuid")) { exit(1); } echo posix_geteuid();') || fail 'PHP POSIX identity check is unavailable'
  [[ "$cli_uid" == "$uid" ]] || fail "PHP CLI uid=$cli_uid differs from deploy uid=$uid"
  if [[ "$php_web_user" == auto ]]; then
    command -v ps >/dev/null || fail 'Process inspection is unavailable; audit and configure php_web_user explicitly'
    worker_users=$(ps -eo uid=,comm= | awk '$2 ~ /^(lsphp|php-fpm|php-cgi)/ {print $1}')
    grep -qx "$uid" <<< "$worker_users" || fail "Cannot verify a PHP web worker running as deploy uid=$uid. Audit the vhost PHP user and configure php_web_user explicitly; no permissions changed"
    echo "PHP web worker found for deploy uid=$uid (same-user hosting policy)"
  else
    [[ "$php_web_user" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_-]*$ ]] || fail 'Invalid configured PHP web user'
    [[ "$(id -u "$php_web_user")" == "$uid" ]] || fail "Configured PHP web user $php_web_user differs from deployment user; shared-group ownership needs a separate audited policy"
  fi
  for path in storage storage/app storage/app/private storage/app/public storage/framework storage/framework/sessions storage/framework/views storage/framework/cache storage/framework/cache/data storage/logs bootstrap bootstrap/cache; do
    safe_directory "$path"
    if [[ ! -d "$path" ]]; then (umask 002; mkdir -p -- "$path"); fi
    check_writable "$path"
  done
  # Probe only runtime roots, never descend into existing uploads/media.
  "$php_bin" <<'WRITABLE_PHP'
<?php
foreach (['storage/app/private', 'storage/app/public', 'storage/framework/sessions', 'storage/framework/views', 'storage/framework/cache/data', 'storage/logs', 'bootstrap/cache'] as $directory) {
    $probe = tempnam($directory, '.deploy-access-');
    if ($probe === false || realpath(dirname($probe)) !== realpath($directory)) {
        file_put_contents('php://stderr', "Writable probe failed: {$directory}\n");
        exit(1);
    }
    if (file_put_contents($probe, 'probe') === false || !unlink($probe)) {
        file_put_contents('php://stderr', "Write/remove probe failed: {$directory}\n");
        exit(1);
    }
}
WRITABLE_PHP
}
check_public_permissions() {
  local path mode owner group metadata
  local public_roots=(.)
  if [[ "$profile" == laravel-vite ]]; then public_roots+=(public public/build); fi
  for path in "${public_roots[@]}" "${asset_dirs[@]}"; do
    safe_directory "$path"
    (umask 022; mkdir -p -- "$path")
    metadata=$(stat -c '%u %g %a' -- "$path") || fail "Cannot read permission/ownership metadata: $path"
    read -r owner group mode <<< "$metadata"
    echo "Public directory $path: uid=$owner gid=$group mode=$mode; deploy uid=$(id -u)"
    if [[ ! -w "$path" ]] || (( (8#$mode & 0005) != 0005 )); then
      fail "Public directory $path is not writable by deploy/readable and traversable by web server (mode=$mode); no recursive chmod attempted"
    fi
  done
}

[[ "$target" == /* && "$target" != / && "$target" != *$'\n'* ]]
[[ -d "$target" ]] || fail "Destination does not exist. Create the application directory before deploying: $target"
[[ -w "$target" && "$(cd -- "$target" && pwd -P)" == "$target" ]] || fail 'Destination must be writable and canonical, with no symlink ancestors'
find "$target" -maxdepth 0 -printf '' || fail 'GNU-compatible find with -printf is required'
stat -c %s -- "$target" >/dev/null || fail 'GNU-compatible stat with -c is required'
[[ "$run" =~ ^[a-f0-9]{32}$ && "$keep" =~ ^[0-9]+$ && "$days" =~ ^[0-9]+$ ]]
(( keep >= 2 && days >= 1 ))
[[ "$max_files" =~ ^[0-9]+$ && "$max_bytes" =~ ^[0-9]+$ && "$incoming_bytes" =~ ^[0-9]+$ ]]
(( max_files > 0 && max_bytes > 0 ))
cd -- "$target"
state="$(dirname -- "$target")/.$(basename -- "$target").hostinger-ci"
[[ -w "$(dirname -- "$target")" ]] || fail 'The destination parent must be writable for deployment inventories'
[[ ! -L "$state" ]]
if [[ -e "$state" ]]; then
  [[ -d "$state" && -f "$state/owner" && ! -L "$state/owner" && "$(cat "$state/owner")" == "$target" ]]
  [[ -z "$(find "$state" -type l -print -quit)" ]]
fi

IFS=: read -r -a asset_dirs <<< "$immutable"
safe_asset() {
  local path=$1 dir part walk
  [[ "$path" =~ ^[a-zA-Z0-9_./-]+$ && "$path" != /* && "$path" != *..* ]] || return 1
  for dir in "${asset_dirs[@]}"; do
    if [[ "$path" == "$dir/"* ]]; then
      walk=$target
      IFS=/ read -r -a parts <<< "$path"
      for part in "${parts[@]}"; do
        walk="$walk/$part"
        [[ ! -L "$walk" ]] || return 1
      done
      return 0
    fi
  done
  return 1
}
for dir in "${asset_dirs[@]}"; do
  safe_asset "$dir/check" || { echo 'Unsafe immutable directory' >&2; exit 1; }
  if [[ -d "$dir" ]]; then
    [[ -z "$(find "$dir" -type l -print -quit)" ]]
  fi
done

case "$phase" in
  prepare)
    if [[ "$profile" == laravel-vite ]]; then
      command -v "$php_bin" >/dev/null || fail "PHP CLI ($php_bin) is required for Laravel"
      [[ -f .env ]] || fail 'Create the production .env on the server before deployment; local credentials are excluded'
      if [[ -n "$php_expected" ]]; then
        [[ "$("$php_bin" -r 'echo PHP_MAJOR_VERSION, ".", PHP_MINOR_VERSION;')" == "$php_expected" ]] || fail "PHP CLI must match configured version $php_expected"
      fi
      finish_stage
      start_stage storage-permissions
      check_runtime_permissions
      finish_stage
      start_stage public-permissions
      check_public_permissions
      finish_stage
    else
      finish_stage
      start_stage public-permissions
      check_public_permissions
      finish_stage
    fi
    start_stage remote-retention-preflight
    missing_files=0
    reused_bytes=0
    # Check every collision before transferring any assets. Immutable URLs must
    # never acquire different bytes, even when an output contains an unhashed file.
    while read -r hash path; do
      [[ "$hash" =~ ^[a-f0-9]{64}$ ]]
      safe_asset "$path"
      if [[ -e "$path" ]]; then
        [[ -f "$path" && "$(sha256sum -- "$path" | cut -d ' ' -f 1)" == "$hash" ]] || {
          echo "Immutable asset collision: $path" >&2; exit 1;
        }
        reused_bytes=$((reused_bytes + $(stat -c %s -- "$path")))
      else
        missing_files=$((missing_files + 1))
      fi
    done <<< "$incoming"
    retained_files=0
    retained_bytes=0
    baseline_bytes=0
    for dir in "${asset_dirs[@]}" "$state"; do
      [[ -d "$dir" ]] || continue
      # Batch the scan rather than spawning stat once per retained file. A find
      # permission/error must fail the budget check instead of undercounting.
      totals=$(find "$dir" -type f -printf '%s\n' | awk '{count++; bytes+=$1} END {printf "%.0f %.0f\n", count, bytes}')
      read -r dir_files dir_bytes <<< "$totals"
      retained_files=$((retained_files + dir_files))
      retained_bytes=$((retained_bytes + dir_bytes))
      if [[ "$dir" != "$state" && ! -f "$state/baseline-done" ]]; then
        # Conservatively reserve inventory space for every legacy file, even
        # though only recognizable hashed filenames will actually be adopted.
        path_bytes=$(find "$dir" -type f -printf '%p\0' | wc -c)
        baseline_bytes=$((baseline_bytes + dir_files * 66 + path_bytes))
      fi
    done
    # Reserve four inventory files; include assets from failed uploads too.
    projected_files=$((retained_files + missing_files + 4))
    additional_bytes=$((incoming_bytes > reused_bytes ? incoming_bytes - reused_bytes : 0))
    projected_bytes=$((retained_bytes + additional_bytes + baseline_bytes + ${#incoming} * 2 + 4096))
    echo "Retention budget: at most $projected_files files and $projected_bytes bytes after this upload (limits: $max_files / $max_bytes)"
    (( projected_files <= max_files && projected_bytes <= max_bytes )) || fail 'Retained asset/inventory budget would be exceeded. Review usage and expired inventories before uploading; do not shorten retention blindly'
    echo 'Remote prerequisites and retention budget passed; no application files have been changed'
    umask 077
    mkdir -p -- "$state"
    printf '%s' "$target" > "$state/owner"
    [[ ! -L "$state/history" && ! -L "$state/pending" ]]
    mkdir -p -- "$state/history" "$state/pending"
    # Adopt existing hashed assets once, so upgrading from the old generator
    # keeps old chunks and eventually cleans them up too. Unknown files stay.
    if [[ ! -f "$state/baseline-done" ]]; then
      baseline="$state/history/00000000000000-baseline.txt"
      rm -f -- "$baseline"
      : > "$baseline"
      for dir in "${asset_dirs[@]}"; do
        [[ -d "$dir" ]] || continue
        baseline_scan=$(mktemp "$state/scan.XXXXXX")
        find "$dir" -type f -print0 > "$baseline_scan"
        while IFS= read -r -d '' path; do
          if [[ "$(basename -- "$path")" =~ [.-][a-zA-Z0-9_-]{8,}\. ]]; then
            safe_asset "$path"
            hash=$(sha256sum -- "$path" | cut -d ' ' -f 1)
            printf '%s  %s\n' "$hash" "$path" >> "$baseline"
          fi
        done < "$baseline_scan"
        rm -f -- "$baseline_scan"
      done
      touch -- "$state/baseline-done"
    fi
    printf '%s\n' "$incoming" > "$state/pending/$run.txt"
    for dir in "${asset_dirs[@]}"; do (umask 022; mkdir -p -- "$dir"); done
    finish_stage
    if [[ "$profile" == laravel-vite && "$maintenance" == true && -f artisan ]]; then
      start_stage maintenance-down
      echo 'Entering maintenance mode during deployment'
      entered_maintenance=true
      "$php_bin" artisan down --retry=15
      finish_stage
    fi
    ;;
  optimize)
    finish_stage
    if [[ "$profile" == laravel-vite ]]; then
      [[ -f artisan ]]
      start_stage storage-permissions
      check_runtime_permissions
      finish_stage
      start_stage public-permissions
      check_public_permissions
      finish_stage
      if grep -qE '^APP_KEY=\s*$' .env || ! grep -q '^APP_KEY=' .env; then
        start_stage application-key
        echo 'Generating application encryption key'
        "$php_bin" artisan key:generate --force
        finish_stage
      fi
      start_stage cache-clear
      "$php_bin" artisan optimize:clear
      finish_stage
      if [[ "$migrate" == true ]]; then
        start_stage migration
        echo 'Explicit database migration option enabled; failures are not automatically rolled back'
        "$php_bin" artisan migrate --force
        finish_stage
      fi
      start_stage cache-build
      "$php_bin" artisan config:cache
      "$php_bin" artisan route:cache
      "$php_bin" artisan view:cache
      finish_stage
      if [[ "$maintenance" == true ]]; then
        start_stage maintenance-up
        echo 'Exiting maintenance mode after deployment'
        "$php_bin" artisan up
        finish_stage
      fi
    fi
    ;;
  cleanup)
    finish_stage
    [[ -d "$state/history" && ! -L "$state/history" && ! -L "$state/pending" ]]
    [[ -f "$state/pending/$run.txt" && ! -L "$state/pending/$run.txt" ]]
    published="$state/history/$(date -u +%Y%m%d%H%M%S)-$run.txt"
    mv -- "$state/pending/$run.txt" "$published"
    if [[ "$http_verified" == true ]]; then
      verified_snapshot=$(mktemp "$state/last-verified.XXXXXX")
      cat -- "$published" > "$verified_snapshot"
      mv -- "$verified_snapshot" "$state/last-verified.txt"
    fi
    hist_tmp=$(mktemp "$state/hist.XXXXXX")
    find "$state/history" -maxdepth 1 -type f -name '*.txt' | sort -r > "$hist_tmp"
    mapfile -t histories < "$hist_tmp"
    rm -f -- "$hist_tmp"
    retained=$(mktemp)
    trap 'rm -f -- "$retained"' EXIT
    # HTTP failures must not age out the last publicly verified application's
    # assets, even if many subsequent publications cannot be verified.
    if [[ -f "$state/last-verified.txt" ]]; then cat -- "$state/last-verified.txt" >> "$retained"; fi
    expired=()
    now=$(date +%s)
    index=0
    for inventory in "${histories[@]}"; do
      if (( index < keep || now - $(stat -c %Y -- "$inventory") < days * 86400 )); then
        cat -- "$inventory" >> "$retained"
      else
        expired+=("$inventory")
      fi
      index=$((index + 1))
    done
    # Failed deployments are protected for the grace period, then reclaimed.
    pending_tmp=$(mktemp "$state/pending.XXXXXX")
    find "$state/pending" -maxdepth 1 -type f -name '*.txt' -print0 > "$pending_tmp"
    while IFS= read -r -d '' inventory; do
      if (( now - $(stat -c %Y -- "$inventory") < days * 86400 )); then
        cat -- "$inventory" >> "$retained"
      else
        expired+=("$inventory")
      fi
    done < "$pending_tmp"
    rm -f -- "$pending_tmp"
    deleted=0
    for inventory in "${expired[@]}"; do
      while read -r hash path; do
        [[ "$hash" =~ ^[a-f0-9]{64}$ ]]
        safe_asset "$path"
        if ! grep -Fqx -- "$hash  $path" "$retained" && [[ -f "$path" ]]; then
          # Never delete a file subsequently changed outside this deployment.
          if [[ "$(sha256sum -- "$path" | cut -d ' ' -f 1)" == "$hash" ]]; then
            rm -- "$path"
            deleted=$((deleted + 1))
          fi
        fi
      done < "$inventory"
      rm -- "$inventory"
    done
    echo "Asset cleanup: $deleted files removed; last $keep releases and $days days protected."
    ;;
  *) echo "Unknown deployment phase: $phase" >&2; exit 1 ;;
esac
