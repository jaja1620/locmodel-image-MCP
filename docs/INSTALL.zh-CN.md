# 安装与使用说明 —— locmodel-image 3.0.0

[English](./INSTALL.md) | 简体中文

`locmodel-image` 把本机 LocModel（`stable-diffusion.cpp` / `sd-server`）的画图引擎封装成一个
MCP 服务和一个命令行桥接器。全部在本机离线运行：不需要 API Key，不联网，不按张计费。

提供两种安装方式，**任选其一**即可，不必都装。

| 方式 | 适用 | 目标机需要 Python 吗 |
| --- | --- | --- |
| **A. 安装程序（单文件 exe）** —— 推荐 | 普通用户、设计人员、办公电脑 | **不需要** |
| **B. 源码安装（pip）** | 开发者、需要改代码的人 | 需要，Python 3.9+ |

两种方式最终注册的是同一个 MCP 服务，因此「使用」部分完全一致。

---

## 0. 前置条件（两种方式都需要）

1. **Windows 10/11（x64）**；源码方式另支持 Linux / macOS。
2. **已安装 LocModel 桌面端**，并至少下载了一个可加载的画图模型。
   引擎本体 `sd-server.exe` 与模型文件由 LocModel 提供，本项目不下载也不分发它们。
3. **显存够用**。8 GB（如 RTX 3060 Ti）跑 Q2_K/Q6_K 量化模型很从容；文生图 6 GB 可用；
   局部重绘建议 8 GB 及以上。
4. 若要工具直挂，需要**支持 MCP 的客户端**：Claude Desktop、Claude Code、Cursor、VS Code、
   Windsurf、DSH、Hermes 或 Marvis。

安装前先确认引擎活着：打开 LocModel，加载一个画图模型，窗口保持不关。
端口不要假设，服务会自动发现（v2.0 起五级自发现机制）。

---

## 1. 方式 A —— 通过安装软件安装（单文件 exe，无需 Python）

### 1.1 下载

