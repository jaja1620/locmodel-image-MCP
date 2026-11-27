---
name: locmodel-image
description: 用本机 LocModel（stable-diffusion.cpp / sd-server）的离线引擎出图、改图、生成视频与做对口型视频。当用户要求「画一张 / 生成图片 / 出个图 / 给我几张方案草图 / 把这段描述画出来 / 生成配图」，或要求「改这张图 / 把图里的 X 换成 Y / 在图上加个东西 / 只改这块 / 局部重绘 / 在这张图基础上重画」且希望用本机模型、离线处理时使用。覆盖提示词优化、参数选择、掩码标注、局部重绘与整图重画、参考图改图（保持人物/主体不变只改指定内容）、模型选择与切换、调用入口（MCP 工具或 CLI）、失败排查全流程。不适用：需要精确文字排版的海报（本地扩散模型文字拼写不可靠）、需要联网检索的图。视频部分覆盖 LocModel「视频」选项卡下的两个子界面：「生成视频」（引擎 vid_gen：文生/图生视频）与「对口型视频」（InfiniteTalk + Wan2.1-I2V-14B-480P 独立管线，上传人物图片/视频 + 音频让嘴型跟着唱）。对口型视频必须由用户在 LocModel 里下载并挂载专用模型与运行环境，未挂载将导致整体任务失败，无降级路径。
version: 4.1.0
license: MIT
---

# LocModel Image（本地离线出图）

本技能把本机 LocModel 内置的 `sd-server` 引擎（stable-diffusion.cpp 后端）当作画图工具使用：
**文字提示词进，PNG 文件出，全程离线、不联网、不产生费用。**

两种调用形态，二选一或并用：

| 形态 | 入口 | 适用 |
| --- | --- | --- |
| **MCP 工具**（首选） | 图片：`generate_image` / `ref_edit` / `edit_image` / `redraw_image` / `list_edit_models` / `load_model` / `locmodel_status` / `list_models` / `engine_doctor` / `engine_discover`；视频：`generate_video` / `job_status` / `cancel_job` / `list_recipes`（子界面「生成视频」）、`lipsync_status` / `lipsync_generate`（子界面「对口型视频」） | 客户端支持 MCP 时，工具直挂、有 schema 约束，不依赖模型是否"想起"读文档 |
| **CLI 桥接器** | `python scripts/locmodel_image.py <子命令>`（图片）；`... video <子命令>`（生成视频）；`... lipsync status|run`（对口型视频） | 脚本化、批处理、无 MCP 环境、排障 |

## 能力边界（必须先判断）

可以做：
- 文生图（txt2img），单张或一次多张（`--count`，上限取引擎 `limits.max_batch_count`，本机实测 8）
- 自定尺寸 / 长宽比（1:1 4:3 3:4 3:2 2:3 16:9 9:16 21:9 2:1 1:2）；**显式给 width/height 就按你给的出**，工具不再偷偷放大到 768 或压到 1536
- 固定 seed 复现、负面提示词、步数 / sampler / CFG 覆盖
- 输出格式 `png` / `jpeg` / `webp`（`output_format`，JPEG/WebP 可配 `output_compression`）+ 同名 `_meta.json`（记录 prompt、seed、尺寸、耗时，便于复现）
- 二次放大：hires fix 按**倍数**（`hires_scale`）或按**目标尺寸**（`hires_target_width/height`）两种给法
- **改图**：给定一张已有图片，用一句话指定"这块改成什么"（局部重绘 / inpaint），或"整体按新描述重画"（`redraw`）
- **参考图改图**（`ref_edit` 或 `generate_image(ref_images=...)`）：给 1–8 张参考图，只写"要改什么"，引擎以参考图锚定主体身份（换发型 / 换服装 / 换背景而人不变成另一个人）
- 掩码来源三选一：掩码 PNG、矩形 / 多边形坐标、按颜色阈值选区域；掩码外的像素由 CPU 侧还原为原图，不会被改动
- 模型选择：自动按优先级挑可编辑模型，也能指定具体模型或让引擎先加载
- **生成视频**：`generate_video` 走引擎 `POST /sdcpp/v1/vid_gen`，文生视频 / 图生视频（`init_image`）/ 首尾帧（`end_image`）/ 参考图（`ref_images`）/ VACE 控制帧，输出 webm / webp / avi；异步 job + `job_status` 续查
- **对口型视频**：`lipsync_generate` 用一张人物图片 + 一段音频，生成嘴型跟着歌/念白走的 mp4（InfiniteTalk + Wan2.1-I2V-14B-480P，LocModel 自带独立管线）——**前提是用户已在 LocModel 里挂载专用模型与运行环境**，见下文「对口型视频」

