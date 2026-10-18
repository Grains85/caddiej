#!/usr/bin/env bash
# Create a clean CaddieJ bundle for code review.
#
# Developer tooling for macOS, Linux, and Git for Windows bash. It is not a
# runtime dependency. ZIP creation uses Unix zip/unzip when they are
# available, and otherwise falls back to Windows PowerShell
# (System.IO.Compression) so Git Bash users do not need extra packages.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="$(basename "$REPO_ROOT")"
TEXT_BUNDLE_NAME="CaddieJ-review.txt"
TEXT_BUNDLE_PATH="${REPO_ROOT}/dist/${TEXT_BUNDLE_NAME}"
ARCHIVE_NAME="CaddieJ-review.zip"
ARCHIVE_PATH="${REPO_ROOT}/dist/${ARCHIVE_NAME}"

should_prune_path() {
  local rel="$1"
  local base="${rel##*/}"

  # Review code and metadata, not recordings or previously generated archives.
  # Case-insensitive patterns also work with the system Bash 3.2 on macOS.
  case "$base" in
    *.[mM][pP]4|*.[mM][oO][vV]|*.[mM]4[vV]|*.[aA][vV][iI]|*.[mM][kK][vV]|*.[wW][eE][bB][mM]|*.[wW][mM][vV]|*.[mM][pP][gG]|*.[mM][pP][eE][gG]|*.[mM][tT][sS]|*.[mM]2[tT][sS])
      return 0
      ;;
    *.[zZ][iI][pP]|*.[zZ][iI][pP][xX]|*.7[zZ]|*.[rR][aA][rR]|*.[tT][aA][rR]|*.[gG][zZ]|*.[tT][gG][zZ]|*.[bB][zZ]2|*.[tT][bB][zZ]2|*.[xX][zZ]|*.[tT][xX][zZ]|*.[zZ][sS][tT]|*.[iI][sS][oO]|*.[dD][mM][gG]|*.[wW][hH][lL])
      return 0
      ;;
  esac

  case "$base" in
    .env)
      return 0
      ;;
    .env.*)
      [[ "$base" == ".env.example" ]] && return 1
      return 0
      ;;
    .DS_Store)
      return 0
      ;;
    caddiej.sqlite3|caddiej.sqlite3-wal|caddiej.sqlite3-shm)
      return 0
      ;;
  esac

  case "$rel" in
    .pytest_cache|.pytest_cache/*|*/.pytest_cache|*/.pytest_cache/*)
      return 0
      ;;
    .mypy_cache|.mypy_cache/*|*/.mypy_cache|*/.mypy_cache/*)
      return 0
      ;;
    .ruff_cache|.ruff_cache/*|*/.ruff_cache|*/.ruff_cache/*)
      return 0
      ;;
    .caddiej|.caddiej/*)
      return 0
      ;;
    __pycache__|__pycache__/*|*/__pycache__|*/__pycache__/*)
      return 0
      ;;
    *.pyc|*.pyo)
      return 0
      ;;
    *.sqlite|*.sqlite3|*.sqlite3-wal|*.sqlite3-shm)
      return 0
      ;;
    *.egg-info|*.egg-info/*)
      return 0
      ;;
  esac

  return 1
}

find_packaged_files() {
  find "$REPO_ROOT" -mindepth 1 \
    \( -name .git -o -name .venv -o -name media -o -name dist -o -name build \
       -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \
       -o -name .caddiej -o -name __pycache__ -o -name "*.egg-info" \
       -o -path "$REPO_ROOT/research/temporal_evidence/_deps" \) -prune -o \
    -type f -print0
}

collect_packaged_files() {
  # main freezes this manifest once, so every stage uses the same selection.
  if [[ -n "${PACKAGED_MANIFEST:-}" ]]; then
    cat "$PACKAGED_MANIFEST"
    return
  fi
  local rel path candidates
  candidates="$(mktemp)" || return 1
  # Do not use process substitution here: it hides a failed find command.
  if ! find_packaged_files > "$candidates"; then
    rm -f "$candidates"
    return 1
  fi
  while IFS= read -r -d '' path; do
    rel="${path#"${REPO_ROOT}/"}"
    if should_prune_path "$rel"; then
      continue
    fi
    if ! printf '%s\0' "$rel"; then
      rm -f "$candidates"
      return 1
    fi
  done < "$candidates"
  rm -f "$candidates"
}

is_text_review_file() {
  local rel="$1"
  local base="${rel##*/}"
  local ext="${base##*.}"

  case "$base" in
    .env.example|.gitignore|.editorconfig|LICENSE|Makefile|Dockerfile)
      return 0
      ;;
  esac

  [[ "$base" == "$ext" ]] && return 1

  # Bash 3.2 compatible, without spawning tr for every file.
  case "$ext" in
    [pP][yY]|[mM][dD]|[tT][oO][mM][lL]|[jJ][sS][oO][nN]|[yY][aA][mM][lL]|[yY][mM][lL]|[hH][tT][mM][lL]|[cC][sS][sS]|[jJ][sS]|[tT][sS]|[sS][hH]|[cC][fF][gG]|[iI][nN][iI]|[tT][xX][tT])
      return 0
      ;;
  esac

  return 1
}

