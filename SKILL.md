---
name: workbuddy-cache-mover
description: 迁移 WorkBuddy 主数据/缓存目录（~/.workbuddy，包含 binaries、plugins、blobs、artifact-index 等大文件，通常数 GB）到用户指定的其他磁盘位置，并用软链接（macOS/Linux symlink / Windows junction）接管原路径，实现"缓存目录位置不可修改"前提下的实际存储搬迁。推荐一键 migrate 模式：脚本自动关闭 WorkBuddy → 静止后复制+严格校验 → 建软链接 → 自动重开 WorkBuddy。适用于：系统盘/C盘/根分区空间不足、想把 WorkBuddy 数据搬到外置盘/数据盘、.workbuddy 占用空间太大。触发词：迁移缓存、缓存搬家、缓存目录太大、C盘满了、磁盘空间不足、.workbuddy 占空间、软链接搬移、清理迁移备份、删除历史缓存备份、恢复原状、把缓存搬回去、move cache directory、symlink cache、junction。注意：本技能只处理 ~/.workbuddy，不处理 Electron 缓存（~/Library/Caches、~/Library/Application Support）。
---

# WorkBuddy 缓存目录迁移（~/.workbuddy → 软链接接管）

WorkBuddy 的主数据/缓存目录固定在 `~/.workbuddy`（Windows 为 `%USERPROFILE%\.workbuddy`），
应用内不提供修改位置的入口。本技能通过 **自动关闭 WorkBuddy → 复制+严格校验 → 原目录改名
备份 → 原路径建软链接 → 自动重开 WorkBuddy** 一条链完成迁移，全程对用户近乎无感。

**范围红线：只迁移 `~/.workbuddy`。** `~/Library/Caches`、`~/Library/Application Support`
下的 Electron 缓存/用户数据一律不动——用户明确要求时也要劝阻。

**为什么不先在线复制再离线切换（旧版方案）**：WorkBuddy 运行时会持续写文件并持有文件锁，
在线复制会出现复制错误且副本一直在过期；因此本版本把复制放进关闭应用之后的一键流程里，
源目录静止后单遍复制即可，无需增量收敛，校验可以做到逐文件严格比对。

## 安全红线（必须遵守）

1. **migrate 会自动关闭并重新打开 WorkBuddy**：关闭前先优雅退出（macOS osascript / Windows
   taskkill），20 秒未退出则 SIGTERM，再不行 SIGKILL 兜底；迁移完成后自动重启 WorkBuddy。
2. **禁止在 WorkBuddy 会话内直接运行 migrate**（会把自己所在的会话杀掉，脚本可能随会话
   中断）：在会话内只能 detect / make-runner / cleanup-backup / 状态确认；migrate 必须通过
   make-runner 生成的脚本或用户在终端执行。沙箱内进程检测失败会返回 unknown，migrate 同样拒绝。
3. **原目录从不删除**：migrate 把原目录改名为 `~/.workbuddy.wb-migration-backup` 保留；
   确认稳定后由用户发起「清理备份」动作删除（进废纸篓）。
4. **目标位置必须由用户明确指定**，禁止自行猜测；确认前把最终路径完整展示给用户。
5. 复制后做**严格校验**（文件清单+大小逐项一致），任何不一致直接中止，源目录原样保留；
   建软链接失败自动恢复原目录。

## 环境要求

- Python ≥ 3.8，仅标准库，无第三方依赖。
- macOS/Linux：`os.symlink`；Windows：junction（`mklink /J`，免管理员，目标须本地卷），
  网络路径需管理员 symlink。
- 目标卷剩余空间须大于 `~/.workbuddy` 体积（脚本自动检查 2% 余量）。

## 脚本

`~/.workbuddy/skills/workbuddy-cache-mover/scripts/cache_mover.py`
（下文用 `$SCRIPT` 指代其绝对路径）

## 工作流程

### 第 1 步：detect（识别环境 + 定位目录）

```bash
python3 $SCRIPT detect
```

输出：操作系统、`~/.workbuddy` 总体积/文件数、Top 子目录体积分布、磁盘剩余空间、
是否已是软链接（已迁移状态会给出 plan 与提示）、检测到的 WorkBuddy 进程。
把结果整理成表格转述给用户。**若已是软链接则停止，告知现状即可。**

### 第 2 步：问用户目标位置（必须）

用 AskUserQuestion 让用户指定**目标基准目录**（例如 `/Volumes/xxx/workbuddy-data`、
Windows `D:\wb-data`）。向用户确认最终路径与预期行为：

- 源：`~/.workbuddy`（X GB）
- 目标：`<用户指定基准>/.workbuddy`
- 过程：脚本会自动关闭 WorkBuddy，迁移完成后自动重新打开
- 备份：`~/.workbuddy.wb-migration-backup` 保留

