---
name: locmodel-image
description: 用本机 LocModel（stable-diffusion.cpp / sd-server）的离线画图引擎出图。当用户要求「画一张 / 生成图片 / 出个图 / 给我几张方案草图 / 把这段描述画出来 / 生成配图」且希望用本机模型、离线出图、批量出方案时使用。覆盖提示词优化、参数选择、调用入口（MCP 工具或 CLI）、失败排查全流程。不适用：需要精确文字排版的海报（本地扩散模型文字拼写不可靠）、需要联网检索的图、视频生成。
version: 2.0.0
license: MIT
---

# LocModel Image（本地离线出图）

本技能把本机 LocModel 内置的 `sd-server` 引擎（stable-diffusion.cpp 后端）当作画图工具使用：
**文字提示词进，PNG 文件出，全程离线、不联网、不产生费用。**

两种调用形态，二选一或并用：

| 形态 | 入口 | 适用 |
| --- | --- | --- |
| **MCP 工具**（首选） | `generate_image` / `locmodel_status` / `list_models` / `engine_doctor` / `engine_discover` | 客户端支持 MCP 时，工具直挂、有 schema 约束，不依赖模型是否"想起"读文档 |
| **CLI 桥接器** | `python scripts/locmodel_image.py <子命令>` | 脚本化、批处理、无 MCP 环境、排障 |

## 能力边界（必须先判断）

可以做：
- 文生图（txt2img），单张或一次 1–4 张（`--count`）
- 自定尺寸 / 长宽比（1:1 4:3 3:4 3:2 2:3 16:9 9:16 21:9 2:1 1:2）
- 固定 seed 复现、负面提示词、步数 / sampler / CFG 覆盖
- 输出 PNG + 同名 `_meta.json`（记录 prompt、seed、尺寸、耗时，便于复现）

做不到（不要承诺）：
- 图内精确文字、版式排版 → 后期在 PPT/PS/Figma 里叠字
- 联网素材、写实人脸精修、视频生成
- 图生图 / 局部重绘：引擎有相关接口，但本技能未验证，不要承诺

## 调用前先做两件事

1. **`locmodel_status` 或 `locmodel_image.py status`**：确认引擎在、模型已加载、显存够。
   - `engine.state` 非 `ready` → 先让用户在 LocModel 里加载一个画图模型
   - `gpu.free_mb` 偏低是常态（模型常驻显存），**不阻塞出图**；只有连续 `generation_failed` 且分类为 `capacity` 时，才提示用户关闭占显存程序（LM Studio、浏览器硬件加速、另一个画图引擎）
2. **端口**：不要假设端口。v2.0 五级自发现：`LOCMODEL_SD_PORT` 环境变量 → 缓存 config.json → 解析 `sd-server` 进程命令行 → 候选端口扫描 → 本地监听端口扫描。发现失败再 `engine_discover` 强制重扫。

## 提示词优化规范（由本侧 Agent 完成，不交给本地小模型）

本地文本编码器（Qwen3-4B 级）能读懂完整句子，但行业审美与术语储备不足。**提示词由你（Agent）写好后直接下发生成**，不要指望引擎自动扩写。

结构模板：

```
<主体> + <场景/环境> + <构图与视角> + <光线> + <材质与细节> + <风格/介质> + <画质词>
```

要点：
- 具体名词优先于形容词堆叠：`curved glass curtain wall` 优于 `beautiful modern building`
- 摄影类加镜头语言：`35mm`, `wide angle`, `eye level`, `golden hour`
- 建筑/室内类加材质与光：`warm oak`, `terrazzo floor`, `soft north light`
- 风格锚点：`architectural visualization`, `watercolour sketch`, `isometric diagram`, `flat vector illustration`
- 负面提示词只在确有需要时写：`text, watermark, logo, extra fingers, distorted perspective`
- **不要**写"高清 8K 超写实大师作品"这类无信息量堆砌，本地模型对这些词不敏感

场景预设（可直接复用）：

| 场景 | 提示词骨架 | 建议尺寸 |
| --- | --- | --- |
| 建筑外观效果 | `modern modular building, <facade material>, <time of day>, wide angle, architectural photography` | 16:9（1280×720） |
| 室内空间 | `interior of <room>, <furniture>, <light direction>, 35mm, cozy atmosphere` | 4:3（1024×768） |
| 概念草图 | `concept sketch of <subject>, loose ink lines, light watercolor wash, white paper` | 3:2 |
| 平面/示意 | `minimal flat illustration of <subject>, limited palette, geometric shapes, no text` | 1:1 |
| 竖版海报底图 | `vertical composition, <subject>, large empty space on top, gradient background, no text` | 9:16（720×1280） |
| 材质/局部 | `close-up of <material>, raking light, high detail texture` | 1:1 |

### English prompt guide（英文提示词同样适用）

```text
<subject> + <scene/context> + <composition & viewpoint> + <lighting> + <material/detail> + <style/medium> + <quality anchors>
```

- Concrete nouns beat adjective stacks: `curved glass curtain wall` > `beautiful modern building`.
- Add camera language for photographic looks: `35mm`, `wide angle`, `eye level`, `golden hour`.
- Name materials and light for architecture: `warm oak`, `terrazzo floor`, `soft north light`.
- Style anchors that work well: `architectural visualization`, `watercolour sketch`, `isometric diagram`, `flat vector illustration`.
- Use a negative prompt only when needed: `text, watermark, logo, extra fingers, distorted perspective`.
- Skip empty hype (`8K ultra realistic masterpiece`); small local models ignore it and it dilutes the real tokens.
- The turbo model runs at low step counts, so keep prompts under ~60 tokens and lead with the subject.