做不到（不要承诺）：
- 图内精确文字、版式排版 → 后期在 PPT/PS/Figma 里叠字
- 联网素材、写实人脸精修、带音效/配乐的多镜头叙事剪辑（本技能只出单条无声视频片段，对口型除外——它自带原曲音轨）
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
| "这个人不变，把发型换成短发"、"换件外套"、"背景换成海边"、"保持脸，只改颜色" | `ref_edit` | 参考图通道，身份由参考图锚定，不是重画 |
| 没有原图 | `generate_image` | 文生图 |

### 参考图改图：身份不变，只改指定的东西（v4.1 / LocModel 3.6.5+）

用户说"人别变、只改发型/衣服/背景"时**必须**走这条通道。用 `edit_image` 会重绘主体、人脸被换成另一个人；用 `redraw_image` 也不保身份。

```python
ref_edit(prompt="same person, short black bob with a blunt fringe",
         ref_images=[r"D:\in\me.png"],          # 1–8 张，路径 / data URI / 裸 base64 均可
         ref_fit="follow_first",                 # 默认：画布随第一张参考图；要给自己的宽高就传 "image"
         ref_max_pixels=1048576,                 # 像素预算，默认 1024²，上限 4194304
         seed=42)
```

四条规矩：

1. **提示词只写改动内容**（"same person, ..."），不要把原图里的人再描述一遍，否则等于让模型重画。
2. **不要传 `strength`**。参考图通道不切片 sigmas，`strength` 传了也不生效；工具直接拒绝 `ref_strength` 并指路 `redraw_image`。同传 `init_image` 会被自动剥离并记进 `notes`。
3. **单数 `ref_image` 会报错**。引擎对单数字段不报错而是静默丢弃，出图退化成纯文生图（`fn main()`）；工具在客户端就拦下，改写成 `ref_images=[...]`。
4. **看 `engine_trace` 验收**。`fn main(reference_0)` + `ref_latents=[reference_latent_0]` 才是真走了通道；`fn main()` 就是没绑上参考图。摘要里的 `engine:` 行直接给结论。

预算与尺寸：画布默认按第一张参考图的长宽比推导，压到 `ref_max_pixels` 以内并按 recipe 的 `size_multiple`（本机 16）对齐；`ref_fit="image"` 时用你给的 `width`/`height`，超出预算会提示但照出。

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

## 视频：先分清两个子界面（分错了必失败）

LocModel 的「视频」选项卡下并排两个子界面，底层实现完全不同，工具不能互换：

| GUI 子界面 | 界面形态 | 底层实现 | 本技能入口 | 走引擎端口 |
| --- | --- | --- | --- | --- |
| 视频 → **生成视频** | 填提示词 / 首帧图，选模型与尺寸 | sd-server 的 `POST /sdcpp/v1/vid_gen`（Wan2.2-TI2V、Wan2.1、LTX-2.3、MiniMax-H3 等扩散 recipe） | `generate_video` → `job_status` | 是（sd-server 端口） |
| 视频 → **对口型视频** | **独立子界面**：上传人物图片（或视频）+ 一首歌 | InfiniteTalk + Wan2.1-I2V-14B-480P，独立 Python 3.12 venv、独立模型目录、本地 `runner.py` 子进程 | `lipsync_status` → `lipsync_generate` | **否**（与 sd-server 完全隔离） |