is_binary_file() {
  local file="$1"
  local description

  description="$(file -b "$file")"
  [[ "$description" == *"text"* ]] && return 1
  [[ "$description" == "empty" ]] && return 1
  return 0
}

collect_text_review_files() {
  local rel abs

  while IFS= read -r -d '' rel; do
    is_text_review_file "$rel" || continue
    abs="${REPO_ROOT}/${rel}"
    is_binary_file "$abs" && continue
    printf '%s\0' "$rel"
  done < <(collect_packaged_files)
}

looks_like_placeholder_value() {
  local value="$1"
  local lower

  [[ -z "$value" ]] && return 0

  if [[ "$value" == *"..."* ]]; then
    return 0
  fi

  if [[ "$value" == the-key-from-* ]]; then
    return 0
  fi

  lower="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  case "$lower" in
    changeme|change-me|placeholder|example|your-key-here|your_key_here|insert-key-here|insert_key_here|xxx|xxxxx|redacted|none|n/a|na|todo|tbd|secret|do-not-log)
      return 0
      ;;
  esac

  if [[ "$value" == \<* ]] || [[ "$value" == *"your-"* ]] || [[ "$value" == *"YOUR_"* ]]; then
    return 0
  fi

  return 1
}

check_assignment_secret() {
  local file="$1"
  local pattern_name="$2"
  local regex="$3"
  local line value

  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ,[[:space:]]*$ ]] && continue
    [[ "$line" =~ $regex ]] || continue

    value="${BASH_REMATCH[3]}"
    value="${value%%#*}"
    value="${value%"${value##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%,}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"

    if looks_like_placeholder_value "$value"; then
      continue
    fi

    printf 'ERROR: Possible secret detected (%s) in %s\n' "$pattern_name" "$file" >&2
    return 1
  done < "$file"
}

check_private_key_block() {
  local file="$1"

  if grep -Eq 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY' "$file"; then
    printf 'ERROR: Possible secret detected (PRIVATE KEY block) in %s\n' "$file" >&2
    return 1
  fi

  return 0
}

