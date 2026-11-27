# 从仓库直接安装

这是第三条安装路线：**直接从 GitHub 仓库部署，不用 exe，不用 pip，不用 clone**。它与原有两条
路线并存，互不覆盖、互不干扰。

| 路线 | 需要 | 适合谁 |
| --- | --- | --- |
| `locmodel-image-setup.exe` | Windows，无需其它 | 人在 Windows 上点一次鼠标；它还会把 MCP 注册进检测到的宿主 |
| 从源码 | `git`、Python | 开发者，需要 `pip install .` 和 `PATH` 里的 `locmodel-image` 命令 |
| **从仓库安装**（本页） | Python 3.8+、网络 | **需要自己完成部署的 AI 智能体**、没有构建环境的 Linux/macOS/Windows、以及任何想要可脚本化、哈希校验、装进自有目录的人 |

部署两件东西：

- **MCP 服务器** —— `scripts/*.py`，stdio JSON-RPC 服务。装到
  `%LOCALAPPDATA%\locmodel-image\repo`（Windows）或
  `${XDG_DATA_HOME:-~/.local/share}/locmodel-image/repo`（Linux/macOS）。
- **Skill** —— `SKILL.md`，纯 Markdown。装到 `<宿主 skills 目录>/locmodel-image/SKILL.md`。

不编译，不写 `site-packages`。载荷是纯标准库 Python，"安装"实际就是"把 7 个 `.py` 和 1 个 `.md`
复制到两个目录并校验哈希"。

---

## `install/` 里有什么

| 文件 | 作用 |
| --- | --- |
| `manifest.json` | 机器可读载荷清单：版本、ref、每个文件的 SHA-256 与大小、宿主路径表、校验命令 |
| `SHA256SUMS` | 同样的哈希，`sha256sum -c` 格式 |
| `hosts.json` | 宿主 → 目录表（Windows 与 Unix 两种路径），并标注哪些宿主自己管理 MCP 配置 |
| `AGENT-INSTALL.md` | 写给 AI 智能体的安装规范：三条路径（跑安装器 / 自己抓文件 / 转交人工）、确切 URL、校验、故障表 |
| `install-from-repo.ps1` | Windows 安装器：下载 → 校验 → 部署 → 写报告 → 可选注册宿主 |
| `install-from-repo.sh` | Linux/macOS 同款 |

---

## Windows

```powershell
# 下载安装器、执行、删除
$d = Join-Path $env:TEMP 'locmodel-image-install'
New-Item -ItemType Directory -Force -Path $d | Out-Null
Invoke-WebRequest 'https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/v4.1.0/install/install-from-repo.ps1' `
  -OutFile "$d\install-from-repo.ps1" -UseBasicParsing
