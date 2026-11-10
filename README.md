# locmodel-image-MCP

> GitHub repository: [jaja1620/locmodel-image-MCP](https://github.com/jaja1620/locmodel-image-MCP) — the installable package, the CLI command and the MCP server are still named `locmodel-image`.

**Turn your local LocModel / stable-diffusion.cpp image engine into a first-class tool for AI agents — offline, free, scriptable.**

`locmodel-image` is a zero-dependency bridge that exposes the image engine bundled inside
[LocModel](https://github.com/LocalAI) (an `sd-server` built on stable-diffusion.cpp) through two
interfaces:

| Interface | Entry point | Use it when |
| --- | --- | --- |
| **MCP server** (recommended) | `scripts/locmodel_mcp.py` — tools `generate_image`, `locmodel_status`, `list_models`, `engine_doctor`, `engine_discover` | Your AI client speaks Model Context Protocol (Claude Desktop, Cursor, Marvis, Continue, …). The model gets a typed tool instead of memorising shell commands. |
| **CLI bridge** | `scripts/locmodel_image.py` | Scripts, batch jobs, CI, or clients without MCP support. |

Both share the same engine layer: port auto-discovery, VRAM-aware size guard, failure
classification with retry, cross-process generation lock, structured JSON output and meaningful
exit codes.

> **English** · [简体中文](README.zh-CN.md)

---

## Table of contents

- [Why this exists](#why-this-exists)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Connect as an MCP server](#connect-as-an-mcp-server)
- [Install the companion Skill](#install-the-companion-skill)
- [Edit an existing image](#edit-an-existing-image)
- [CLI reference](#cli-reference)
- [Configuration reference](#configuration-reference)
- [Sizing and VRAM strategy](#sizing-and-vram-strategy)
- [How it works](#how-it-works)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Compatibility and limitations](#compatibility-and-limitations)
- [What changed in 3.0](#what-changed-in-30)
- [What changed in 2.0](#what-changed-in-20)
- [License](#license)

---

## Why this exists

LocModel ships a complete local image pipeline but exposes it as a desktop GUI. Agents and
scripts cannot drive a GUI reliably, and hard-coding `http://127.0.0.1:8790` breaks the moment the
port changes, the model changes, or the machine has less VRAM than yours.

This project solves exactly those three problems:

1. **Discovery instead of hard-coded ports** — five fallback layers, including parsing the
   `sd-server` process command line (the most reliable source, because LocModel chooses the port
   at launch).
2. **Guard instead of mysterious failures** — the engine silently fails on *tiny* images
   (512×512 can return `no results`) while succeeding on much larger ones. The guard works on
   **total pixel area**, not on a single edge, and rescales proportionally so the requested aspect
   ratio survives.
3. **Portability instead of "works on my machine"** — no hard-coded user paths, resolutions,
   samplers or Python installs. Every default is discovered or overridable.

---

## Requirements

| Item | Requirement |
| --- | --- |
| Python | 3.8+ (3.10+ recommended). **Standard library only — nothing to `pip install`.** |
| Engine | LocModel desktop app with a diffusion model loaded, **or** any `sd-server` (stable-diffusion.cpp) exposing the `/sdcpp/v1/*` HTTP API |
| OS | Windows 10/11, macOS, Linux |
| GPU | Any GPU the engine itself supports. 6 GB VRAM is usable; 8 GB is comfortable at 1024² |

The bridge does **not** load models and does **not** manage the engine lifecycle — start LocModel
and load a model first. That is a deliberate boundary: engine startup differs per hardware and
per model, and getting it wrong is the number-one cause of "workspace capacity" crashes.

---

## Install

Two ways to install — pick **one**:

- **Installer (single-file exe, no Python needed)** — recommended for normal use. Download
  `locmodel-image-setup.exe` from [Releases](https://github.com/jaja1620/locmodel-image-MCP/releases),
  double-click it, confirm once, done. It unpacks the runtime into `%LOCALAPPDATA%\locmodel-image`
  and registers the MCP server into every supported client it detects. Marvis users then run
  `locmodel-image-setup.exe emit marvis` and paste the printed instructions into chat.
- **From source (needs Python 3.9+, the original way)** — clone and run, as described below.

Detailed, bilingual step-by-step guide (install + usage + troubleshooting):
[docs/INSTALL.md](docs/INSTALL.md) (English) · [docs/INSTALL.zh-CN.md](docs/INSTALL.zh-CN.md) (中文)

### From source

Nothing to build. Clone and run:

```bash
git clone https://github.com/jaja1620/locmodel-image-MCP.git
cd locmodel-image-MCP
python scripts/locmodel_image.py doctor
```

`doctor` prints a checklist (Python version, platform, config path, engine port and how it was
found, model, GPU free memory, output directory writability) and exits non-zero if something
essential is missing.

Optional — install as a package so `locmodel-image` is on your `PATH`:

```bash
pip install .
locmodel-image doctor
```

---

## Quick start

```bash
# 1. Is the engine reachable, and where?
python scripts/locmodel_image.py status

# 2. Validate parameters without spending GPU time
python scripts/locmodel_image.py generate --prompt "courtyard at dusk" --aspect 16:9 --dry-run

# 3. Generate
python scripts/locmodel_image.py generate \
  --prompt "modern modular building, warm timber facade, dusk, wide angle, architectural photography" \
  --aspect 16:9 --out ./output --seed 424242

# 4. Two variants, fixed seed for reproducibility
python scripts/locmodel_image.py generate --prompt "..." --count 2 --seed 424242
```

Every run writes:

```
output/locmodel_20261109_171204_1_seed424242.png
output/locmodel_20261109_171204_meta.json
```

`_meta.json` records the effective prompt, seed, size after guard correction, steps, sampler, CFG,
requested-vs-final size and elapsed seconds — so any image can be reproduced or audited later.

## Edit an existing image

v3.0 adds two verbs on top of text-to-image: **repaint one region** and **redraw the whole frame**.

| You want | Use | Original pixels |
| --- | --- | --- |
| Change one area, keep the rest | `edit_image` (needs a mask) | Preserved outside the mask — enforced, not hoped for |
| Same composition, new style/subject | `redraw_image` | Used as a structural reference only |

### Give the mask three ways

**Regions as JSON** — no image editor needed:

```json
{"unit": "norm", "regions": [
  {"type": "rect", "x": [0.12, 0.10, 0.48, 0.42]},
  {"type": "polygon", "points": [[0.60, 0.62], [0.92, 0.60], [0.78, 0.94]]}
]}
```

`unit` is `norm` (0–1, resolution-independent, default) or `px`. Shapes are unioned.

**A mask PNG** — white = repaint, black = keep. Rescaled automatically if the size differs.

**A colour threshold** — "retexture that solid-colour wall" without drawing anything.

**By hand** — open `annotator/mask_annotator.html` in any browser (single file, no install).
Drag an image in, draw rectangles / polygons / freehand, then export `mask.png` or copy the
normalised JSON straight into `edit_image`.

### Model selection is explicit, never silent

```bash
python scripts/locmodel_image.py edit-models
```

Prints every model, which ones can edit regions, and which one would be picked automatically.
The default priority is `qwen-image-edit` → `flux2-klein` → `qwen-image`.

Discovery cost is bounded and cached, because probing for an engine that is not running used to
be the slowest thing in the tool: process names are checked with `tasklist` before PowerShell is
started to read command lines, `netstat` output is used to skip candidate ports that cannot be
listening, and the resulting snapshot is reused for `edit.snapshot_cache_ttl` seconds (default
120) across processes. A warm call answers in ~0.3 s; pass `--refresh` after loading a model by
hand in the LocModel window:

```bash
python scripts/locmodel_image.py edit-models --refresh
```

- `edit.prefer_loaded` (default `true`) — if an editable model is already in VRAM, use it instead
  of swapping to a "better" one. A swap means unload + reload: 30–60 s for a 4B model, longer for
  a 20B, and it can OOM on a card that was already tight.
- `edit.auto_load` (default `true`) — load the highest-priority editable model when nothing is
  loaded. Without it the priority list would be decorative.
- Pass `model` and it is used **exactly** — an unusable model raises an error listing the
  alternatives rather than quietly substituting another one.
- Only the `edit` family (Qwen-Image-Edit, FLUX.2 klein) can repaint a region. Pure generators
  (Qwen-Image, Z-Image, SDXL, SD1.5) can only `redraw_image`.

Every response carries a `selection` block (`requested` / `chosen` / `family` / `rank` / `reason` /
`auto_loaded` / `candidates`) so you can explain why a particular model ran.

### Outside the mask cannot change

The engine is not trusted to respect the mask. Results are recomposited on the CPU —
`result = where(mask > 127, edited, original)` — so pixels outside the mask are byte-identical to
the source. `protected_pixels` / `redrawn_pixels` are reported for auditing.

The file at `files[].path` **is** the protected image; the engine's unguarded output is kept
beside it as `*_raw.png` and exposed as `files[].raw` (plus `recomposed[].raw`). `files[].protected`
is `true` when recomposition succeeded. Only hand `files[].path` to the user — `_raw` exists for
"what did the engine actually paint" debugging.

```bash
# repaint a region
python scripts/locmodel_image.py edit --image ./output/v1.png \
  --prompt "night-time glass curtain wall, interior lights on" \
  --region 0.25,0.18,0.62,0.55 --strength 0.8

# repaint using an exported mask
python scripts/locmodel_image.py edit --image ./output/v1.png --mask ./output/mask.png \
  --prompt "oak slat ceiling"

# same composition, watercolour
python scripts/locmodel_image.py redraw --image ./output/v1.png \
  --prompt "watercolour architectural sketch, loose ink lines, white paper" --strength 0.65
```

The source image is never overwritten; each edit lands in a new file so variants can be compared.

---

## Connect as an MCP server

The server speaks **newline-delimited JSON-RPC 2.0** over stdio (it also accepts LSP-style
`Content-Length` framing, for clients that prefer it). Protocol versions `2025-06-18`,
`2025-03-26` and `2024-11-05` are negotiated.

### Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "locmodel-image": {
      "command": "python",
      "args": ["C:/tools/locmodel-image-MCP/scripts/locmodel_mcp.py"],
      "env": { "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1" }
    }
  }
}
```

On Windows use the **absolute path of a real interpreter** (for example
`C:/Users/you/AppData/Local/Programs/Python/Python312/python.exe`); the bare `python` may resolve
to the Microsoft Store stub, which silently exits.

### Marvis

```json
{
  "name": "locmodel-image",
  "config": {
    "command": "C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
    "args": ["C:\\tools\\locmodel-image-MCP\\scripts\\locmodel_mcp.py"],
    "env": { "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1" }
  }
}
```

### Any other client

Point it at `scripts/locmodel_mcp.py` with stdio transport. Pin the port if discovery is
unreliable on your machine:

```json
"env": { "LOCMODEL_SD_PORT": "8790", "PYTHONIOENCODING": "utf-8" }
```

### Tool surface

| Tool | Arguments | Returns |
| --- | --- | --- |
| `generate_image` | `prompt` (required), `negative_prompt`, `width`, `height`, `aspect`, `steps`, `sampler`, `seed`, `count`, `output_dir`, `filename_prefix`, `model`, `timeout_s` | Image paths, effective seed, final size, elapsed time |
| `locmodel_status` | — | Engine state, port + how it was found, model, engine defaults, GPU memory |
| `edit_image` | `init_image` (required), `prompt` (required), `mask_image` \| `regions`, `strength`, `negative_prompt`, `model`, `steps`, `sampler`, `cfg_scale`, `seed`, `mask_blur`, `inpaint_padding`, `channel`, `out_dir`, `filename_prefix`, `verify`, `dry_run` | Edited image paths, mask stats, `selection` (model actually used), `verify` block |
| `redraw_image` | `init_image` (required), `prompt` (required), `strength`, + the same optional set | Redrawn image paths, `selection`, `verify` |
| `list_edit_models` | — | Models with family/rank, which can edit regions, and the automatic pick |
| `load_model` | `name` (required), `timeout` | Loads/switches the engine model, verifies it took effect |
| `list_models` | — | Models reported by the engine |
| `engine_doctor` | — | Full self-check with pass/fail per item |
| `engine_discover` | — | Force a port re-scan and report the result |

`generate_image` returns `isError: true` with a human-readable `hint` on failure, so the agent can
act on it instead of retrying blindly.

---

## Install the companion Skill

`SKILL.md` is the prompt-side counterpart of the MCP server: it tells an agent *how to write
prompts*, which aspect ratio suits which subject, which parameters matter, and how to interpret
each failure — knowledge a JSON schema cannot carry.

The two are complementary, not alternatives:

- **MCP** = the hands (typed tool, always available)
- **Skill** = the craft (prompt structure, presets, parameter judgement)

Install by copying this repository (or just `SKILL.md`) into your client's skills directory:

```bash
# Claude / generic agent skills directory
cp -r . ~/.claude/skills/locmodel-image

# Marvis (Windows)
xcopy /E /I . "%APPDATA%\Tencent\Marvis\User\<your-id>\skills\custom\locmodel-image"
```

Skills that support front-matter (`name`, `description`, `version`, `license`) will pick the
metadata up automatically.

---

## CLI reference

```
locmodel_image.py [--config PATH] [--verbose] [--log-file PATH] <command> [options]
```

| Command | Purpose |
| --- | --- |
| `status` | Engine port (and detection source), model, engine defaults, GPU memory |
| `discover` | Force re-discovery, print port + source |
| `models` | List models the engine reports |
| `doctor` | Environment self-check; exit code reflects health |
| `generate` | Submit a job and wait for the image |
| `edit` | Repaint a masked region of an existing image |
| `redraw` | Re-draw a whole image from a new prompt |
| `edit-models` | Models that can edit, their priority, and the automatic pick |
| `load-model` | Load or switch the model inside the engine |
| `config show\|get\|set\|path` | Inspect or modify configuration |

### `generate` options

| Option | Default | Notes |
| --- | --- | --- |
| `--prompt` | required | Non-empty string |
| `--negative` | `""` | Negative prompt |
| `--width` / `--height` | engine default | Positive integers; corrected by the guard |
| `--aspect` | — | `1:1 4:3 3:4 3:2 2:3 16:9 9:16 21:9 2:1 1:2`; mutually exclusive with `--width/--height` |
| `--steps` | engine default | Higher is slower with diminishing returns |
| `--sampler` | engine default | e.g. `euler`, `dpm++2m` |
| `--cfg` | engine default | Turbo/distilled models usually want 1.0–1.5 |
| `--seed` | `-1` (random) | Fix it to reproduce |
| `--count` | `1` | Capped by `generate.max_count` (default 4) |
| `--model` | — | Override model name on multi-model engines |
| `--out` | config `output.dir` | Output directory (created if missing) |
| `--prefix` | `locmodel` | Filename prefix |
| `--timeout` | `generate.timeout` | Seconds per job |
| `--retry` | `generate.retry` | Retries with a new seed on transient failure |
| `--no-guard` | off | Send the requested size verbatim |
| `--dry-run` | off | Print the HTTP payload and exit — no GPU time spent |

### `edit` options

| Option | Default | Notes |
| --- | --- | --- |
| `--image` | required | Source image (never modified) |
| `--prompt` | required | What should appear *inside* the mask |
| `--mask` | — | Mask PNG, white = repaint. Wins over `--region` |
| `--region` | — | `x1,y1,x2,y2`, repeatable. Coordinates follow `--unit` |
| `--unit` | `norm` | `norm` (0–1) or `px` |
| `--invert` | off | Invert the generated mask |
| `--strength` | `0.75` | Denoising strength; raise to repaint more freely |
| `--mask-blur` | `8` | Feather the mask edge to avoid seams |
| `--padding` | `32` | Grow the mask before the engine sees it |
| `--channel` | `auto` | `img_gen` (async) → `img2img` (sync) fallback |
| `--model` | auto | Exact model; error rather than substitution |
| `--verify` | `agent` | Re-read the output and check it |
| `--dry-run` | off | Print the payload (no engine contact, no GPU time) |

`redraw` takes the same options minus the mask ones, and defaults to `--strength 0.6`.

### Exit codes

| Code | Name | Meaning |
| --- | --- | --- |
| 0 | `ok` | Success |
| 1 | `generic_error` | Unexpected error (network, filesystem, JSON) |
| 2 | `generation_failed` | Engine accepted the job but produced no image |
| 3 | `engine_not_found` | No reachable engine after all discovery layers |
| 4 | `bad_arguments` | Invalid input (empty prompt, `count` over limit, `width=0`, unknown aspect, …) |

Scripts can branch on the code; agents can branch on the `error` field in the JSON body.

---

## Configuration reference

Location (first match wins):

1. `--config <path>`
2. `LOCMODEL_IMAGE_CONFIG` environment variable
3. `~/.locmodel-image/config.json` (created on first `config set`)

Resolution order for the port:

1. `LOCMODEL_SD_PORT` environment variable
2. cached value in `config.json`
3. command line of a running `sd-server` process (most reliable)
4. scan of `port_candidates`
5. scan of local listening ports

Keys (defaults shown):

```jsonc
{
  "port": null,                        // cached port; null = auto-discover every run
  "host": "127.0.0.1",
  "port_candidates": [8790, 8791, ...],// fallback scan list
  "engine": {
    "process_names": ["sd-server", "sd-server.exe", "sd.exe", "stable-diffusion"],
    "capabilities_path": "/sdcpp/v1/capabilities",
    "img_gen_path": "/sdcpp/v1/img_gen",
    "jobs_path": "/sdcpp/v1/jobs",
    "models_path": "/sdapi/v1/sd-models",
    "status_path": "/sdcpp/v1/models/status",
    "probe_timeout": 1.5
  },
  "size": {
    "min_area": 589824,                // 768x768 — below this, engines fail
    "multiple": 8,                     // align edges to this
    "max_side": 1536,                  // auto-shrinks with VRAM when max_side_auto
    "max_side_auto": true,
    "hard_max_side": 2048
  },
  "generate": {
    "timeout": 900,
    "retry": 2,
    "poll_interval": 2.0,
    "max_count": 4,
    "max_download_mb": 64,
    "lock": true,
    "lock_wait": 900
  },
  "edit": {
    "priority": ["qwen-image-edit", "flux2-klein", "qwen-image"],
    "prefer_loaded": true,             // keep the loaded model, don't swap for a better rank
    "auto_load": true,                 // load the best editable model when none is loaded
    "auto_load_timeout": 240,
    "channel": "auto",                 // img_gen -> img2img fallback
    "strength": 0.75,                  // region edit
    "redraw_strength": 0.6,            // whole-image redraw
    "timeout": 900,
    "mask_blur": 8,
    "inpaint_padding": 32,
    "verify": "agent",
    "models_dirs": [],                 // extra folders to scan when the engine reports none
    "load_endpoint": null,
    "resolved_channel": null,
    "engine_cache_ttl": 20,            // seconds; in-process memo
    "snapshot_cache_ttl": 120          // seconds; the snapshot survives processes
  },
  "discover": {
    "process_cache_ttl": 20,           // reuse the process command lines
    "budget_seconds": 20,              // hard ceiling for all port probing
    "listen_budget_seconds": 4         // ceiling for the netstat leftover sweep
  },
  "gpu": { "min_free_mb_warn": 500 },
  "output": { "dir": "./output" }
}
```

Useful recipes:

```bash
locmodel_image.py config set engine.probe_timeout 3      # slow / remote machine
locmodel_image.py config set generate.max_count 2        # 6 GB VRAM users
locmodel_image.py config set size.max_side_auto false    # pin the ceiling yourself
locmodel_image.py config set generate.lock false          # single-process only
locmodel_image.py config set edit.prefer_loaded false     # always swap to the top-priority model
locmodel_image.py config set edit.priority '["flux2-klein","qwen-image-edit"]'
locmodel_image.py config set edit.models_dirs '["F:/AI-Models"]'  # scan when the engine is idle
```

---

## Sizing and VRAM strategy

The guard protects against the two failure modes that produce misleading errors:
**too few pixels** (`generate_image returned no results`) and **too many pixels** (OOM / workspace
capacity check, which on Windows can surface as `ucrtbase.dll 0xc0000409 / BEX64`).

Rules applied in order:

1. `width`/`height` (or the aspect preset) is normalised to `size.multiple`
2. total area below `size.min_area` → scaled up proportionally (aspect ratio preserved)
3. longest edge beyond the effective ceiling → scaled down proportionally
4. the effective ceiling is `size.max_side`, lowered automatically from measured VRAM when
   `max_side_auto` is on, never above `hard_max_side`

Practical presets:

| VRAM | Suggested | Notes |
| --- | --- | --- |
| 4–6 GB | 768×768, 1024×576, `count`=1 | keep `generate.retry` ≥ 1 |
| 8 GB | 1024×1024, 1280×720, `count`≤2 | the tested sweet spot for turbo models |
| 12 GB+ | 1536×864, 1536×1536 | raise `size.max_side` explicitly |

One more habit that prevents most mysterious failures: **keep exactly one image engine resident**.
Running LocModel plus an LM Studio model plus a second diffusion engine on the same GPU is the
classic cause of `workspace capacity check` crashes. When switching models inside LocModel, restart
LocModel so the previous engine releases VRAM.

---

## How it works

```
┌──────────────┐   MCP stdio   ┌──────────────────┐   HTTP    ┌──────────────┐
│ AI client    │ ────────────► │ locmodel_mcp.py  │ ────────► │              │
│ (Claude,     │               │  or              │  /sdcpp   │  sd-server   │
│  Cursor,     │ ◄──────────── │ locmodel_image.py│ ◄──────── │ (LocModel)   │
│  Marvis)     │  JSON result  └──────────────────┘  polling  └──────────────┘
└──────────────┘                       │
                                       ▼
                              PNG + _meta.json on disk
```

Job flow: `POST /sdcpp/v1/img_gen` → poll `GET /sdcpp/v1/jobs/<id>` (`queued` → `running` →
`succeeded`) → decode `result.images[].b64_json` (or fetch a loopback-only URL, up to
`max_download_mb`) → write PNG and metadata.

Failures are classified rather than blindly retried:

| Class | Trigger | Action |
| --- | --- | --- |
| `capacity` | `no results`, workspace/allocation errors | retry with the same prompt, shrunk ×0.85 |
| `parameter` | HTTP 400 / invalid argument | no retry — the request itself is wrong |
| `transient` | timeouts, connection resets | retry as-is |

A cross-process lock in `~/.locmodel-image/generate.lock` serialises generations, because two jobs
competing for one GPU is the most common cause of capacity failures. Stale locks from dead PIDs are
reclaimed immediately; locks older than `lock_wait + 600 s` are reclaimed as expired.

---

## Testing

```bash
# Unit tests — no engine required
python -m pytest tests/test_core.py -q

# End-to-end matrix — needs a live engine
python tests/e2e_matrix.py --stage core      # status / models / doctor / dry-run
python tests/e2e_matrix.py --stage gen       # real generations, aspect presets, batch, guard
python tests/e2e_matrix.py --stage diag      # bad arguments, missing engine, config round-trip
python tests/e2e_matrix.py --stage mcp       # MCP handshake, framing, tool calls
python tests/e2e_matrix.py --stage all       # everything (slow: ~10 min on 8 GB)
```

Each stage writes a JSON report and prints a pass/fail summary. Stages are split so a flaky GPU
session cannot hide which part broke.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `engine_not_found` (exit 3) | LocModel closed, no model loaded, or unusual port | Start LocModel and load a model → `discover`; or set `LOCMODEL_SD_PORT` |
| `generation_failed` + `no results` | Not enough free VRAM, or two engines competing | Close LM Studio / browser hardware acceleration; one engine only; lower size or `count` |
| Job stuck in `queued` | Engine has a single slot and is busy | Wait; check no orphaned generator process holds the lock |
| Generation > 3 min | Size too large, steps too high, GPU shared | Lower size, restore default steps, free the GPU |
| Prompt seems ignored | CFG too low for a distilled model | Try `--cfg 1.5`; accept that turbo models follow prompts loosely |
| Text renders as gibberish | Diffusion models cannot spell | Do not fight it — overlay text in PPT/PS/Figma |
| MCP client shows no tools | Wrong interpreter, or Windows Store `python` stub | Use the absolute interpreter path; set `PYTHONIOENCODING=utf-8` |
| `workspace capacity check` / `BEX64` crash | Two engines resident; VRAM exhausted | Restart LocModel so a single engine owns the GPU |

Run `doctor` first for anything odd — it reports port source, model, GPU free memory and output
directory writability in one shot.

---

## Compatibility and limitations

- **API surface**: targets the `/sdcpp/v1/*` API of stable-diffusion.cpp servers. It is *not*
  Automatic1111-compatible (`/sdapi/v1/txt2img` is not used). Paths are configurable, so a fork
  with slightly different routes can be adapted via `config set engine.*_path`.
- **Discovery is heuristic**: on machines where several engines run simultaneously, pin the port
  explicitly. Guessing wrong is silent otherwise, which is why `status` always reports *how* the
  port was found.
- **No model management**: loading/unloading, model switching and VRAM budgeting remain LocModel's
  job. This tool never kills processes or unloads models on your behalf.
- **Not implemented**: img2img, inpainting, upscaling, video generation. The engine advertises
  some of these, but they are unverified here — they are deliberately absent rather than
  half-working.
- **Text in images**: a limitation of the models, not of the bridge.

---

## What changed in 3.0

- **`edit_image` / `redraw_image`** — prompt-driven editing on an existing image, without
  downloading a dedicated inpainting checkpoint. The engine's existing `init_image` + `mask_image`
  path is used; masks are built on the CPU with the standard library only.
- **Mask outside the mask is guaranteed** — results are recomposited on the CPU against the
  original, so "keep everything except this area" is a real guarantee rather than a hope.
- **Model priority without silent surprises** — `edit.priority` plus `edit.prefer_loaded` /
  `edit.auto_load`, and an explicit error instead of a substitution when the requested model cannot
  edit.
- **`list_edit_models` / `load_model`** — see which models can edit, why one was chosen, and load or
  switch models without leaving the agent.
- **`annotator/mask_annotator.html`** — a single-file browser mask painter (rect / polygon /
  brush) that exports a mask PNG or normalised JSON.
- **Faster probes** — engine discovery is cached (`edit.engine_cache_ttl`), so a closed engine no
  longer costs a scan on every call.
- Still **zero dependencies**: standard library only, Python 3.8+.

## What changed in 2.0

See [CHANGELOG.md](docs/CHANGELOG.md) for the full list, and [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md) for every hard-coded assumption that had to go before this could run on someone else's machine. Highlights:

- No hard-coded user paths, ports, resolutions, samplers or Python installs
- Area-based size guard with VRAM-aware ceiling and `--aspect` presets
- Failure classification (`capacity` / `parameter` / `transient`) with proportional shrink on retry
- Cross-process generation lock with stale-lock reclamation
- `--dry-run`, `--no-guard`, `--log-file`, `--config`, `config get/set/path`
- Stable exit codes; `expects_error` handling instead of crashes
- MCP: protocol negotiation, dual framing (newline + `Content-Length`), `engine_doctor` /
  `engine_discover` tools, `isError` with actionable hints
- Tests: 46 unit tests + staged e2e matrix

---

## License

MIT — see [LICENSE](LICENSE).
