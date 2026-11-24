#!/bin/sh
# locmodel-image . install from repository (Linux / macOS)
#
# Deploys two things, straight from the GitHub repo, with no exe and no pip:
#
#   MCP    scripts/*.py  ->  ~/.local/share/locmodel-image/repo/scripts
#   Skill  SKILL.md      ->  <host skills dir>/locmodel-image/SKILL.md
#
# Every file is fetched at the pinned ref and verified against install/manifest.json
# (SHA-256). Host config files are only touched when --register is given, and a
# .bak-<timestamp> copy is kept first.
#
# This is additive: an exe install under ~/.local/share/locmodel-image (or
# %LOCALAPPDATA%) is never modified.
#
# Usage:
#   sh install-from-repo.sh                        # mcp + skill, auto-detect hosts
#   sh install-from-repo.sh --host claude --register claude
#   sh install-from-repo.sh --component skill --host all
#   sh install-from-repo.sh --source local --from-path ./locmodel-image-repo-install-v4.0.0
#   sh install-from-repo.sh --dry-run
#
# Exit codes: 0 ok, 1 usage, 2 download/verify failure, 3 deploy failure.

set -eu

COMPONENT="both"
HOSTSEL="auto"
REF=""
INSTALL_ROOT=""
SOURCE="manifest"
FROM_PATH=""
REGISTER=""
DRY_RUN=0
FORCE=0
CREATE_HOST_DIRS=0
LOCAL_ROOT=""
HOSTS_JSON=""

die()  { printf '    ERR %s\n' "$1" >&2; exit "${2:-1}"; }
info() { printf '    %s\n' "$1"; }
ok()   { printf '    OK  %s\n' "$1"; }
warn() { printf '    !!  %s\n' "$1"; }
step() { STEPNO=$((STEPNO + 1)); printf '[%s] %s\n' "$STEPNO" "$1"; }
STEPNO=0

while [ $# -gt 0 ]; do
  case "$1" in
    --component) COMPONENT="$2"; shift 2 ;;
    --host)      HOSTSEL="$2"; shift 2 ;;
    --ref)       REF="$2"; shift 2 ;;
    --install-root) INSTALL_ROOT="$2"; shift 2 ;;
    --source)    SOURCE="$2"; shift 2 ;;
    --from-path) FROM_PATH="$2"; shift 2 ;;
    --register)  REGISTER="$2"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --force)     FORCE=1; shift ;;
    --create-host-dirs) CREATE_HOST_DIRS=1; shift ;;
    -h|--help)   sed -n '2,30p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" 1 ;;
  esac
done

case "$COMPONENT" in mcp|skill|both) ;; *) die "--component must be mcp|skill|both" 1 ;; esac
case "$SOURCE" in manifest|zip|local) ;; *) die "--source must be manifest|zip|local" 1 ;; esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

command -v python3 >/dev/null 2>&1 || die "python3 is required (the MCP server itself needs it)"
PY=python3

fetch() { # fetch <url> <dest>
  mkdir -p "$(dirname "$2")"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 2 -A "locmodel-image-repo-install" -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    die "neither curl nor wget is available" 2
  fi
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else $PY -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  fi
}

json_get() { # json_get <file> <python-expr on m>
  $PY - "$1" "$2" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
m = data
print(eval(sys.argv[2]))
PYEOF
}

printf '\nlocmodel-image  .  install from repository (posix)\n'
printf 'MCP server + companion Skill, standard-library Python, no pip, no exe\n\n'

# ------------------------------------------------------------------ 1. manifest
step 'Load install manifest'
if [ "$SOURCE" = "local" ]; then
  [ -n "$FROM_PATH" ] || die "--source local needs --from-path <repo root or unzipped release>" 1
  LOCAL_ROOT=$(CDPATH= cd -- "$FROM_PATH" && pwd)
  [ -f "$LOCAL_ROOT/install/manifest.json" ] || {
    if [ -f "$LOCAL_ROOT/manifest.json" ]; then LOCAL_ROOT=$(dirname "$LOCAL_ROOT"); else die "manifest.json not found under $LOCAL_ROOT" 1; fi
  }
  MANIFEST="$LOCAL_ROOT/install/manifest.json"
  ok "local source: $LOCAL_ROOT"
elif [ -f "$SCRIPT_DIR/manifest.json" ]; then
  MANIFEST="$SCRIPT_DIR/manifest.json"
  ok "manifest bundled with script"
else
  R="${REF:-v4.0.0}"
  MANIFEST="${TMPDIR:-/tmp}/locmodel-image-manifest.json"
  fetch "https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/$R/install/manifest.json" "$MANIFEST"
  ok "manifest fetched from $R"
fi

VERSION=$(json_get "$MANIFEST" 'm["version"]')
RAW_BASE=$(json_get "$MANIFEST" 'm["raw_base"]')
ASSET_ZIP=$(json_get "$MANIFEST" 'm["asset_zip"]')
if [ -n "$REF" ]; then RAW_BASE="https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/$REF"; fi
info "version $VERSION"
info "raw     $RAW_BASE"

