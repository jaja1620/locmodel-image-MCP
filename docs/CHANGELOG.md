# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [4.1.0] — 2026-11-27

Synced with LocModel 3.6.5. The engine had grown a reference-image channel that this bridge never
exposed, so "keep this person, change the hair" was impossible from an agent: the only edit paths
were `init_image` (which re-renders the subject and quietly substitutes a different face) and
`redraw` (which does not keep identity either). 4.1 exposes the channel the engine already has, and
makes a *silent* downgrade to text-to-image visible in the result.

### Added

- **`ref_edit` tool** — identity-preserving edit: you pass one or more reference images plus a
  prompt that describes only the change, and the engine anchors on the references instead of
  re-rendering from noise. Same engine layer as `generate_image` (port discovery, lock, size guard,
  meta sidecar, dry-run), but a separate tool so an agent does not have to remember which argument
  combination switches channels. Tool surface: 19.
- **`ref_images` / `ref_fit` / `ref_max_pixels` on `generate_image`** — the same capability for
  callers that already hold a prompt; giving `ref_images` selects the reference channel.
  - `ref_images`: 1–8 paths, `data:` URIs or bare base64; local files are read, sniffed by magic
    bytes and re-encoded as a data URI (`max_mb` 8 each). Mixed sources are fine.
  - `ref_fit`: `follow_first` (default) derives the canvas from the first reference within the
    pixel budget; `image` keeps your explicit `width`/`height`.
  - `ref_max_pixels`: int, 65536–4194304, default 1048576 (1024²). The 4.0 guard's 550k ceiling
    was sized for the 3.2-era engine; 3.6.5 renders 1.5 MP fine, so the ceiling moved up while the
    default stayed conservative.
- **Engine read-back** (`engine_trace` in every result, plus the `engine:` summary line): the
  engine's own `generation_info` is parsed for the built signature (`fn main(reference_0)`) and the
  predictor call (`ref_latents = [reference_latent_0]`). A reference edit that silently degraded to
  text-to-image is now a reported fact, not a picture the user has to spot by eye.
- **`engine_discover` / `locmodel_status`** report `recipe`, `size_multiple`, `ref_capable` and the
  recipe probe; `engine_doctor` gained `reference_images` and `recipe` checks.
- **Workflow parameter surface grew to 53 keys**: `ref_images` / `ref_fit` / `ref_max_pixels`,
  `cache_mode` / `cache_option`, `qwen_image_layers`, `scm_mask` / `scm_policy_dynamic`,
  `control_image` / `control_strength` / `control_type`. Array coercion (`min_items`/`max_items`)
  was added so list-valued parameters validate client-side like scalars.
- **`features_by_mode` and `runtime.runtime_recipe` are now honoured** — capability adoption reads
  the per-mode feature map and the live recipe's `effective_features` and `size_multiple`, so
  support is judged against the mode actually active rather than the union of all modes.
- **`size_multiple`-aware sizing** — the old hard-coded 16-pixel alignment is now read from the
  recipe (16 on flux-2-klein), so a non-multiple canvas is aligned the way the engine will align it
  instead of being refused.
- **CLI**: `--ref-image` (repeatable) / `--ref-fit` / `--ref-max-pixels` on `generate`, plus a
  `ref-edit` sub-command.

### Changed

- On the reference channel `init_image` and `strength` are stripped from the payload and reported
  in `notes`: `strength` has no effect there (the engine slices no sigmas), and passing `init_image`
  alongside references is how the "different face" failure happens. `negative_prompt` is kept but
  reported as not taking effect. `steps > 12` is kept and flagged.
- `ref_strength` is rejected with a pointer to `redraw_image`, rather than being accepted and
  ignored.

### Fixed

- A singular `ref_image` is rejected explicitly (`bad_arguments`) instead of being dropped by the
  engine, which built `fn main()` with no reference latents and returned a plain text-to-image
  picture that looked like a successful edit.

### Verified on this machine (LocModel 3.6.5, flux-2-klein-4b Q4_0, RTX-class GPU)

- single reference 1152×896, 12.2 s → `fn main(reference_0)`, one bound latent
- two references 1152×896, 32.7 s → `fn main(reference_0, reference_1)`, two bound latents
- no reference 768×768, 12.6 s → `fn main()`, no latent, reported as plain text-to-image
- canvas derivation respects the budget (`1152×896`, aspect 1.286) and `ref_fit="image"` keeps a
  caller-supplied size
- `pytest tests -q`: 150 passed, 0 failed
- not exercised here: `generate_video` and `lipsync_*` (no video weights mounted on this machine),
  and the new P1 workflow keys (`control_image`, `scm_*`, `cache_*`) — they are passed through and
  feature-gated, but were not rendered.

