# What had to change to make this publishable — v1.0 → v2.0

v1.0 worked on exactly one machine: mine. Every item below is something that broke
(or would have broken) the moment someone else ran it, plus the fix that shipped in 2.0.
Ordered by how early a new user would hit it.

**Legend** — 「通用性」= portability across machines / GPUs / OSes, 「可靠性」= behaves
correctly when things go wrong, 「接入」= how easily an agent or script can drive it,
「文档」= do I understand it without asking the author.

---

## 1. Things that were hard-coded to one machine (portability)

| # | v1.0 problem | v2.0 fix | Kind |
| --- | --- | --- | --- |
| 1 | Engine port `8790` was written into the code. LocModel picks a free port, and two users never get the same one. | Port is discovered in five ranked steps: `LOCMODEL_SD_PORT` env → cached `config.json` → parse the `sd-server` process command line → probe `port_candidates` (8790–8799, 7860, 7861, 8080, 8188, 5000, 1234, 3000, 8000) → scan locally listening sockets. `engine_discover` prints which step won. | 通用性 |
| 2 | Model file names and directories were assumed (`F:\AI-Models\...\z_image_turbo-Q6_K.gguf`). | Nothing references a model path. Models come from `/sdcpp/v1/models/status`; `list_models` shows what the user actually has. | 通用性 |
| 3 | Width/height/steps/sampler/CFG were fixed at the values that happened to work here (1024×1024, 8 steps, euler, cfg 1.0). | All are read from `/sdcpp/v1/capabilities` as engine defaults; overrides are optional. A user running a non-turbo model gets their model's real defaults. | 通用性 |
| 4 | Python interpreter path was absolute (`...\Python314\python.exe`). | `pyproject.toml` installs a `locmodel-image` console script; MCP config uses `python`/`python3` from PATH, documented per platform. | 通用性 |
| 5 | Output always landed in the session directory. | `output.dir` (default `./output`), plus `--output-dir` per call. | 通用性 |
| 6 | "Min edge 768px" was the sizing rule. Wrong on a 4 GB card (OOM) and wrong for wide images (unnecessary upscale). | Rule is **total pixel area** (`size.min_area`, default 768² ≈ 590k px) with aspect ratio preserved; long edge capped by `size.max_side`, which auto-tunes from free VRAM. | 通用性 |
| 7 | Long edge cap was a constant, so it either OOM'd on small cards or wasted headroom on big ones. | `size.max_side` derives from free VRAM (`max_side_auto`), bounded by `hard_max_side` (2048). Measured here: 1024 → 1368 as the GPU freed up. | 通用性 |
| 8 | No way to ask for a shape, only pixel numbers. | `--aspect` with ten presets: 1:1, 4:3, 3:4, 3:2, 2:3, 16:9, 9:16, 21:9, 2:1, 1:2. | 通用性 |
| 9 | Process discovery used WMI only. | Windows uses `Win32_Process`/`tasklist`, POSIX uses `ps -axo pid,command`; failure to read GPU info degrades to a warning instead of an exception. | 通用性 |

## 2. Things that failed silently or left the GPU busy (reliability)

| # | v1.0 problem | v2.0 fix | Kind |
| --- | --- | --- | --- |
| 10 | Engine returned `generate_image returned no results` and the tool just said "failed". Root cause here was a VRAM workspace allocation failure. | Failures are classified `capacity` / `parameter` / `transient`. `capacity` retries with progressively smaller area (×0.85 per attempt, up to `generate.retry`); `parameter` errors are not retried (retrying a bad sampler forever helps nobody). | 可靠性 |
| 11 | A slow GPU could stall a single poll past the request timeout, and the whole job was abandoned even though the engine was still working. | Poll read timeout is treated as "keep waiting"; the job is followed until `generate.timeout` (default 900 s). | 可靠性 |
| 12 | When the client gave up, the engine kept rendering for minutes. Orphan jobs held the GPU and made the *next* run look broken. | On give-up the bridge cancels the job (`/sdcpp/v1/jobs/<id>/cancel`, best-effort, recorded in `attempts`). | 可靠性 |
| 13 | Two agents (or two terminals) generating at once fought over VRAM and both failed. | Cross-process lock at `~/.locmodel-image/generate.lock` holding `<pid> <iso>`; stale locks are reclaimed when the PID is gone; wait budget `generate.lock_wait` (900 s). | 可靠性 |
| 14 | Exit status was always 0, so scripts could not branch on "engine not running". | Exit codes: 0 ok, 1 generic, 2 `generation_failed`, 3 `engine_not_found`, 4 `bad_arguments`. Every error carries a `kind` and a `hint`. | 接入 |
| 15 | Finding out whether parameters were sane meant burning a real generation. | `--dry-run` resolves port, model, size guard and effective params without touching the GPU. | 可靠性 |
| 16 | Debugging a failure on someone else's machine required screen sharing. | `--log-file` writes a full trace (discovery steps, poll states, attempts) next to the output. | 文档 |
| 17 | A stale `generate.lock` after a crash blocked every later run. | Lock is created atomically (`O_CREAT|O_EXCL`), validated against a live PID, and reclaimed automatically. | 可靠性 |