run_security_check() {
  local rel scan_list key_matches
  scan_list="$REVIEW_TEMP_DIR/scan.nul"
  key_matches="$REVIEW_TEMP_DIR/private-key-paths.txt"
  printf 'Running lightweight secret scan on files to be packaged...\n'

  # One awk pass over each file; xargs batches paths within the OS argument
  # limit. Prefix ./ so leading '-' and '=' in filenames stay literal paths.
  # Only paths/pattern names are printed, never matching secret values.
  if ! (
    cd "$REPO_ROOT" || exit 1
    while IFS= read -r -d '' rel; do
      [[ "${rel##*/}" == ".env.example" ]] && continue
      printf './%s\0' "$rel"
    done < <(collect_packaged_files) > "$scan_list"
    # grep -a also finds key blocks after NUL bytes in binary assets. Some
    # system awk implementations cannot match those bytes reliably.
    if ! LC_ALL=C xargs -0 sh -c '
      grep -alE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" "$@"
      result=$?
      [ "$result" -le 1 ]
    ' sh < "$scan_list" > "$key_matches"; then
      printf 'ERROR: Could not complete private-key scan.\n' >&2
      exit 1
    fi
    if [[ -s "$key_matches" ]]; then
      while IFS= read -r rel; do
        printf 'ERROR: Possible secret detected (PRIVATE KEY block) in %s\n' "$rel" >&2
      done < "$key_matches"
      exit 1
    fi
    # Scan arbitrary binary assets as bytes, not as locale-dependent UTF-8.
    LC_ALL=C xargs -0 awk '
        function placeholder(value, lower) {
          if (value == "" || index(value, "...") ||
              value ~ /^the-key-from-/ || value ~ /^</ ||
              index(value, "your-") || index(value, "YOUR_")) return 1
          lower = tolower(value)
          return lower ~ /^(changeme|change-me|placeholder|example|your-key-here|your_key_here|insert-key-here|insert_key_here|xxx|xxxxx|redacted|none|n\/a|na|todo|tbd|secret|do-not-log)$/
        }
        function report(pattern) {
          printf "ERROR: Possible secret detected (%s) in %s\n", pattern, FILENAME > "/dev/stderr"
          bad = 1
        }
        FILENAME ~ /(^|\/)\.env\.example$/ { next }
        /^[[:space:]]*#/ { next }
        # Preserve the original scan scope for Python keyword arguments and
        # tuple entries; these are not dotenv-style assignments.
        FILENAME ~ /\.py$/ && /,[[:space:]]*$/ { next }
        {
          line = $0
          sub(/^(export[[:space:]]+)?[[:space:]]*/, "", line)
          if (line !~ /^(OPENAI_API_KEY|OBS_PASSWORD|AWS_SECRET_ACCESS_KEY|COACHING_API_KEY|ASSISTANT_API_TOKEN|LIGHTING_APPLICATION_KEY)[[:space:]]*=/) next
          name = line
          sub(/[[:space:]]*=.*$/, "", name)
          sub(/^[^=]*=[[:space:]]*/, "", line)
          sub(/#.*/, "", line)
          sub(/[[:space:]]+$/, "", line)
          sub(/^[[:space:]]+/, "", line)
          sub(/,$/, "", line)
          sub(/"$/, "", line)
          sub(/^"/, "", line)
          # Single quotes are represented with an octal escape for bash.
          sub(/\047$/, "", line)
          sub(/^\047/, "", line)
          if (!placeholder(line)) report(name)
        }
        END { exit bad ? 1 : 0 }
      ' < "$scan_list"
  ); then
    return 1
  fi
  printf 'Secret scan passed.\n'
}

write_file_section() {
  local rel="$1"
  local abs="${REPO_ROOT}/${rel}"

  printf '========================================================================\n'
  printf 'FILE: %s\n' "$rel"
  printf '========================================================================\n'
  printf '\n'
  cat "$abs"
  printf '\n'
}

create_text_bundle() {
  local rel sorted_files file_count

  sorted_files="$(mktemp)"
  collect_text_review_files | tr '\0' '\n' | LC_ALL=C sort > "$sorted_files"
  file_count="$(wc -l < "$sorted_files" | tr -d ' ')"

  {
    printf '%s review bundle\n' "$PROJECT_NAME"
    printf 'Generated: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    printf 'Files: %s\n' "$file_count"
    printf 'Note: Secrets, databases, media, and virtualenv directories are excluded.\n'
    printf '\n'

    while IFS= read -r rel; do
      [[ -n "$rel" ]] || continue
      write_file_section "$rel"
    done < "$sorted_files"
  } > "$TEXT_BUNDLE_PATH"

  rm -f "$sorted_files"
}

ZIP_BACKEND=""
UNZIP_BACKEND=""
POWERSHELL_CMD=""

is_windows_bash() {
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      return 0
      ;;
  esac
  return 1
}

to_mixed_path() {
  local path="$1"

  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$path"
    return
  fi

  printf '%s' "$path"
}

resolve_powershell() {
  local candidate

  [[ -n "$POWERSHELL_CMD" ]] && return 0

  if command -v powershell.exe >/dev/null 2>&1; then
    POWERSHELL_CMD="$(command -v powershell.exe)"
    return 0
  fi

  if command -v powershell >/dev/null 2>&1; then
    POWERSHELL_CMD="$(command -v powershell)"
    return 0
  fi

  for candidate in \
    "/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe" \
    "/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe"; do
    if [[ -x "$candidate" ]]; then
      POWERSHELL_CMD="$candidate"
      return 0
    fi
  done

  return 1
}

run_powershell_file() {
  local script_path="$1"
  local win_script
  win_script="$(to_mixed_path "$script_path")"

  # Git Bash may rewrite arguments that look like Windows paths. Disable
  # that conversion so mixed-style paths reach PowerShell unchanged.
  # Pass a file rather than stdin: MSYS pipes to powershell.exe silently
  # drop larger scripts, which looks like a successful no-op.
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
    "$POWERSHELL_CMD" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$win_script"
}

write_powershell_script() {
  local dest="$1"
  cat > "$dest"
}

make_powershell_temp() {
  local tmp
  tmp="$(mktemp)"
  rm -f "$tmp"
  printf '%s.ps1' "$tmp"
}

select_zip_backend() {
  if [[ "${CADDIEJ_ZIP_BACKEND:-}" == "zip" ]]; then
    if command -v zip >/dev/null 2>&1; then
      ZIP_BACKEND="zip"
      return
    fi
    ZIP_BACKEND="missing"
    return
  fi

  if [[ "${CADDIEJ_ZIP_BACKEND:-}" == "powershell" ]]; then
    if is_windows_bash && resolve_powershell; then
      ZIP_BACKEND="powershell"
      return
    fi
    ZIP_BACKEND="missing"
    return
  fi

  if command -v zip >/dev/null 2>&1; then
    ZIP_BACKEND="zip"
    return
  fi

  if is_windows_bash && resolve_powershell; then
    ZIP_BACKEND="powershell"
    return
  fi

  ZIP_BACKEND="missing"
}

select_unzip_backend() {
  if [[ "${CADDIEJ_UNZIP_BACKEND:-}" == "unzip" ]]; then
    if command -v unzip >/dev/null 2>&1; then
      UNZIP_BACKEND="unzip"
      return
    fi
    UNZIP_BACKEND="missing"
    return
  fi

  if [[ "${CADDIEJ_UNZIP_BACKEND:-}" == "powershell" ]]; then
    if is_windows_bash && resolve_powershell; then
      UNZIP_BACKEND="powershell"
      return
    fi
    UNZIP_BACKEND="missing"
    return
  fi

  if command -v unzip >/dev/null 2>&1; then
    UNZIP_BACKEND="unzip"
    return
  fi

  if is_windows_bash && resolve_powershell; then
    UNZIP_BACKEND="powershell"
    return
  fi

  UNZIP_BACKEND="missing"
}

ensure_archive_backends() {
  select_zip_backend
  select_unzip_backend

  if [[ "$ZIP_BACKEND" == "missing" ]]; then
    abort_packaging 'neither zip nor Windows PowerShell is available to create the archive'
  fi

  if [[ "$UNZIP_BACKEND" == "missing" ]]; then
    abort_packaging 'neither unzip nor Windows PowerShell is available to verify the archive'
  fi

  printf 'ZIP create backend: %s\n' "$ZIP_BACKEND"
  printf 'ZIP list backend: %s\n' "$UNZIP_BACKEND"
}

create_archive_zip() {
  # A single zip process avoids repeatedly reopening and rewriting the archive.
  # main rejects newline-containing paths, as both ZIP backends use line lists.
  (
    cd "$REPO_ROOT" || exit 1
    collect_packaged_files | tr '\0' '\n' | zip -q -1 "$ARCHIVE_PATH" -@
  )
}

create_archive_powershell() {
  local list_file ps1

  # Compress-Archive flattens mixed-directory files into the zip root and
  # would package whole trees if pointed at folders, so collect_packaged_files
  # exclusions would not stay authoritative. ZipFile writes each selected
  # relative path as the archive entry name.
  list_file="$(mktemp)"
  ps1="$(make_powershell_temp)"
  collect_packaged_files | tr '\0' '\n' | sed '/^$/d' > "$list_file"

  export CADDIEJ_PS_REPO_ROOT
  export CADDIEJ_PS_ARCHIVE_PATH
  export CADDIEJ_PS_LIST_FILE
  CADDIEJ_PS_REPO_ROOT="$(to_mixed_path "$REPO_ROOT")"
  CADDIEJ_PS_ARCHIVE_PATH="$(to_mixed_path "$ARCHIVE_PATH")"
  CADDIEJ_PS_LIST_FILE="$(to_mixed_path "$list_file")"

  write_powershell_script "$ps1" <<'EOF'
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
  Add-Type -AssemblyName System.IO.Compression
  Add-Type -AssemblyName System.IO.Compression.FileSystem

  $repoRoot = $env:CADDIEJ_PS_REPO_ROOT
  $archivePath = $env:CADDIEJ_PS_ARCHIVE_PATH
  $listFile = $env:CADDIEJ_PS_LIST_FILE

  if ([string]::IsNullOrWhiteSpace($repoRoot) -or
      [string]::IsNullOrWhiteSpace($archivePath) -or
      [string]::IsNullOrWhiteSpace($listFile)) {
    throw 'PowerShell ZIP helper is missing required path environment variables'
  }

  if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
  }

  $zip = [System.IO.Compression.ZipFile]::Open(
    $archivePath,
    [System.IO.Compression.ZipArchiveMode]::Create
  )
  try {
    Get-Content -LiteralPath $listFile | ForEach-Object {
      $relUnix = ($_ -replace '\\', '/')
      if ($relUnix -eq '') {
        return
      }

      $full = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::Combine($repoRoot, ($relUnix -replace '/', '\'))
      )
      if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw "Packaged file missing: $relUnix"
      }

      [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $zip,
        $full,
        $relUnix,
        [System.IO.Compression.CompressionLevel]::Fastest
      )
    }
  } finally {
    $zip.Dispose()
  }
} catch {
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 1
}
EOF

  if ! run_powershell_file "$ps1"; then
    rm -f "$list_file" "$ps1"
    abort_packaging 'PowerShell failed to create ZIP archive'
  fi

  rm -f "$list_file" "$ps1"
  unset CADDIEJ_PS_REPO_ROOT CADDIEJ_PS_ARCHIVE_PATH CADDIEJ_PS_LIST_FILE

  if [[ ! -f "$ARCHIVE_PATH" ]]; then
    abort_packaging 'PowerShell reported success but did not create the ZIP archive'
  fi
}

create_archive() {
  case "$ZIP_BACKEND" in
    zip)
      create_archive_zip
      ;;
    powershell)
      create_archive_powershell
      ;;
    *)
      abort_packaging 'neither zip nor Windows PowerShell is available to create the archive'
      ;;
  esac
}