powershell -NoProfile -ExecutionPolicy Bypass -File "$d\install-from-repo.ps1"
```

不需要管理员权限，不写注册表。

## Linux / macOS

**已实测范围。** Windows 路径（PowerShell 5.1）在 Windows 10 上做过端到端实测：`manifest` / `zip` / `local` 三种来源、哈希校验、宿主注册、MCP `initialize` 握手均通过。POSIX 脚本读取同一份 `manifest.json`、执行同一套校验，但未在真实 macOS/Linux 主机上跑过——若机器没有 `sh` 或某一步失败，改用 `AGENT-INSTALL.md` 的路径 B（自己抓取 payload 文件）并把失败步骤报上来。


```sh
curl -fsSL https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/v4.1.0/install/install-from-repo.sh -o /tmp/locmodel-install.sh
sh /tmp/locmodel-install.sh
```

## 离线安装

Release 资产 `locmodel-image-repo-install-v4.1.0.zip` 内含 `scripts/`、`SKILL.md`、`install/` 与
`docs/`。解压后：

```powershell
.\install\install-from-repo.ps1 -Source local -FromPath .
```
```sh
sh ./install/install-from-repo.sh --source local --from-path .
```

`local` 模式不触网，仍按随包的 `install/manifest.json` 校验哈希。

---

## 参数

| PowerShell | sh | 含义 |
| --- | --- | --- |
| `-Component mcp\|skill\|both` | `--component` | 默认 `both` |
| `-Host auto\|claude\|marvis\|workbuddy\|cursor\|vscode\|generic\|all` | `--host` | `auto` = 凡是父目录已存在的宿主都写一份 skill |
| `-Register claude\|cursor\|vscode\|marvis\|workbuddy\|all` | `--register` | 顺带把 MCP 条目写进该宿主配置，先留 `.bak-<时间戳>` |
| `-InstallRoot <dir>` | `--install-root` | Python 载荷落点 |
| `-Ref <git ref>` | `--ref` | 默认用 manifest 里固定的 `v4.1.0` |
| `-Source manifest\|zip\|local` | `--source` | `manifest` 逐文件抓（默认）、`zip` 抓单个 release 资产、`local` 用 `-FromPath` |
| `-FromPath <dir>` | `--from-path` | `local` 模式的源目录 |
| `-DryRun` | `--dry-run` | 只打印计划，不写盘 |
| `-Force` | `--force` | 哈希不匹配也继续（放弃完整性保证，不建议） |
| `-CreateHostDirs` | `--create-host-dirs` | 宿主未安装过也强行建 skills 目录 |

sh 退出码：`0` 成功、`1` 参数错误、`2` 下载/校验失败、`3` 部署失败。

---

## 注册 MCP

`-Register` 覆盖 Claude Desktop、Cursor、VS Code——在原 JSON 里合并单个 `locmodel-image` 条目并留备份。
Marvis 与 WorkBuddy 在运行时会自己回写配置，所以安装器只打印片段：

```json
{
  "mcpServers": {
    "locmodel-image": {
      "command": "C:/Users/you/AppData/Local/Programs/Python/Python312/python.exe",
      "args": ["C:/Users/you/AppData/Local/locmodel-image/repo/scripts/locmodel_mcp.py"],
      "env": { "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1" }
    }
  }
}
```

粘到 **Marvis → 设置 → MCP → 添加自定义服务**，或 **WorkBuddy → 设置 → MCP**。
Windows 上务必写真实解释器绝对路径：裸 `python` 可能命中 Microsoft Store 的占位程序，静默退出，
表现为"装好了但连不上"。

---

## 验证

```powershell
# 1. 服务器是否真在说 MCP？（不需要引擎）
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}' | python "$env:LOCALAPPDATA\locmodel-image\repo\scripts\locmodel_mcp.py"
```

输出里必须出现 `"serverInfo"`。然后打开 LocModel（或任一 `sd-server`）并加载模型：

```powershell
python "$env:LOCALAPPDATA\locmodel-image\repo\scripts\locmodel_image.py" doctor
python "$env:LOCALAPPDATA\locmodel-image\repo\scripts\locmodel_image.py" status
```

引擎没起时 `doctor` 返回非零——那是引擎问题，不是安装问题。安装报告在
`<MCP_ROOT>/install-manifest.json`。

---

## 卸载

删掉两个目录，再把宿主配置里的 `locmodel-image` 条目去掉：

```
%LOCALAPPDATA%\locmodel-image\repo
<skills>\locmodel-image
```

状态就这些。没有注册表键、没有服务、没有 `PATH` 项、没动 `site-packages`。安装器改过的配置旁边
都留着 `*.bak-<时间戳>`。

---

## 与 exe 安装的关系

`locmodel-image-setup.exe` 占据 `%LOCALAPPDATA%\locmodel-image` 并写入冻结运行时。本路线装进
`repo\` 子目录，不读取 exe 目录，两者可以共存；安装器拒绝覆盖 exe 目录，卸载任一不影响另一。
建议只让一个真正跑起来——在同一个宿主里用同名注册两个，宿主会加载它最后读到的那个。

---

## 给智能体的一段话

把下面这句丢给你的智能体就够了：

> 读 `https://raw.githubusercontent.com/jaja1620/locmodel-image-MCP/v4.1.0/install/AGENT-INSTALL.md`，
> 按它把 locmodel-image（MCP 服务器 + 配套 skill）装到本机。

那份规范是自洽的：确切 URL、各系统两个落点目录、哈希表、两条可用的验证命令、各宿主配置键，
以及明确列出"不许动"的东西。
