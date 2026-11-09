# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

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
