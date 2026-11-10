# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [3.0.0] — 2026-11-10

Image editing on top of text-to-image. No new model download, no new dependency: the engine's
existing `init_image` / `mask_image` path is driven from the CLI and MCP, and every mask is built
on the CPU with the standard library.

### Added

- `edit_image` (MCP) / `edit` (CLI) — repaint only a masked region of an existing image
- `redraw_image` (MCP) / `redraw` (CLI) — re-draw a whole image from a new prompt, keeping the
  source as a structural reference
- `list_edit_models` (MCP) / `edit-models` (CLI) — models with family, rank, region-editing
  capability, and the automatic pick, plus actionable `notes` when nothing is installed
- `load_model` (MCP) / `load-model` (CLI) — load or switch the engine model and verify the switch
  by re-reading the current model
- Three mask sources: a mask PNG (white = repaint), normalised/absolute rectangles and polygons,
  or a colour threshold; `brush` and `circle` shapes are accepted too
- CPU-side recomposition — `result = where(mask > 127, edited, original)` — so pixels outside the
  mask are byte-identical to the source; `protected_pixels` / `redrawn_pixels` are reported
- `annotator/mask_annotator.html` — single-file browser mask painter (rectangle / polygon /
  brush) that exports a mask PNG or normalised JSON
- Engine channel negotiation: `edit.channel = auto` tries the asynchronous `img_gen` endpoint and
  falls back to the synchronous `img2img` one, remembering the winner in
  `edit.resolved_channel`
- Output verification (`edit.verify`) that re-reads produced files and reports dimensions

### Changed

- `edit.priority` orders edit candidates (`qwen-image-edit` → `flux2-klein` → `qwen-image`);
  a model requested by name is used **exactly** or rejected with the candidate list, never
  silently substituted
- `edit.prefer_loaded` (default `true`): an already-loaded editable model is kept instead of
  swapping to a higher-priority one, because a swap costs 30–60 s for a 4B model and can exhaust
  VRAM
- `edit.auto_load` (default `true`): load the best editable model when none is loaded, with a
  timeout and a read-back check
- Model families are matched together rather than first-pattern-wins, so `Qwen-Image-Edit` can
  never be mistaken for plain `Qwen-Image` even under a reordered priority list
- Engine probing was the slowest thing in the tool (190 s on a managed Windows box with no
  engine running) and is now bounded from four sides: `tasklist` decides whether any engine
  process exists before PowerShell is started to read command lines, process command lines are
  memoised for `discover.process_cache_ttl` seconds, `netstat` listeners are used to skip
  candidate ports that cannot answer, and a single snapshot does one `base_url()` probe instead
  of one per query. A cold call is ~6 s, a warm one ~0.3 s (was 190 s / 190 s)
- The model snapshot is persisted to `edit_snapshot.json` next to the config and reused for
  `edit.snapshot_cache_ttl` seconds (default 120), so separate CLI processes do not re-probe;
  `edit-models --refresh` (or `list_edit_models(..., refresh=true)`) forces a fresh probe, and a
  successful `load_model` invalidates the snapshot
- `pyproject.toml` ships `locmodel_edit` alongside the two existing modules; version bumped to
  `3.0.0` throughout (CLI, MCP handshake, installer)
- The default edit output is now the **protected** image: CPU recomposition overwrites the file
  at `files[].path` and the engine's untouched output is kept beside it as `*_raw.png`. Each
  `files[]` entry gained `protected` and `raw`, and `recomposed[]` gained `raw` next to `of`. Before
  this change the default path held the engine output (verified on a real engine: 818 348 pixels
  outside the mask had changed), so an agent that used `files[0]` silently got an unguarded image
- The installer payload now ships `scripts/locmodel_edit.py` (mandatory) and
  `annotator/mask_annotator.html` (optional); before this the installed tree was missing the edit
  module entirely

### Known limitations

- `crop_upscale` (tiled upscaling of the edited crop) is declared but raises `not_supported`
- Dedicated inpainting checkpoints (SD1.5/SDXL Inpainting, FLUX.1 Fill) are intentionally **not**
  required; region editing relies on the edit-capable model families instead
- Pure text editing inside an image remains out of scope

## [2.0.0] — 2026-11-09

First public release. Rewritten for machines that are **not** the author's.

### Added

- `--aspect` presets (`1:1 4:3 3:4 3:2 2:3 16:9 9:16 21:9 2:1 1:2`) as an alternative to explicit
  `--width/--height`
- VRAM-aware size ceiling: `size.max_side` is lowered automatically from measured free memory when
  `size.max_side_auto` is on, and never exceeds `size.hard_max_side`
- Failure classification — `capacity` / `parameter` / `transient` — with proportional shrink
  (`×0.85`) on capacity retries instead of repeating the same doomed request