按用户原话选工具，不要因为"都是视频"就合并：

- 「生成一段视频 / 文生视频 / 图生视频 / 让这张图动起来 / 来个空镜」→ `generate_video`
- 「对口型 / 让这张照片唱歌 / 人物开口说话 / 数字人播报 / 嘴型对上这段音频」→ `lipsync_generate`
- 只要诉求里出现"音频驱动嘴型"，就不能用 `generate_video`：引擎侧没有 audio→lip 通道，`ref_audios` / `ref_video_audios` 无论传什么都是 400
- 反过来 `lipsync_generate` 也不接管提示词出片：它必须有「一张人物图片或视频 + 一段音频」，只做单人口型驱动

### 生成视频（视频 → 生成视频，引擎 vid_gen）

前置：当前引擎实例必须处于 `vid_gen` 模式。图像实例与视频实例**不能共存**——LocModel 同一时间只跑一个模式：

```
locmodel_image.py video status        # 或 MCP: generate_video(dry_run=true 前置不查这个)
```

`mode.current_mode` 不是 `vid_gen` 时，工具会报 `video_mode_required`；此时让用户在 LocModel「视频 → 生成视频」页面点一次生成（GUI 会把引擎切到该模式），或先跑一次 GUI。**不要**用 `load_model` 去"切模式"，那是图像模型接口。

关键参数（MCP `generate_video` 与 CLI `video generate` 同名）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `prompt` | 必填 | 镜头描述。写"谁在做什么 + 镜头怎么动"，例：`a slow dolly-in on a modular cabin in snowfall, warm interior light` |
| `width` / `height` | 832×480 | 8 的倍数（部分 recipe 要 16 或 32）；显存 8 GB 建议 ≤832×480 |
| `video_frames` | 引擎按 recipe 定 | 帧数会被 recipe 的 `frame_step`（4 / 8 / 17）取整，写多少都不报错，但结果可能不是你写的数 |
| `fps` | 引擎默认（Wan2.2-TI2V 为 16） | 部分 recipe 固定 24 fps，改不动 |
| `steps` / `flow_shift` / `cfg_scale` / `sampler` | 引擎默认（如 4 / 12.0 / 1.0 / euler） | `scheduler` 不要写 `default`，引擎回 400 |
| `output_format` | `webm` | 只能是 `webm` / `webp` / `avi`（**没有 mp4**，要 mp4 请用户自己转，或用 `lipsync_generate`） |
| `init_image` | — | 图生视频首帧：本地路径 / data URI / 裸 base64 都收 |
| `end_image` | — | 首尾帧 recipe 的尾帧 |
| `ref_images` / `control_frames` | — | 参考图数组 / VACE 控制帧数组 |
| `wait_seconds` | `0` | **默认立即返回 `job_id`**：一条 480P 片段要几分钟，MCP 客户端会超时。要阻塞就设成秒数 |

异步三步（默认路径）：

1. `generate_video(prompt=...)` → 立刻拿到 `job_id`、`poll_url`
2. `job_status(job_id=...)` 轮询；`status` 走 `queued` → `running` → `completed`，`progress.stage` 会给出当前阶段
3. `completed` 后工具自动把视频落到输出目录并返回**绝对路径**；`failed` 时看 `error`

引擎只在内存里保留结果约 **600 秒**（`--completed-ttl-seconds=600`），超时不再可取——所以拿到 `job_id` 后要接着轮询，别拖到第二天。

本机实测（LocModel 3.6.1，RTX 3060 Ti 8 GB）：Wan2.2-TI2V-5B-Q4_K_M + umt5-xxl-encoder-Q8_0 + wan2.2_vae，832×480 / 9 帧 / fps16 / 2 steps → 1.27 MB 合法 WebM，耗时 123 s（含首次加载模型约 40 s、VAE 解码 21 s）。