## [4.0.0] — 2026-11-24

Synced with LocModel 3.6.1. LocModel's "视频" tab holds **two unrelated sub-panels**; 4.0 covers
both, and keeps them apart in the tool surface so an agent can no longer answer a lip-sync request
with text-to-video.

### Added

- **`generate_video` / `job_status` / `cancel_job` / `list_recipes`** — the "视频 → 生成视频" sub-panel,
  i.e. the engine's `POST /sdcpp/v1/vid_gen`. Asynchronous by default (`wait_seconds=0` returns a
  `job_id` and `poll_url` immediately) because a 480P clip takes minutes and MCP clients time out;
  `job_status` polls, then decodes `result.b64_json` and writes webm/webp/avi to disk. Format is
  sniffed from RIFF/EBML magic bytes first, so a mislabelled `output_format` cannot corrupt the file.
- **`lipsync_status` / `lipsync_generate`** — the "视频 → 对口型视频" sub-panel: LocModel's built-in
  InfiniteTalk + Wan2.1-I2V-14B-480P pipeline. This is *not* an HTTP endpoint: LocModel writes a
  `runner.py` into its data directory and launches it inside its own Python 3.12 venv, with its own
  ~24 GB weight tree and offline HF flags. The tool locates those artifacts, reports exactly which of
  the 13 required pieces are missing, and runs the job in a subprocess.
- **`scripts/lipsync_runner_embed.py`** — an embedded copy of the runner LocModel writes, so the
  pipeline can still be driven on a machine where the GUI never ran it.
- **CLI pass-through**: `locmodel_image.py video <status|generate|job|cancel|recipes>` and
  `locmodel_image.py lipsync <status|run>`, forwarded before argparse so each bridge owns its parser.
- **`tests/test_video.py`** — 23 tests covering payload building, data-URI wrapping, mode gating,
  async submit/poll/save, magic-byte format detection and the lip-sync readiness matrix.
- **Third install route: from the repository.** `install/` now holds a machine-readable payload
  contract (`manifest.json`: every file with SHA-256 and size, entrypoints, verification commands,
  pinned ref; `SHA256SUMS` in `sha256sum -c` format; `hosts.json` for the host → directory table) and
  the installer that consumes it — `install-from-repo.ps1` (Windows) and `install-from-repo.sh`
  (Linux/macOS). Both deploy the MCP server to `%LOCALAPPDATA%\locmodel-image\repo` (or
  `${XDG_DATA_HOME:-~/.local/share}/locmodel-image/repo`) and `SKILL.md` into the host's skills
  directory, verify every hash before writing, support `manifest` / `zip` / `local` sources
  (so it works offline from the release asset), write a `locmodel-image.repo-install-report/1`
  report, and finish with a real MCP `initialize` handshake. Host config files are only touched on
  request, and always after a `.bak-<timestamp>` copy. The route is additive: it never modifies an
  existing `locmodel-image-setup.exe` install.
- **`install/AGENT-INSTALL.md`** — the install spec written for an AI agent rather than a person:
  three paths (run the installer / fetch the 8 payload files itself / hand a checklist to the human),
  exact URLs, per-OS destinations, the hash table, two verification commands, the per-host config key,
  a symptom → meaning → action table, and an explicit list of things an agent must not touch.
- **`docs/INSTALL-FROM-REPO.md` / `docs/INSTALL-FROM-REPO.zh-CN.md`** — the human-facing guide for
  the same route: flag tables, exit codes, offline install, registration snippet, verification,
  uninstall, and how it coexists with the exe tree.
- **Release asset `locmodel-image-repo-install-v4.0.0.zip`** — `scripts/` + `SKILL.md` + `install/` +
  `docs/` in one file, for machines with no network at install time
  (`-Source local` / `--source local`).

### Changed

- **Sub-panel routing is now documented policy, not a guess.** `SKILL.md` maps user wording to the
  right sub-panel ("生成视频 / 文生视频 / 图生视频" → `generate_video`, "对口型 / 让照片唱歌 / 数字人播报"
  → `lipsync_generate`), lists the models and runtime each one needs, and records the on-machine
  evidence (LocModel 3.6.1 program strings plus the `runner.py` it writes) behind those claims.
- **Tool count 12 → 18, parameter contract 42 → 50.** No existing tool was renamed or re-scoped.
- **`pyproject.toml`**: version 4.0.0, module list now covers every bridge module.

### Fixed

