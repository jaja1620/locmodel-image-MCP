---
name: locmodel-image
description: 用本机 LocModel（stable-diffusion.cpp / sd-server）的离线画图引擎出图与改图。当用户要求「画一张 / 生成图片 / 出个图 / 给我几张方案草图 / 把这段描述画出来 / 生成配图」，或要求「改这张图 / 把图里的 X 换成 Y / 在图上加个东西 / 只改这块 / 局部重绘 / 在这张图基础上重画」且希望用本机模型、离线处理时使用。覆盖提示词优化、参数选择、掩码标注、局部重绘与整图重画、模型选择与切换、调用入口（MCP 工具或 CLI）、失败排查全流程。不适用：需要精确文字排版的海报（本地扩散模型文字拼写不可靠）、需要联网检索的图、视频生成。
version: 3.2.0
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
- 文生图（txt2img），单张或一次多张（`--count`，上限取引擎 `limits.max_batch_count`，本机实测 8）
- 自定尺寸 / 长宽比（1:1 4:3 3:4 3:2 2:3 16:9 9:16 21:9 2:1 1:2）；**显式给 width/height 就按你给的出**，工具不再偷偷放大到 768 或压到 1536
- 固定 seed 复现、负面提示词、步数 / sampler / CFG 覆盖
- 输出格式 `png` / `jpeg` / `webp`（`output_format`，JPEG/WebP 可配 `output_compression`）+ 同名 `_meta.json`（记录 prompt、seed、尺寸、耗时，便于复现）
- 二次放大：hires fix 按**倍数**（`hires_scale`）或按**目标尺寸**（`hires_target_width/height`）两种给法
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

### 三条通道，别走错

| 参数族 | 有哪些 | 怎么传 | 落盘吗 |
| --- | --- | --- | --- |
| 尺寸与内容 | `width` `height` `aspect` `negative_prompt` `prompt` | 工具入参 | 否 |
| 生成工作流（核心 14 项） | 见下表，直接展开 | 单次 `params={...}` 或会话 `set_workflow_params` | 否，只在 HTTP payload |
| 折叠组 | hires / vae_tiling / output / high_noise / slg / raw(lora) | 同上，用户确需调整时再展开 | 否，只在 HTTP payload |

**铁律**：工作流参数一律走 HTTP payload，**永远不要去改 LocModel 的 `config.json`**。
GUI 用自己的面板状态把该文件回写覆盖，改完当场就被抹掉，还会让用户以为改动生效了。

尺寸由宽度高度入参决定，工作流参数不参与尺寸校正；尺寸口子见下面「尺寸策略」。

### 核心参数（14 项，默认值仅供参考）

| 参数 | 默认 | 取值 | 一句话 |
| --- | --- | --- | --- |
| **`cfg`**（别名 `cfg_scale`、`txt_cfg`） | 引擎面板值（flux 系实测 1.0，SD 系常见 7.0） | 0–30 | 1.0–1.5 保构图；2.0+ 更贴提示词但易过曝 |
| **`img_cfg`** | 空（关闭） | 0–30 | 图像条件分支的 CFG，图生图时才用 |
| **`steps`**（`sample_steps`） | 引擎面板值（flux 蒸馏系实测 4，SD 系常见 20） | 1–200 | turbo/flow 模型 4–12 就够；>30 收益看不见、时间翻倍 |
| **`sampler`**（`sample_method`） | 引擎面板值（常见 euler） | 22 种，见 `params list` | 不指定就别指定；`dpm++2m` 是通用升级项 |
| **`scheduler`** | `default` | 19 种，见 `params list` | `karras`/`beta` 更锐，`sgm_uniform` 更顺；`default` 由引擎按模型映射 |
| **`flow_shift`** | 空（自动） | 0–100 | flow matching 模型的低频结构权重，2–5 有感知 |
| **`shifted_timestep`** | 0 | -1–1000 | 时间步偏移，默认别动 |
| **`eta`** | 空 | 0–1 | SDE 采样器的噪声注入；0 确定、1 全随机 |
| **`distilled_guidance`** | 3.5 | 0–30 | 蒸馏/加速模型自带的指导强度 |
| **`slg_scale`** | 0.0 | -10–10 | 跳层引导，0 关闭；±1–3 是常用区间 |
| **`clip_skip`** | -1 | -64–12 | -1 = 引擎默认（通常跳 1 层） |
| **`strength`** | 0.75 | 0–1 | 仅改图/图生图：0.35 微调、0.75 常规、1.0 忽略原图 |
| **`seed`** | -1（随机） | -1 或整数 | 复现、A/B 对比必固定 |
| **`count`** | 1 | 1–8（取引擎 `limits.max_batch_count`） | 显存不足时别超 2 |