HOSTS_JSON="$SCRIPT_DIR/hosts.json"
[ -f "$HOSTS_JSON" ] || HOSTS_JSON="$LOCAL_ROOT/install/hosts.json"

READY=$($PY - "$MANIFEST" "$COMPONENT" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
comp = sys.argv[2]
need = {"mcp": {"mcp", "meta"}, "skill": {"skill"}, "both": {"mcp", "meta", "skill"}}[comp]
for f in m["files"]:
    if f["role"] in need:
        print("%s\t%s\t%s\t%s" % (f["path"], f["sha256"], f["bytes"], 1 if f["required"] else 0))
PYEOF
)
info "payload: $(printf '%s\n' "$READY" | wc -l | tr -d ' ') files"

# ------------------------------------------------------------------ 2. targets
step 'Resolve target directories'
if [ -z "$INSTALL_ROOT" ]; then
  INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/locmodel-image/repo"
fi
info "mcp    -> $INSTALL_ROOT"

host_dir_for() { # host_dir_for <hostname>
  case "$1" in
    claude)    printf '%s\n' "$HOME/.claude/skills/locmodel-image" ;;
    marvis)    printf '%s\n' "$HOME/.marvis/skills/custom/locmodel-image" ;;
    workbuddy) printf '%s\n' "$HOME/.workbuddy/skills/locmodel-image" ;;
    cursor)    printf '%s\n' "$HOME/.cursor/skills/locmodel-image" ;;
    vscode)    printf '%s\n' "$HOME/.vscode/skills/locmodel-image" ;;
    generic)   printf '%s\n' "$HOME/.agent-skills/locmodel-image" ;;
    *)         printf '\n' ;;
  esac
}

if [ "$HOSTSEL" = "auto" ]; then
  WANTED=""
  for h in claude marvis workbuddy cursor vscode; do
    d=$(host_dir_for "$h")
    if [ -n "$d" ] && [ -d "$(dirname "$d")" ]; then WANTED="$WANTED $h"; fi
  done
  if [ -z "$WANTED" ]; then WANTED=" generic"; fi
elif [ "$HOSTSEL" = "all" ]; then
  WANTED=" claude marvis workbuddy cursor vscode generic"
else
  WANTED=" $HOSTSEL"
fi
for h in $WANTED; do info "skill  -> $h : $(host_dir_for "$h")"; done

if [ "$DRY_RUN" -eq 1 ]; then
  warn 'dry run: nothing will be downloaded or written'
  printf '%s\n' "$READY" | while IFS="$(printf '\t')" read -r p h b r; do
    printf '    would fetch %s  (%s B, sha256 %s...)\n' "$p" "$b" "$(printf '%s' "$h" | cut -c1-12)"
  done
  exit 0
fi

# ------------------------------------------------------------------ 3. fetch + verify
step "Fetch payload (source: $SOURCE)"
STAGE="${TMPDIR:-/tmp}/locmodel-image-stage-$$"
mkdir -p "$STAGE"
trap 'rm -rf "$STAGE"' EXIT INT TERM
info "staging in $STAGE"

if [ "$SOURCE" = "zip" ]; then
  ZIP="$STAGE/payload.zip"
  info "downloading $ASSET_ZIP"
  fetch "$ASSET_ZIP" "$ZIP"
  mkdir -p "$STAGE/unz"
  if command -v unzip >/dev/null 2>&1; then unzip -q "$ZIP" -d "$STAGE/unz"
  else $PY -c 'import sys,zipfile;zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$ZIP" "$STAGE/unz"
  fi
  LOCAL_ROOT="$STAGE/unz"
  ok "unpacked $(wc -c <"$ZIP" | tr -d ' ') bytes"
fi

FAILED_REQUIRED=""
printf '%s\n' "$READY" | while IFS="$(printf '\t')" read -r p h b req; do
  if [ -z "$p" ]; then continue; fi
  dest="$STAGE/$p"
  mkdir -p "$(dirname "$dest")"
  if [ "$SOURCE" = "local" ] || [ "$SOURCE" = "zip" ]; then
    cp "$LOCAL_ROOT/$p" "$dest" || { warn "$p: missing in source tree"; echo "$p" >> "$STAGE/failed"; continue; }
  else
    fetch "$RAW_BASE/$p" "$dest" || { warn "$p: download failed"; echo "$p" >> "$STAGE/failed"; continue; }
  fi
  got=$(sha256_of "$dest")
  if [ "$got" != "$h" ]; then
    if [ "$FORCE" -eq 1 ]; then warn "$p: sha256 mismatch (ignored, --force)"
    else warn "$p: sha256 mismatch"; echo "$p" >> "$STAGE/failed"; rm -f "$dest"; continue; fi
  fi
  ok "$p"
done

if [ -f "$STAGE/failed" ]; then
  HARD=""
  while read -r bad; do
    if printf '%s\n' "$READY" | awk -F'\t' -v p="$bad" '$1==p && $4=="1"{f=1} END{exit !f}'; then HARD="$HARD $bad"; fi
  done < "$STAGE/failed"
  if [ -n "$HARD" ]; then die "required files failed:$HARD" 2; fi