list_archive_entries_unzip() {
  unzip -Z1 "$ARCHIVE_PATH"
}

list_archive_entries_powershell() {
  local out_file ps1

  out_file="$(mktemp)"
  ps1="$(make_powershell_temp)"
  export CADDIEJ_PS_ARCHIVE_PATH
  export CADDIEJ_PS_OUT_FILE
  CADDIEJ_PS_ARCHIVE_PATH="$(to_mixed_path "$ARCHIVE_PATH")"
  CADDIEJ_PS_OUT_FILE="$(to_mixed_path "$out_file")"

  write_powershell_script "$ps1" <<'EOF'
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
  Add-Type -AssemblyName System.IO.Compression.FileSystem

  $archivePath = $env:CADDIEJ_PS_ARCHIVE_PATH
  $outFile = $env:CADDIEJ_PS_OUT_FILE

  if ([string]::IsNullOrWhiteSpace($archivePath) -or
      [string]::IsNullOrWhiteSpace($outFile)) {
    throw 'PowerShell ZIP helper is missing required path environment variables'
  }

  $zip = [System.IO.Compression.ZipFile]::OpenRead($archivePath)
  try {
    $names = New-Object System.Collections.Generic.List[string]
    foreach ($entry in $zip.Entries) {
      $name = $entry.FullName.Replace('\', '/')
      if ($name.EndsWith('/')) {
        continue
      }
      $names.Add($name)
    }
  } finally {
    $zip.Dispose()
  }

  $utf8NoBom = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllLines($outFile, $names.ToArray(), $utf8NoBom)
} catch {
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 1
}
EOF

  if ! run_powershell_file "$ps1"; then
    rm -f "$out_file" "$ps1"
    abort_packaging 'PowerShell failed to list ZIP archive'
  fi

  cat "$out_file"
  rm -f "$out_file" "$ps1"
  unset CADDIEJ_PS_ARCHIVE_PATH CADDIEJ_PS_OUT_FILE
}

