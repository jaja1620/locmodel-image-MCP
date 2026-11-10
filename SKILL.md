---
name: locmodel-image
description: 用本机 LocModel（stable-diffusion.cpp / sd-server）的离线画图引擎出图与改图。当用户要求「画一张 / 生成图片 / 出个图 / 给我几张方案草图 / 把这段描述画出来 / 生成配图」，或要求「改这张图 / 把图里的 X 换成 Y / 在图上加个东西 / 只改这块 / 局部重绘 / 在这张图基础上重画」且希望用本机模型、离线处理时使用。覆盖提示词优化、参数选择、掩码标注、局部重绘与整图重画、模型选择与切换、调用入口（MCP 工具或 CLI）、失败排查全流程。不适用：需要精确文字排版的海报（本地扩散模型文字拼写不可靠）、需要联网检索的图、视频生成。
version: 3.0.0
license: MIT
---

# LocModel Image（本地离线出图）

本技能把本机 LocModel 内置的 `sd-server` 引擎（stable-diffusion.cpp 后端）当作画图工具使用：
**文字提示词进，PNG 文件出，全程离线、不联网、不产生费用。**

两种调用形态，二选一或并用：

| 形态 | 入口 | 适用 |
| --- | --- | --- |
| **MCP 工具**（首选） | `generate_image` / `edit_image` / `redraw_image` / `list_edit_models` / `load_model` / `locmodel_status` / `list_models` / `engine_doctor` / `engine_discover` | 客户端支持 MCP 时，工具直挂、有 schema 约束，不依赖模型是否"想起"读文档 |
| **CLI 桥接器** | `python scripts/locmodel_image.py <子命令>` | 脚本化、批处理、无 MCP 环境、排障 |

## 能力边界（必须先判断）

可以做：
- 文生图（txt2img），单张或一次 1–4 张（`--count`）
- 自定尺寸 / 长宽比（1:1 4:3 3:4 3:2 2:3 16:9 9:16 21:9 2:1 1:2）
- 固定 seed 复现、负面提示词、步数 / sampler / CFG 覆盖
- 输出 PNG + 同名 `_meta.json`（记录 prompt、seed、尺寸、耗时，便于复现）
- **改图**：给定一张已有图片，用一句话指定"这块改成什么"（局部重绘 / inpaint），或"整体按新描述重画"（`redraw`）
- 掩码来源三选一：掩码 PNG、矩形 / 多边形坐标、按颜色阈值选区域；掩码外的像素由 CPU 侧还原为原图，不会被改动
- 模型选择：自动按优先级挑可编辑模型，也能指定具体模型或让引擎先加载

做不到（不要承诺）：
- 图内精确文字、版式排版 → 后期在 PPT/PS/Figma 里叠字
- 联网素材、写实人脸精修、视频生成
- **纯文字编辑**（改海报里的字）→ 扩散模型做不到，说清楚并建议后期叠字
- 需要专用 inpaint 权重才能保证质量的极端场景（如整脸换人）→ 用 `redraw_image` 整图重画，或说明能力边界

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

## 改图：局部重绘与整图重画

用户已经有图、只想改其中一部分或按新描述重画时走这里。**不要重新 `generate_image` 从头画一张糊弄过去。**

| 用户说 | 用哪个 | 说明 |
| --- | --- | --- |
| "把这块改成 X"、"这个人手里的杯子换成咖啡"、"去掉这个电线杆" | `edit_image` | 只重绘掩码内，掩码外原样保留 |
| "按这个描述重画一张"、"风格换成水彩"、"整体重做" | `redraw_image` | 整图重画，原图只作为构图与色彩参考 |
| 没有原图 | `generate_image` | 文生图 |

### 掩码：三种给法，按现场条件挑

1. **矩形 / 多边形坐标**（最简单，优先用）
   ```json
   {"unit": "norm", "regions": [
     {"type": "rect", "x": [0.12, 0.10, 0.48, 0.42]},
     {"type": "polygon", "points": [[0.60, 0.62], [0.92, 0.60], [0.78, 0.94]]}
   ]}
   ```
   `unit` 默认 `norm`（0–1 归一化，与分辨率无关）；要按像素就写 `unit: "px"`。多种形状可叠加，取并集。

2. **掩码 PNG**：白=重绘，黑=保留，尺寸与原图一致（不一致会自动缩放）。

3. **颜色阈值**：按原图某片颜色选区域，适合"把这个纯色墙面换材质"这类。

**人工标注**：`annotator/mask_annotator.html` 是单文件网页（双击即开、无需安装），拖入图片后用矩形 / 多边形 / 画笔圈出区域，直接导出 `mask.png` 或复制坐标 JSON，粘给 `edit_image` 即可。

### 模型：先看再选，不要瞎猜

```
list_edit_models()   # 或 locmodel_image.py edit-models
```
返回 `auto.region_edit` / `auto.redraw` 两个"不传 model 时会用的模型"、`counts`、`notes`。默认优先级由 `edit.priority` 决定（出厂 `qwen-image-edit` → `flux2-klein` → `qwen-image`）。

结果有缓存（`edit.snapshot_cache_ttl`，默认 120 秒，跨进程生效），热调用约 0.3 秒。你在 LocModel 窗口里手动换过模型后要立刻确认，就传 `refresh=true`（CLI 加 `--refresh`）强制重探。

