#!/usr/bin/env bash
# Create a clean CaddieJ bundle for code review.
#
# POSIX developer tooling (macOS and Linux). It is not a runtime dependency
# and is not expected to run on Windows; use Git for Windows' bash if you
# need the archive from a Windows machine.
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
       -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -prune -o \
    -type f -print0
}

collect_packaged_files() {
  local rel path

  while IFS= read -r -d '' path; do
    rel="${path#"${REPO_ROOT}/"}"
    if should_prune_path "$rel"; then
      continue
    fi
    printf '%s\0' "$rel"
  done < <(find_packaged_files)
}

is_text_review_file() {
  local rel="$1"
  local base="${rel##*/}"
  local ext="${base##*.}"
  local ext_lower

  case "$base" in
    .env.example|.gitignore|.editorconfig|LICENSE|Makefile|Dockerfile)
      return 0
      ;;
  esac

  [[ "$base" == "$ext" ]] && return 1

  ext_lower="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"
  case "$ext_lower" in
    py|md|toml|json|yaml|yml|html|css|js|ts|sh|cfg|ini|txt)
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
  local rel abs base

  printf 'Running lightweight secret scan on files to be packaged...\n'

  while IFS= read -r -d '' rel; do
    abs="${REPO_ROOT}/${rel}"
    [[ -f "$abs" ]] || continue

    base="${rel##*/}"
    [[ "$base" == ".env.example" ]] && continue

    check_private_key_block "$abs" || return 1
    check_assignment_secret "$abs" 'OPENAI_API_KEY' '^(export[[:space:]]+)?[[:space:]]*(OPENAI_API_KEY)[[:space:]]*=[[:space:]]*(.*)$' || return 1
    check_assignment_secret "$abs" 'OBS_PASSWORD' '^(export[[:space:]]+)?[[:space:]]*(OBS_PASSWORD)[[:space:]]*=[[:space:]]*(.*)$' || return 1
    check_assignment_secret "$abs" 'AWS_SECRET_ACCESS_KEY' '^(export[[:space:]]+)?[[:space:]]*(AWS_SECRET_ACCESS_KEY)[[:space:]]*=[[:space:]]*(.*)$' || return 1
    check_assignment_secret "$abs" 'COACHING_API_KEY' '^(export[[:space:]]+)?[[:space:]]*(COACHING_API_KEY)[[:space:]]*=[[:space:]]*(.*)$' || return 1
    check_assignment_secret "$abs" 'ASSISTANT_API_TOKEN' '^(export[[:space:]]+)?[[:space:]]*(ASSISTANT_API_TOKEN)[[:space:]]*=[[:space:]]*(.*)$' || return 1
    check_assignment_secret "$abs" 'LIGHTING_APPLICATION_KEY' '^(export[[:space:]]+)?[[:space:]]*(LIGHTING_APPLICATION_KEY)[[:space:]]*=[[:space:]]*(.*)$' || return 1
  done < <(collect_packaged_files)

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

create_archive() {
  local rel

  while IFS= read -r -d '' rel; do
    (
      cd "$REPO_ROOT" || exit 1
      zip -q "$ARCHIVE_PATH" "$rel"
    )
  done < <(collect_packaged_files)
}

list_archive_entries() {
  unzip -Z1 "$ARCHIVE_PATH"
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
  local text_file_count archive_file_count text_size archive_size

  mkdir -p "${REPO_ROOT}/dist"
  rm -f "$TEXT_BUNDLE_PATH" "$ARCHIVE_PATH"

  run_security_check
  create_text_bundle
  verify_text_bundle
  create_archive
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
  printf '    Path:  %s\n' "$ARCHIVE_PATH"
  printf '    Size:  %s\n' "$(format_size "$archive_size")"
  printf '    Files: %s\n' "$archive_file_count"
}

main "$@"