用户明确同意后才进入下一步。

### 第 3 步：生成一键脚本并交给用户

```bash
python3 $SCRIPT make-runner --dest-base "<用户指定基准目录>"
```

生成 `~/.wb-cache-move/run_migrate.command`（macOS，可双击）/ `run_migrate.bat`（Windows），
以及 `migrate-args.json`（存目标路径，规避 bat 中文路径编码问题）。

告知用户操作（原样转述）：**双击运行 `~/.wb-cache-move/run_migrate.command`，脚本会自动
完成关闭 WorkBuddy → 复制+校验 → 建软链接 → 重新打开 WorkBuddy，看到 Done 即完成。
迁移期间（几分钟）不要使用 WorkBuddy。**

### 第 4 步：验证

用户迁移完成（WorkBuddy 已被脚本自动重新打开）后，运行 `python3 $SCRIPT detect`
确认显示「已迁移状态」，并请用户确认应用工作正常（会话、文件、技能都能用）。

plan 文件由 migrate 自动生成在 `~/.wb-cache-move/plan-*.json`（`swapped: true`）。

## 用户可主动发起的收尾动作

### 动作一：迁移稳定后清理历史缓存备份（cleanup-backup）

用户说「迁移完了，把备份删了」「清理历史缓存」时触发。

1. 先跑 `python3 $SCRIPT detect` 确认处于已迁移状态（软链接正常指向目标，应用工作正常）
2. 用 AskUserQuestion 与用户确认：展示备份路径与体积，说明删除后 rollback/restore 将不可用
   （数据仍在迁移目标位置，不会丢）
3. 执行（默认移入系统废纸篓，可反悔）：

```bash
python3 $SCRIPT cleanup-backup --plan <plan路径> --yes
```

- 脚本会校验软链接仍指向目标，状态不符直接拒绝
- `--hard-delete` 为永久删除（不进废纸篓），除非用户明确要求否则不要使用
- 清理备份**不需要退出 WorkBuddy**（备份目录不在使用中）

### 动作二：彻底恢复原状（restore）

用户说「恢复原状」「搬回去」「还原迁移」时触发。与 rollback 的区别：
rollback 只把原目录恢复（目标副本与 plan 保留，便于再次迁移）；
**restore 是一切回到迁移前**——数据放回原位 + 迁移副本移入废纸篓 + 清理 plan/runner 痕迹。

1. 与用户确认：恢复后目标位置的副本也会被移入废纸篓（`--keep-target` 可保留）
2. restore 需要移动 `~/.workbuddy`，同样不能在会话内直接跑；生成 restore 版独立脚本：

```bash
python3 $SCRIPT make-runner --command restore
```

3. 用户退出 WorkBuddy → 双击 `~/.wb-cache-move/run_restore.command`（.bat）→ 重开 WorkBuddy

内部顺序：移除软链接 → 备份恢复为 `~/.workbuddy` → 目标副本移入废纸篓 → 清理 plan/runner。
若备份已被动作一删除，restore 会**拒绝执行**并提示数据只在目标位置，
此时只能人工把目标目录改名/移动回原路径。

### 仅回滚（rollback，保留迁移能力）

用户说「先退回原来的位置，但目标副本先留着」时使用（同样需退出 WorkBuddy 后执行）：

```bash
python3 $SCRIPT rollback --plan <plan路径>
```

## 旧版两阶段模式（copy + swap，不推荐）

`copy`（在线复制）+ `swap`（离线改名+建链）仍保留作兼容，但**不要再使用**：
WorkBuddy 运行时复制会遇到文件锁报错、文件持续增长导致副本过期。仅当用户明确理解
风险并要求时使用。当前主流程是 migrate 一键模式。

## 常见问题

| 现象 | 说明 |
| --- | --- |
| migrate 在会话内运行被拒（unknown/受限环境） | 设计行为：migrate 要关闭 WorkBuddy，只能通过 runner 或终端运行 |
| migrate 自动退出 WorkBuddy 失败 | 用户手动退出后重新双击 runner 即可（脚本未做任何改动） |
| 严格校验失败（多出/缺失/大小不一致文件） | 目标目录有脏数据或磁盘问题；脚本中止且源目录原样保留，排查后重跑 |
| Windows mklink /J 失败 | 目标是网络路径时 junction 不可用，需管理员 `mklink /D` |
| 想迁移到另一块位置（二次搬迁） | 先 rollback 恢复，再重新一键 migrate 到新位置 |
| cleanup-backup 后想恢复原状 | 备份已删，脚本拒绝；人工把目标目录改名回 `~/.workbuddy` 即可 |