| Use case | Prompt skeleton | Suggested aspect |
| --- | --- | --- |
| Building exterior | `modern modular building, <facade material>, <time of day>, wide angle, architectural photography` | 16:9 |
| Interior | `interior of <room>, <furniture>, <light direction>, 35mm, cozy atmosphere` | 4:3 |
| Concept sketch | `concept sketch of <subject>, loose ink lines, light watercolour wash, white paper` | 3:2 |
| Diagram / flat art | `minimal flat illustration of <subject>, limited palette, geometric shapes, no text` | 1:1 |
| Vertical poster base | `vertical composition, <subject>, large empty space on top, gradient background, no text` | 9:16 |
| Material close-up | `close-up of <material>, raking light, high detail texture` | 1:1 |

## 参数选择

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `width`/`height` | 引擎默认（常见 1024×1024） | 由面积守卫自动校正，见下 |
| `aspect` | 无 | 用长宽比代替具体尺寸；与 width/height 二选一 |
| `steps` | 引擎默认（turbo 模型常见 8） | 一般不调；>20 明显变慢且收益小 |
| `cfg` | 引擎默认（turbo 常见 1.0） | 1.0–1.5 保构图；2.0+ 更贴提示词但易过曝 |
| `sampler` | 引擎默认（常见 euler） | 不指定就别指定 |
| `seed` | -1（随机） | 复现用固定值；批量对比同一 prompt 时固定 seed 只看尺寸/参数差异 |
| `count` | 1 | 上限 `generate.max_count`（默认 4），显存不足时别超过 2 |
| `negative` | 空 | 见提示词规范 |

**尺寸守卫（按总像素面积，不按单边）**：引擎对过小的图会失败（512×512 实测报 `no results`），对过大的图会 OOM。
- 下限 `size.min_area`（默认 768² ≈ 59 万像素），小于则**等比放大**，保留用户长宽比
- 上限 `size.max_side`（默认 1536，且随显存自动调整），超过则等比缩小
- 边长对齐 `size.multiple`（默认 8）
- 确实要原样发送 → `--no-guard`（自担风险）

## 调用方式

MCP 工具：
```
generate_image(prompt="...", aspect="16:9", out_dir="<会话 output 目录>", seed=12345)
```

CLI：
```bash
python scripts/locmodel_image.py generate --prompt "..." --aspect 16:9 --out <目录> --seed 12345
python scripts/locmodel_image.py status
python scripts/locmodel_image.py doctor
```

输出：`<out>/locmodel_<YYYYmmdd_HHMMSS>_<i>_seed<seed>.png` + `_meta.json`。

**落盘位置**：优先当前会话的结果产物目录（output），不要写桌面或系统临时目录。

## 失败排查（按报错对号入座）

| 现象 | 退出码 | 原因 | 处理 |
| --- | --- | --- | --- |
| `engine_not_found` | 3 | 端口未发现 / LocModel 没开 / 引擎未加载模型 | 打开 LocModel 并加载模型 → `engine_discover`；或显式设 `LOCMODEL_SD_PORT` |
| `generation_failed` + `no results` | 2 | 显存不足（工作区分配失败）或多引擎抢卡 | 关掉 LM Studio / 浏览器硬件加速；只保留一个画图引擎；降低尺寸或 `count` |
| 一直 `queued` 不动 | — | 引擎单 slot，前面的作业还没完 | 等待；确认没有别的出图进程（v2.0 有跨进程锁，会自动排队） |
| 出图很慢（>3 分钟） | — | 尺寸过大 / 步数过高 / 显卡被占用 | 降尺寸、`steps` 回默认、关后台 GPU 程序 |
| 提示词被忽略 | — | `cfg` 太低 | `--cfg 1.5~2.0`；turbo 蒸馏模型对提示词敏感度低是正常现象 |
| 文字糊成乱码 | — | 扩散模型固有限制 | 不要试图修，改在后期叠字 |
| 客户端连不上 MCP | — | Python 路径 / 编码 / 缺少 schema 校验 | 用绝对路径的 python；设 `PYTHONIOENCODING=utf-8`；`doctor` 自检 |

## 常用命令速查

```bash
# 环境自检（Python 版本 / 引擎 / 模型 / 显存 / 输出目录 / 配置）
python scripts/locmodel_image.py doctor

# 只打印将发送的 payload，不出图（快速验参数）
python scripts/locmodel_image.py generate --prompt "test" --aspect 1:1 --dry-run

# 配置读写
python scripts/locmodel_image.py config show
python scripts/locmodel_image.py config get size.max_side
python scripts/locmodel_image.py config set size.max_side 1280
python scripts/locmodel_image.py config path

# 强制重新发现端口
python scripts/locmodel_image.py discover
```

## 给用户的交付话术

- 出图后列出**真实生成的 PNG 绝对路径**（不要虚构），批量时说明每张 seed 便于复现
- 若参数被守卫自动调整过，明确说明"你要 512×512，已按引擎下限放大到 768×768"
- 失败时给出具体下一步（关哪个程序、改哪个参数），不要只说"失败了"
