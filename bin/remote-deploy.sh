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

fail() { echo "Hostinger $phase: $*" >&2; exit 1; }
trap 'echo "Hostinger remote phase $phase failed at line $LINENO" >&2' ERR
cleanup_trap() {
  local code=$?
  if [[ "$phase" == prepare && "$maintenance" == true && -f artisan && $code -ne 0 ]]; then
    echo 'Deployment prepare aborted; recovering from maintenance mode' >&2
    "$php_bin" artisan up 2>/dev/null || true
  fi
}
trap cleanup_trap EXIT

(( BASH_VERSINFO[0] >= 4 )) || fail 'Bash 4 or newer is required'
for tool in find sha256sum cut stat dirname basename cat mkdir printf touch mv sort mktemp date grep rm rsync awk wc; do
  command -v "$tool" >/dev/null || fail "Required server command is unavailable: $tool"
done

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
        [[ "$("$php_bin" -r 'echo PHP_MAJOR_VERSION, ".", PHP_MINOR_VERSION;')" == "$php_expected" ]] || fail "PHP CLI ($php_bin) must match configured version $php_expected"
      fi
      if [[ "$maintenance" == true && -f artisan ]]; then
        echo 'Entering maintenance mode during deployment'
        "$php_bin" artisan down --retry=15 2>/dev/null || true
      fi
    fi
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
    for dir in "${asset_dirs[@]}"; do mkdir -p -- "$dir"; done
    ;;
  optimize)
    if [[ "$profile" == laravel-vite ]]; then
      [[ -f artisan ]]
      mkdir -p storage/framework/{sessions,views,cache/data} storage/logs bootstrap/cache
      chmod -R 775 storage bootstrap/cache 2>/dev/null || true
      chmod 755 . public 2>/dev/null || true
      find public -type d -exec chmod 755 {} + 2>/dev/null || true
      find public -type f -exec chmod 644 {} + 2>/dev/null || true
      if grep -qE '^APP_KEY=\s*$' .env 2>/dev/null || ! grep -q '^APP_KEY=' .env 2>/dev/null; then
        echo 'Generating application encryption key'
        "$php_bin" artisan key:generate --force
      fi
      if [[ "$migrate" == true ]]; then
        echo 'Explicit database migration option enabled; failures are not automatically rolled back'
        "$php_bin" artisan migrate --force
      fi
      "$php_bin" artisan optimize:clear || true
      "$php_bin" artisan config:cache
      "$php_bin" artisan route:cache
      "$php_bin" artisan view:cache
      if [[ "$maintenance" == true ]]; then
        echo 'Exiting maintenance mode after deployment'
        "$php_bin" artisan up 2>/dev/null || true
      fi
    fi
    ;;
  cleanup)
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
    if [[ "$profile" == laravel-vite && "$maintenance" == true && -f artisan ]]; then
      php artisan up 2>/dev/null || true
    fi
    echo "Asset cleanup: $deleted files removed; last $keep releases and $days days protected."
    ;;
  *) echo "Unknown deployment phase: $phase" >&2; exit 1 ;;
esac
