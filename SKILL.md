---
name: workbuddy-cache-mover
description: 将 WorkBuddy 桌面端 ~/.workbuddy 主数据目录迁往用户指定磁盘，通过目录链接保留原路径。支持 Windows、macOS、Linux 的环境检测、独立启动脚本、实时进度、SHA-256 校验、迁移、二次搬迁、恢复与中断恢复；鸿蒙电脑版识别后明确提示暂不能迁移。用于系统盘空间不足、缓存搬家、恢复原位和清理旧副本；不处理 Electron 的其他缓存/配置目录。
---

# WorkBuddy 数据目录迁移

迁移的是整个 `~/.workbuddy`，Windows 为 `%USERPROFILE%\.workbuddy`，其中包含程序、插件和文件数据。原路径改为目录链接，应用继续使用原地址。

只操作该目录，不扩展到 `Library/Caches`、`Library/Application Support` 等 Electron 目录。恢复的数据源始终是链接指向的当前实际数据，不能用旧快照覆盖迁移后的变更。

## 平台与能力边界

| 环境 | 支持方式 |
|---|---|
| Windows | Python ≥3.8；`.bat` 保留结果窗口；本地卷 junction，UNC 需要 symlink 权限 |
| macOS | Python ≥3.8；可双击 `.command`；POSIX symlink |
| Linux（含 UOS、银河麒麟） | Python ≥3.8；终端执行 `.sh`；POSIX symlink；重开使用检测到的原可执行文件 |
| 鸿蒙电脑版 | 明确识别并提示暂不能迁移；尚无已验证的本地数据路径、Python 运行环境、进程与链接接口 |

官网列出了鸿蒙电脑版，但不能据此推断它使用 Linux 的 `~/.workbuddy` 和命令。遇到原生鸿蒙，先说明上述限制，不运行迁移命令、不修改数据。若不能运行 Python，依据用户确认的系统类型给出限制说明；不能把无法执行的 detect 描述为已完成。

需要独立系统 Python，解释器、Python 环境和执行脚本均须位于待迁移数据目录之外。仅使用 Python 标准库，不依赖 rsync、robocopy 或第三方 Python 包。源码入口是本 Skill 的 `scripts/cache_mover.py`，下文 `$SCRIPT` 表示其绝对路径。

## 执行约束

- 目标基准目录必须由用户指定；执行前完整展示最终目标 `<基准目录>/.workbuddy`、数据体积、自动关闭/重开应用的行为。已有授权覆盖相同目标，不重复询问。
- migrate、restore、rollback、recover 会管理 WorkBuddy 进程，不能在 WorkBuddy 会话内直接执行。生成独立 runner，由用户在系统终端或双击执行。detect、状态查询和生成 runner 不关闭应用。
- 关闭窗口不等于退出应用。检测主程序、托盘和子进程；按进程路径、父子关系及启动身份确认归属，持续追踪主程序退出后的助手。先请求退出，超时结束已核对的进程树，再确认全部退出；禁止按 CodeBuddy/electron 通用进程名全局结束。不完整的进程检测必须停止。
- 无法检测进程时停止，不把 `unknown` 当作已退出。重复执行由内核独占锁拒绝，进程崩溃后锁会自动释放；不要手工删除锁文件来绕过正在运行的任务。
- 普通非空目标目录拒绝合并、覆盖。只有本工具记录的已校验副本或临时副本可在核对状态后复用。复用临时副本会按当前源数据重新复制与校验，不承诺百分比断点续传。
- 迁移、二次搬迁和恢复均先在专用临时目录复制，复制时计算源文件 SHA-256，复制完成后独立读取副本校验内容、目录、链接目标和权限；核对源目录在复制与校验期间未变化，读取失败即停止。复用已发布副本或重新启动进程时，重新读取源文件校验，不信任跨运行缓存摘要。校验后复查源目录及进程，再进行短暂路径切换。
- 目标盘与恢复盘需有最新数据体积加 2% 余量；同卷也采用复制、校验、切换，以保留切换前的可用数据路径。不要再承诺同卷瞬时完成。
- 旧数据默认进系统废纸篓，Windows 使用原生 Shell 回收接口并拒绝永久删除降级；系统不支持回收时保留旧副本、报告完成但有提醒。`--keep-backup` 可保留。进废纸篓仍占空间，用户确认正常后清空才释放。永久删除仅在用户明确要求时使用 `--hard-delete`。
- 不宣称“任何时点都安全”“零丢失”。外部程序重新写入、断电、磁盘失联等可能需要恢复确认；存在异常时保留数据和阶段记录，不盲目覆盖。