```bash
# 提交并立即返回 job_id（默认）
python scripts/locmodel_image.py video generate --prompt "slow dolly-in on a wooden cabin, snowfall" \
  --width 832 --height 480 --frames 9 --fps 16 --out "D:/out"

# 直接阻塞等到完成（一条 480P 片子通常 2–5 分钟，留足 --timeout）
python scripts/locmodel_image.py video generate --prompt "..." --wait 900

# 续查 + 取消 + 看 video recipe
python scripts/locmodel_image.py video job <job_id> --out "D:/out"
python scripts/locmodel_image.py video cancel <job_id>
python scripts/locmodel_image.py video recipes
```

常见坑：

- 视频实例在跑时**图像出图会全部失败**（模式互斥）：要么让用户切回图像页面，要么等视频跑完
- 文本编码器 umt5-xxl 的 Q8 版要 ≥6 GB **空闲内存**，不够时报 `RAM weight capacity exceeded`，最终表现为 `generate_video returned no results`
- `high_noise_steps` 只对双专家模型（Wan2.2-A14B）有效，Wan2.2-TI2V 传它直接 400
- 引擎对未知字段静默 202，所以"没报错但没反应"多半是参数名写错，客户端校验才是唯一防线

### 对口型视频（视频 → 对口型视频，LocModel 独立管线）

**这不是 sd-server 的功能，是 LocModel 内置的另一套管线。**证据：LocModel 3.6.1 主程序字符串表，以及它写到磁盘的 `%LOCALAPPDATA%\LocModel\lipsync\runner.py`（本技能内置同源副本 `scripts/lipsync_runner_embed.py`，用于 GUI 没跑过、`runner.py` 还没落盘时自动补齐）。

> **硬约束：未在 LocModel 挂载专用模型 → 整体任务失败。**
> 这条管线不下载即不可用：没有模型、没有 venv、没有 `runner.py` 就直接失败，**本工具不会降级、不会改走 `generate_video`、不会调用云端**。遇到这种情况的唯一正解是让用户打开 LocModel →「视频」→「对口型视频」→「一键准备」把模型与运行环境装完，再重新调用。

它用的模型（都在 LocModel 的「一键准备」里，约 24.2 GB）：

| 角色 | 文件 / 组件 | 说明 |
| --- | --- | --- |
| 视频主干 DiT | `Wan2.1-I2V-14B-480P` | high 版 fp8（约 17 GB）/ small 版 Q4_K_M GGUF |
| 口型驱动 | `InfiniteTalk Single` | 音频交叉注意力权重；high=fp8，small=Q4_K_M GGUF |
| 加速 LoRA | `lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16` | 4 步蒸馏，推理时叠加（`accel=true` 时启用） |
| 文本编码 | high：`t5_fp8.safetensors` + `t5_map_fp8.json`（optimum-quanto fp8）；small：`umt5-xxl-encoder-Q8_0.gguf` | 配 `umt5-xxl` tokenizer；两档任选其一即算就绪，**不要**因为没有 fp8 就判定缺件 |
| 图像条件 | open-clip `ViT-H/14` + `xlm-roberta-large` | 人物外观条件 |
| VAE | `Wan2.1_VAE.pth` | 编解码 |
| 音频特征 | `chinese-wav2vec2-base`（`wav2vec/`） | 中文音频特征提取 |
| 人声分离 | Demucs `hybrid-transformer 955717e8-...th` | `vocals=auto` 时先分离人声再驱动口型 |

运行环境（与 LocModel 本体完全隔离，约 6 GB）：

