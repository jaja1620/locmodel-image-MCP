# locmodel-image-MCP

> GitHub 仓库：[jaja1620/locmodel-image-MCP](https://github.com/jaja1620/locmodel-image-MCP) ——可安装的包名、CLI 命令与 MCP 服务名仍为 `locmodel-image`（不带 `-MCP` 后缀）。

**把本机 LocModel / stable-diffusion.cpp 的离线画图引擎，变成 AI Agent 可直接调用的工具——离线、免费、可脚本化。**

`locmodel-image` 是一个**零第三方依赖**的桥接层，把 LocModel 内置的 `sd-server`（stable-diffusion.cpp 后端）图像引擎通过两种形态暴露出来：

| 形态 | 入口 | 适用场景 |
| --- | --- | --- |
| **MCP 服务**（首选） | `scripts/locmodel_mcp.py`，提供 `generate_image`、`locmodel_status`、`list_models`、`engine_doctor`、`engine_discover` 五个工具 | 客户端支持 MCP（Claude Desktop、Cursor、Marvis、Continue 等）。模型拿到的是有 schema 约束的工具，而不是需要记忆的 shell 命令 |
| **CLI 桥接器** | `scripts/locmodel_image.py` | 脚本化、批量任务、CI，或不支持 MCP 的客户端 |

两者共用同一套引擎层：端口自发现、显存感知尺寸守卫、故障分类重试、跨进程生成锁、结构化 JSON 输出与明确的退出码。

> [English](README.md) · **简体中文**

---

## 目录

- [为什么需要它](#为什么需要它)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [改图：局部重绘与整图重画](#改图局部重绘与整图重画)
- [接入 MCP 服务](#接入-mcp-服务)
- [安装配套 Skill](#安装配套-skill)
- [CLI 参考](#cli-参考)
- [配置参考](#配置参考)
- [尺寸与显存策略](#尺寸与显存策略)
- [工作原理](#工作原理)
- [测试](#测试)
- [故障排查](#故障排查)
- [兼容性与限制](#兼容性与限制)
- [3.0 变更](#30-变更)
- [2.0 变更](#20-变更)
- [许可证](#许可证)

---

## 为什么需要它

LocModel 自带完整的本地出图链路，但只提供桌面 GUI。Agent 和脚本无法可靠地驱动 GUI，而把 `http://127.0.0.1:8790` 写死在代码里，一旦端口变化、模型切换、或换台显存更小的机器就立刻失效。

本项目只解决这三个具体问题：

1. **自发现，而非硬编码端口**——五级兜底，其中"解析 `sd-server` 进程命令行"最可靠，因为端口是 LocModel 启动时决定的。
2. **守卫，而非神秘失败**——引擎对**过小**的图会静默失败（512×512 可能返回 `no results`），对更大的图却正常。守卫按**总像素面积**判断，且等比缩放，保住用户要的长宽比。
3. **可移植，而非"只在我机器上能用"**——不写死用户路径、分辨率、采样器或 Python 安装位置；每个默认值都可发现、可覆盖。

---

## 环境要求

| 项 | 要求 |
| --- | --- |
| Python | 3.8+（推荐 3.10+）。**仅标准库，无需 `pip install` 任何东西** |
| 引擎 | LocModel 桌面端已加载扩散模型，**或**任意暴露 `/sdcpp/v1/*` 接口的 `sd-server`（stable-diffusion.cpp） |
| 系统 | Windows 10/11、macOS、Linux |
| 显卡 | 引擎本身支持的任意 GPU。6 GB 显存可用，8 GB 在 1024² 下较舒适 |

本工具**不加载模型、不管理引擎生命周期**——请先启动 LocModel 并加载模型。这是一条有意的边界：引擎启动参数随硬件与模型而异，弄错正是「workspace capacity」崩溃的头号原因。

---

## 安装

两种方式，**任选其一**：

- **安装程序（单文件 exe，无需 Python）** —— 日常使用推荐。到
  [Releases](https://github.com/jaja1620/locmodel-image-MCP/releases) 下载 `locmodel-image-setup.exe`，
  双击、确认一次即可。它会解包到 `%LOCALAPPDATA%\locmodel-image`，并把 MCP 服务注册进本机检测到的
  所有受支持客户端。Marvis 用户再执行 `locmodel-image-setup.exe emit marvis`，把打印出的指令粘到
  对话框里即可。
- **源码安装（需要 Python 3.9+，原有方式）** —— 克隆后直接运行，见下。

详细的双语逐步说明（安装 + 使用 + 故障排查）：
[docs/INSTALL.zh-CN.md](docs/INSTALL.zh-CN.md)（中文）· [docs/INSTALL.md](docs/INSTALL.md)（English）

### 源码安装

无需编译，克隆即用：

```bash
git clone https://github.com/jaja1620/locmodel-image-MCP.git
cd locmodel-image-MCP
python scripts/locmodel_image.py doctor
```

`doctor` 会逐项检查（Python 版本、平台、配置路径、引擎端口及发现方式、模型、显卡空闲显存、输出目录可写性），关键项缺失时以非零码退出。

可选——安装为包，让 `locmodel-image` 进入 `PATH`：

```bash
pip install .
locmodel-image doctor
```

---

## 快速开始

```bash
# 1. 引擎是否可达、端口在哪
python scripts/locmodel_image.py status

# 2. 只校验参数，不占用 GPU
python scripts/locmodel_image.py generate --prompt "courtyard at dusk" --aspect 16:9 --dry-run

# 3. 正式出图
python scripts/locmodel_image.py generate \
  --prompt "modern modular building, warm timber facade, dusk, wide angle, architectural photography" \
  --aspect 16:9 --out ./output --seed 424242

# 4. 固定 seed 出两张做对比
python scripts/locmodel_image.py generate --prompt "..." --count 2 --seed 424242
```

每次运行产出：

```
output/locmodel_20261109_171204_1_seed424242.png
output/locmodel_20261109_171204_meta.json
```

`_meta.json` 记录实际生效的提示词、seed、守卫校正后的尺寸、步数、采样器、CFG、请求尺寸与实际尺寸、耗时——任何一张图都能复现或追溯。

---

## 改图：局部重绘与整图重画

v3.0 在文生图之上加了两个动作：**只改一块**和**整图重画**。

| 你的需求 | 用哪个 | 原图像素 |
| --- | --- | --- |
| 只改某个区域，其他不动 | `edit_image`（需要掩码） | 掩码外由 CPU 合成保留，是硬保证 |
| 构图不变，风格/主体换掉 | `redraw_image` | 仅作构图与色彩参考 |

### 掩码怎么给：三种，按现场条件挑

**矩形/多边形坐标**（不用打开任何图片编辑器）：

```json
{"unit": "norm", "regions": [
  {"type": "rect", "x": [0.12, 0.10, 0.48, 0.42]},
  {"type": "polygon", "points": [[0.60, 0.62], [0.92, 0.60], [0.78, 0.94]]}
]}
```

`unit` 默认 `norm`（0–1 归一化，与分辨率无关），要按像素就写 `px`。多个形状取并集。

**掩码 PNG**：白=重绘，黑=保留，尺寸不一致会自动缩放。

**颜色阈值**：按原图某片颜色选区域，适合"把这块纯色墙换材质"。

**人工标注**：`annotator/mask_annotator.html` 是单文件网页（双击即开、不用安装）。拖入图片后用矩形 / 多边形 / 画笔圈出区域，直接导出 `mask.png` 或复制归一化坐标 JSON，粘给 `edit_image`。

### 模型选择：明确，绝不偷偷替换

```bash
python scripts/locmodel_image.py edit-models
```

列出所有模型、哪些能做局部重绘、不传 model 时会自动选谁。默认优先级 `qwen-image-edit` → `flux2-klein` → `qwen-image`。

探测成本做了收敛和缓存：先用 `tasklist` 判断引擎进程在不在，在才启动 PowerShell 读命令行；用 `netstat` 的监听列表先排除不可能应答的候选端口；结果按 `edit.snapshot_cache_ttl`（默认 120 秒）跨进程复用。热调用约 0.3 秒。手动在 LocModel 窗口里换了模型后，加 `--refresh` 立即重测：

```bash
python scripts/locmodel_image.py edit-models --refresh
```

- `edit.prefer_loaded`（默认 `true`）——显存里已有可编辑模型就直接用，不为"升到更高优先级"去切模型。切模型要卸载重载：4B 约 30–60 秒，20B 更久，显存本来就紧的卡还可能直接 OOM。
- `edit.auto_load`（默认 `true`）——没加载任何可编辑模型时自动加载优先级最高的那个。关掉它，优先级表就形同虚设。
- 传了 `model` 就严格用它；该模型不能改图时报错并列出候选，**不会换成别的模型**。
- 只有 `edit` 族（Qwen-Image-Edit、FLUX.2 klein）能做局部重绘。纯生图模型（Qwen-Image、Z-Image、SDXL、SD1.5）只能 `redraw_image`。

每次返回都带 `selection` 块（`requested` / `chosen` / `family` / `rank` / `reason` / `auto_loaded` / `candidates`），能解释清楚为什么用了这个模型。

### 掩码外不可能被改动

不信任引擎自觉遵守掩码。结果在 CPU 侧合成——`result = where(mask > 127, edited, original)`——掩码外的像素与原图逐字节一致。返回里的 `protected_pixels` / `redrawn_pixels` 可用于核对。

`files[].path` 指向的就是保护后的图；引擎未保护的原始输出另存为同目录 `*_raw.png`（字段 `files[].raw`、`recomposed[].raw`），`files[].protected` 表示合成是否成功。交付给用户只给 `files[].path`，`_raw` 仅用于排查引擎实际画了什么。

```bash
# 局部重绘
python scripts/locmodel_image.py edit --image ./output/v1.png \
  --prompt "night-time glass curtain wall, interior lights on" \
  --region 0.25,0.18,0.62,0.55 --strength 0.8

# 用导出好的掩码
python scripts/locmodel_image.py edit --image ./output/v1.png --mask ./output/mask.png \
  --prompt "oak slat ceiling"

# 构图不变，改水彩风
python scripts/locmodel_image.py redraw --image ./output/v1.png \
  --prompt "watercolour architectural sketch, loose ink lines, white paper" --strength 0.65
```

原图永不覆盖，每次改图都是新文件，方便对比取舍。

---

## 接入 MCP 服务

服务以 **newline-delimited JSON-RPC 2.0** 走 stdio（同时兼容 LSP 风格 `Content-Length` 分帧），协议版本 `2025-06-18`、`2025-03-26`、`2024-11-05` 自动协商。

### Claude Desktop

`claude_desktop_config.json`：

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

Windows 上必须写**真实解释器的绝对路径**（例如 `C:/Users/you/AppData/Local/Programs/Python/Python312/python.exe`）；裸写 `python` 可能命中 Microsoft Store 占位程序，静默退出。

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

### 其它客户端

stdio 传输，指向 `scripts/locmodel_mcp.py` 即可。若自发现在本机不可靠，直接钉死端口：

```json
"env": { "LOCMODEL_SD_PORT": "8790", "PYTHONIOENCODING": "utf-8" }
```

### 工具清单

| 工具 | 参数 | 返回 |
| --- | --- | --- |
| `generate_image` | `prompt`（必填）、`negative_prompt`、`width`、`height`、`aspect`、`steps`、`sampler`、`seed`、`count`、`output_dir`、`filename_prefix`、`model`、`timeout_s` | 图片路径、实际 seed、最终尺寸、耗时 |
| `locmodel_status` | — | 引擎状态、端口及发现方式、模型、引擎默认参数、显存 |
| `edit_image` | init_image（必填）、prompt（必填）、mask_image 或 regions、strength、negative_prompt、model、steps、sampler、cfg_scale、seed、mask_blur、inpaint_padding、channel、out_dir、filename_prefix、verify、dry_run | 改图路径、掩码统计、`selection`（实际用哪个模型）、`verify` 校验块 |
| `redraw_image` | init_image（必填）、prompt（必填）、strength，其余同上（无掩码参数） | 重画路径、`selection`、`verify` |
| `list_edit_models` | — | 模型、族/优先级、能否局部重绘、自动选择结果 |
| `load_model` | name（必填）、timeout | 加载/切换引擎模型，并回读核对是否生效 |
| `list_models` | — | 引擎报告的模型列表 |
| `engine_doctor` | — | 逐项自检结果 |
| `engine_discover` | — | 强制重扫端口并返回结果 |

`generate_image` 失败时返回 `isError: true` 并附可执行 `hint`，Agent 能据此调整而不是盲目重试。

---

## 安装配套 Skill

`SKILL.md` 是 MCP 服务在**提示词侧**的搭档：它告诉 Agent 提示词怎么写、什么题材配什么长宽比、哪些参数值得调、每种失败怎么读——这些是 JSON schema 装不下的经验。

两者互补，不是替代关系：

- **MCP** = 手（有类型约束的工具，随时可用）
- **Skill** = 手艺（提示词结构、场景预设、参数判断）

把本仓库（或仅 `SKILL.md`）复制到客户端的技能目录即可：

```bash
# Claude / 通用 Agent 技能目录
cp -r . ~/.claude/skills/locmodel-image

# Marvis（Windows）
xcopy /E /I . "%APPDATA%\Tencent\Marvis\User\<your-id>\skills\custom\locmodel-image"
```

支持 front-matter 的客户端会自动读取 `name`、`description`、`version`、`license`。

---

## CLI 参考

```
locmodel_image.py [--config PATH] [--verbose] [--log-file PATH] <子命令> [参数]
```

| 子命令 | 作用 |
| --- | --- |
| `status` | 引擎端口（含发现来源）、模型、引擎默认参数、显存 |
| `discover` | 强制重新发现端口，输出端口与来源 |
| `models` | 列出引擎报告的模型 |
| `doctor` | 环境自检，退出码反映健康状况 |
| `generate` | 提交任务并等待出图 |
| `edit` | 只重绘已有图片的掩码区域 |
| `redraw` | 按新提示词整图重画 |
| `edit-models` | 能改图的模型、优先级与自动选择结果 |
| `load-model` | 在引擎里加载或切换模型 |
| `config show\|get\|set\|path` | 查看或修改配置 |

### `generate` 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--prompt` | 必填 | 非空字符串 |
| `--negative` | 空 | 负面提示词 |
| `--width` / `--height` | 引擎默认 | 正整数，会被守卫校正 |
| `--aspect` | — | `1:1 4:3 3:4 3:2 2:3 16:9 9:16 21:9 2:1 1:2`，与 `--width/--height` 互斥 |
| `--steps` | 引擎默认 | 越高越慢，收益递减 |
| `--sampler` | 引擎默认 | 如 `euler`、`dpm++2m` |
| `--cfg` | 引擎默认 | 蒸馏/turbo 模型通常 1.0–1.5 |
| `--seed` | `-1`（随机） | 需复现时固定 |
| `--count` | `1` | 受 `generate.max_count` 限制（默认 4） |
| `--model` | — | 多模型引擎下指定模型名 |
| `--out` | 配置 `output.dir` | 输出目录（不存在会自动创建） |
| `--prefix` | `locmodel` | 文件名前缀 |
| `--timeout` | `generate.timeout` | 单任务超时秒数 |
| `--retry` | `generate.retry` | 瞬时失败时换 seed 重试次数 |
| `--no-guard` | 关 | 原样发送请求尺寸 |
| `--dry-run` | 关 | 只打印 HTTP payload 就退出，不占用 GPU |

### `edit` 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--image` | 必填 | 源图（不会被修改） |
| `--prompt` | 必填 | 掩码内应该出现什么 |
| `--mask` | — | 掩码 PNG，白=重绘。给了它 `--region` 就失效 |
| `--region` | — | `x1,y1,x2,y2`，可重复；坐标按 `--unit` 解释 |
| `--unit` | `norm` | `norm`（0–1）或 `px` |
| `--invert` | 关 | 反转生成的掩码 |
| `--strength` | `0.75` | 去噪强度，越大越放开重画 |
| `--mask-blur` | `8` | 掩码边缘羽化，减少接缝 |
| `--padding` | `32` | 交给引擎前把掩码外扩一圈 |
| `--channel` | `auto` | 先异步 `img_gen`，不支持再落同步 `img2img` |
| `--model` | 自动 | 指定模型；不能用就报错，不做替换 |
| `--verify` | `agent` | 回读产物做校验 |
| `--dry-run` | 关 | 只打印 payload，不接触引擎、不占 GPU |

`redraw` 用同样的选项，去掉掩码相关，默认 `--strength 0.6`。

### 退出码

| 码 | 名称 | 含义 |
| --- | --- | --- |
| 0 | `ok` | 成功 |
| 1 | `generic_error` | 未预期错误（网络、文件系统、JSON） |
| 2 | `generation_failed` | 引擎受理但未产出图片 |
| 3 | `engine_not_found` | 所有发现层级都没找到可用引擎 |
| 4 | `bad_arguments` | 参数非法（空提示词、`count` 超限、`width=0`、未知长宽比等） |

脚本可按退出码分支，Agent 可按 JSON 中的 `error` 字段分支。

---

## 配置参考

配置文件位置（先命中先用）：

1. `--config <路径>`
2. 环境变量 `LOCMODEL_IMAGE_CONFIG`
3. `~/.locmodel-image/config.json`（首次 `config set` 时创建）

端口解析顺序：

1. 环境变量 `LOCMODEL_SD_PORT`
2. `config.json` 中的缓存值
3. 运行中的 `sd-server` 进程命令行（最可靠）
4. `port_candidates` 候选端口扫描
5. 本地监听端口扫描

配置键（含默认值）：

```jsonc
{
  "port": null,                        // 缓存端口；null 表示每次自动发现
  "host": "127.0.0.1",
  "port_candidates": [8790, 8791, ...],// 兜底扫描列表
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
    "min_area": 589824,                // 768x768，低于此面积引擎容易失败
    "multiple": 8,                     // 边长对齐粒度
    "max_side": 1536,                  // 开启 max_side_auto 时随显存自动下调
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
    "prefer_loaded": true,             // 已加载的模型在优先级表里就不切模型
    "auto_load": true,                 // 没加载时自动加载最优先的可编辑模型
    "auto_load_timeout": 240,
    "channel": "auto",                 // img_gen → img2img 兜底
    "strength": 0.75,                  // 局部重绘
    "redraw_strength": 0.6,            // 整图重画
    "timeout": 900,
    "mask_blur": 8,
    "inpaint_padding": 32,
    "verify": "agent",
    "models_dirs": [],                 // 引擎不报模型时额外扫描的目录
    "load_endpoint": null,
    "resolved_channel": null,
    "engine_cache_ttl": 20,            // 秒；进程内记忆
    "snapshot_cache_ttl": 120          // 秒；快照跨进程复用
  },
  "discover": {
    "process_cache_ttl": 20,           // 复用进程命令行结果
    "budget_seconds": 20,              // 全部端口探测的硬上限
    "listen_budget_seconds": 4         // netstat 剩余端口扫描的上限
  },
  "gpu": { "min_free_mb_warn": 500 },
  "output": { "dir": "./output" }
}
```

常用配方：

```bash
locmodel_image.py config set engine.probe_timeout 3      # 机器慢或跨机访问
locmodel_image.py config set generate.max_count 2        # 6 GB 显存用户
locmodel_image.py config set size.max_side_auto false    # 自己钉死上限
locmodel_image.py config set generate.lock false          # 仅单进程使用
```

---

## 尺寸与显存策略

守卫针对两种会产生误导性报错的失败：
**像素太少**（`generate_image returned no results`）与**像素太多**（OOM / workspace capacity check，在 Windows 上可能表现为 `ucrtbase.dll 0xc0000409 / BEX64`）。

按顺序应用规则：

1. `width`/`height`（或长宽比预设）对齐到 `size.multiple`
2. 总面积低于 `size.min_area` → 等比放大（保留长宽比）
3. 最长边超过有效上限 → 等比缩小
4. 有效上限为 `size.max_side`；开启 `max_side_auto` 时按实测显存自动下调，且不超过 `hard_max_side`

实用配置：

| 显存 | 建议 | 备注 |
| --- | --- | --- |
| 4–6 GB | 768×768、1024×576，`count`=1 | `generate.retry` 至少 1 |
| 8 GB | 1024×1024、1280×720，`count`≤2 | turbo 模型实测最佳区间 |
| 12 GB+ | 1536×864、1536×1536 | 需显式调高 `size.max_side` |

还有一条能规避大多数神秘失败的习惯：**只保留一个图像引擎常驻**。LocModel、LM Studio 模型、第二个扩散引擎同时占同一块显卡，是 `workspace capacity check` 崩溃的经典成因。在 LocModel 内切换画图模型时，重启 LocModel 让旧引擎释放显存。

---

## 工作原理

```
┌──────────────┐   MCP stdio   ┌──────────────────┐   HTTP    ┌──────────────┐
│ AI 客户端    │ ────────────► │ locmodel_mcp.py  │ ────────► │              │
│ (Claude、    │               │  或              │  /sdcpp   │  sd-server   │
│  Cursor、    │ ◄──────────── │ locmodel_image.py│ ◄──────── │ (LocModel)   │
│  Marvis)     │  JSON 结果    └──────────────────┘   轮询    └──────────────┘
└──────────────┘                       │
                                       ▼
                              落盘 PNG + _meta.json
```

任务流程：`POST /sdcpp/v1/img_gen` → 轮询 `GET /sdcpp/v1/jobs/<id>`（`queued` → `running` → `succeeded`）→ 解码 `result.images[].b64_json`（或抓取仅限回环地址的 URL，上限 `max_download_mb`）→ 写 PNG 与元数据。

失败分类而非盲目重试：

| 类别 | 触发 | 动作 |
| --- | --- | --- |
| `capacity` | `no results`、workspace/分配类错误 | 换 seed 重试，尺寸等比缩小 ×0.85 |
| `parameter` | HTTP 400 / 参数非法 | 不重试——请求本身有问题 |
| `transient` | 超时、连接重置 | 原样重试 |

跨进程锁 `~/.locmodel-image/generate.lock` 让并发生成串行化——两个任务抢一块卡是容量类失败最常见的诱因。死 PID 留下的锁立即回收；超过 `lock_wait + 600 s` 的锁按过期回收。

---

## 测试

```bash
# 单元测试——不需要引擎
python -m pytest tests/test_core.py -q

# 端到端矩阵——需要引擎在线
python tests/e2e_matrix.py --stage core      # status / models / doctor / dry-run
python tests/e2e_matrix.py --stage gen       # 真实出图、长宽比、批量、守卫
python tests/e2e_matrix.py --stage diag      # 非法参数、无引擎、配置往返
python tests/e2e_matrix.py --stage mcp       # MCP 握手、分帧、工具调用
python tests/e2e_matrix.py --stage all       # 全量（8 GB 卡约 10 分钟）
```

每个阶段写 JSON 报告并打印通过/失败摘要。分阶段是为了让偶发的 GPU 异常无法掩盖具体坏在哪一环。

---

## 故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `engine_not_found`（退出码 3） | LocModel 没开、未加载模型、端口特殊 | 启动 LocModel 并加载模型 → `discover`；或设 `LOCMODEL_SD_PORT` |
| `generation_failed` + `no results` | 空闲显存不足，或两个引擎互抢 | 关闭 LM Studio / 浏览器硬件加速；只留一个引擎；降低尺寸或 `count` |
| 任务长期 `queued` | 引擎单 slot 且正忙 | 等待；确认没有孤儿进程占着锁 |
| 出图超过 3 分钟 | 尺寸过大、步数过高、显卡被共享 | 降尺寸、恢复默认步数、释放显卡 |
| 提示词像被忽略 | 蒸馏模型下 CFG 太低 | 试 `--cfg 1.5`；接受 turbo 模型对提示词跟随较松 |
| 文字糊成乱码 | 扩散模型固有限制 | 不要硬修，后期在 PPT/PS/Figma 叠字 |
| MCP 客户端看不到工具 | 解释器不对，或命中 Windows Store 的 `python` 占位 | 用解释器绝对路径；设 `PYTHONIOENCODING=utf-8` |
| `workspace capacity check` / `BEX64` 崩溃 | 两个引擎常驻，显存耗尽 | 重启 LocModel，让单个引擎独占显卡 |

任何异常先跑 `doctor`——它一次性给出端口来源、模型、空闲显存与输出目录可写性。

---

## 兼容性与限制

- **接口面**：面向 stable-diffusion.cpp 服务的 `/sdcpp/v1/*` API，**不兼容** Automatic1111（不使用 `/sdapi/v1/txt2img`）。路径均可配置，路由略有差异的分支可用 `config set engine.*_path` 适配。
- **自发现是启发式**：同机跑多个引擎时请显式钉死端口，否则可能猜错且无声——这也是 `status` 始终报告"端口是怎么找到的"的原因。
- **不做模型管理**：加载/卸载、模型切换、显存预算仍归 LocModel 管。本工具绝不替你杀进程或卸载模型。
- **未实现**：图生图、局部重绘、放大、视频生成。引擎广告了其中部分能力，但本项目未验证——宁可留空，不做半成品。
- **图内文字**：模型固有限制，与本桥接层无关。

---

## 3.0 变更
完整列表见 [CHANGELOG.md](docs/CHANGELOG.md)。要点：
- **`edit_image` / `redraw_image`**：给一张已有图片、用一句话改图，不需要另下专用 inpaint 权重。走引擎已有的 `init_image` + `mask_image` 通道，掩码在 CPU 侧用标准库生成
- **掩码外保证不动**：结果在 CPU 侧与原图合成，"除这块之外都不变"是真保证而不是奢望
- **模型优先级但不偷换**：`edit.priority` 配合 `edit.prefer_loaded` / `edit.auto_load`；指定的模型不能用时明确报错并给候选
- **`list_edit_models` / `load_model`**：能看谁会改图、为什么选它，也能直接加载/切换模型
- **`annotator/mask_annotator.html`**：单文件网页标注器（矩形/多边形/画笔），导出掩码 PNG 或归一化 JSON
- **探测加速**：引擎探测带缓存（`edit.engine_cache_ttl`），引擎没开不必每次调用都重扫端口
- 仍是**零依赖**：只用标准库，Python 3.8+

## 2.0 变更

完整列表见 [CHANGELOG.md](docs/CHANGELOG.md)；**为了让这套东西能在别人机器上跑，v1.0 里哪些写死的假设必须拆掉**，逐项说明见 [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md)。要点：

- 移除硬编码的用户路径、端口、分辨率、采样器与 Python 安装位置
- 基于面积的尺寸守卫，显存感知上限，支持 `--aspect` 预设
- 失败分类（`capacity` / `parameter` / `transient`），capacity 类重试时等比缩小
- 跨进程生成锁，支持残留锁与过期锁回收
- `--dry-run`、`--no-guard`、`--log-file`、`--config`、`config get/set/path`
- 稳定退出码；非法输入走 `expects_error` 分支而非抛栈
- MCP：协议版本协商、双分帧（newline + `Content-Length`）、`engine_doctor` / `engine_discover` 工具、失败返回 `isError` 并附可执行提示
- 测试：46 项单元测试 + 分阶段 e2e 矩阵

---

## 许可证

MIT，见 [LICENSE](LICENSE)。