- **省内存版的就绪判定不再把 `t5_fp8` 当必需件。** 该档真实布局是
  `split_files/vae/wan_2.1_vae.safetensors`（小写、不在 `base/` 下）+ `umt5-xxl-encoder-Q8_0.gguf`
  文本编码器，没有 `t5_fp8.safetensors` / `t5_map_fp8.json`。原实现按高质量档的文件名硬判，
  small 档会被误报为 `missing: umt5/xlm_roberta` 而永远 `ok=false`。现在文本编码器两档任一即算就绪，
  tokenizer 按路径独立探测，`base` 只作 runner 拼相对路径的锚点并允许三级回退。
- **`lipsync_status(models_dir=...)` 的覆盖生效了。** 原先环境变量/配置优先级高于参数，导致显式传入的
  模型目录被静默忽略，`models_root` 回显的是程序侧默认路径。
- **冻结 exe 里 RAM 探测不再静默失手**：`_ram_gb()` 依次尝试 psutil → `GlobalMemoryStatusEx` → PowerShell `Win32_ComputerSystem`，全部失败时把原因写进 `hardware.ram_error`，避免 auto 档位在拿不到内存时无声退化为省内存版而误导诊断。
- **CLI 可发现性**：`locmodel_image.py --help` 现在列出被提前转发的 `video` / `lipsync` 两组子命令
  （原先它们绕开 argparse，`--help` 里完全看不到）；新增 `lipsync help` 打印用法与"未挂载即整体失败"的硬约束。
- **MCP 的 `lipsync_status` 返回精简**：去掉同名 verbose 事实表，只留 agent 可行动的字段
  （`ok` / `variant` / `models` / `missing` / `paths` / `hardware` / `mount_required` / `mount_hint` / `blocking`）。
- **仓库安装的哈希与分发内容对齐了。** `install/manifest.json` 原先对工作区里的 CRLF 副本求 SHA-256，而 `raw.githubusercontent.com` 与 release 里的 zip 分发的是仓库中的 LF blob，于是 `-Source manifest` 下载下来的每个脚本都会校验失败、安装无法完成。现在 `files[].sha256` / `files[].bytes` 一律按 git blob 计算，`SHA256SUMS` 与 `AGENT-INSTALL.md` 里的示例哈希同步重算；`manifest` / `zip` / `local` 三种来源的校验结果一致。

### Known constraints

- **Lip sync fails hard when the weights are not installed** (`lipsync_not_mounted`). This is by
  design: there is no fallback to `generate_video`, no cloud call, and no attempt to install weights
  behind the user's back. The only fix is LocModel → 视频 → 对口型视频 → 一键准备.
- **Image and video instances are mutually exclusive.** One engine instance serves one mode, so
  image generation fails while a `vid_gen` instance owns the GPU.
- **`ref_audios` / `ref_video_audios` remain unusable** on the 3.6.1 engine build: any value is
  rejected with 400. They are passed through only when a caller supplies them, and are documented as
  experimental.
- **Video results expire**: the engine keeps completed jobs for ~600 s (`--completed-ttl-seconds`),
  so a `job_id` must be polled promptly.

## [3.2.0] — 2026-11-18

Synced with LocModel 3.3.0 / sd-server. The 3.1 contract assumed a fixed engine and hard-coded
what it could have asked for; 3.3.0 advertises more (`features`, `limits`, `defaults_by_mode`,
`upscalers` as objects) and spells some enumerations differently, which turned one of those
assumptions into a hard bug.

### Fixed

- **Latent upscalers were unusable.** The tool hard-coded `Latent(nearest)`; the engine spells it
  `Latent (nearest)` and answers `400 invalid generation parameters` for the space-less form. All
  five Latent variants now match, and `adopt_capabilities()` overwrites the enumeration from the
  live `upscalers` list so a future rename cannot break it again.