fi

# ------------------------------------------------------------------ 4. deploy
step 'Deploy'
if [ "$COMPONENT" != "skill" ]; then
  mkdir -p "$INSTALL_ROOT"
  ( cd "$STAGE" && tar cf - scripts pyproject.toml LICENSE 2>/dev/null ) | ( cd "$INSTALL_ROOT" && tar xf - ) || die "copy failed" 3
  chmod +x "$INSTALL_ROOT/scripts/locmodel_mcp.py" "$INSTALL_ROOT/scripts/locmodel_image.py" 2>/dev/null || true
  ok "MCP runtime -> $INSTALL_ROOT"
fi
if [ "$COMPONENT" != "mcp" ]; then
  for h in $WANTED; do
    d=$(host_dir_for "$h")
    [ -n "$d" ] || { warn "unknown host $h - skipped"; continue; }
    if [ ! -d "$(dirname "$d")" ] && [ "$CREATE_HOST_DIRS" -eq 0 ]; then
      warn "$h : parent dir does not exist, skipped (pass --create-host-dirs to force)"; continue
    fi
    mkdir -p "$d"
    cp "$STAGE/SKILL.md" "$d/SKILL.md"
    ok "skill -> $d"
  done
fi

# ------------------------------------------------------------------ 5. report
step 'Write install-manifest.json'
mkdir -p "$INSTALL_ROOT"
cat > "$INSTALL_ROOT/install-manifest.json" <<EOF
{
  "schema": "locmodel-image.repo-install-report/1",
  "version": "$VERSION",
  "source": "$SOURCE",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "mcp_root": "$INSTALL_ROOT",
  "python": "$(command -v "$PY")",
  "entrypoint": "$INSTALL_ROOT/scripts/locmodel_mcp.py",
  "hosts": "$(printf '%s' "$WANTED" | sed 's/^ //')"
}
EOF
ok "$INSTALL_ROOT/install-manifest.json"

# ------------------------------------------------------------------ 6. registration
step 'Host registration'
craft_mcp_json() {
  cat <<EOF
{
  "mcpServers": {
    "locmodel-image": {
      "command": "$(command -v "$PY")",
      "args": ["$INSTALL_ROOT/scripts/locmodel_mcp.py"],
      "env": { "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1" }
    }
  }
}
EOF
}

cfg_for() {
  case "$1" in
    claude) printf '%s\n' "$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
    cursor) printf '%s\n' "$HOME/.cursor/mcp.json" ;;
    vscode) printf '%s\n' "$HOME/.config/Code/User/mcp.json" ;;
    *) printf '\n' ;;
  esac
}

if [ -n "$REGISTER" ]; then
  for h in $(echo "$REGISTER" | tr ',' ' '); do
    if [ "$h" = "all" ]; then h="claude cursor vscode"; fi
    for one in $h; do
      cfg=$(cfg_for "$one")
      if [ -z "$cfg" ]; then warn "$one : config is managed by the app - use the JSON below"; continue; fi
      mkdir -p "$(dirname "$cfg")"
      if [ -f "$cfg" ]; then cp "$cfg" "$cfg.bak-$(date +%Y%m%d%H%M%S)"; fi
      $PY - "$cfg" "$INSTALL_ROOT/scripts/locmodel_mcp.py" "$(command -v "$PY")" "$one" <<'PYEOF'
import json, os, sys
cfg_path, entry, py, host = sys.argv[1:5]
try:
    data = json.load(open(cfg_path, encoding="utf-8"))
except Exception:
    data = {}
key = "servers" if host == "vscode" else "mcpServers"
data.setdefault(key, {})["locmodel-image"] = {
    "command": py,
    "args": [entry],
    "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
}
json.dump(data, open(cfg_path, "w", encoding="utf-8"), indent=2)
print("    OK  %s : written %s (backup kept)" % (host, cfg_path))
PYEOF
    done
  done
fi

printf '\nPaste this into any host that manages its own MCP config:\n'
craft_mcp_json

# ------------------------------------------------------------------ 7. verify
step 'Verify'
export PYTHONDONTWRITEBYTECODE=1
HS=$(printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"repo-install-check","version":"1"}}}\n' \
     | "$PY" "$INSTALL_ROOT/scripts/locmodel_mcp.py" 2>/dev/null || true)
case "$HS" in
  *serverInfo*) ok 'stdio handshake responded (serverInfo present)' ;;
  *) warn "handshake produced no serverInfo: $(printf '%s' "$HS" | head -c 300)" ;;
esac
"$PY" "$INSTALL_ROOT/scripts/locmodel_image.py" doctor 2>&1 | head -10 | while read -r line; do info "$line"; done || true

printf '\nDone.\n'
printf '  MCP   %s/scripts/locmodel_mcp.py\n' "$INSTALL_ROOT"
printf '  Skill %s\n\n' "$WANTED"
printf 'Next: start LocModel (or any sd-server on 127.0.0.1) with a model loaded, then\n'
printf '  %s "%s/scripts/locmodel_image.py" status\n' "$PY" "$INSTALL_ROOT"