默认值随模型和面板变，**不要背数字**，用 `params list` / `get_workflow_params` 读实时值：
`source=engine` 表示从 `/sdcpp/v1/capabilities` 读到，`source=tool` 表示引擎没报、用内置兜底。

### 折叠组（默认不动，确需调整再展开）

<details><summary>hires fix：二次放大（9 项）</summary>

| 参数 | 默认 | 取值 | 说明 |
| --- | --- | --- | --- |
| `hires_enabled` | false | true/false | 关时其余 hires 参数不生效 |
| `hires_upscaler` | 引擎值 | `None` `Lanczos` `Nearest` `Latent` `Latent (nearest)` `Latent (nearest-exact)` `Latent (antialiased)` `Latent (bicubic)` `Latent (bicubic antialiased)` | **照抄引擎拼写，Latent 系括号前有空格**；工具会做模糊匹配，但别自己发明写法。Latent 系保细节，Lanczos 最快 |
| `hires_scale` | 2.0 | 1–8 | 按倍数放大，2.0 = 边长翻倍、像素 4 倍；与 `hires_target_*` 二选一，给了目标尺寸就以目标尺寸为准 |
| `hires_target_width` / `hires_target_height` | 0（不指定） | 64–8192 | 目标尺寸，上限受引擎 `limits.max_upscale_*` 约束（本机 8192） |
| `hires_steps` | 0（=沿用主步数） | 1–200 | 第二遍步数 |
| `hires_denoising_strength` | 0.7 | 0–1 | 0.3–0.5 保原图，>0.6 开始重画 |
| `hires_upscale_tile_size` | 128 | 0–4096 | 大图分块放大，0 = 不分块 |
| `hires_custom_sigmas` | 空 | sigma 列表 | 自定义第二遍噪声表，一般不动 |

</details>

<details><summary>VAE tiling：低显存救命键（8 项）</summary>

| 参数 | 默认 | 取值 | 说明 |
| --- | --- | --- | --- |
| `vae_tiling` | false | true/false | 大画布 OOM 时先开这个 |
| `vae_tile_size_w` / `vae_tile_size_h` | 0 | 0–4096 | 0 = 引擎自选 |
| `vae_rel_size_w` / `vae_rel_size_h` | 0.0 | 0–1 | 按画布比例切块，与上面的绝对尺寸二选一 |
| `vae_extra_tiling_args` | 空 | 键值对 | 引擎新增的透传口，非必要不用 |
| `vae_target_overlap` | 0.5 | 0–1 | 拼缝处重叠比例，太低会出接缝 |
| `vae_temporal_tiling` | false | true/false | 视频模型用；图片任务保持关闭 |

</details>

<details><summary>output：输出格式（2 项）</summary>

| 参数 | 默认 | 取值 | 说明 |
| --- | --- | --- | --- |
| `output_format` | png | `png` / `jpeg` / `webp` | 要小体积交付（PPT、微信传图）用 `jpeg`；需要透明底必须留 `png` |
| `output_compression` | 100 | 0–100 | 仅 jpeg/webp 有意义，80–90 肉眼无损、体积减半 |

</details>

<details><summary>high noise：双模型/高噪分支（3 项）</summary>

| 参数 | 默认 | 取值 |
| --- | --- | --- |
| `high_noise_steps` | 引擎值 | 1–200 |
| `high_noise_cfg` | 引擎值 | 0–30 |
| `high_noise_sampler` | 引擎值 | 22 种采样器 |

</details>

<details><summary>SLG 细节（3 项）</summary>

`slg_layers`（默认 `[7,8,9]`）、`slg_layer_start`（0–1）、`slg_layer_end`（0–1）。
只调 `slg_scale` 就够，这三项是精调时才动。

</details>

<details><summary>raw：原样透传（3 项）</summary>

`custom_sigmas`（sigma 列表）、`extra_sample_args`（键值对）、`lora`
（`[{"path":"x.safetensors","multiplier":0.8}]`，需要引擎启动时带 `--lora-model-dir`，否则不可用）。

</details>

### 作用域：单次 / 本会话 / 恢复默认