- **Explicit sizes were silently rewritten.** `--width 512 --height 512` came back as 768×768 and
  anything above 1536 was pulled down. Two size tiers now exist: a caller-supplied width/height is
  honoured (only the engine's `limits` may clamp it, with a warning), while tool-chosen sizes keep
  the old area floor / VRAM-tiered long edge.
- **Fallback defaults contradicted the engine.** The tool fell back to `cfg 7.0 / steps 20`; the
  shipped flux model runs `cfg 1.0 / steps 4 / euler`. `FALLBACK` now mirrors the engine, and
  `defaults_by_mode` is read when `defaults` is absent.
- `count` was capped at 4 while the engine accepts 8; the ceiling now comes from
  `limits.max_batch_count`.
- Enum matching is now whitespace/punctuation-insensitive, so `latent(nearest)` and `JPEG` are
  accepted and normalised to the engine spelling instead of being rejected.

### Added

- `adopt_capabilities()` / `engine_limits()` / `feature_enabled()` / `unsupported_keys()` in
  `locmodel_workflow.py` — live enumerations, limits and features replace the offline baseline;
  requesting a knob the loaded model cannot honour produces a note instead of a silent no-op
- New parameters: `hires_scale` (multiplier upscale), `hires_custom_sigmas`, `output_format`
  (`png` / `jpeg` / `webp`), `output_compression`, `vae_rel_size_w` / `vae_rel_size_h`,
  `vae_extra_tiling_args` — 42 parameters total
- `guard` and `dry_run` are now first-class MCP arguments on `generate_image`, so a caller can
  inspect the exact payload (`size_policy` included) before spending GPU time
- `locmodel_status` reports `limits`, `features`, `current_mode`, `supported_modes` and the
  engine-adopted enumerations
- `get_workflow_params` marks rows the loaded model cannot use (`unavailable_reason`) and accepts
  `include_folded=true`
- Tests: `CapabilityAdoptTests` (10 cases), explicit-size cases in `test_core.py`, and rewritten
  e2e matrix coverage (`cli.size_policy`, `cli.new_params`, `gen.explicit_640`, `gen.jpeg_output`,
  `cli.explicit_512`)

### Changed

- `size.hard_max_side` default raised 2048 → 4096 to match `limits.max_width/height`
- `generate.max_count` default raised 4 → 8
- `Latent(nearest)`-style spellings still work for upscalers via fuzzy matching, but the engine's
  canonical spacing is what gets sent

### Installer

- `locsetup.payload.PAYLOAD_FILES` was missing `scripts/locmodel_workflow.py` since 3.1.0, so an
  exe built from 3.1/3.2 sources would have installed a payload that cannot import its own core.
  Added as a mandatory file. The gap was the list, not the check: `stage()` already aborts with
  `payload incomplete` and `verify()` already flags mandatory files at error level.
- `installer/locsetup/__init__.py` reported `3.0.0`; it now tracks `pyproject.toml` (3.2.0), so
  `install-manifest.json` and `VERSION` no longer claim an older release.
- `locsetup/runtime_deps.py` puts the payload's stdlib imports (`urllib.error`, `http.client`,
  `ssl`, ...) into the PyInstaller dependency graph. Without it a frozen `--serve` started fine
  and answered `tools/list`, then failed every call with `Core module locmodel_image.py could not
  be imported: No module named 'urllib.error'`.
- Build no longer sweeps `scripts/__pycache__/*.pyc` into the payload; `installer/README.md` now
  documents the exact PyInstaller command and the three failure modes above.
- Verified end to end on the frozen build: sandbox install → `--version 3.2.0` → 12 tools →
  `locmodel_status` against a live 8790 engine → `generate_image` 640×640 (explicit size honoured)
  → PNG written.

### Notes

- `generate_video` is still deferred: the engine advertises no video mode in `supported_modes`.

## [3.1.0] — 2026-11-12

Workflow parameters reach the engine. Everything the engine accepts in `sample_params`,
`hires`, `vae_tiling_params`, `high_noise_sample_params` and `lora` can now be set from the tool,
and nothing is written to `config.json`: the LocModel GUI owns that file and rewrites it from its
own panel state, so the HTTP payload is the only channel that survives.

### Added

- `scripts/locmodel_workflow.py` — the parameter contract: alias table, type/range validation,
  live-default discovery from `/sdcpp/v1/capabilities`, three-layer resolution, payload injection
- `params` argument on `generate_image` (MCP) and `--param key=value` on `generate` (CLI)
- `set_workflow_params` / `get_workflow_params` / `reset_workflow_params` (MCP) and
  `params list | show | set | reset` (CLI) — session scope lives in process memory and dies with it
- `edit_image` / `redraw_image` inherit session parameters, so one `set_workflow_params` covers a
  whole batch of edits
- Live defaults are printed with a `source` column: `engine` (advertised), `tool` (fallback),
  `session` (overridden), `unset`
- `tests/test_workflow.py` — 16 cases covering aliases, coercion, rejection, payload paths,
  precedence, session lifetime and the documented core-key band

### Fixed

- `generate_image` accepted `steps` and `sampler` but silently dropped `cfg`; it is wired through
- Illegal values are rejected client-side with `bad_arguments`. The engine answers `202 queued` for
  junk (`scheduler="bogus"`, `steps="abc"`) and then hangs the job, so validation cannot be left to
  the server.

### Notes

- Parameter table is deliberately small (14 core keys, folded `hires` / `vae_tiling` /
  `high_noise` / `slg` / raw groups), and everything is omitted from the payload unless asked for.
  A session that never touches a parameter behaves exactly as before: the GUI panel decides.
- `generate_video` is deferred to 3.2.

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
