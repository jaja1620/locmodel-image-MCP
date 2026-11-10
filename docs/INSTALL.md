# Installation and Usage Guide — locmodel-image 3.0.0

English | [简体中文](./INSTALL.zh-CN.md)

`locmodel-image` exposes the local LocModel / `stable-diffusion.cpp` (`sd-server`) image engine as an
MCP server plus a command-line bridge. Everything runs offline on your own machine: no API key,
no cloud call, no per-image cost.

There are two ways to install it. **Pick one** — you do not need both.

| Way | Best for | Target machine needs Python? |
| --- | --- | --- |
| **A. Installer (single-file exe)** — recommended | Normal users, designers, office machines | **No** |
| **B. From source (pip)** | Developers, people who want to modify the code | Yes, Python 3.9+ |

Both ways end up registering the same MCP server, so the **usage** section is identical.

---

## 0. Prerequisites (both ways)

1. **Windows 10/11 (x64)** or Linux/macOS for source install.
2. **LocModel desktop app installed**, with at least one image model downloaded and loadable.
   The engine binary `sd-server.exe` and the model files come from LocModel itself; this project
   does not download or redistribute them.
3. **Enough VRAM.** 8 GB (e.g. RTX 3060 Ti) runs the Q2_K/Q6_K quantised models comfortably.
   Text-to-image works with 6 GB; region editing is happiest at 8 GB and above.
4. **An MCP-capable client** if you want tool calling: Claude Desktop, Claude Code, Cursor,
   VS Code, Windsurf, DSH, Hermes, or Marvis.

Verify the engine is alive before installing: open LocModel, load an image model, keep the window
open. A quick check is `http://127.0.0.1:8790` being reachable (the port may differ; the server
auto-discovers it).

---

## 1. Way A — Install from the installer (single-file exe, no Python needed)

### 1.1 Download