选择规则（记住这几条，能解释清楚每一次为什么用了某个模型）：
- 不传 `model` 且引擎里已有可编辑模型 → 直接用它，`selection.reason = "loaded"`
- 不传 `model` 且啥也没加载 → `edit.auto_load=true` 时自动加载优先级最高的那个（`auto_loaded` 会标出来）
- `edit.prefer_loaded=true` 时，已加载模型只要在优先级表里就不为"升到更高优先级"去切模型——切模型要卸载重载，4B 约 30–60 秒，20B 更久，还可能爆显存
- 传了 `model` → 严格用它；不存在或不能改图就报错，**不会偷偷换成别的模型**
- 只有 `edit` 族（Qwen-Image-Edit / FLUX.2 klein）能做局部重绘；纯生图模型（Qwen-Image、Z-Image、SDXL、SD1.5）只能 `redraw_image` 整图重画

### 关键参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `strength` | `0.75`（edit）/ `0.6`（redraw） | 越高越"放手重画"。局部重绘 0.6–0.85；想保住原图细节就往 0.5 调，想彻底换掉就往 0.9 调 |
| `mask_blur` | 8 | 掩码边缘羽化，减少接缝。硬边物体用小值，柔和过渡用大值 |
| `inpaint_padding` | 32 | 掩码外扩一圈再交给引擎，避免边缘切割感 |
| `channel` | `auto` | 先试异步 `img_gen`，不支持再落 `img2img`；一般不用管 |
| `verify` | `agent` | 回读产物做校验。不要关 |
| `negative` | 空 | 局部重绘时写"保留什么"往往比"去掉什么"更有用 |

### 示例

```bash
# 局部重绘：把窗户那片改成夜景玻璃幕墙
python scripts/locmodel_image.py edit --image "D:/out/v1.png" \
  --prompt "night-time glass curtain wall, interior lights on, warm reflections" \
  --region 0.25,0.18,0.62,0.55 --strength 0.8

# 用现成掩码
python scripts/locmodel_image.py edit --image "D:/out/v1.png" --mask "D:/out/mask.png" \
  --prompt "oak slat ceiling"

# 整图重画成水彩风
python scripts/locmodel_image.py redraw --image "D:/out/v1.png" \
  --prompt "watercolour architectural sketch, loose ink lines, white paper" --strength 0.65
```

MCP 侧同参数：`edit_image(init_image="D:/out/v1.png", prompt="...", regions={...})`。

### 交付时必须说清的三件事

1. **掩码外没有被改动**——这是硬保证（CPU 侧合成，不依赖引擎自觉），可以明确告诉用户
2. **实际用了哪个模型、为什么**——引用 `selection.chosen` 与 `selection.reason`
3. **改坏了怎么办**——原图没被覆盖，新图是独立文件，让用户对比后再决定留下哪张

`files[].path` 返回的就是**已保护**的图（掩码外=原图）；引擎未经保护的原始输出另存在同目录
`*_raw.png`，字段为 `files[].raw`，只在排查"引擎到底画了什么"时才需要，正常交付不要给用户。

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
| `model_unavailable` | — | 本机没有任何可编辑模型（Qwen-Image-Edit / FLUX.2 klein 都没装） | 告知缺哪个族，让用户在 LocModel 模型菜单下载；或改用 `redraw_image` |
| `model_unsupported` | — | 指定的模型只能生图，不能局部重绘 | 换成 `edit` 族模型，或改用 `redraw_image` |
| `model_not_found` | — | 指定的模型名不在本机 | `list_edit_models` 拿真实名字，照抄，别改大小写猜 |
| `model_not_loaded` | — | `edit.auto_load=false` 且目标模型没加载 | `load_model(name)` 显式加载，或把 `edit.auto_load` 打开 |
| `bad_arguments` + mask | — | 掩码空白、覆盖整图、不是 8-bit PNG、带 Adam7 交错 | 空白说明没画上；整图覆盖请改用 `redraw_image`；PNG 重导出为 8-bit 非交错 |
| 改图后接缝明显 | — | `strength` 太高或 `mask_blur` 太小 | 降 `strength`、加 `mask_blur`、扩 `inpaint_padding` |
| 掩码内几乎没变化 | — | `strength` 太低，或提示词没描述"变成什么" | `strength` 提到 0.8 以上；提示词写具体名词而非"换一下" |

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

# 改图
python scripts/locmodel_image.py edit-models          # 有哪些模型能改图、自动会选谁
python scripts/locmodel_image.py edit --image a.png --prompt "..." --region 0.1,0.1,0.5,0.4
python scripts/locmodel_image.py edit --image a.png --mask mask.png --prompt "..." --dry-run
python scripts/locmodel_image.py redraw --image a.png --prompt "..."
python scripts/locmodel_image.py load-model "Qwen-Image-Edit"
```

## 给用户的交付话术

- 出图后列出**真实生成的 PNG 绝对路径**（不要虚构），批量时说明每张 seed 便于复现
- 若参数被守卫自动调整过，明确说明"你要 512×512，已按引擎下限放大到 768×768"
- 失败时给出具体下一步（关哪个程序、改哪个参数），不要只说"失败了"
