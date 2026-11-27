# Install from the repository

This page covers the third install route: **deploy straight from the GitHub repo, no exe, no pip,
no clone**. It sits next to the two older routes and never touches them.

| Route | Needs | Best for |
| --- | --- | --- |
| `locmodel-image-setup.exe` | Windows, nothing else | a person double-clicking once on Windows; it also registers the MCP server into every host it detects |
| From source | `git`, Python | contributors, anyone who wants `pip install .` and a `locmodel-image` command on `PATH` |
| **From the repository** (this page) | Python 3.8+, network | **an AI agent that has to deploy itself**, Linux/macOS/Windows without building anything, and anyone who wants a scriptable, hash-verified install into a directory they control |

Two pieces get deployed:

- **MCP server** — `scripts/*.py`, a stdio JSON-RPC server. Goes to
  `%LOCALAPPDATA%\locmodel-image\repo` (Windows) or
  `${XDG_DATA_HOME:-~/.local/share}/locmodel-image/repo` (Linux/macOS).
- **Skill** — `SKILL.md`, Markdown only. Goes to `<host skills dir>/locmodel-image/SKILL.md`.

Nothing is compiled and nothing is installed into `site-packages`. The payload is pure standard
library Python, so an install is literally "copy 7 `.py` files and 1 `.md` file to two directories
and verify their hashes".

---

## What is in `install/`

| File | Purpose |
| --- | --- |
| `manifest.json` | machine-readable payload: version, ref, every file with its SHA-256 and size, host path table, verification commands |
| `SHA256SUMS` | the same hashes in `sha256sum -c` format |
| `hosts.json` | host → directory table (Windows and Unix paths), plus which hosts manage their own MCP config |
| `AGENT-INSTALL.md` | the install spec written for an AI agent: three paths (run the installer / fetch files yourself / hand off to the human), exact URLs, verification, failure table |
| `install-from-repo.ps1` | Windows installer: download → verify → deploy → report → optional host registration |
| `install-from-repo.sh` | the same for Linux/macOS |

---

## Windows

```powershell
# download the installer, run it, remove it
$d = Join-Path $env:TEMP 'locmodel-image-install'
New-Item -ItemType Directory -Force -Path $d | Out-Null
Invoke-WebRequest 'https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/v4.1.0/install/install-from-repo.ps1' `
  -OutFile "$d\install-from-repo.ps1" -UseBasicParsing
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\install-from-repo.ps1"
```

If your execution policy blocks local scripts, `-ExecutionPolicy Bypass` above already handles it;
nothing is written to the registry and no admin rights are needed.

## Linux / macOS

**Tested where.** The Windows path (PowerShell 5.1) was exercised end to end on Windows 10: manifest / zip / local sources, hash verification, host registration and the MCP `initialize` handshake. The POSIX script consumes the same `manifest.json` and runs the same checks, but it has not been exercised on a real macOS/Linux host — if `sh` is unavailable or a step fails, fall back to path B in `AGENT-INSTALL.md` (fetch the payload files yourself) and report the failing step.


```sh
curl -fsSL https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/v4.1.0/install/install-from-repo.sh -o /tmp/locmodel-install.sh
sh /tmp/locmodel-install.sh
```

## Offline

Release asset `locmodel-image-repo-install-v4.1.0.zip` contains `scripts/`, `SKILL.md`, `install/`
and `docs/`. Unzip it on the target machine, then:

```powershell
.\install\install-from-repo.ps1 -Source local -FromPath .
```
```sh
sh ./install/install-from-repo.sh --source local --from-path .
```

`-Source local` / `--source local` never touches the network, and hashes are still verified against
the bundled `install/manifest.json`.

---

## Flags

| PowerShell | sh | Meaning |
| --- | --- | --- |
| `-Component mcp\|skill\|both` | `--component` | default `both` |
| `-Host auto\|claude\|marvis\|workbuddy\|cursor\|vscode\|generic\|all` | `--host` | `auto` writes the skill to every host directory whose parent already exists |
| `-Register claude\|cursor\|vscode\|marvis\|workbuddy\|all` | `--register` | also write the MCP entry into that host's config. A `.bak-<timestamp>` is kept first |
| `-InstallRoot <dir>` | `--install-root` | where the Python payload goes |
| `-Ref <git ref>` | `--ref` | default: the ref pinned in the manifest (`v4.1.0`) |
| `-Source manifest\|zip\|local` | `--source` | `manifest` fetches file by file (default), `zip` fetches one release asset, `local` uses `-FromPath` |
| `-FromPath <dir>` | `--from-path` | source tree for `local` |
| `-DryRun` | `--dry-run` | print the plan, write nothing |
| `-Force` | `--force` | continue past a hash mismatch (you lose the integrity guarantee — don't) |
| `-CreateHostDirs` | `--create-host-dirs` | create the host skills directory even if the host was never installed |

Exit codes (sh): `0` ok, `1` usage, `2` download/verify, `3` deploy.

---

## Register the server

`-Register` covers Claude Desktop, Cursor and VS Code — it merges a single `locmodel-image` entry
into the existing JSON and keeps a backup. Marvis and WorkBuddy manage their own config at runtime,
so the installer prints the snippet instead:

```json
{
  "mcpServers": {
    "locmodel-image": {
      "command": "C:/Users/you/AppData/Local/Programs/Python/Python312/python.exe",
      "args": ["C:/Users/you/AppData/Local/locmodel-image/repo/scripts/locmodel_mcp.py"],
      "env": { "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1" }
    }
  }
}
```

Paste it into **Marvis → Settings → MCP → Add custom server**, or **WorkBuddy → Settings → MCP**.
On Windows always use a real interpreter path: bare `python` may be the Microsoft Store stub, which
exits silently and makes the server look "installed but dead".

---

## Verify

```powershell
# 1. does the server speak MCP? (engine not needed)
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}' | python "$env:LOCALAPPDATA\locmodel-image\repo\scripts\locmodel_mcp.py"
```

The output must contain `"serverInfo"`. Then, with LocModel (or any `sd-server`) running and a
model loaded:

```powershell
python "$env:LOCALAPPDATA\locmodel-image\repo\scripts\locmodel_image.py" doctor
python "$env:LOCALAPPDATA\locmodel-image\repo\scripts\locmodel_image.py" status
```

`doctor` exits non-zero when the *engine* is missing — that is an engine problem, not an install
problem. The install report is at `<MCP_ROOT>/install-manifest.json`.

---

## Uninstall

Delete the two roots and remove the `locmodel-image` entry from the host config:

```
%LOCALAPPDATA%\locmodel-image\repo
<skills>\locmodel-image
```

That is the whole state. No registry keys, no services, no `PATH` entries, no `site-packages`
changes. A backup of any config the installer touched is next to it as `*.bak-<timestamp>`.

---

## Relation to the exe install

`locmodel-image-setup.exe` owns `%LOCALAPPDATA%\locmodel-image` and writes a frozen runtime there.
This route installs into the `repo\` subdirectory and reads nothing from the exe tree, so the two can
coexist. The installer refuses to overwrite the exe tree, and uninstalling one leaves the other
intact. Pick the one you actually want to run — registering both under the same name in one host
will make the host load whichever entry it reads last.

---

## For agents hitting this page

Give your agent this line and nothing else:

> Read `https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/v4.1.0/install/AGENT-INSTALL.md`
> and follow it to install locmodel-image (MCP server + companion skill) on this machine.

The spec is self-contained: it states the exact URLs, the two destination directories per OS, the
hash table, two working verification commands, the per-host config key, and the list of things not
to touch.