- Cross-process generation lock (`generate.lock`) with immediate reclamation of locks owned by
  dead PIDs and expiry of locks older than `lock_wait + 600 s`
- CLI flags: `--dry-run` (print payload, spend no GPU time), `--no-guard`, `--log-file`,
  `--config`, `--retry`, `--timeout`
- `config show|get|set|path` subcommands
- Stable exit codes: `0 ok`, `1 generic_error`, `2 generation_failed`, `3 engine_not_found`,
  `4 bad_arguments`
- MCP tools `engine_doctor` and `engine_discover`; every failure returns `isError: true` with an
  actionable `hint`
- MCP protocol negotiation across `2025-06-18`, `2025-03-26`, `2024-11-05`; stdio accepts both
  newline-delimited JSON and LSP-style `Content-Length` framing
- `SKILL.md` companion skill: prompt structure, scene presets, parameter guidance, failure table
- Bilingual documentation (`README.md`, `README.zh-CN.md`) and a staged e2e matrix

### Changed

- **Port discovery** is now five-layered: `LOCMODEL_SD_PORT` → cached config → parsing the
  `sd-server` process command line → candidate-port scan → local listening-port scan. `status`
  always reports *how* the port was found
- **Size guard** now protects by **total pixel area** (`size.min_area`, default 768²) rather than
  by a single edge, and rescales proportionally so the requested aspect ratio survives. This
  follows the measured engine behaviour where 512×512 fails while 1280×720 succeeds
- Engine defaults (`width`, `height`, `steps`, `sampler`, `cfg`, `batch_count`) are read from
  `/sdcpp/v1/capabilities` at runtime instead of being hard-coded
- `--config` now applies to every read **and** write, including the lock file location
- Invalid input is rejected before any GPU work starts (empty prompt, `count` over
  `generate.max_count`, non-positive integers, unknown aspect), returning exit code 4

### Removed

- All hard-coded user paths, model paths, ports, resolutions, sampler names and Python install
  locations from the previous development build
- The Automatic1111 code path (`/sdapi/v1/txt2img`), which never worked against these engines

### Fixed

- Generation could hang behind a stale lock after an interrupted run
- `config set` silently wrote to the default config path when `--config` pointed elsewhere
- Empty or unparsable engine responses surfaced as tracebacks instead of `generation_failed`
- **MCP `Content-Length` framing produced zero responses.** The header/body reader consumed the
  `\r\n` pair one byte at a time and left `\r\n` at the head of the body, so every framed message
  failed JSON parsing. stdin is now read line-by-line, tolerating both framings. Caught by the
  e2e case `mcp.content_length_framing` (was `responses: 0`, now `responses: 2`)
- A single slow poll could abandon a job the engine was still rendering. Poll read timeouts are
  now treated as "keep waiting" until `generate.timeout`
- Giving up on a job left the engine rendering into the void, blocking the next run. The bridge
  now issues a best-effort `POST /sdcpp/v1/jobs/<id>/cancel` and records it in `attempts`
- `gpu.min_free_mb_warn` default was 2500 MB, which fires on every healthy run once a model is
  resident. Lowered to 500 MB and reworded as informational rather than blocking

### Verification (2.0.0)

Run on Windows 10 19045, RTX 3060 Ti 8 GB, Z-Image Turbo Q6_K, engine port discovered as 8790.

| Stage | Cases | Result |
| --- | --- | --- |
| unit (`pytest tests/test_core.py`) | 46 | 46 passed |
| `core` — version, doctor, status, models, dry-run | 5 | 5 passed |
| `diag` — bad arguments, no engine, config round-trip | 3 | 3 passed |
| `mcp` — protocol, Content-Length framing, generate_image | 3 | 3 passed |
| `gen` — 768², 16:9, 640² upscaled, 9:16, batch of 2, `--no-guard` | 6 | 6 passed |

Measured sizes after the guard: 768×768, 1368×768 (16:9), 768×1368 (9:16), 1184×888 (4:3),
with per-image latency 34–53 s. Aspect ratios are preserved in every case.

### Known limitations

- img2img, inpainting, upscaling and video generation are **not** implemented, although the engine
  advertises related endpoints. They are omitted rather than shipped unverified.
- Port auto-discovery is heuristic; on multi-engine machines pin `LOCMODEL_SD_PORT`.
- On 8 GB GPUs with a large text encoder resident, free VRAM can drop below 512 MB in steady
  state. Generation then becomes slow but still succeeds; `gpu.min_free_mb_warn` reports it.

## [1.0.0] — 2026-11-09

Internal baseline: MCP server with `generate_image` / `locmodel_status` / `list_models`, CLI bridge
with `status` / `discover` / `models` / `generate`, `SKILL.md` prompt guide. Hard-coded to one
machine (ports, model paths, resolutions). Kept as a reference tag only.