list_archive_entries() {
  case "$UNZIP_BACKEND" in
    unzip)
      list_archive_entries_unzip
      ;;
    powershell)
      list_archive_entries_powershell
      ;;
    *)
      abort_packaging 'neither unzip nor Windows PowerShell is available to verify the archive'
      ;;
  esac
}

format_size() {
  local bytes="$1"

  if (( bytes >= 1048576 )); then
    printf '%.1f MiB' "$(awk "BEGIN { printf \"%.1f\", ${bytes} / 1048576 }")"
  elif (( bytes >= 1024 )); then
    printf '%.1f KiB' "$(awk "BEGIN { printf \"%.1f\", ${bytes} / 1024 }")"
  else
    printf '%s bytes' "$bytes"
  fi
}

abort_packaging() {
  local reason="$1"

  rm -f "$TEXT_BUNDLE_PATH" "$ARCHIVE_PATH"
  printf 'ERROR: %s\n' "$reason" >&2
  printf 'Generated review outputs were deleted.\n' >&2
  exit 1
}

verify_text_bundle() {
  local listing file_count

  printf 'Verifying text bundle contents...\n'

  listing="$(mktemp)"
  grep '^FILE: ' "$TEXT_BUNDLE_PATH" | sed 's/^FILE: //' > "$listing" || true

  grep -q '^src/' "$listing" || abort_packaging 'text bundle missing src/'
  grep -q '^tests/' "$listing" || abort_packaging 'text bundle missing tests/'
  grep -qx 'pyproject.toml' "$listing" || abort_packaging 'text bundle missing pyproject.toml'
  grep -qx 'ARCHITECTURE.md' "$listing" || abort_packaging 'text bundle missing ARCHITECTURE.md'
  grep -qx 'ROADMAP.md' "$listing" || abort_packaging 'text bundle missing ROADMAP.md'

  if grep -q '^\.git/' "$listing"; then
    rm -f "$listing"
    abort_packaging 'text bundle contains .git/'
  fi

  if grep -q '^\.venv/' "$listing"; then
    rm -f "$listing"
    abort_packaging 'text bundle contains .venv/'
  fi

  while IFS= read -r entry; do
    local base="${entry##*/}"
    if [[ "$base" == ".env" ]]; then
      rm -f "$listing"
      abort_packaging "text bundle contains env file: ${entry}"
    fi
    if [[ "$base" == .env.* && "$base" != ".env.example" ]]; then
      rm -f "$listing"
      abort_packaging "text bundle contains env file: ${entry}"
    fi
  done < "$listing"

  file_count="$(wc -l < "$listing" | tr -d ' ')"
  if [[ "$file_count" -eq 0 ]]; then
    rm -f "$listing"
    abort_packaging 'text bundle contains no files'
  fi

  rm -f "$listing"
  printf 'Text bundle verification passed.\n'
}