## 推荐流程

### 1. 检测

```bash
python3 "$SCRIPT" detect
```

整理系统、架构、数据位置、体积、进程状态及未完成记录。已经迁移时告知实际位置；只有用户要求二次搬迁才继续。原路径不可访问时先检查磁盘与阶段记录，不新建空目录冒充已恢复。

### 2. 确定目标并生成 runner

```bash
python3 "$SCRIPT" make-runner --dest-base "<用户指定基准目录>"
```

一次操作只交付一个已验证解释器的启动入口。不要额外生成“对照组”、已知会失败的 PATH 版本或自行改写的 portable 启动脚本。参数 JSON 与 `cache_mover.py` 是配套文件，不是额外启动入口。

生成器实际探测 Python 版本、可执行路径与环境位置；优先绑定生成时的可用独立解释器。已有便携 Python 时用 `--python "<解释器绝对路径>"` 指定，例如：

```bash
python3 "$SCRIPT" make-runner --command restore --plan "<记录路径>" --python "<数据目录外的 python.exe>"
```

解释器不可用时停止生成，并说明原因；不要下载解释器或修改系统设置来绕过。

默认生成：

- Windows：`~/.wb-cache-move/run_migrate.bat`
- macOS：`~/.wb-cache-move/run_migrate.command`
- Linux：`~/.wb-cache-move/run_migrate.sh`，使用 `sh "完整脚本路径"` 执行

Windows/macOS 可双击；Linux 是否双击打开终端由桌面文件管理器决定，优先给出终端执行方式。无需按键开始，迁移过程中不要使用或重新打开 WorkBuddy。

Windows 结果窗口保持打开，读完后关闭或输入 `EXIT`；macOS/Linux 在交互终端读完后按 Enter 结束，非交互运行直接返回真实退出码。

目标中文路径通过 JSON 传递，已验证的解释器路径按平台正确引用，脚本与参数按 runner 自身所在目录定位。更新 Skill 后必须重新生成 runner，旧文件不会自动更新。`--out` 会把所需脚本与参数一起放到指定启动器目录。

复制与校验默认 4 路并发，任务队列有上限，保留逐文件落盘和原路径保护。可用 `--workers 1` 改为串行，或在 1–8 范围调整；机械盘和低速网络不能承诺提高并发会更快。生成 runner 时的设置写入参数文件，无需用户手改代码。

### 3. 观察与验证

复制、扫描、校验、等待退出和清理等长阶段每两秒更新。交互终端在同一行刷新，按终端宽度截断；复制显示进度条、百分比、实际字节、条目、耗时和速度。普通消息与错误另起行，不被进度覆盖。重定向输出只显示阶段起止，完整轮询采样仍写入日志。总量未知时只显示已处理量，不伪造百分比或预计剩余时间。

Windows 的扫描、复制和内容校验使用 Unicode 长路径接口（支持本地盘及 UNC），临时文件使用独立短文件名，避免在长原文件名后追加后缀；不要求修改注册表。

结束后执行 detect，并让用户确认会话、文件、插件和技能可用。`SUCCESS` 表示脚本操作完成，不代表已完成用户侧应用检查。

## 结果、日志与记录

所有平台的 runner 和直接终端命令共用日志与结果语义：

| 退出码 | 结果 | 后续处理 |
|---|---|---|
| 0 | SUCCESS | 操作完成，检查应用正常 |
| 2 | COMPLETED_WITH_WARNINGS | 数据操作完成；按提醒处理清理或重开失败 |
| 1 | FAILED / FAILED_SAFE | 阅读原因和当前路径；有明确原路径保护时显示 FAILED_SAFE |
| 3 | RECOVERY_REQUIRED | 操作可能已部分完成，先 detect，再 recover |
| 4 | BUSY | 已有任务运行，等待原任务结束 |
| 5 | UNSUPPORTED_ENVIRONMENT | 环境不具备迁移能力，不执行搬迁 |
| 130 | INTERRUPTED | 用户中断，先核对阶段记录和实际路径 |