## 3. Things that made agent integration brittle (integration)

| # | v1.0 problem | v2.0 fix | Kind |
| --- | --- | --- | --- |
| 18 | MCP handshake assumed one protocol version. | Version negotiation across `2025-06-18`, `2025-03-26`, `2024-11-05`; unknown versions fall back to the newest supported. | 接入 |
| 19 | Server only understood newline-delimited JSON. Hosts that send LSP `Content-Length` frames got zero responses. (This was a real bug found by the e2e matrix in this release — responses: 0.) | Both framings accepted on stdin: newline-delimited JSON *and* `Content-Length` + blank line + body. Covered by an e2e case. | 接入 |
| 20 | Logging went to stdout, which corrupts the protocol stream. | stdout carries protocol traffic only; all logging goes to stderr. | 接入 |
| 21 | Only `generate_image` existed, so "why is it broken" was unanswerable from inside the agent. | Five tools: `generate_image`, `locmodel_status`, `list_models`, `engine_doctor`, `engine_discover`. | 接入 |
| 22 | Config lived only in code constants. | Layered config: `DEFAULTS` → `~/.locmodel-image/config.json` → `--config <path>` → CLI flags. `config show/get/set/path` manages it. | 接入 |
| 23 | Using it meant editing a Python file. | `pip install -e .` gives the `locmodel-image` command; a zero-dependency MCP server needs no third-party packages at all. | 通用性 |

## 4. Things a new user could not verify (documentation & testing)

| # | v1.0 problem | v2.0 fix | Kind |
| --- | --- | --- | --- |
| 24 | No tests. Every change was verified by me looking at a picture. | 46 unit tests (no GPU needed) + an end-to-end matrix split into stages `core` / `diag` / `gen` / `mcp`, with `--quick` and `--gen-timeout` for slow machines. | 文档 |
| 25 | Only Chinese docs, written for one reader. | `README.md` (English) + `README.zh-CN.md` (中文), `SKILL.md` with prompt guides in both languages, `docs/CHANGELOG.md`, plus runnable `examples/`. | 文档 |
| 26 | Capability claims were optimistic. | The "cannot do" list is explicit: no reliable in-image text, no img2img / inpaint / upscale (engine has endpoints, unverified here), no model download. | 文档 |

## 5. Deliberately not done in 2.0

| Item | Why not |
| --- | --- |
| img2img / inpainting / upscaling | The engine exposes related endpoints, but the img2img path was never verified end-to-end here. Shipping unverified paths is how a "portable" tool stops being portable. |
| Automatic model download | Model choice is a user decision (VRAM, license, quality); silently pulling 5 GB is not a good default. |
| Progress events / streaming | The job API only exposes `queued/running/succeeded/failed`; a percentage would be fabricated. |
| GUI | Out of scope for an MCP/CLI bridge. |
| Linux/macOS *testing* | Code paths are written and reviewed, but only Windows 10 + RTX 3060 Ti was actually executed. Treat other platforms as untested-but-supported-shaped. |
| Prompt rewriting inside the tool | Deliberately left to the calling agent. The local Qwen3-4B text encoder reads full sentences but has a weaker visual vocabulary; letting the small model rewrite prompts measurably lowers image quality. |
