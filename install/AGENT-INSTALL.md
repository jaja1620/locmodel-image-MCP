# Install locmodel-image from the repository (instructions for an AI agent)

**Audience: an AI agent, not a human.** This file is a machine-executable install
specification. If you are a person, read [../docs/INSTALL-FROM-REPO.md](../docs/INSTALL-FROM-REPO.md) instead.

Read this whole file before acting, then follow exactly one path from §2. Do not invent
paths, versions or flags that are not written here.

---

## 0. What you are installing

| Piece | What it is | Where it goes |
| --- | --- | --- |
| **MCP server** | `scripts/*.py` — a stdio JSON-RPC 2.0 server exposing `generate_image`, `edit_image`, `redraw_image`, `generate_video`, `job_status`, `lipsync_*`, `engine_doctor`, … | a runtime directory you own: `%LOCALAPPDATA%\locmodel-image\repo` (Windows) or `${XDG_DATA_HOME:-~/.local/share}/locmodel-image/repo` (Linux/macOS) |
| **Skill** | `SKILL.md` — the craft layer: prompt structure, aspect presets, parameter judgement, failure interpretation. Plain Markdown, no runtime. | the host's skills directory, as `<skills>/locmodel-image/SKILL.md` |

They are complementary. The MCP server is the hands; the skill is the craft. Installing only the
MCP server is valid — the skill is a bonus, not a dependency.

Both are **pure Python standard library**. There is nothing to `pip install`, nothing to compile,
no exe to run. Requirements: **Python 3.8+** and, at *use* time, a running LocModel desktop app
(or any `sd-server`) on `127.0.0.1` with a model loaded. Installing does not need the engine.

Repository: `https://github.com/jaja1620/locmodel-image-MCP` · ref: **`v4.1.0`** · license: MIT.

---

## 1. Facts you must use verbatim

```
REPO        = jaja1620/locmodel-image-MCP
REF         = v4.1.0
RAW_BASE    = https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/v4.1.0
MANIFEST    = $RAW_BASE/install/manifest.json
SHA_SUMS    = $RAW_BASE/install/SHA256SUMS
ASSET_ZIP   = https://github.com/jaja1620/locmodel-image-MCP/releases/download/v4.1.0/locmodel-image-repo-install-v4.1.0.zip
MCP_ENTRY   = scripts/locmodel_mcp.py
SKILL_FILE  = SKILL.md
```

If the user asked for a different ref (a branch, `main`, another tag), replace `v4.1.0` in
`RAW_BASE` and `ASSET_ZIP` and say so in your final report. Do not mix refs between files.

---

## 2. Pick one path

| Path | Use when | Cost |
| --- | --- | --- |
| **A — run the installer** | You can execute shell commands (PowerShell on Windows, `sh` on Linux/macOS) | 1 command, verifies hashes itself |
| **B — fetch files yourself** | You can download URLs and write files, but cannot (or must not) execute scripts | ~8 downloads + 8 writes |
| **C — hand it to the human** | You have neither download nor write ability, or the user asked for manual steps | 0 actions, 1 message |

Path A and B produce the same on-disk result. Never do both in one run.

---

## 3. Path A — run the installer

### Windows (PowerShell 5.1+)

```powershell
# one-liner: download the installer, run it, delete it
$d = Join-Path $env:TEMP 'locmodel-image-install'; New-Item -ItemType Directory -Force -Path $d | Out-Null
Invoke-WebRequest "$RAW_BASE/install/install-from-repo.ps1" -OutFile "$d\install-from-repo.ps1" -UseBasicParsing
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\install-from-repo.ps1" -Source manifest
```

Useful flags: `-Component mcp|skill|both` (default `both`) · `-Host auto|claude|marvis|workbuddy|cursor|vscode|generic`
· `-Register claude|cursor|vscode|all` (writes the host config, keeps a `.bak`) · `-InstallRoot <dir>`
· `-DryRun` · `-Source zip|local` · `-FromPath <dir>`.

### Linux / macOS

```sh
curl -fsSL "$RAW_BASE/install/install-from-repo.sh" -o /tmp/locmodel-install.sh
sh /tmp/locmodel-install.sh --source manifest
```

Same flags in GNU style: `--component`, `--host`, `--register`, `--install-root`, `--dry-run`,
`--source zip|local`, `--from-path`.

Success criteria: the script exits 0, prints `OK` for every payload file, prints
`stdio handshake responded (serverInfo present)`, and ends with `Done.`

---

> Verified scope: the Windows route is exercised end to end on Windows 10 (sources `manifest`, `zip`,
> `local`; hash verification; host registration; MCP `initialize` handshake). The POSIX script reuses the
> same `manifest.json` and the same checks, but it is not exercised on real macOS/Linux hosts — if `sh` is
> missing or a step fails there, use path B below.