- 独立 **venv（Python 3.12，uv 创建）**，不是 LocModel 自带的 Python
- 关键包：`torch(CUDA)`、`triton-windows<3.4`、`sageattention==1.0.6`、`diffusers==0.33.1`、`transformers==4.51.3`、`accelerate==1.6.0`、`optimum-quanto==0.2.6`、`gguf==0.17.1`、`demucs==4.0.1`、`openunmix==1.3.0`、`numpy==1.26.4`
- 环境变量：`TRITON_CACHE_DIR=<lipsync>/triton-cache`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、`PYTHONUTF8=1`
- 硬件：**必须 NVIDIA CUDA**，显存 ≥8 GB；small 版建议 ≥16 GB 内存，high 版 ≥48 GB 内存
- 路径：模型根默认 `<模型目录>/lipsync`（本机是 `F:\AI-Models\lipsync`，可在 LocModel 设置里改）；程序侧 `<程序数据目录>\lipsync\`（venv、`runner.py`、`jobs\`）

版本选择（GUI 里的「自动 / 高质量版 / 省内存版」）：`auto` 会按内存自动决定——本机 16 GB 内存 → 自动选**省内存版**（high 版要 48 GB 内存起步）。可用环境变量 `LOCMODEL_LIPSYNC_VARIANT=high|small|auto` 或配置 `lipsync.variant` 固定。

#### 调用流程（顺序不能颠倒）

1. **`lipsync_status()`** —— 必做第一步。返回 `ok`、`variant`（实际会用的版本及理由）、`models`（13 个必需件的真实路径）、`missing`（缺哪些）、`paths`、`hardware`。
2. `ok=false` → **立刻停手，把缺口告诉用户**：列出 `missing[].need`，并给出"LocModel → 视频 → 对口型视频 → 一键准备"这一步。不要尝试自己 pip 装、不要下载权重、不要伪造路径。
3. `ok=true` → `lipsync_generate(image=..., audio=...)`。

```bash
python scripts/locmodel_image.py lipsync status          # 就绪体检（JSON）
python scripts/locmodel_image.py lipsync run --image "D:/me.jpg" --audio "D:/song.mp3" \
  --prompt "a young woman singing warmly to the camera in a cafe" --res 480 --output-dir "D:/out"
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `image` | 必填 | 人物图片（png/jpg/webp/bmp/tif）或人物视频（mp4/mov/mkv/webm…） |
| `audio` | 必填 | 歌或念白（mp3/wav/m4a/flac/ogg/aac） |
| `prompt` | `a person is singing and speaking to the camera, natural head motion` | 场景 / 行为描述 |
| `negative` | LocModel 内置负面词 | 一般不用动 |
| `res` | `480` | `480` / `720` / `custom`；8 GB 显存就用 480 |
| `width` / `height` | — | 仅 `res=custom` 时必填，256–1920，会被对齐到 16 的倍数 |
| `crop` | 全图 | 人脸裁切框（归一化），人像在画面里偏小时可指定 |
| `steps` | 引擎默认（加速 LoRA 下 4 步左右） | 调高更慢 |
| `accel` | true | 用 lightx2v 蒸馏 LoRA 少步出片 |
| `vocals` | `auto` | 先 Demucs 分离人声再驱动口型；原曲音轨仍保留在成片里 |
| `color_fix` | 1.0 | 色彩校正强度 |
| `crf` | 18 | 成片质量（LocModel GUI 出厂 17）；越高越小越糊 |
| `vae_tiled` / `sage` | false / true | 省显存 / 用 SageAttention 加速 |

产物：`LocModel_<时间戳>_lipsync.mp4`（libx264，25 fps，音轨用原曲），工具返回**绝对路径**。

耗时要有心理预期，也要提前告诉用户：**4090 上 480P 一分钟的歌要十几分钟到半小时**；3060 Ti 8 GB + 16 GB 内存更慢，长音频建议先剪到 30–60 秒试片。别把它当"几秒钟出片"的工具，也不要因为超时就反复重提——重提只会让队列更长。

#### 两档布局的文件名对照（看 status 的 missing 时用）

