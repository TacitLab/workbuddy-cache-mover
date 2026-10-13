---
name: workbuddy-cache-mover
description: 迁移 WorkBuddy 主数据/缓存目录（~/.workbuddy，包含 binaries、plugins、blobs、artifact-index 等大文件，通常数 GB）到用户指定的其他磁盘位置，并用软链接（macOS/Linux symlink / Windows junction）接管原路径，实现"缓存目录位置不可修改"前提下的实际存储搬迁。适用于：系统盘/C盘/根分区空间不足、想把 WorkBuddy 数据搬到外置盘/数据盘、.workbuddy 占用空间太大。触发词：迁移缓存、缓存搬家、缓存目录太大、C盘满了、磁盘空间不足、.workbuddy 占空间、软链接搬移、move cache directory、symlink cache、junction。注意：本技能只处理 ~/.workbuddy，不处理 Electron 缓存（~/Library/Caches、~/Library/Application Support）。
---

# WorkBuddy 缓存目录迁移（~/.workbuddy → 软链接接管）

WorkBuddy 的主数据/缓存目录固定在 `~/.workbuddy`（Windows 为 `%USERPROFILE%\.workbuddy`），
应用内不提供修改位置的入口。本技能通过 **复制 → 改名备份 → 原路径建软链接** 三步，
把实际存储搬到用户指定位置，对 WorkBuddy 完全透明。

**范围红线：只迁移 `~/.workbuddy`。** `~/Library/Caches`、`~/Library/Application Support`
下的 Electron 缓存/用户数据（Cookies、Local Storage 等）一律不动——用户明确要求时也要劝阻。

## 安全红线（必须遵守）

1. **copy 阶段在线可做**（只读源目录，随时可中断重来）；**swap 阶段必须完全退出 WorkBuddy 后执行**
   （脚本会自动检测进程并拒绝，在 WorkBuddy 会话内直接 swap 必然被拒，这是设计行为）。
2. **原目录从不删除**：swap 时把原目录改名为 `~/.workbuddy.wb-migration-backup` 保留；
   确认应用稳定运行数日后，由用户自行删除（或用户明确确认后协助用废纸篓删除）。
3. **目标位置必须由用户明确指定**，禁止自行猜测默认路径；确认前把最终路径完整展示给用户。
4. swap 前必须存在已通过校验的 plan（`~/.wb-cache-move/plan-*.json`，`verified: true`）。
5. 建链接失败时脚本会自动恢复原目录；校验失败则禁止 swap。

## 环境要求

- Python ≥ 3.8，仅标准库，无第三方依赖。
- macOS/Linux：`os.symlink`，无需管理员权限。
- Windows：优先 junction（`mklink /J`，无需管理员，目标须在本地卷）；网络路径需管理员 symlink。
- 目标卷剩余空间须大于 `~/.workbuddy` 体积（脚本自动检查 2% 余量）。

## 脚本

`~/.workbuddy/skills/workbuddy-cache-mover/scripts/cache_mover.py`
（下文用 `$SCRIPT` 指代其绝对路径；用 python3 或受管 Python 均可执行）

## 工作流程

### 第 1 步：detect（识别环境 + 定位目录）

```bash
python3 $SCRIPT detect
```

输出：操作系统、`~/.workbuddy` 总体积/文件数、Top 子目录体积分布、磁盘剩余空间、
是否已是软链接（已迁移状态会直接给出 plan 与回滚提示）、当前检测到的 WorkBuddy 进程。
把结果整理成表格转述给用户。**若已是软链接则停止，告知现状即可。**

### 第 2 步：问用户目标位置（必须）

用 AskUserQuestion 让用户指定**目标基准目录**（例如外置盘 `/Volumes/xxx/workbuddy-data`、
Windows `D:\wb-data`）。向用户确认最终路径：

- 源：`~/.workbuddy`（X GB）
- 目标：`<用户指定基准>/.workbuddy`（脚本会在基准目录下自动创建同名目录）
- 备份：`~/.workbuddy.wb-migration-backup`（swap 后保留）

用户明确同意后才进入下一步。

### 第 3 步：copy（在线安全，阶段 1）

```bash
python3 $SCRIPT copy --dest-base "<用户指定基准目录>"
```

数 GB 需数分钟，建议后台运行。脚本执行：rsync/robocopy 全量复制 → 二次增量同步 →
完整性校验（允许运行中新增的 ≤10 个小文件漂移）。成功后生成 plan 并打印路径。

校验失败：通常因运行中的应用持续写入，重跑一次 copy（增量，很快）即可。

### 第 4 步：swap（离线执行，阶段 2）

在 WorkBuddy 会话内运行 swap 会被进程检测拒绝（exit code 3），正确做法是生成独立脚本：

```bash
python3 $SCRIPT make-runner --plan <plan路径>
```

生成 `~/.wb-cache-move/run_swap.command`（macOS，可双击）/ `run_swap.bat`（Windows），
内含自包含的脚本副本，不依赖 skill 目录。

然后告知用户操作序列（原样转述）：
1. **完全退出 WorkBuddy**（macOS: Cmd+Q，不是关窗口；Windows: 托盘图标退出）
2. 双击运行 `~/.wb-cache-move/run_swap.command`（或 `run_swap.bat`）
3. 看到「迁移完成」提示后，重新打开 WorkBuddy

swap 脚本内部逻辑：最终增量同步 → 原目录改名备份 → 建软链接 → 链接校验
（任何一步失败自动恢复原目录）。

### 第 5 步：验证与收尾

用户重新打开 WorkBuddy 后，运行 `python3 $SCRIPT detect` 确认显示「已迁移状态」，
并请用户确认应用工作正常（会话、文件、技能都能用）。

收尾提醒：
- 备份 `~/.workbuddy.wb-migration-backup` 保留，观察数日稳定后用户自行删除；
  如用户要求协助删除，须再次明确确认后仅删除该备份目录，用系统废纸篓机制。
- plan 文件保留在 `~/.wb-cache-move/`，回滚时需要。

## 回滚

```bash
python3 $SCRIPT rollback --plan <plan路径>
```

同样需要退出 WorkBuddy。回滚 = 移除软链接 + 备份改名回 `~/.workbuddy`，
数据无损；迁移出去的副本保留在目标位置，由用户决定是否删除。
（若用户已删备份，则回滚不可用，需人工把目标目录改回原路径。）

## 常见问题

| 现象 | 说明 |
| --- | --- |
| copy 校验失败 | 应用运行中持续写入，重跑 copy（增量收敛）即可 |
| swap 提示进程正在运行 exit 3 | 设计行为，走 make-runner 离线路径 |
| Windows mklink /J 失败 | 目标是网络路径时 junction 不可用，需管理员 `mklink /D` |
| 想迁移到另一块位置（二次搬迁） | 先 rollback 恢复，再 copy 到新位置 |