## 4. Path B — fetch the files yourself

### 4.1 Resolve the two target directories

```
MCP_ROOT   = %LOCALAPPDATA%\locmodel-image\repo                 (Windows)
             ${XDG_DATA_HOME:-$HOME/.local/share}/locmodel-image/repo   (Linux/macOS)

SKILL_ROOT = <host skills dir>/locmodel-image
```

`<host skills dir>` — use the **first** row that already exists on this machine; if none of them
exist, use the row for the host you are running inside, or `generic` as the last resort:

| Host | Windows | Linux / macOS |
| --- | --- | --- |
| Claude Code / Claude skills | `%USERPROFILE%\.claude\skills` | `~/.claude/skills` |
| Marvis | `%USERPROFILE%\.marvis\skills\custom` | `~/.marvis/skills/custom` |
| WorkBuddy | `%USERPROFILE%\.workbuddy\skills` | `~/.workbuddy/skills` |
| Cursor | `%USERPROFILE%\.cursor\skills` | `~/.cursor/skills` |
| VS Code | `%USERPROFILE%\.vscode\skills` | `~/.vscode/skills` |
| generic / any other agent | `%USERPROFILE%\.agent-skills` | `~/.agent-skills` |

### 4.2 Download each file to its destination

Create `MCP_ROOT\scripts`, then fetch these into `MCP_ROOT` keeping the relative path:

| # | URL | destination | required |
| --- | --- | --- | --- |
| 1 | `{RAW_BASE}/scripts/locmodel_mcp.py` | `MCP_ROOT/scripts/locmodel_mcp.py` | yes |
| 2 | `{RAW_BASE}/scripts/locmodel_image.py` | `MCP_ROOT/scripts/locmodel_image.py` | yes |
| 3 | `{RAW_BASE}/scripts/locmodel_edit.py` | `MCP_ROOT/scripts/locmodel_edit.py` | yes |
| 4 | `{RAW_BASE}/scripts/locmodel_workflow.py` | `MCP_ROOT/scripts/locmodel_workflow.py` | yes |
| 5 | `{RAW_BASE}/scripts/locmodel_video.py` | `MCP_ROOT/scripts/locmodel_video.py` | yes |
| 6 | `{RAW_BASE}/scripts/locmodel_lipsync.py` | `MCP_ROOT/scripts/locmodel_lipsync.py` | yes |
| 7 | `{RAW_BASE}/scripts/lipsync_runner_embed.py` | `MCP_ROOT/scripts/lipsync_runner_embed.py` | yes |
| 8 | `{RAW_BASE}/SKILL.md` | `SKILL_ROOT/SKILL.md` | yes (skip only if the user said "MCP only") |
| 9 | `{RAW_BASE}/pyproject.toml` | `MCP_ROOT/pyproject.toml` | no (needed only for `pip install .`) |
| 10 | `{RAW_BASE}/LICENSE` | `MCP_ROOT/LICENSE` | no |

Write bytes **unmodified** — no BOM, no line-ending translation, no wrapping. `SKILL.md` is UTF-8
with Unix line endings; rewriting it invalidates the hash check below and can break the YAML
front-matter.

### 4.3 Verify (do not skip)

Compute SHA-256 of every downloaded file and compare with `{RAW_BASE}/install/SHA256SUMS`
(format: `<sha256>  <relative path>`, paths relative to the repository root, so compare
`SKILL.md` for the skill file and `scripts/x.py` for the modules):

```
d5e7c311264996395c9c94fefea43d867b14d0a5bc747a3228d4f3390aa0e420  SKILL.md
8a325cc7d349fae68b868269bd2ad935766abb3fe90aa6a37c8e20b0f7377ac4  scripts/locmodel_mcp.py
```

If one hash mismatches: delete that file, re-download once, re-check. If it still mismatches, stop
and report which file differs — do **not** keep a file whose hash you could not confirm.

Then confirm the server actually speaks MCP (one `initialize` request, EOF afterwards):

```powershell
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"agent-install","version":"1"}}}' | & python "$MCP_ROOT\scripts\locmodel_mcp.py"
```

```sh
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"agent-install","version":"1"}}}\n' | python3 "$MCP_ROOT/scripts/locmodel_mcp.py"
```

Pass = the JSON response contains `"serverInfo"`. Anything else = failure; report the raw output.
The engine does not need to be running for this check.

### 4.4 Write the install report

Write `MCP_ROOT/install-manifest.json` so later agents can find what you did:

```json
{
  "schema": "locmodel-image.repo-install-report/1",
  "version": "4.1.0",
  "source": "manual-fetch",
  "installed_at": "<ISO-8601 timestamp>",
  "mcp_root": "<MCP_ROOT>",
  "python": "<absolute path of the interpreter>",
  "entrypoint": "<MCP_ROOT>/scripts/locmodel_mcp.py",
  "hosts": ["<skill host you used>"]
}
```

---

## 5. Register the server with the host

Use the **absolute path of a real interpreter**, and prefer forward slashes on Windows.

`command` = the interpreter, `args[0]` = `MCP_ROOT/scripts/locmodel_mcp.py`, `env` =
`PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`.

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

| Host | Config file | Top-level key |
| --- | --- | --- |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` (Win) / `~/Library/Application Support/Claude/claude_desktop_config.json` (mac) / `~/.config/Claude/claude_desktop_config.json` (Linux) | `mcpServers` |
| Cursor | `~/.cursor/mcp.json` | `mcpServers` |
| VS Code (native MCP) | `<User>/mcp.json` (`%APPDATA%\Code\User\mcp.json`) | `servers` |
| Marvis | managed in the app: Settings → MCP → Add custom server | paste the JSON above |
| WorkBuddy | managed in the app; or a plugin dir under `~/.workbuddy/plugins/marketplaces` | paste, or plugin `plugin.json` with `mcpServers` |

Rules for editing host config:

1. **Back up first** — copy to `<file>.bak-<timestamp>`. Always.
2. **Merge, never replace.** Keep every existing key. Parse → add/replace the single
   `locmodel-image` entry → write back with 2-space indent.
3. If the file exists but is not valid JSON, do **not** overwrite it. Rename it to
   `<file>.broken-<timestamp>`, report it, and let the human decide.
4. Never touch a config you were not asked to touch. Registering into the host that is running
   you is the default; other hosts only on request.
5. Marvis and WorkBuddy rewrite their config at runtime — for those, print the JSON and let the
   user paste it, unless the app is closed.

---

## 6. Failure handling

| Symptom | Meaning | Action |
| --- | --- | --- |
| `python` not found, or `python` opens the Microsoft Store | bare `python` resolved to the Store stub | use the absolute interpreter path; try `py -0p`, `where python3`, `%LOCALAPPDATA%\Programs\Python\Python3*\python.exe` |
| `sha256 mismatch` | proxy/CDN rewrite, wrong ref, or truncated download | re-download once from `RAW_BASE`; if it persists, report the path + both hashes and stop |
| handshake prints Python traceback | missing module — the payload is incomplete | re-check that **all 7** `scripts/*.py` are present; `locmodel_mcp.py` imports them as siblings |
| handshake prints `WARNING core import failed` but still answers | partial deploy | install the remaining modules; the warning must disappear |
| `doctor` reports engine unreachable | normal — the engine is not running | not an install failure. Tell the user to start LocModel with a model loaded |
| port scanning is slow or lands on the wrong port | several engines on the box | set `LOCMODEL_SD_PORT` in `env`, e.g. `"LOCMODEL_SD_PORT": "8790"` |
| skill not picked up by the host | wrong directory or in-memory index | confirm the path is exactly `<skills>/locmodel-image/SKILL.md` and restart the host |
| a previous exe install exists at `%LOCALAPPDATA%\locmodel-image` | `locmodel-image-setup.exe` owns that tree | do **not** overwrite it. This repo install lives in the `repo\` subdirectory by design |

Constraints — treat these as hard rules:

- Do **not** `pip install` anything. The package is standard library only.
- Do **not** modify, move or delete anything under an existing `locmodel-image-setup.exe` install
  (`%LOCALAPPDATA%\locmodel-image` on Windows). This install is additive and lives in `repo\`.
- Do **not** start, restart or kill LocModel, and do not load models — the bridge deliberately
  does not manage the engine lifecycle.
- Do **not** write outside `MCP_ROOT`, `SKILL_ROOT` and the one host config file you were asked to
  edit. No system directories, no `PATH` changes, no registry, no services.
- Do **not** disable hash verification to "make it work". A hash mismatch is information.

---

## 7. Report back

State, in this order:

1. which path you took (A or B) and the ref used;
2. `MCP_ROOT` and the absolute `locmodel_mcp.py` path;
3. the skill host used and its `SKILL.md` path (or "skill skipped, MCP only");
4. verification result: hashes confirmed (n/n) and `serverInfo` seen yes/no;
5. which host config you edited, and that a `.bak-*` backup exists — or "not registered, JSON
   printed for the user to paste";
6. the one thing the user still has to do themselves: start LocModel (or `sd-server`) with a model
   loaded. Nothing else.

Do not claim success for a step you did not verify. If a step failed, say which one and stop.