`lipsync_status` 不写死发行文件名，而是按 `<模型目录>\lipsync\` 下的路径特征逐个探（大小写不敏感）。两档布局不同，下面是它实际认的东西：

| 角色（`missing[].need`） | 认的路径特征 | 两档差异 |
| --- | --- | --- |
| `dit` | `Wan2*I2V*14B*480P*.(safetensors\|gguf)`（排除 infinitetalk / lightx2v / lora / t5） | high 是 `fp8` safetensors，small 是 `Q4_K_M` gguf |
| `infinitetalk` | `infinitetalk*.(safetensors\|gguf)` | high = fp8，small = Q4_K_M |
| `t5` | `t5_fp8.safetensors` / `t5*.safetensors` / `umt5-xxl-encoder*.gguf` | **两档任一即算就绪**：high 用 fp8（还要 `t5_map_fp8.json`），small 用 Q8_0 gguf 且不需要 map |
| `vae` | `Wan2.1_VAE.pth` / `Wan*VAE*.(pth\|safetensors)` / `wan*vae*.safetensors` | high 在 `base/` 下；small 是 `split_files/vae/wan_2.1_vae.safetensors`（小写、不在 base 下） |
| `umt5` | `google/umt5-xxl/` 或 `umt5-xxl/` 目录 | 两档共用 |
| `xlm_roberta` | `xlm-roberta-large/` 或 `xlm_roberta_large/` 目录 | 两档共用 |
| `clip` | `models_clip_open-clip*.pth` | 两档共用 |
| `wav2vec` | `wav2vec/{preprocessor_config,config}.json` 或 `chinese-wav2vec2*config.json` | 两档共用 |
| `demucs` | 任意 `*.th` | 两档共用 |
| `lora` | `lightx2v*.safetensors` | 两档共用（缺它不影响 `ok`，只在 `notes` 里提示） |
| `base` | `base/config.json`（锚点） | 缺失时回退到 tokenizer 的 `base/` 祖先，再回退到 VAE 父目录 |

本机 16 GB 内存 → `auto` 判为省内存版，所以只探 small 这一列；small 齐了就 `ok=true`，不会因为缺 fp8 而卡住。

#### 本机实测状态（2026-11-24，写文档时）

| 检查项 | 结果 |
| --- | --- |
| 模型根 `F:\AI-Models\lipsync` | **不存在**（`D:` / `F:` / `G:` 三个 AI-Models 盘都没有 `lipsync` 目录） |
| 程序侧 `%LOCALAPPDATA%\LocModel\lipsync` | 目录存在但**为空**：没有 venv、没有 `code\`、没有 `runner.py`、没有 `jobs\` |
| `settings.json` 里 `lipsync_variant` | `""`（用户没选过版本） |
| `lipsync_status` | `ok=false`，13 个必需件全部 `missing`，判定为省内存版（auto：15.8 GB RAM < 48 GB） |
| `lipsync_generate` | 直接抛 `lipsync_not_mounted`，提示"未在 LocModel 挂载专用模型将导致整体任务失败" |

也就是说：**本机当前调不了对口型视频**，这是模型未挂载的正常结果，不是 bug。用户挂载完成后再跑 `lipsync status`，`ok=true` 才可生成。

#### 对口型视频失败对照

| 现象 / 错误码 | 原因 | 处理 |
| --- | --- | --- |
| `lipsync_not_mounted` | 专用模型或 venv 没挂载 | 让用户去 LocModel「视频 → 对口型视频 → 一键准备」，装完重试；**不要重试同一个调用** |
| `runner_missing` | `runner.py` 还没被 GUI 写出，内置副本也缺失 | 让用户在 GUI 里点一次「开始生成」，LocModel 会写出 `runner.py` |
| `bad_res` | `res=custom` 没给宽高，或宽高超 256–1920 | 改 `res=480` 或给合法宽高 |
| `bad_arguments` | 图片/音频扩展名不在白名单，或文件不存在 | 换格式（图片 png/jpg，音频 mp3/wav） |
| CUDA / 显存相关报错 | 显存不够（8 GB 下 720P 基本必爆） | 回到 480P，关掉其它占显存程序 |
| 出片嘴型呆滞 / 不同步 | 音频里有人声以外的强伴奏、或音频太长 | `vocals=auto` 保留；把歌剪短到一段副歌再试 |
| 跑很久没动静 | 正常：模型加载 + 逐段生成，一分钟歌十几分钟起 | 耐心等，别重复提交 |

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
| `video_mode_required` | — | 引擎当前实例不是 `vid_gen` 模式（图像实例与视频实例互斥） | 让用户在 LocModel「视频 → 生成视频」点一次生成切模式；不要用 `load_model` 切 |
| `RAM weight capacity exceeded` → `generate_video returned no results` | 2 | umt5-xxl Q8 文本编码器要 ≥6 GB 空闲内存 | 关掉占内存的程序；换更小的编码器或先用 480P 小配置 |
| 视频 job 一直 `queued` | — | 引擎单 slot，视频一次只跑一个作业 | 等前一个跑完；`job_status` 会返回 `queue_position` |
| job `failed` 且已经过了 10 分钟才查 | — | 引擎只保留完成结果 600 秒 | 重新提交；下次拿到 `job_id` 立刻轮询 |
| `bad_arguments` + `ref_image` | — | 传了单数 `ref_image` | 引擎会静默忽略它、退化成纯文生图；改传 `ref_images=[...]` |
| `bad_arguments` + `ref_strength` | — | 参考图通道没有 strength 语义 | 删掉该参数；要按强度重画用 `redraw_image` |
| 参考图改图后人物变成另一个人 | — | 没走 ref 通道（退化为文生图），或同传了 `init_image` | 看摘要 `engine:` 行有没有 `ref_latents`；确认工具是 `ref_edit` |
| 参考图改图"改不动" | — | 提示词把原图描述了一遍，或要改的幅度太小 | 提示词只留改动内容；必要时换 seed；局部改动改用 `edit_image` + 掩码 |
| `lipsync_not_mounted` | — | 对口型专用模型 / venv 未挂载 | 让用户走 LocModel「视频 → 对口型视频 → 一键准备」，装完再 `lipsync status` |

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

# 子界面「生成视频」（引擎 vid_gen，异步）
python scripts/locmodel_image.py video status                 # 当前引擎模式 + capabilities
python scripts/locmodel_image.py video generate --prompt "slow dolly-in on a wooden cabin" --frames 9 --fps 16
python scripts/locmodel_image.py video job <job_id> --out "D:/out"
python scripts/locmodel_image.py video cancel <job_id>
python scripts/locmodel_image.py video recipes                 # 3.6.x 才有；3.3.0 会 404

# 子界面「对口型视频」（InfiniteTalk 独立管线，先体检再出片）
python scripts/locmodel_image.py lipsync status
python scripts/locmodel_image.py lipsync run --image "D:/me.jpg" --audio "D:/song.mp3" --res 480 --output-dir "D:/out"
```