verify_archive() {
  local listing entry base

  printf 'Verifying archive contents...\n'

  listing="$(mktemp)"
  list_archive_entries > "$listing"

  grep -q '^src/' "$listing" || abort_packaging 'archive missing src/'
  grep -q '^tests/' "$listing" || abort_packaging 'archive missing tests/'
  grep -qx 'pyproject.toml' "$listing" || abort_packaging 'archive missing pyproject.toml'
  grep -qx 'ARCHITECTURE.md' "$listing" || abort_packaging 'archive missing ARCHITECTURE.md'

  if grep -q '^\.git/' "$listing"; then
    rm -f "$listing"
    abort_packaging 'archive contains .git/'
  fi

  if grep -q '^\.venv/' "$listing"; then
    rm -f "$listing"
    abort_packaging 'archive contains .venv/'
  fi

  if grep -q '^media/' "$listing"; then
    rm -f "$listing"
    abort_packaging 'archive contains media/'
  fi

  if grep -q '^dist/' "$listing"; then
    rm -f "$listing"
    abort_packaging 'archive contains dist/'
  fi

  while IFS= read -r entry; do
    base="${entry##*/}"
    if [[ "$base" == ".env" ]]; then
      rm -f "$listing"
      abort_packaging "archive contains env file: ${entry}"
    fi
    if [[ "$base" == .env.* && "$base" != ".env.example" ]]; then
      rm -f "$listing"
      abort_packaging "archive contains env file: ${entry}"
    fi
  done < "$listing"

  if grep -Eq '\.(sqlite3?|sqlite3-wal|sqlite3-shm)$' "$listing"; then
    rm -f "$listing"
    abort_packaging 'archive contains SQLite database files'
  fi

  rm -f "$listing"
  printf 'Archive verification passed.\n'
}