From the [Releases page](https://github.com/jaja1620/locmodel-image-MCP/releases) pick
`v3.0.0` and download:

| File | Size | SHA-256 |
| --- | --- | --- |
| `locmodel-image-setup.exe` | 7 928 599 bytes (7.56 MB) | `ED1C09CAFC020B862E8FB2A3C0FB88EDE85B220EBC5EB4D9FDFA2D5C1A2526D1` |

Optional integrity check (PowerShell):

```powershell
Get-FileHash .\locmodel-image-setup.exe -Algorithm SHA256
# must print ED1C09CAFC020B862E8FB2A3C0FB88EDE85B220EBC5EB4D9FDFA2D5C1A2526D1
```

### 1.2 Install

**Double-click** `locmodel-image-setup.exe`. It is a console program: a window opens, prints what
it is about to do, and waits for you to confirm. Press Enter / `y` to continue. When it finishes
it prints a summary and stays open so you can read it — the window does not vanish.

By default it installs to `%LOCALAPPDATA%\locmodel-image` and registers the MCP server into every
supported client it finds on the machine.

Prefer the command line? Open PowerShell in the download folder:

```powershell
# fully automatic, accept defaults
.\locmodel-image-setup.exe install --mode exe --yes

# only register into specific clients
.\locmodel-image-setup.exe install --targets dsh,hermes --yes

# dry run: show what would be written, change nothing
.\locmodel-image-setup.exe install --dry-run

# install somewhere else
.\locmodel-image-setup.exe install --dir "D:\Tools\locmodel-image" --yes
```

> Windows SmartScreen may warn about an unsigned exe. Choose "More info" → "Run anyway", or verify
> the SHA-256 above first.

### 1.3 What the installer actually does

1. Unpacks the runtime payload into the install directory (`%LOCALAPPDATA%\locmodel-image` by default):
   `scripts/locmodel_image.py`, `scripts/locmodel_mcp.py`, `scripts/locmodel_edit.py`,
   `SKILL.md`, `README*`, `annotator/mask_annotator.html`, plus a copy of the installer itself
   which acts as the MCP stdio server process.
2. Registers the MCP server entry into each detected client config.
3. Runs a smoke test (MCP handshake + tool listing) and an engine self-check.
4. Leaves existing client configuration untouched except for its own single entry.

Supported clients and the files it edits:

| Client | Config file | Key |
| --- | --- | --- |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` | `mcpServers` |
| Claude Code | `~/.claude.json` or `~/.claude/settings.json` | `mcpServers` |
| Cursor | `%APPDATA%\Cursor\User\mcp.json` or `~/.cursor/mcp.json` | `mcpServers` |
| VS Code | `%APPDATA%\Code\User\mcp.json` | `servers` (with `type: stdio`) |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |
| DSH | `~/.dsh/mcp.json` | `mcpServers` |
| Hermes | `~/.hermes/config.yaml` | `mcp_servers` |
| Marvis | no file config | prints `mcp_install` / `skill_install` instructions to paste into chat |

Safety behaviour worth knowing: configs are backed up to `*.bak.<timestamp>` before editing; only
its own entry is added or removed; re-running is idempotent (`unchanged`, no extra backups); an
unparsable JSON config is refused rather than "repaired".

### 1.4 Marvis users: one extra step

Marvis takes MCP servers and Skills through chat instructions, not through a file. Run:

```powershell
.\locmodel-image-setup.exe emit marvis
```

It prints two blocks (`mcp_install` and `skill_install`). Paste them into the Marvis chat and it
will register the server and install the Skill for you.

### 1.5 Verify

```powershell
.\locmodel-image-setup.exe list         # which clients got the entry, and whether it matches
.\locmodel-image-setup.exe doctor       # Python/engine/model/VRAM/output dir/config self-check
```

`doctor` exits `0` when everything passes, `1` when a check fails. If the engine is not running it
reports "engine not ready" with a hint — that does **not** mean the MCP registration failed.

### 1.6 Update and uninstall

```powershell
# update: run the new exe again; it overwrites in place and keeps client configs consistent
.\locmodel-image-setup.exe install --mode exe --yes

# uninstall but keep the install directory
.\locmodel-image-setup.exe uninstall --yes

# uninstall and delete the directory
.\locmodel-image-setup.exe uninstall --purge --yes
```

If the exe is still held by a running client process, uninstall refuses to delete anything and
tells you which process to close — it never leaves a half-deleted state.

Exit codes: `0` ok, `1` self-check failed, `2` bad arguments/environment, `130` interrupted.

---

## 2. Way B — Install from source (pip, the original way)

```bash
git clone https://github.com/jaja1620/locmodel-image-MCP.git
cd locmodel-image-MCP
pip install -e .
```

Requirements: Python 3.9+ and nothing else — the bridge is zero-dependency (standard library only).

Register the MCP server manually. The generic JSON form (Claude Desktop / Cursor / Cursor-like
clients):

```json
{
  "mcpServers": {
    "locmodel-image": {
      "command": "python",
      "args": ["C:/path/to/locmodel-image-MCP/scripts/locmodel_mcp.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

Use an **absolute** path for both `python` and the script. On Windows, prefer the full interpreter
path (e.g. `C:/Python311/python.exe`) if several Pythons exist.

Or run the CLI directly with no client at all:

```bash
python scripts/locmodel_image.py status
python scripts/locmodel_image.py generate --prompt "modern modular building, dusk, wide angle" --aspect 16:9
```

The installer (Way A) can also do this registration for you in Python mode — `--mode python --python <path>`.

---

## 3. How to use it

### 3.1 MCP tools (9 of them)

| Tool | What it does |
| --- | --- |
| `generate_image` | Text-to-image. Prompt, size or aspect, seed, count 1–4, negative prompt. |
| `edit_image` | Inpainting. Redraw **only** inside a mask; outside pixels are restored to the original on the CPU side. |
| `redraw_image` | Whole-image redraw. Original is used as composition/colour reference only. |
| `list_edit_models` | Which models can edit, which one auto-selection would pick, and why. |
| `load_model` | Explicitly load a named model into the engine. |
| `locmodel_status` | Engine state, loaded model, VRAM, config summary. |
| `list_models` | Models available in the local LocModel installation. |
| `engine_doctor` | Engine/port/model diagnostics. |
| `engine_discover` | Force a re-scan of the engine port (five-level discovery). |

Typical first call in any client:

```text
locmodel_status()                                  # is the engine ready?
generate_image(prompt="interior of a timber meeting room, soft north light, 35mm",
               aspect="4:3", out_dir="D:/out")
```

Result files: `<out>/locmodel_<YYYYmmdd_HHMMSS>_<i>_seed<seed>.png` plus a matching `_meta.json`
recording prompt, seed, size and elapsed time so you can reproduce it.

### 3.2 Editing an existing image

Three ways to give the mask — use the simplest that fits:

1. **Rectangles / polygons** (recommended, resolution independent):
   `regions={"unit":"norm","regions":[{"type":"rect","x":[0.12,0.10,0.48,0.42]}]}`
2. **Mask PNG**: white = repaint, black = keep; size is rescaled automatically.
3. **Colour threshold**: select an area by colour, handy for "swap this flat wall material".

No coordinates at hand? `annotator/mask_annotator.html` is a single-file page — double-click it,
drag an image in, draw rectangles/polygons/brush strokes, then export `mask.png` or copy the JSON
coordinates and paste them into `edit_image`.

```text
edit_image(init_image="D:/out/v1.png",
           prompt="night-time glass curtain wall, interior lights on, warm reflections",
           regions={"unit":"norm","regions":[{"type":"rect","x":[0.25,0.18,0.62,0.55]}]},
           strength=0.8)
```

Guarantees worth stating when you hand results back:

- **Pixels outside the mask are not changed** — this is enforced by recomposition on the CPU side,
  not by trusting the engine.
- `files[].path` is the **protected** image. The engine's raw output is saved next to it as
  `*_raw.png` (`files[].raw`) and is only for troubleshooting "what did the engine actually paint".
- The original image is never overwritten; a new file is written each time, so you can compare
  and keep whichever you prefer.

### 3.3 CLI cheat sheet

```bash
# self-check: python version / engine / models / VRAM / output dir / config
python scripts/locmodel_image.py doctor

# print the payload without generating (fast parameter sanity check)
python scripts/locmodel_image.py generate --prompt "test" --aspect 1:1 --dry-run

# generate
python scripts/locmodel_image.py generate --prompt "..." --aspect 16:9 --out D:/out --seed 12345

# which models can edit; what auto-selection would choose
python scripts/locmodel_image.py edit-models

# inpaint by rectangle, by mask file, or redraw the whole image
python scripts/locmodel_image.py edit --image a.png --prompt "oak slat ceiling" --region 0.1,0.1,0.5,0.4 --strength 0.8
python scripts/locmodel_image.py edit --image a.png --mask mask.png --prompt "..."
python scripts/locmodel_image.py redraw --image a.png --prompt "watercolour architectural sketch" --strength 0.65

# model and port control
python scripts/locmodel_image.py load-model "Qwen-Image-Edit"
python scripts/locmodel_image.py discover

# config
python scripts/locmodel_image.py config show
python scripts/locmodel_image.py config get size.max_side
python scripts/locmodel_image.py config set size.max_side 1280
python scripts/locmodel_image.py config path
```

### 3.4 Prompts that work with small local models

Build the prompt as
`<subject> + <scene> + <composition/viewpoint> + <lighting> + <material/detail> + <style> + <quality anchors>`.

- Concrete nouns beat adjective stacks: `curved glass curtain wall` > `beautiful modern building`.
- Add camera language for photographic looks: `35mm`, `wide angle`, `eye level`, `golden hour`.
- Name materials and light: `warm oak`, `terrazzo floor`, `soft north light`.
- Useful style anchors: `architectural visualization`, `watercolour sketch`, `isometric diagram`.
- Keep prompts under ~60 tokens; turbo models run at low step counts and dilute long prompts.

### 3.5 Key configuration

Config file: `%USERPROFILE%\.locmodel-image\config.json` (override with `LOCMODEL_IMAGE_CONFIG`).

| Key | Default | Meaning |
| --- | --- | --- |
| `size.min_area` | 768² ≈ 590k px | Smaller requests are upscaled, aspect ratio kept |
| `size.max_side` | 1536 (auto by VRAM) | Larger requests are downscaled |
| `generate.max_count` | 4 | Upper bound for one `count` call |
| `edit.priority` | `qwen-image-edit`, `flux2-klein`, `qwen-image` | Auto model selection order |
| `edit.auto_load` | `true` | Load the best available edit model automatically |
| `edit.prefer_loaded` | `true` | Do not switch models just to climb the priority list (switching costs 30–60 s) |
| `edit.snapshot_cache_ttl` | 120 s | Model-list cache, shared across processes |

The engine port is discovered in five levels: `LOCMODEL_SD_PORT` → cached `config.json` →
`sd-server` process command line → candidate port scan → local listening-port scan.

---

## 4. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `engine_not_found` (exit 3) | LocModel closed, or no model loaded | Open LocModel, load an image model, then `engine_discover` |
| `generation_failed` + `no results` | Out of VRAM, or two engines competing | Close LM Studio / GPU-heavy browser tabs; reduce size or `count` |
| Stuck in `queued` | The engine has a single slot | Wait; other generation jobs are ahead |
| Very slow (>3 min) | Too large / too many steps / GPU busy | Lower size, reset `steps`, free the GPU |
| Prompt seems ignored | `cfg` too low | `--cfg 1.5`–`2.0` |
| Text in image is gibberish | Inherent to diffusion models | Do not fight it — composite text afterwards in PPT/PS/Figma |
| Client cannot connect to MCP | Wrong Python path / encoding | Absolute path to `python`; set `PYTHONIOENCODING=utf-8`; run `doctor` |
| `model_unavailable` | No editable model installed | Download a Qwen-Image-Edit / FLUX.2 klein model in LocModel, or use `redraw_image` |
| `bad_arguments` + mask | Blank mask, mask covering everything, non 8-bit PNG | Draw something; use `redraw_image` for full-frame edits; re-export as 8-bit non-interlaced PNG |
| Visible seam after edit | `strength` too high or `mask_blur` too low | Lower `strength`, raise `mask_blur` / `inpaint_padding` |
| Nothing changed inside the mask | `strength` too low, or prompt does not say what it becomes | Raise `strength` above 0.8; describe the target as concrete nouns |

---

## 5. What is new in 3.0.0 (one-line summary)

Region-accurate editing (`edit_image` / `redraw_image` / `list_edit_models` / `load_model`), a
single-file HTML mask annotator, and a **hard guarantee that pixels outside the mask are
untouched** — the engine's own output is preserved separately as `*_raw.png`. The installer now
ships `scripts/locmodel_edit.py` and `annotator/mask_annotator.html` in its payload.

License: MIT. Issues and pull requests: https://github.com/jaja1620/locmodel-image-MCP