## 给用户的交付话术

- 出图后列出**真实生成的图片绝对路径**（不要虚构），批量时说明每张 seed 便于复现
- 尺寸按用户给的原样出时，明确说"就是 512×512"；只有超出引擎硬限（4096）或显存不够才提示，且说清是**警告还是已改尺寸**
- 若开了 hires，说明是"按 2 倍放大"还是"放大到 2048×1152"，并给出实际产出像素
- 失败时给出具体下一步（关哪个程序、改哪个参数），不要只说"失败了"
- 视频出片后同样只报**真实文件绝对路径**；引擎只输出 webm/webp/avi，用户要 mp4 时说明"可以转码"或改用对口型管线（它直接出 mp4）
- 视频默认异步：拿到 `job_id` 就告诉用户"已提交，我盯着"，然后轮询，不要反复重提同一个任务
- 对口型视频交付时必须说清三件事：① 用的是 InfiniteTalk + Wan2.1-I2V-14B-480P 本地管线，不是文生视频；② 成片保留原曲音轨、25 fps、mp4；③ 耗时随显卡与音频长度变化（4090 上 480P 一分钟歌约十几分钟到半小时）
- 对口型**未挂载模型**时，明确告知"没在 LocModel 挂载专用模型会导致整体任务失败，无法降级"，并指向「视频 → 对口型视频 → 一键准备」，不要承诺"我帮你装好"
