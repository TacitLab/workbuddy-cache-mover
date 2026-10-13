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
3. **不长期保留备份**：迁移数据经严格校验（两轮+进程复查）一致后，原始数据自动**移入系统
   废纸篓**（可反悔，--keep-backup 可保留）。后续 rollback/restore 的数据源永远是**软链接
   指向的当前实际位置**（含迁移后全部变更），绝不使用旧快照——用备份恢复会导致迁移期间
   的变更全部丢失。
4. **目标位置必须由用户明确指定**，禁止自行猜测；确认前把最终路径完整展示给用户。
5. 复制后做**严格校验**（文件清单+大小逐项一致，两轮+进程复查），任何不一致直接中止，
   源目录原样保留；建软链接失败自动恢复原目录。**迁移中途用户打开 WorkBuddy 的任何时点
   都会被拦截并安全中止**（详见常见问题表），重跑即可，无数据损失。

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
- 迁移完成后：校验通过即把原始数据移入废纸篓（不占双倍空间；如反悔可从废纸篓找回）

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

### 二次搬迁（A → B，自动重定向）

用户已迁移到 A，又想搬到 B 时：**重新走第 2-3 步即可**（问新目标位置 → make-runner）。
migrate 检测到 `~/.workbuddy` 已是软链接时自动切换为重定向迁移：

- 数据源 = A 的当前实际内容（含 A 期间全部变更）
- 同卷 → rename 瞬时完成；跨卷 → 复制 + 严格校验
- 软链接改指 B；A 旧数据清理（同卷已随 rename 消失 / 跨卷进废纸篓）
- plan 自动更新（记录 redirected_from），rollback/restore 语义不变

## 用户可主动发起的收尾动作

### 动作一：清理残留的原始数据（cleanup-backup，兜底用）

migrate 默认会自动把原始数据移入废纸篓；本命令仅在两种情况用到：migrate 时自动清理失败
（无废纸篓工具的环境）、或当时用了 `--keep-backup` 事后想清理。

```bash
python3 $SCRIPT cleanup-backup --plan <plan路径> --yes
```

- 默认移入系统废纸篓（可反悔）；`--hard-delete` 永久删除仅在用户明确要求时使用
- 脚本会校验当前状态（软链接指向 or 已回滚），状态异常直接拒绝
- 清理**不需要退出 WorkBuddy**

### 动作二：彻底恢复原状（restore）

用户说「恢复原状」「搬回去」「还原迁移」时触发。

**数据源 = 软链接指向的当前实际位置**（含迁移后全部变更），绝不用旧快照：
移除软链接 → 当前数据迁回 `~/.workbuddy`（同卷 rename 瞬间完成 / 跨卷复制+严格校验，
失败自动恢复软链接）→ 清理旧快照（如有）与 plan/runner 痕迹 → 一切回到迁移前，
**迁移期间的变更零丢失**。

1. restore 需要移动 `~/.workbuddy`，不能在会话内直接跑；生成 restore 版独立脚本：

```bash
python3 $SCRIPT make-runner --command restore
```

2. 用户退出 WorkBuddy → 双击 `~/.wb-cache-move/run_restore.command`（.bat）→ 重开 WorkBuddy

### 仅回滚（rollback，保留迁移能力）

用户说「数据搬回原位，但以后可能再迁」时使用（同样需退出 WorkBuddy 后执行）。
数据源同样是当前实际位置（含迁移后全部变更），同卷 rename / 跨卷复制+校验：

```bash
python3 $SCRIPT rollback --plan <plan路径>
```

## 常见问题

| 现象 | 说明 |
| --- | --- |
| migrate 在会话内运行被拒（unknown/受限环境） | 设计行为：migrate 要关闭 WorkBuddy，只能通过 runner 或终端运行 |
| migrate 自动退出 WorkBuddy 失败 | 用户手动退出后重新双击 runner 即可（脚本未做任何改动） |
| **迁移中途用户打开了 WorkBuddy** | 复制期间打开：严格校验发现差异 → 中止，源目录无损；校验期间打开：进程复查 + 二轮校验双重拦截 → 中止；改名瞬间打开（极小概率）：rename 异常被捕获 → 中止，源目录无损。所有时点均"宁可中止不冒险"，重跑即可 |
| 严格校验失败（多出/缺失/大小不一致文件） | 目标目录有脏数据或迁移期间有外部写入；脚本中止且源目录原样保留，排查后重跑 |
| Windows mklink /J 失败 | 目标是网络路径时 junction 不可用，需管理员 `mklink /D` |
| 想迁移到另一块位置（二次搬迁） | 直接再跑一次 migrate（runner 重新生成即可）：检测到已迁移状态会自动走「重定向迁移」，数据从当前实际位置直达新目标（同卷瞬时/跨卷复制校验），无需先 rollback |
| 重定向迁移到相同位置 | 脚本检测新目标与当前实际位置相同，直接拒绝（无意义操作） |
| rollback/restore 后数据会回退到迁移时吗 | 不会。数据源是软链接指向的当前实际位置，迁移后的全部变更都保留 |
| 废纸篓里的原始数据何时清 | 由系统/用户自行管理；迁移稳定后清空废纸篓即可 |