count_text_bundle_files() {
  grep -c '^FILE: ' "$TEXT_BUNDLE_PATH"
}

main() {
  local text_file_count archive_file_count text_size archive_size rel

  # Keep temporary manifests outside the source tree and clean them on failure.
  REVIEW_TEMP_DIR="$(mktemp -d)"
  trap 'rm -rf -- "$REVIEW_TEMP_DIR"' EXIT
  if ! collect_packaged_files > "$REVIEW_TEMP_DIR/packaged.nul"; then
    printf 'ERROR: File collection failed; packaging stopped.\n' >&2
    exit 1
  fi
  if [[ ! -s "$REVIEW_TEMP_DIR/packaged.nul" ]]; then
    printf 'ERROR: No files selected; packaging stopped.\n' >&2
    exit 1
  fi
  PACKAGED_MANIFEST="$REVIEW_TEMP_DIR/packaged.nul"
  while IFS= read -r -d '' rel; do
    if [[ "$rel" == *$'\n'* || "$rel" == *$'\r'* ]]; then
      printf 'ERROR: Review bundles cannot represent filenames containing line breaks.\n' >&2
      exit 1
    fi
  done < "$PACKAGED_MANIFEST"

  mkdir -p "${REPO_ROOT}/dist"
  rm -f "$TEXT_BUNDLE_PATH" "$ARCHIVE_PATH"

  ensure_archive_backends
  run_security_check || abort_packaging 'secret scan failed'
  create_text_bundle
  verify_text_bundle
  create_archive || abort_packaging 'archive creation failed'
  verify_archive

  text_file_count="$(count_text_bundle_files)"
  text_size="$(wc -c < "$TEXT_BUNDLE_PATH" | tr -d ' ')"
  archive_file_count="$(list_archive_entries | wc -l | tr -d ' ')"
  archive_size="$(wc -c < "$ARCHIVE_PATH" | tr -d ' ')"

  printf '\nReview bundle created successfully.\n'
  printf '  Text bundle (primary):\n'
  printf '    Path:  %s\n' "$TEXT_BUNDLE_PATH"
  printf '    Size:  %s\n' "$(format_size "$text_size")"
  printf '    Files: %s\n' "$text_file_count"
  printf '  ZIP archive (supplementary):\n'
  printf '    Path:    %s\n' "$ARCHIVE_PATH"
  printf '    Size:    %s\n' "$(format_size "$archive_size")"
  printf '    Files:   %s\n' "$archive_file_count"
  printf '    Create:  %s\n' "$ZIP_BACKEND"
  printf '    List:    %s\n' "$UNZIP_BACKEND"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
