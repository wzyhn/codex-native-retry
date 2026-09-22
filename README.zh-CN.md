# Codex Native Retry

非官方、实验性的 Windows Codex CLI 容量错误自动恢复工具。

只通过原 thread 的 `turn/start + input: []` 继续，不发“继续”，不重发原 prompt，不创建新对话，不覆盖模型或权限。本项目与 OpenAI 无隶属或背书关系。

## 一键启动

需要 Python 3.10+ 和支持共享 app-server 的 Codex CLI，代码只使用 Python 标准库。

```powershell
git clone https://github.com/wzyhn/codex-native-retry.git
cd codex-native-retry
python .\src\codex_native_retry.py diagnose
.\start.ps1
```

`start.ps1` 会启动一个共享 app-server（优先使用 managed daemon，不可用时启动 bundled runtime）和一个后台 watcher。watcher 监听整个 `%USERPROFILE%\.codex\sessions`，因此同一个 Codex home 下的共享 daemon 对话都会应用规则。

每个 CLI 对话要连接到共享 daemon：

```powershell
codex --remote unix:// resume
```

旧的 stdio CLI 进程需要先退出，再用 `--remote` 恢复同一对话。普通 stdio 对话有自己的 app-server，工具不能安全接管或注入。

先观察而不执行：

```powershell
.\start.ps1 -DryRun
```

如果找不到 runtime，可以指定路径：

```powershell
.\start.ps1 -Runtime 'C:\path\to\codex.exe'
```

## 何时生效

- 只处理明确的 capacity 错误，优先结构化 `serverOverloaded`。
- `willRetry=true` 时不介入，交给 Codex 自己重试。
- watcher 启动时也会检查已经落盘的 terminal capacity 错误。
- 发出请求前确认对话已加载、没有活动 turn，而且最新一轮仍是那次 capacity 失败。
- 新 turn、手工 Retry、新用户消息或不明确的 transport 结果都会取消旧计划。
- 连续 capacity 失败的等待节奏是：立即、3 秒、5 秒、10 秒、15 秒、30 秒，此后每约 60 秒一次；默认加入 ±20% 随机抖动，单次最长 60 秒。
- 一次 continuation 成功或进入新的任务后，连续失败计数清零，下一次从立即开始。

再次 capacity 时仍然继续使用原 thread 的空输入 continuation。接受请求只表示 server 接收了 turn，不表示容量已经恢复或任务已经完成。

## 配置与日志

`install.ps1` 会创建 `%APPDATA%\CodexNativeRetry\config.json`，默认配置：

```json
{
  "enabled": true,
  "dry_run": true,
  "retry_mode": "dry_run",
  "retry_delays_seconds": [0, 3, 5, 10, 15, 30, 60],
  "max_delay_seconds": 60,
  "max_attempts": 0,
  "jitter_ratio": 0.2,
  "poll_seconds": 5,
  "runtime_path": null
}
```

`max_attempts: 0` 表示持续重试到 60 秒间隔，直到任务恢复、用户操作取消或发生不确定 transport 结果；设置正整数可以限制次数。

```powershell
.\install.ps1 -StartWithWindows -Live  # 可选：登录 Windows 后自动启动 live 模式
python .\src\codex_native_retry.py diagnose
Get-Content "$env:APPDATA\CodexNativeRetry\events.jsonl" -Tail 20
.\uninstall.ps1                         # 移除开机启动项
```

日志只保留时间、短 hash、错误类型、willRetry、尝试次数和结果，不保存对话正文、prompt、tool 参数或 tool 输出。

## 限制

同一个 Codex home 只运行一个 watcher。当前没有跨进程持久化 retry ledger，读取 thread 状态和发送 `turn/start` 也不是服务端原子操作；工具会在发现竞态或 transport 不确定时停止，不会盲目重发。

第一版只支持 CLI 共享 app-server，不处理 Desktop UI、远程 server 或其他错误类型。

## 开发

```powershell
python -m unittest discover -s tests -v
```

项目在私有主仓库维护，此仓库是单向生成的公开镜像。Issue 或补丁会先在私有源头审核，再同步到这里。

详细研究见 [research](docs/native-retry-research.md)、[protocol](docs/protocol-notes.md)、[architecture](docs/architecture.md)、[equivalence evidence](docs/native-retry-equivalence.md) 和 [safety boundaries](docs/safety-invariants.md)。

许可证：[MIT](LICENSE)。
