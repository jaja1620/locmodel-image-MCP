# locmodel-image

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
- [CLI reference](#cli-reference)
- [Configuration reference](#configuration-reference)
- [Sizing and VRAM strategy](#sizing-and-vram-strategy)
- [How it works](#how-it-works)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Compatibility and limitations](#compatibility-and-limitations)
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

Nothing to build. Clone and run:

```bash
git clone https://github.com/<your-account>/locmodel-image.git
cd locmodel-image
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
      "args": ["C:/tools/locmodel-image/scripts/locmodel_mcp.py"],
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
    "args": ["C:\\tools\\locmodel-image\\scripts\\locmodel_mcp.py"],
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
