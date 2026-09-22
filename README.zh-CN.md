# Codex Native Retry

非官方、实验性的 Windows Codex CLI 容量错误自动恢复工具。

只通过原 thread 的 `turn/start + input: []` 继续，不发“继续”，不重发原 prompt，不创建新对话，不覆盖模型或权限。本项目与 OpenAI 无隶属或背书关系。

## 使用

需要 Python 3.10+ 和支持共享 app-server 的 Codex CLI。代码使用 Python 标准库，无需 pip 安装依赖。

```powershell
git clone https://github.com/wzyhn/codex-native-retry.git
cd codex-native-retry
python .\src\codex_native_retry.py diagnose
python .\src\codex_native_retry.py watch --dry-run
```

真实恢复需要三个终端：

```powershell
# 终端 1：启动本机共享服务，保持窗口打开
codex app-server --listen unix://

# 终端 2：选择原对话恢复；先退出原来的 stdio CLI
codex --remote unix:// resume

# 终端 3：在本项目根目录运行 watcher
python .\src\codex_native_retry.py watch --live --retry-mode native_cli
```

已安装 managed standalone 的用户也可以通过 `codex app-server daemon start` 启动服务。普通 stdio 对话不能被 watcher 直接接管。

运行时自动发现目前检查 Desktop runtime 缓存。没有检测到时，用 `--runtime "你的codex.exe完整路径"` 指定真实 CLI 二进制，也可在配置中设置 `runtime_path`。

## 何时生效

- 只处理明确的 capacity 错误，优先结构化 `serverOverloaded`。
- Codex 自己仍在重试时不介入。
- 启动前已发生的 capacity 失败也会检查。
- 发出请求前确认对话已加载、没有活动 turn，而且最新一轮仍是那次 capacity 失败。
- 默认约 5、10、20、40、60 秒退避，加入随机抖动，最多 5 次。
- 被服务器接受不代表容量已经恢复；再次 capacity 时继续退避。
- 请求结果不确定时停止，避免重复发送。

## 配置与日志

```powershell
.\install.ps1
python .\src\codex_native_retry.py diagnose
Get-Content "$env:APPDATA\CodexNativeRetry\events.jsonl" -Tail 20
```

配置位于 `%APPDATA%\CodexNativeRetry\config.json`，默认 dry-run。可选 `install.ps1 -StartWithWindows` 添加 dry-run 自启；`uninstall.ps1` 移除自启。

同一个 Codex home **只运行一个 watcher**。当前去重和次数预算只在进程内有效，重启会重置。状态检查和开始 turn 之间仍有竞态窗口，尚不能承诺绝对无重复副作用。诊断包含本地路径，分享前检查脱敏。

项目在私有主仓库维护，此仓库为单向公开镜像。欢迎提交 Issue 或补丁，维护者在上游应用后统一同步。

详细配置、兼容版本及限制见 [English README](README.md)。许可证为 [MIT](LICENSE)。