| 想要的效果 | MCP | CLI | 生命周期 |
| --- | --- | --- | --- |
| 只改这一次 | `generate_image(prompt=..., params={"cfg": 3.5})` | `--param cfg=3.5` | 该次调用 |
| 后面这一串图都改 | `set_workflow_params(params={"cfg":3.5,"scheduler":"karras"})` | 同一进程内 `params set --param cfg=3.5` | MCP 进程内存，进程退出即失效 |
| 全部恢复默认 | `reset_workflow_params()` | `params reset` | 立刻回到 GUI 面板设置 |
| 只恢复某一项 | `reset_workflow_params(keys=["cfg"])` | 重新 `params set` 覆盖 | 同上 |

优先级：**单次 `params` > 会话参数 > LocModel GUI 面板 / 引擎默认**。

会话参数只存在 MCP 进程里，不写任何文件，进程重启自动清空 —— 这是刻意的：
只有这样才不会和 GUI 抢 `config.json`。

**默认行为**：用户没点名要调参数时，**不要主动设置任何工作流参数**，一切按 LocModel
GUI 面板的现有设置出图。参数是用户提要求时才启用的开关，不是每次都要填的表。

**尺寸策略（两档，先看用户有没有给数）**：

| 情形 | 工具怎么做 | 你要说什么 |
| --- | --- | --- |
| 用户**给了** width/height | **按给的数原样出**（只做 8 的倍数对齐）。只有超出引擎硬限（`limits.max_width/max_height`，本机 4096；下限 64）才缩回，并留 warning | 原话照给即可；若回执带显存警告，转述"这张会吃 ~X GB，可能爆显存" |
| 用户**没给**、走 `aspect` 预设 | 工具自己算：下限 `size.min_area`（默认 768² ≈ 59 万像素），上限 `size.max_side`（默认 1536，随显存自动调整），边长对齐 8 | 说明按 A 比例出的是多少像素 |

- 显存估算：`vram_mb_per_mp`（默认 1600 MB/百万像素）对比可用显存，超了只**警告**、不偷偷改尺寸
- 想要工具自己做面积兜底（老行为）→ 显式尺寸 + `guard=true`（MCP）/ 去掉 `--no-guard`（CLI）
- 确实要原样发送、连引擎硬限也不管 → `guard=false`（引擎侧仍可能 400，自担风险）
- 小画布（< 59 万像素）引擎有时返回 `no results`，回执会提示，失败就抬尺寸重试

## 调用方式

MCP 工具：
```
generate_image(prompt="...", aspect="16:9", out_dir="<会话 output 目录>", seed=12345)

# 本次生效
generate_image(prompt="...", params={"cfg": 3.5, "scheduler": "karras"})

# 本会话后续全部生效
set_workflow_params(params={"cfg": 3.5, "steps": 12})
get_workflow_params()            # 看实时默认值、取值范围、来源
get_workflow_params(include_folded=true)   # 连折叠组一起看（hires / vae / output ...）
reset_workflow_params()          # 一条命令全部恢复默认

# 尺寸：给了就按给的数出；只想看将发送什么，先 dry_run
generate_image(prompt="...", width=2048, height=1152, out_dir="<目录>")
generate_image(prompt="...", width=2048, height=1152, dry_run=true)   # 只回 payload + 显存警告
```

CLI：
```bash
python scripts/locmodel_image.py generate --prompt "..." --aspect 16:9 --out <目录> --seed 12345
python scripts/locmodel_image.py generate --prompt "..." --param cfg=3.5 --param scheduler=karras
python scripts/locmodel_image.py params list          # 核心参数 + 实时默认值
python scripts/locmodel_image.py params list --all    # 含折叠组
python scripts/locmodel_image.py params reset         # 全部恢复默认
python scripts/locmodel_image.py status
python scripts/locmodel_image.py doctor
```

输出：`<out>/locmodel_<YYYYmmdd_HHMMSS>_<i>_seed<seed>.<ext>` + `_meta.json`，扩展名随 `output_format`（png / jpg / webp）。

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
python scripts/locmodel_image.py config get size.min_area
python scripts/locmodel_image.py config set size.min_area 589824
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

- 出图后列出**真实生成的图片绝对路径**（不要虚构），批量时说明每张 seed 便于复现
- 尺寸按用户给的原样出时，明确说"就是 512×512"；只有超出引擎硬限（4096）或显存不够才提示，且说清是**警告还是已改尺寸**
- 若开了 hires，说明是"按 2 倍放大"还是"放大到 2048×1152"，并给出实际产出像素
- 失败时给出具体下一步（关哪个程序、改哪个参数），不要只说"失败了"