打开 [Releases 页面](https://github.com/jaja1620/locmodel-image-MCP/releases)，选择 `v3.0.0`，下载：

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `locmodel-image-setup.exe` | 7 928 599 字节（7.56 MB） | `ED1C09CAFC020B862E8FB2A3C0FB88EDE85B220EBC5EB4D9FDFA2D5C1A2526D1` |

可选的完整性校验（PowerShell）：

```powershell
Get-FileHash .\locmodel-image-setup.exe -Algorithm SHA256
# 应输出 ED1C09CAFC020B862E8FB2A3C0FB88EDE85B220EBC5EB4D9FDFA2D5C1A2526D1
```

### 1.2 安装

**双击** `locmodel-image-setup.exe`。它是控制台程序：会弹出一个窗口，先列出即将执行的操作，
等你确认后按回车（或输入 `y`）继续。装完打印汇总并停住等你阅读，窗口不会一闪就消失。

默认安装到 `%LOCALAPPDATA%\locmodel-image`，并把 MCP 服务注册到本机检测到的所有受支持客户端。

偏好命令行？在下载目录打开 PowerShell：

```powershell
# 全自动，全部用默认值
.\locmodel-image-setup.exe install --mode exe --yes

# 只注册到指定客户端
.\locmodel-image-setup.exe install --targets dsh,hermes --yes

# 预演：只显示将要写入的内容，不改任何文件
.\locmodel-image-setup.exe install --dry-run

# 装到别的位置
.\locmodel-image-setup.exe install --dir "D:\Tools\locmodel-image" --yes
```

> Windows SmartScreen 可能对未签名 exe 报警。选择「更多信息」→「仍要运行」，
> 或先按上面的 SHA-256 校验。

### 1.3 安装程序具体做了什么

1. 把运行载荷解包到安装目录（默认 `%LOCALAPPDATA%\locmodel-image`）：
   `scripts/locmodel_image.py`、`scripts/locmodel_mcp.py`、`scripts/locmodel_edit.py`、
   `SKILL.md`、`README*`、`annotator/mask_annotator.html`，外加安装器自身的一份副本——
   这份副本同时充当常驻的 MCP stdio 服务进程。
2. 把 MCP 服务条目写入检测到的各客户端配置。
3. 跑一遍冒烟测试（MCP 握手 + 工具列表）与引擎自检。
4. 除自己那一条外，不动客户端原有配置内容。

支持的客户端与写入位置：

| 客户端 | 配置文件 | 写入键 |
| --- | --- | --- |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` | `mcpServers` |
| Claude Code | `~/.claude.json` 或 `~/.claude/settings.json` | `mcpServers` |
| Cursor | `%APPDATA%\Cursor\User\mcp.json` 或 `~/.cursor/mcp.json` | `mcpServers` |
| VS Code | `%APPDATA%\Code\User\mcp.json` | `servers`（带 `type: stdio`） |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |
| DSH | `~/.dsh/mcp.json` | `mcpServers` |
| Hermes | `~/.hermes/config.yaml` | `mcp_servers` |
| Marvis | 无文件配置 | 由 `emit marvis` 输出 `mcp_install` / `skill_install` 指令 |

几项值得知道的安全行为：改配置前先备份为 `*.bak.<时间戳>`；只增删自己那一条，未知键原样保留；
重复安装返回 `unchanged`，不产生多余备份；现有 JSON 解析失败时直接拒绝写入，不做「修复性」覆盖。

### 1.4 Marvis 用户：多一步

Marvis 走对话指令，不写文件。执行：

```powershell
.\locmodel-image-setup.exe emit marvis
```

它会打印两段（`mcp_install` 与 `skill_install`），粘到 Marvis 对话框里让它执行，服务与 Skill
就都注册好了。

### 1.5 验证

```powershell
.\locmodel-image-setup.exe list         # 哪些客户端拿到了条目、是否与实际一致
.\locmodel-image-setup.exe doctor       # Python/引擎/模型/显存/输出目录/配置 自检
```

`doctor` 全绿退出码 `0`，有不通过项退出 `1`。引擎没开时它会报「引擎未就绪」并给提示，
这**不代表** MCP 注册失败。

### 1.6 升级与卸载

```powershell
# 升级：直接再运行新版 exe，原地覆盖并保持客户端配置一致
.\locmodel-image-setup.exe install --mode exe --yes

# 卸载但保留安装目录
.\locmodel-image-setup.exe uninstall --yes

# 卸载并清空目录
.\locmodel-image-setup.exe uninstall --purge --yes
```

若 exe 仍被宿主进程占用，卸载会拒绝删除任何文件并告诉你该关哪个进程，绝不留半删状态。

退出码：`0` 成功、`1` 自检不过、`2` 参数或环境错误、`130` 用户中断。

---

## 2. 方式 B —— 源码安装（pip，原有方式）

```bash
git clone https://github.com/jaja1620/locmodel-image-MCP.git
cd locmodel-image-MCP
pip install -e .
```

要求 Python 3.9+，无需其他依赖——桥接器零依赖，只用标准库。

手动注册 MCP 服务。通用 JSON 写法（Claude Desktop / Cursor 等）：

```json
{
  "mcpServers": {
    "locmodel-image": {
      "command": "python",
      "args": ["C:/path/to/locmodel-image-MCP/scripts/locmodel_mcp.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

`python` 与脚本路径都用**绝对路径**。Windows 上若装了多个 Python，请写完整解释器路径
（如 `C:/Python311/python.exe`）。

完全不用客户端也能跑命令行：

```bash
python scripts/locmodel_image.py status
python scripts/locmodel_image.py generate --prompt "modern modular building, dusk, wide angle" --aspect 16:9
```

方式 A 的安装器也能替你完成这类注册：`--mode python --python <解释器路径>`。

---

## 3. 怎么用

### 3.1 MCP 工具（共 9 个）

| 工具 | 作用 |
| --- | --- |
| `generate_image` | 文生图。提示词、尺寸或长宽比、seed、一次 1–4 张、负面提示词。 |
| `edit_image` | 局部重绘。只在掩码内重画，掩码外像素由 CPU 侧还原为原图。 |
| `redraw_image` | 整图重画，原图只作为构图与色彩参考。 |
| `list_edit_models` | 哪些模型能改图、自动选择会挑谁、为什么。 |
| `load_model` | 显式把指定模型加载进引擎。 |
| `locmodel_status` | 引擎状态、已加载模型、显存、配置摘要。 |
| `list_models` | 本机 LocModel 里可用的模型。 |
| `engine_doctor` | 引擎/端口/模型诊断。 |
| `engine_discover` | 强制重新扫描引擎端口（五级发现）。 |

在任何客户端里的典型第一步：

```text
locmodel_status()                                  # 引擎就绪了吗
generate_image(prompt="interior of a timber meeting room, soft north light, 35mm",
               aspect="4:3", out_dir="D:/out")
```

产物：`<out>/locmodel_<YYYYmmdd_HHMMSS>_<i>_seed<seed>.png`，同名 `_meta.json` 记录提示词、
seed、尺寸与耗时，便于复现。

### 3.2 改一张已有的图

掩码有三种给法，挑现场最方便的：

1. **矩形 / 多边形坐标**（推荐，与分辨率无关）：
   `regions={"unit":"norm","regions":[{"type":"rect","x":[0.12,0.10,0.48,0.42]}]}`
2. **掩码 PNG**：白=重绘，黑=保留；尺寸不一致会自动缩放。
3. **颜色阈值**：按颜色选区域，适合「把这面纯色墙换材质」。

手上没有坐标？`annotator/mask_annotator.html` 是单文件网页——双击打开，把图拖进去，
用矩形/多边形/画笔圈出区域，导出 `mask.png` 或复制坐标 JSON，粘给 `edit_image` 即可。

```text
edit_image(init_image="D:/out/v1.png",
           prompt="night-time glass curtain wall, interior lights on, warm reflections",
           regions={"unit":"norm","regions":[{"type":"rect","x":[0.25,0.18,0.62,0.55]}]},
           strength=0.8)
```

交付时值得说清的三件事：

- **掩码外没有被改动**——这是硬保证，由 CPU 侧合成实现，不依赖引擎自觉。
- `files[].path` 是**已保护**的图；引擎未经保护的原始输出另存为同目录 `*_raw.png`
  （`files[].raw`），只在排查「引擎到底画了什么」时才需要看。
- 原图不会被覆盖，每次都写新文件，用户可对比后决定留哪张。

### 3.3 命令行速查

```bash
# 环境自检：Python 版本 / 引擎 / 模型 / 显存 / 输出目录 / 配置
python scripts/locmodel_image.py doctor

# 只打印将发送的 payload，不出图（快速验参数）
python scripts/locmodel_image.py generate --prompt "test" --aspect 1:1 --dry-run

# 出图
python scripts/locmodel_image.py generate --prompt "..." --aspect 16:9 --out D:/out --seed 12345

# 哪些模型能改图、自动会选谁
python scripts/locmodel_image.py edit-models

# 局部重绘（矩形 / 掩码文件）、整图重画
python scripts/locmodel_image.py edit --image a.png --prompt "oak slat ceiling" --region 0.1,0.1,0.5,0.4 --strength 0.8
python scripts/locmodel_image.py edit --image a.png --mask mask.png --prompt "..."
python scripts/locmodel_image.py redraw --image a.png --prompt "watercolour architectural sketch" --strength 0.65

# 模型与端口
python scripts/locmodel_image.py load-model "Qwen-Image-Edit"
python scripts/locmodel_image.py discover

# 配置
python scripts/locmodel_image.py config show
python scripts/locmodel_image.py config get size.max_side
python scripts/locmodel_image.py config set size.max_side 1280
python scripts/locmodel_image.py config path
```

### 3.4 小模型也能读懂的提示词写法

结构：`<主体> + <场景> + <构图与视角> + <光线> + <材质与细节> + <风格> + <画质词>`。

- 具体名词优于形容词堆叠：`curved glass curtain wall` 好于 `beautiful modern building`。
- 摄影感加镜头语言：`35mm`、`wide angle`、`eye level`、`golden hour`。
- 建筑/室内写清材质与光：`warm oak`、`terrazzo floor`、`soft north light`。
- 可用的风格锚点：`architectural visualization`、`watercolour sketch`、`isometric diagram`。
- 提示词控制在 ~60 token 以内；turbo 模型步数少，长提示词反而稀释有效信息。

### 3.5 关键配置项

配置文件：`%USERPROFILE%\.locmodel-image\config.json`（可用 `LOCMODEL_IMAGE_CONFIG` 覆盖）。

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `size.min_area` | 768² ≈ 59 万像素 | 小于此值等比放大，保留长宽比 |
| `size.max_side` | 1536（随显存自动调整） | 大于此值等比缩小 |
| `generate.max_count` | 4 | 一次 `count` 的上限 |
| `edit.priority` | `qwen-image-edit`、`flux2-klein`、`qwen-image` | 自动选模型的优先级 |
| `edit.auto_load` | `true` | 自动加载可用的最优改图模型 |
| `edit.prefer_loaded` | `true` | 不为「升优先级」白切模型（切换一次 30–60 秒） |
| `edit.snapshot_cache_ttl` | 120 秒 | 模型列表缓存，跨进程生效 |

引擎端口五级发现：`LOCMODEL_SD_PORT` → 缓存的 `config.json` → `sd-server` 进程命令行 →
候选端口扫描 → 本地监听端口扫描。

---

## 4. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `engine_not_found`（退出码 3） | LocModel 没开，或没加载模型 | 打开 LocModel 并加载画图模型，再 `engine_discover` |
| `generation_failed` + `no results` | 显存不足，或两个引擎抢卡 | 关掉 LM Studio / 吃显存的浏览器标签；降尺寸或 `count` |
| 一直 `queued` 不动 | 引擎单 slot | 等待，前面还有作业 |
| 出图很慢（>3 分钟） | 尺寸过大 / 步数过高 / 显卡被占 | 降尺寸、`steps` 回默认、释放 GPU |
| 提示词像没生效 | `cfg` 太低 | `--cfg 1.5`–`2.0` |
| 图里文字成乱码 | 扩散模型固有限制 | 别硬修，后期在 PPT/PS/Figma 叠字 |
| 客户端连不上 MCP | Python 路径 / 编码不对 | 用绝对路径的 `python`；设 `PYTHONIOENCODING=utf-8`；跑 `doctor` |
| `model_unavailable` | 本机没有可编辑模型 | 在 LocModel 里下载 Qwen-Image-Edit / FLUX.2 klein，或改用 `redraw_image` |
| `bad_arguments` + mask | 掩码空白、覆盖整图、非 8-bit PNG | 先画上内容；整图改请用 `redraw_image`；重导出为 8-bit 非交错 PNG |
| 改完接缝明显 | `strength` 太高或 `mask_blur` 太小 | 降 `strength`，加 `mask_blur` / `inpaint_padding` |
| 掩码内几乎没变 | `strength` 太低，或提示词没说「变成什么」 | `strength` 提到 0.8 以上；用具体名词描述目标 |

---

## 5. 3.0.0 新增了什么（一句话）

区域精准改图（`edit_image` / `redraw_image` / `list_edit_models` / `load_model`）、单文件 HTML
掩码标注器，以及**掩码外像素绝不被改动**的硬保证——引擎原始输出另存为 `*_raw.png` 供排查。
安装器的载荷已补入 `scripts/locmodel_edit.py` 与 `annotator/mask_annotator.html`。

许可证：MIT。问题与建议：https://github.com/jaja1620/locmodel-image-MCP