日志位置：

- Windows：`%LOCALAPPDATA%\WorkBuddyCacheMover\logs`
- macOS：`~/Library/Logs/WorkBuddyCacheMover`
- Linux：`$XDG_STATE_HOME/WorkBuddyCacheMover/logs`，默认 `~/.local/state/WorkBuddyCacheMover/logs`

日志记录阶段、完整异常与退出码。退出码 2 要明确说明数据操作已完成及具体待处理项，不描述为整次迁移失败。解释器启动前失败会留在终端显示，此时没有 Python 运行日志。

阶段记录为 `~/.wb-cache-move/plan-*.json`，在每个关键操作前持久化，并以临时文件原子替换。`operation.lock` 保留在该目录；不要在任务运行时删除该目录。恢复完成后保留阶段记录与启动器便于诊断，不在正在执行的 Windows `.bat` 中删除自身；确认无需恢复且没有任务运行后，可手动删除迁移工具目录。日志独立保留。

## 中断恢复与重试

先 detect 核对实际路径。复制阶段失败且记录明确为 `aborted`、原路径仍可用时，可以修复原因后重新生成 runner 重试，保留并核对临时副本；不要仅凭退出码 1 要求 recover。其他未完成记录会阻止新迁移并展示对应记录。先检查磁盘仍已连接，再生成恢复确认 runner：

```bash
python3 "$SCRIPT" make-runner --command recover --plan "<明确的记录路径>"
```

用户独立执行。recover 根据记录和实际路径：确认已完成的切换，或恢复旧链接/原目录；不能确定身份时拒绝覆盖并保留全部数据。它不直接删除旧副本，也不自动重新开始复制。

确认原路径恢复可用后，重新生成并执行 migrate/restore runner。已有临时副本仍保留，只有能与工具记录匹配的副本才可复用。磁盘剩余空间不足时需要先处理保留副本；不自动永久删除未知目录。

旧版 v1 记录可以用于常规迁回；没有阶段记录的中断不能自动推断，需检查实际路径。旧快照缺少内容身份或发生变化时保留，不能将其当作可靠恢复数据源。

## 二次搬迁、恢复和清理

二次搬迁 A → B：重新指定 B 并 make-runner。数据源为 A 的当前内容，同样执行复制、内容校验、切换和旧副本清理。

彻底迁回默认路径并处理历史快照：

```bash
python3 "$SCRIPT" make-runner --command restore --plan "<记录路径>"
```

仅迁回最新数据、保留历史快照：

```bash
python3 "$SCRIPT" make-runner --command rollback --plan "<记录路径>"
```

recover、restore、rollback 均必须独立执行。复制失败时原链接保持可用；切换失败会尝试恢复原链接，仍不可用时明确报告 RECOVERY_REQUIRED。

清理残留旧副本（不会主动退出应用，但应在没有迁移任务时进行）：

```bash
python3 "$SCRIPT" cleanup-backup --plan "<记录路径>" --yes
```

恢复已经完成、仅旧位置回收失败时，不重跑 restore，也不需要 recover。使用本次完成的 restore/rollback 记录执行上述 cleanup-backup，仅清理旧位置；核对当前真实目录及旧副本内容摘要，内容变更则保留。

清理前核对当前路径及旧副本内容身份。清理失败不把已成功迁移/恢复的数据操作误报为整体失败；输出完成但有提醒，保留副本。

## 验证维护

精简 Skill 安装包只含本文与执行主体；以下测试需使用 GitHub 完整仓库。

```bash
python3 -m unittest discover -s scripts -p "test_*.py" -v
```

测试使用临时目录，不操作真实 WorkBuddy。GitHub Actions 在 Windows、macOS Intel/Apple Silicon、Linux 上验证迁移、恢复、故障注入、进程树/孤儿助手/PID 复用、检测失败、独占锁、日志、长路径、单行进度和中文便携解释器原生启动器，并覆盖 Python 3.8 与现代版本。仅测试能力与工具行为，不将其等同于真实用户应用或鸿蒙实机验证。
