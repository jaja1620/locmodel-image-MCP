# locmodel-image 安装器

一键把 `locmodel-image` 的 MCP 服务与 Skill 部署到目标机。目标机**不需要 Python**：
安装器本身是 PyInstaller 打出的单文件 exe，既是安装程序，也是常驻的 MCP stdio 服务进程。

## 产物

```
build/dist/locmodel-image-setup.exe
```

## 用法

| 操作 | 命令 |
| --- | --- |
| 双击运行 | 直接运行 exe，按提示确认即可（默认安装到 `%LOCALAPPDATA%\locmodel-image`） |
| 全自动安装 | `locmodel-image-setup.exe install --mode exe --yes` |
| 只装到指定宿主 | `... install --targets dsh,hermes --yes` |
| 安装预演 | `... install --dry-run` |
| 自检 | `... doctor` |
| 查看状态 | `... list` |
| 卸载（保留目录） | `... uninstall --yes` |
| 卸载并清目录 | `... uninstall --purge --yes` |
| Marvis 注册指令 | `... emit marvis`（或 `--host marvis`） |
| 作为 MCP 服务运行 | `... --serve`（由宿主调用，勿手动执行） |

退出码：`0` 成功、`1` 失败（自检不过）、`2` 参数或环境错误、`130` 用户中断。

## 支持的宿主

| 宿主 | 配置文件 | 写入位置 |
| --- | --- | --- |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` | `mcpServers` |
| Claude Code | `~/.claude.json` 或 `~/.claude/settings.json` | `mcpServers` |
| Cursor | `%APPDATA%\Cursor\User\mcp.json` 或 `~/.cursor/mcp.json` | `mcpServers` |
| VS Code | `%APPDATA%\Code\User\mcp.json` | `servers`（带 `type: stdio`） |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |
| DSH | `~/.dsh/mcp.json` | `mcpServers` |
| Hermes | `~/.hermes/config.yaml` | `mcp_servers`（YAML 行级插入） |
| Marvis | 无文件配置 | 由 `emit marvis` 输出 `mcp_install` / `skill_install` 指令，粘到对话里执行 |

自动模式只处理**已存在**的宿主配置目录；不会凭空在未安装的宿主下创建配置。
可用 `--all` 强制覆盖全部，或 `--targets` 精确指定。

## 安全设计

- 写宿主配置前先备份为 `*.bak.<时间戳>`；只增删自己那一条，不重写整个文件，未知键原样保留。
- 幂等：重复安装返回 `unchanged`，不产生多余备份。
- 现有 JSON 解析失败时拒绝写入，不做“修复性”覆盖。
- YAML 只在 `mcp_servers` 段内做行级插入；段不存在时追加新的顶层段，写入后做结构校验，失败即回滚并保留备份。
- 卸载时先清宿主配置，再删目录；若 exe 仍被宿主进程占用则拒绝删除任何文件并提示处理办法，绝不留半删状态。
- MCP 走 stdio：`--console` 打包，协议流量只走 stdout，日志只走 stderr。

## 已验证场景

单机实测（Windows 10 19045，目标机无独立 Python）：

1. 双击无参数走交互式安装，结束不闪退。
2. exe 模式安装同时写入 Claude Desktop / Cursor / VS Code / DSH / Hermes，五个宿主全部成功，既有条目 `keepme`、`other` 均保留。
3. 重复安装：JSON 宿主全部 `unchanged`，YAML 宿主第二次起 `unchanged`，不再产生 `.bak`。
4. `emit` 的三种写法（位置参数、`--host`、默认）输出一致，均含 `mcp_install` 与 `skill_install`。
5. `uninstall --purge` 在 exe 被占用时拒绝并给出可操作提示；占用释放后（`taskkill /T`）彻底删除目录、宿主条目清空为 `{}`。
6. `--serve` 手工握手：`initialize` → `tools/list`（5 个工具）→ `tools/call locmodel_status` 全部通；引擎未启动时返回可读提示并给出修复建议，不抛栈。
7. 中文 + 空格路径下的安装、自检、卸载均正常。
8. 安装器单元/集成测试 28 项全绿（`python -m unittest discover -s installer/tests`）。

## 待目标机自行确认

- 真实出图需要本机 LocModel 引擎在线（`sd-server.exe` 监听中）。安装器 `doctor` 会检查引擎、模型、显存与输出目录，引擎不在时报“引擎未就绪”，不影响 MCP 注册本身。
- Marvis 的注册走对话指令，安装器只负责生成指令文本。
