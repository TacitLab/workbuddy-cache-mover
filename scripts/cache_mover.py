#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 缓存目录迁移工具（workbuddy-cache-mover）

把 ~/.workbuddy 主数据/缓存目录迁移到其他磁盘位置，并在原路径创建
软链接（macOS/Linux: symlink；Windows: junction），在"应用内无法修改
缓存目录"的前提下实现实际存储位置变更，对 WorkBuddy 完全透明。

范围红线：只迁移 ~/.workbuddy。~/Library/Caches、
~/Library/Application Support 等 Electron 缓存/用户数据一律不碰。

子命令:
  migrate  --dest-base <dir>          一键迁移（推荐）：自动关闭 WorkBuddy → 复制 → 校验
                                      → 改名备份+建软链接 → 自动重开 WorkBuddy
  detect                              查看 ~/.workbuddy 状态/体积分布/磁盘空间
  make-runner [--dest-base <dir>]     生成可双击运行的独立脚本（默认一键 migrate 模式）
  rollback --plan <plan.json>         仅回滚：当前数据迁回原位（含迁移期间变更），
                                      保留旧快照与 plan
  cleanup-backup --plan <plan.json>   迁移稳定后删除历史备份（默认移入废纸篓）
  restore --plan <plan.json>          彻底恢复原状：当前数据迁回原位+清旧快照+清痕迹
                                      （恢复数据源=软链接指向的当前实际位置，绝不用
                                      旧快照，迁移期间的变更零丢失）

仅使用标准库，无第三方依赖。Python >= 3.8。
"""

import argparse
import codecs
import datetime
import glob as globmod
import json
import os
import re
import shutil
import signal
import stat as statmod
import subprocess
import sys
import time
import traceback

DEFAULT_SRC = os.path.join(os.path.expanduser("~"), ".workbuddy")
PLAN_DIR = os.path.join(os.path.expanduser("~"), ".wb-cache-move")
BACKUP_SUFFIX = ".wb-migration-backup"
NAME_PAT = re.compile(r"(workbuddy|codebuddy)", re.IGNORECASE)
WIN_SYMLINK_HINT = (
    "Windows 下创建目录链接优先使用 junction（mklink /J，无需管理员权限，但要求目标在本地卷）。"
    "若目标是网络路径(UNC)，需要管理员权限创建真正的 symlink，"
    "或以管理员身份运行：mklink /D \"<原路径>\" \"<目标路径>\"。"
)


def _utf8_guard():
    """Windows 控制台编码兜底，避免中文输出报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = getattr(stream, "encoding", None)
            if enc and codecs.lookup(enc).name != "utf-8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def fmt_bytes(n):
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return ("%.1f%s" % (f, unit)) if unit != "B" else ("%dB" % int(f))
        f /= 1024.0
    return "%dB" % n


def is_within(parent, child):
    parent = os.path.normpath(parent)
    child = os.path.normpath(child)
    return child == parent or child.startswith(parent + os.sep)


def tree_stats(path):
    """目录总体积（不含目录项自身开销）与文件数。不跟随内部软链接。"""
    total, files, errors = 0, 0, 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
    return total, files, errors


def relative_file_set(path):
    """返回 {相对路径: 文件大小}，用于校验 dest 覆盖了 src 的所有文件。"""
    out = {}
    for root, _dirs, fnames in os.walk(path, followlinks=False):
        for fn in fnames:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, path)
            try:
                out[rel] = os.lstat(fp).st_size
            except OSError:
                pass
    return out


def link_target(path):
    """若 path 是软链接/Windows junction，返回其目标；否则返回 None。"""
    if not os.path.lexists(path):
        return None
    if os.path.islink(path):
        return os.readlink(path)
    if os.name == "nt" and is_reparse_dir(path):
        return os.path.realpath(path)
    return None


def is_reparse_dir(path):
    try:
        st = os.lstat(path)
        return bool(getattr(st, "st_file_attributes", 0) & 0x400) and statmod.S_ISDIR(st.st_mode)
    except OSError:
        return False


def make_link(target, link_path):
    """在 link_path 创建指向 target 的链接（目录）。"""
    target = os.path.abspath(target)
    if os.name == "nt":
        if target.startswith("\\\\"):  # UNC 网络路径，junction 不支持，需管理员 symlink
            os.symlink(target, link_path, target_is_directory=True)
            return
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, target],
            capture_output=True, text=True, errors="replace",
        )
        if r.returncode != 0:
            raise RuntimeError("mklink /J 失败: %s%s" % ((r.stdout or "").strip(), (r.stderr or "").strip()))
        return
    os.symlink(target, link_path)


def remove_link(link_path):
    """移除软链接/ junction 本身（不影响目标内容）。"""
    if os.path.islink(link_path):
        os.unlink(link_path)
        return
    if os.name == "nt" and is_reparse_dir(link_path):
        r = subprocess.run(
            ["cmd", "/c", "rmdir", link_path],
            capture_output=True, text=True, errors="replace",
        )
        if r.returncode != 0:
            raise RuntimeError("rmdir junction 失败: %s%s" % ((r.stdout or "").strip(), (r.stderr or "").strip()))
        return
    raise RuntimeError("路径不是软链接/junction: " + link_path)


def trash_path(path, hard=False):
    """把目录移入系统废纸篓/回收站；hard=True 时永久删除。返回描述字符串。
    这是"删除"操作的唯一推荐实现——个人文件永远先进废纸篓，可反悔。"""
    if hard:
        shutil.rmtree(path)
        return "已永久删除: " + path
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        trash_dir = os.path.join(home, ".Trash")
        os.makedirs(trash_dir, exist_ok=True)
        base = os.path.basename(os.path.abspath(path).rstrip(os.sep))
        target = os.path.join(trash_dir, base)
        i = 1
        while os.path.lexists(target):
            target = os.path.join(trash_dir, "%s %d" % (base, i))
            i += 1
        try:
            os.rename(path, target)
        except OSError:
            shutil.move(path, target)  # 跨卷回退（复制+删除）
        return "已移入废纸篓: " + target
    if os.name == "nt":
        ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
              "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory('%s',"
              "'OnlyErrorDialogs','SendToRecycleBin')" % path.replace("'", "''"))
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, errors="replace")
        if r.returncode == 0:
            return "已移入回收站: " + path
        raise RuntimeError("移入回收站失败(%s)。如确认要永久删除请加 --hard-delete" % ((r.stderr or "").strip()[:200]))
    for tool, extra in (("gio", ["trash"]), ("trash-put", [])):
        exe = shutil.which(tool)
        if exe:
            r = subprocess.run([exe] + extra + [path], capture_output=True, text=True)
            if r.returncode == 0:
                return "已移入系统回收站: " + path
    raise RuntimeError("当前环境无回收站工具。如确认要永久删除请加 --hard-delete")


def detect_app_running():
    """检测 WorkBuddy/CodeBuddy 相关进程是否在运行。
    返回 (state, info)：state ∈ "running" | "not-running" | "unknown"。
    沙箱/受限环境下 ps/tasklist 会被拒绝（PermissionError），此时返回
    "unknown"（宁可误拦不可漏拦，fail-closed）。"""
    hits = []
    try:
        if os.name == "nt":
            r = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, errors="replace",
            )
            for line in (r.stdout or "").splitlines():
                if not line.strip():
                    continue
                name = line.split('","')[0].strip().strip('"')
                if NAME_PAT.search(name):
                    hits.append(("-", name))
        else:
            r = subprocess.run(
                ["ps", "-axo", "pid=,comm="],
                capture_output=True, text=True, errors="replace",
            )
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                pid, _, comm = line.partition(" ")
                base = os.path.basename(comm.strip()).lower()
                if NAME_PAT.search(base) and not base.startswith(
                    ("python", "bash", "zsh", "dash", "sh", "node", "rg", "grep", "cat", "ps")
                ):
                    hits.append((pid, comm.strip()))
    except Exception as ex:
        # 沙箱拒绝执行 ps/tasklist 等：无法判断，按 unknown 处理
        return "unknown", "%s: %s" % (type(ex).__name__, ex)
    return ("running" if hits else "not-running"), hits


# ---------------------------------------------------------------- 应用生命周期（migrate 用）

def mac_app_bundles(procs):
    """从进程列表提取 .app bundle 路径（去重，主程序在前）。"""
    bundles = []
    for _pid, comm in procs:
        m = re.match(r"^(.*?\.app)/Contents/MacOS/", comm)
        if m and m.group(1) not in bundles:
            bundles.append(m.group(1))
    return bundles


def windows_main_exes():
    """记录当前运行的 WorkBuddy 主程序 exe 路径（Windows，用于迁移后重开）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object { $_.Path -match 'workbuddy' } | "
             "Select-Object -ExpandProperty Path -Unique"],
            capture_output=True, text=True, errors="replace")
        return [line.strip() for line in (r.stdout or "").splitlines()
                if line.strip() and line.strip().lower().endswith(".exe")]
    except Exception:
        return []


def wait_apps_gone(timeout):
    for _ in range(timeout):
        time.sleep(1)
        state, _ = detect_app_running()
        if state == "not-running":
            return True
    return False


def kill_running(sig):
    state, procs = detect_app_running()
    if state != "running":
        return
    for pid, _comm in procs:
        try:
            os.kill(int(pid), sig)
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass


def graceful_quit_all(procs):
    """自动退出所有 WorkBuddy/CodeBuddy 进程并等待退出；超时强杀兜底。
    返回迁移完成后需要重新打开的应用路径列表（仅 WorkBuddy 主程序）。"""
    if sys.platform == "darwin":
        bundles = mac_app_bundles(procs)
        for b in bundles:
            subprocess.run(
                ["osascript", "-e", 'tell application "%s" to quit' % b],
                capture_output=True, text=True, errors="replace")
        if not wait_apps_gone(20):
            print("      优雅退出超时，发送 SIGTERM ...")
            kill_running(signal.SIGTERM)
            if not wait_apps_gone(10):
                print("      仍未退出，强制结束 ...")
                kill_running(signal.SIGKILL)
                if not wait_apps_gone(5):
                    raise RuntimeError("无法结束 WorkBuddy 进程，请手动退出后重试")
        return [b for b in bundles if "workbuddy" in os.path.basename(b).lower()]
    if os.name == "nt":
        names = sorted({comm for _pid, comm in procs})
        relaunch = windows_main_exes()
        for n in names:
            subprocess.run(["taskkill", "/IM", n], capture_output=True)
        if not wait_apps_gone(20):
            for n in names:
                subprocess.run(["taskkill", "/IM", n, "/F"], capture_output=True)
            if not wait_apps_gone(10):
                raise RuntimeError("无法结束 WorkBuddy 进程，请手动退出后重试")
        return relaunch
    kill_running(signal.SIGTERM)
    if not wait_apps_gone(15):
        kill_running(signal.SIGKILL)
        if not wait_apps_gone(5):
            raise RuntimeError("无法结束 WorkBuddy 进程，请手动退出后重试")
    return []


def relaunch_apps(paths):
    for p in paths:
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-a", p])
            else:
                subprocess.Popen([p])
        except Exception as ex:
            print("      重启 %s 失败: %s（请手动打开）" % (p, ex))


# ---------------------------------------------------------------- 复制与校验

def run_copy(src, dest):
    """单次全量/增量复制。返回所用方法名。"""
    os.makedirs(dest, exist_ok=True)
    if os.name != "nt" and shutil.which("rsync"):
        r = subprocess.run(
            ["rsync", "-a", "--", src + os.sep, dest + os.sep],
            capture_output=True, text=True, errors="replace",
        )
        if r.returncode != 0:
            raise RuntimeError("rsync 失败(退出码 %d): %s" % (r.returncode, (r.stderr or "")[:500]))
        return "rsync"
    if os.name == "nt":
        proc = subprocess.Popen([
            "robocopy", src, dest,
            "/E", "/COPY:DAT", "/DCOPY:DAT", "/R:2", "/W:1", "/NFL", "/NDL", "/NP",
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace")
        for line in proc.stdout:
            print(line, end="", flush=True)
        rc = proc.wait()
        if rc >= 8:
            raise RuntimeError("robocopy 失败，退出码 %d" % rc)
        return "robocopy"
    # 纯 Python 兜底
    errors = []
    with os.scandir(src) as it:
        entries = list(it)
    for entry in entries:
        s = entry.path
        d = os.path.join(dest, entry.name)
        try:
            if entry.is_symlink():
                if os.path.lexists(d):
                    os.unlink(d)
                os.symlink(os.readlink(s), d)
            elif entry.is_dir(follow_symlinks=False):
                shutil.copytree(s, d, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        except Exception as ex:
            errors.append((s, str(ex)))
    if errors:
        raise RuntimeError("复制失败 %d 项，示例: %s" % (len(errors), errors[0]))
    return "pycopy"




def verify_strict(src, dest):
    """源目录静止状态下的严格校验：文件清单与大小必须逐项一致（migrate 用）。
    源目录静止（应用已关闭），任何不一致都视为失败。"""
    s = relative_file_set(src)
    d = relative_file_set(dest)
    missing = {k: v for k, v in s.items() if k not in d}
    diff = {k: (v, d[k]) for k, v in s.items() if k in d and d[k] != v}
    extra = set(d) - set(s)
    if missing or diff or extra:
        print("  缺失 %d 个文件" % len(missing))
        for k in sorted(missing)[:8]:
            print("    - " + k)
        print("  大小不一致 %d 个文件" % len(diff))
        for k in sorted(diff)[:8]:
            print("    - %s (src %s vs dest %s)" % (k, fmt_bytes(diff[k][0]), fmt_bytes(diff[k][1])))
        print("  目标多出 %d 个文件（源目录中不存在）" % len(extra))
        for k in sorted(extra)[:8]:
            print("    + " + k)
        return False
    print("      校验通过：%d 个文件 / %s，逐项一致。" % (len(s), fmt_bytes(sum(s.values()))))
    return True


# ---------------------------------------------------------------- plan 管理

def write_plan(plan):
    os.makedirs(PLAN_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(PLAN_DIR, "plan-%s.json" % ts)
    i = 1
    while os.path.exists(path):
        path = os.path.join(PLAN_DIR, "plan-%s-%d.json" % (ts, i))
        i += 1
    plan["plan_path"] = path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return path


def load_plan(ref):
    if ref == "latest":
        cands = sorted(globmod.glob(os.path.join(PLAN_DIR, "plan-*.json")))
        if not cands:
            sys.exit("没有找到任何 plan 文件（%s/plan-*.json），请先执行 copy。" % PLAN_DIR)
        ref = cands[-1]
    path = os.path.abspath(os.path.expanduser(ref))
    if not os.path.isfile(path):
        sys.exit("plan 文件不存在: " + path)
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    plan["plan_path"] = path
    return plan


def save_plan(plan):
    with open(plan["plan_path"], "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 子命令

def resolve_src(args_src, force_any_dir):
    src = os.path.abspath(os.path.expanduser(args_src or DEFAULT_SRC))
    if os.path.normpath(src) != os.path.normpath(DEFAULT_SRC) and not force_any_dir:
        sys.exit(
            "安全限制：本工具只迁移默认目录 %s。\n"
            "如确需操作其他目录（如测试），加 --force-any-dir 显式确认。" % DEFAULT_SRC
        )
    return src


def guard_common(src):
    home = os.path.expanduser("~")
    if src in ("/", home, os.path.dirname(home)):
        sys.exit("拒绝操作系统级/用户主目录本身: " + src)
    if is_within(PLAN_DIR, src) or os.path.normpath(src) == os.path.normpath(PLAN_DIR):
        sys.exit("拒绝操作 plan 存放目录: " + src)


def cmd_detect(args):
    src = os.path.abspath(os.path.expanduser(args.src or DEFAULT_SRC))
    guard_common(src)
    print("系统环境: %s (%s)" % (os.name.upper(), sys.platform))
    print("目标目录: %s" % src)
    tgt = link_target(src)
    backup = src + BACKUP_SUFFIX
    print("磁盘剩余空间(主目录所在卷): %s 可用 / 共 %s" % (
        fmt_bytes(shutil.disk_usage(home_of(src)).free),
        fmt_bytes(shutil.disk_usage(home_of(src)).total)))
    if tgt is not None:
        print()
        print("[已迁移状态] %s 是软链接 -> %s" % (src, tgt))
        if os.path.isdir(src):
            b, n, _ = tree_stats(src)
            print("  实际数据(经链接读取): %s / %d 个文件" % (fmt_bytes(b), n))
        if os.path.isdir(backup):
            print("  旧数据备份仍在: %s" % backup)
        plans = sorted(globmod.glob(os.path.join(PLAN_DIR, "plan-*.json")))
        if plans:
            print("  最近的 plan: %s （rollback --plan %s 可回滚）" % (plans[-1], plans[-1]))
        return
    if not os.path.isdir(src):
        print("目录不存在，无需迁移。")
        return
    total, files, errors = tree_stats(src)
    print()
    print("[待迁移状态] 普通目录，总体积 %s / %d 个文件%s" % (
        fmt_bytes(total), files, ("（%d 项读取失败）" % errors) if errors else ""))
    print()
    print("体积 Top 子目录：")
    rows = []
    with os.scandir(src) as it:
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False):
                    rows.append((e.name, tree_stats(e.path)[0]))
                else:
                    rows.append((e.name, e.stat(follow_symlinks=False).st_size))
            except OSError:
                rows.append((e.name, -1))
    for name, size in sorted(rows, key=lambda x: -(x[1] if x[1] > 0 else 0))[:12]:
        print("  %-28s %s" % (name, fmt_bytes(size) if size >= 0 else "读取失败"))
    running, hits = detect_app_running()
    print()
    if running == "running":
        print("当前检测到 WorkBuddy/CodeBuddy 相关进程 %d 个（一键 migrate 会自动退出它们）：" % len(hits))
        for pid, name in hits[:8]:
            print("  - [%s] %s" % (pid, name))
    elif running == "unknown":
        print("无法检测 WorkBuddy 进程状态（当前处于受限沙箱）。migrate 需通过 runner 或终端运行。")
    else:
        print("当前未检测到 WorkBuddy/CodeBuddy 运行进程。")


def home_of(path):
    return os.path.expanduser("~")




def _cmd_redirect(args, src, cur):
    """重定向迁移：当前数据在 A（软链接指向），直接搬到新目标 B。
    数据源 = A 的当前实际内容（含历次迁移后全部变更），与旧快照无关。"""
    old_dest = os.path.abspath(cur)
    if args.args_file:
        af = os.path.abspath(os.path.expanduser(args.args_file))
        if not os.path.isfile(af):
            sys.exit("args 文件不存在: " + af)
        with open(af, encoding="utf-8") as f:
            data = json.load(f)
        dest_base = data.get("dest_base")
        name = data.get("name") or args.name
        if not dest_base:
            sys.exit("args 文件缺少 dest_base: " + af)
    else:
        dest_base = args.dest_base
        name = args.name
    if not dest_base:
        sys.exit("请通过 --dest-base 指定新目标基准目录。")
    dest_base = os.path.abspath(os.path.expanduser(dest_base))
    new_dest = os.path.join(dest_base, name or os.path.basename(src))
    if os.path.normpath(new_dest) == os.path.normpath(old_dest):
        sys.exit("新目标 %s 与当前实际位置相同，无需迁移。" % new_dest)
    if not os.path.isdir(old_dest):
        sys.exit("软链接指向的实际位置不存在: %s，请人工检查。" % old_dest)
    if is_within(old_dest, new_dest) or is_within(new_dest, old_dest):
        sys.exit("新目标与当前实际位置存在嵌套关系，拒绝执行。\n  当前: %s\n  新目标: %s" % (old_dest, new_dest))
    if os.path.isdir(new_dest) and os.listdir(new_dest):
        sys.exit("新目标目录已存在且非空，为避免数据混淆拒绝执行: %s" % new_dest)
    src_bytes, src_files, _ = tree_stats(old_dest)
    os.makedirs(dest_base, exist_ok=True)
    free = shutil.disk_usage(dest_base).free
    same_vol = os.stat(os.path.dirname(old_dest)).st_dev == os.stat(dest_base).st_dev

    manage_apps = (os.path.normpath(src) == os.path.normpath(DEFAULT_SRC)) and not args.skip_app_handling
    relaunch = []
    if manage_apps:
        state, procs = detect_app_running()
        if state == "unknown":
            sys.exit("无法检测 WorkBuddy 进程状态（受限环境）。请通过 runner 或终端运行。")
        if state == "running":
            print("[1/5] 检测到 WorkBuddy 运行中，自动优雅退出 ...")
            relaunch = graceful_quit_all(procs)
            state2, _ = detect_app_running()
            if state2 != "not-running":
                sys.exit("自动退出 WorkBuddy 失败，请手动退出后重新运行。")
        else:
            print("[1/5] WorkBuddy 未在运行。")
    else:
        print("[1/5] 跳过 WorkBuddy 进程管理（非默认目录或 --skip-app-handling）。")

    print("[2/5] 数据从当前实际位置迁到新目标（数据源=当前实际内容，含全部变更）...")
    print("      %s (%s, %d 个文件) -> %s" % (old_dest, fmt_bytes(src_bytes), src_files, new_dest))
    moved = False
    if same_vol:
        try:
            os.rename(old_dest, new_dest)
            moved = True
            print("      同卷瞬时移动完成。")
        except OSError:
            pass
    if not moved:
        if free < src_bytes * 1.02:
            sys.exit("目标卷剩余空间不足: 需要约 %s（含2%余量），仅剩 %s。" % (fmt_bytes(src_bytes), fmt_bytes(free)))
        run_copy(old_dest, new_dest)
        print("      复制完成，严格校验 ...")
        if not verify_strict(old_dest, new_dest):
            sys.exit("校验失败。当前数据仍在 %s 未动；软链接未变，请排查后重试。" % old_dest)

    print("[3/5] 切换软链接指向 ...")
    remove_link(src)
    try:
        make_link(new_dest, src)
    except Exception as ex:
        # 链接创建失败时恢复指向旧位置（rename 场景旧位置已不存在，改试新位置重试）
        try:
            make_link(old_dest if os.path.isdir(old_dest) else new_dest, src)
        except Exception:
            pass
        sys.exit("切换软链接失败（已尝试恢复原链接）: %s\n数据完好，位置: %s" % (ex, new_dest if not os.path.isdir(old_dest) else old_dest))
    tgt = link_target(src)
    if tgt is None or os.path.normpath(os.path.abspath(tgt)) != os.path.normpath(os.path.abspath(new_dest)):
        sys.exit("链接校验失败。数据完好，位置: %s；请人工执行: ln -s '%s' '%s'" % (new_dest, new_dest, src))

    print("[4/5] 清理旧位置数据 ...")
    if args.keep_backup:
        print("      按要求保留旧位置数据: %s" % old_dest)
        kept = True
    else:
        kept = False
        if os.path.isdir(old_dest):
            try:
                msg = trash_path(old_dest)
                print("      " + msg)
            except RuntimeError as ex:
                kept = True
                print("      自动清理失败（%s），旧数据保留在 %s。" % (ex, old_dest))
        else:
            print("      旧位置数据已随同卷移动消失，无需清理。")
        if not kept:
            _rmdir_if_empty(os.path.dirname(old_dest))

    if manage_apps and relaunch and not args.no_relaunch:
        print("[5/5] 重新启动 WorkBuddy ...")
        relaunch_apps(relaunch)
    else:
        print("[5/5] 跳过自动重启。")

    # 更新 plan：优先更新既有 plan（latest），否则新建
    try:
        plan = load_plan("latest")
        plan.update(dest=new_dest, backup=src + BACKUP_SUFFIX, swapped=True,
                    swapped_at=now_iso(), redirected_from=old_dest)
        plan["src_bytes"], plan["src_files"] = src_bytes, src_files
        save_plan(plan)
        path = plan["plan_path"]
    except SystemExit:
        plan = {
            "version": 1, "created_at": now_iso(),
            "platform": "%s/%s" % (os.name, sys.platform),
            "method": "redirect", "src": src, "dest": new_dest,
            "backup": src + BACKUP_SUFFIX, "src_bytes": src_bytes,
            "src_files": src_files, "verified": True, "swapped": True,
            "swapped_at": now_iso(), "redirected_from": old_dest,
        }
        path = write_plan(plan)
    print()
    print("✓ 重定向迁移完成！")
    print("  %s 现为软链接 -> %s （原指向 %s）" % (src, new_dest, old_dest))
    print("  plan: %s" % path)
    if kept:
        print("  旧位置数据保留在: %s，确认后可手动删除。" % old_dest)
    print("  回滚（数据迁回默认路径）: python3 %s rollback --plan %s" % (os.path.abspath(__file__), path))
    print("  彻底恢复原状: python3 %s restore --plan %s" % (os.path.abspath(__file__), path))


def cmd_migrate(args):
    """一键迁移（推荐路径）：自动关闭 WorkBuddy → 单遍复制 → 严格校验
    → 原目录改名备份+建软链接 → 自动重开 WorkBuddy。
    若源路径已是软链接（此前已迁移到位置 A），自动切换为「重定向迁移」：
    数据从 A 直接搬到新目标 B（同卷 rename / 跨卷复制+校验），软链接改指 B，
    A 原位置清理，无需先 rollback 回默认路径。
    重要：本命令会关闭 WorkBuddy，必须以独立进程运行（make-runner 生成的
    脚本或用户终端），不要在 WorkBuddy 会话内直接执行。"""
    src = resolve_src(args.src, args.force_any_dir)
    guard_common(src)
    cur = link_target(src)
    if cur is not None:
        _cmd_redirect(args, src, cur)
        return
    if not os.path.isdir(src):
        sys.exit("源目录不存在: " + src)
    if args.args_file:
        af = os.path.abspath(os.path.expanduser(args.args_file))
        if not os.path.isfile(af):
            sys.exit("args 文件不存在: " + af)
        with open(af, encoding="utf-8") as f:
            data = json.load(f)
        dest_base = data.get("dest_base")
        name = data.get("name") or args.name
        if not dest_base:
            sys.exit("args 文件缺少 dest_base: " + af)
    else:
        if not args.dest_base:
            sys.exit("请通过 --dest-base 指定目标基准目录（或用 make-runner 生成一键脚本）。")
        dest_base = args.dest_base
        name = args.name
    dest_base = os.path.abspath(os.path.expanduser(dest_base))
    dest = os.path.join(dest_base, name or os.path.basename(src))
    if is_within(src, dest) or is_within(dest, src):
        sys.exit("目标位置与源目录存在嵌套关系，拒绝执行。\n  src=%s\n  dest=%s" % (src, dest))
    src_bytes, src_files, _ = tree_stats(src)
    os.makedirs(dest_base, exist_ok=True)
    free = shutil.disk_usage(dest_base).free
    if free < src_bytes * 1.02:
        sys.exit("目标卷剩余空间不足: 需要约 %s（含2%余量），目标卷 %s 仅剩 %s。" % (
            fmt_bytes(src_bytes), dest_base, fmt_bytes(free)))
    if os.path.isdir(dest) and os.listdir(dest):
        print("注意: 目标目录已有内容，将合并复制并在校验时严格比对。")

    manage_apps = (os.path.normpath(src) == os.path.normpath(DEFAULT_SRC)) and not args.skip_app_handling
    relaunch = []
    if manage_apps:
        state, procs = detect_app_running()
        if state == "unknown":
            sys.exit(
                "无法检测 WorkBuddy 进程状态（受限环境: %s）。\n"
                "migrate 会自动关闭 WorkBuddy，不能在 WorkBuddy 会话内直接运行。\n"
                "请使用 make-runner 生成的脚本，或在终端中运行本命令。" % procs)
        if state == "running":
            print("[1/6] 检测到 WorkBuddy 运行中，自动优雅退出 ...")
            relaunch = graceful_quit_all(procs)
            state2, _ = detect_app_running()
            if state2 != "not-running":
                sys.exit("自动退出 WorkBuddy 失败，请手动退出后重新运行。")
            print("      已全部退出，源目录静止。")
        else:
            print("[1/6] WorkBuddy 未在运行。")
    else:
        print("[1/6] 跳过 WorkBuddy 进程管理（非默认目录或 --skip-app-handling）。")

    print("[2/6] 复制 %s (%s, %d 个文件)" % (src, fmt_bytes(src_bytes), src_files))
    print("      -> %s （源目录已静止，单遍复制，视磁盘速度可能需要数分钟）" % dest)
    method = run_copy(src, dest)
    print("      复制完成（%s）。" % method)
    print("[3/6] 严格校验（文件清单与大小逐项比对）...")
    if not verify_strict(src, dest):
        sys.exit("校验失败。未对源目录做任何改动（原样保留），请排查后重新运行 migrate。")
    # TOCTOU 加固：校验期间用户可能重新打开了 WorkBuddy。
    # 1) 进程复查：一旦发现运行立即中止（源目录未动）；
    # 2) 二轮校验：即使进程检测漏网（如检测为 unknown 的场景），源目录若在校验
    #    期间发生任何变化（新文件/大小变化），二轮比对必然暴露，同样中止。
    if manage_apps:
        st, _info = detect_app_running()
        if st != "not-running":
            sys.exit(
                "检测到 WorkBuddy 在迁移过程中被重新打开，已中止。\n"
                "源目录未做任何改动（副本已在目标位置，可安全重跑）。\n"
                "请保持 WorkBuddy 退出状态，重新运行本脚本。")
    print("      复查源目录状态（二轮校验，防迁移期间外部写入）...")
    if not verify_strict(src, dest):
        sys.exit(
            "二轮校验发现源目录在迁移期间发生了变化（可能有程序在写入），已中止。\n"
            "源目录未做任何改动，请确保 WorkBuddy 保持完全退出后重新运行。")
    backup = src + BACKUP_SUFFIX
    if os.path.exists(backup):
        sys.exit("备份路径已存在，请先处理: " + backup)
    print("[4/6] 原目录改名备份并创建软链接 ...")
    try:
        os.rename(src, backup)
    except OSError as ex:
        sys.exit(
            "改名备份失败（WorkBuddy 可能刚被打开并锁定了目录）: %s\n"
            "源目录未做任何改动，请完全退出 WorkBuddy 后重新运行本脚本。" % ex)
    try:
        make_link(dest, src)
    except Exception as ex:
        os.rename(backup, src)
        sys.exit("创建软链接失败（已自动恢复原目录，未造成损失）:\n  %s\n  %s" % (ex, WIN_SYMLINK_HINT))
    tgt = link_target(src)
    if tgt is None or os.path.normpath(os.path.abspath(tgt)) != os.path.normpath(os.path.abspath(dest)):
        remove_link(src)
        os.rename(backup, src)
        sys.exit("链接校验失败，已自动恢复原目录。")
    plan = {
        "version": 1,
        "created_at": now_iso(),
        "platform": "%s/%s" % (os.name, sys.platform),
        "method": "migrate",
        "src": src,
        "dest": dest,
        "backup": backup,
        "src_bytes": src_bytes,
        "src_files": src_files,
        "verified": True,
        "swapped": True,
        "swapped_at": now_iso(),
    }
    path = write_plan(plan)
    print("[5/6] 迁移成功，清理原始数据（移入废纸篓，可反悔）...")
    if args.keep_backup:
        print("      按要求保留原始数据: %s" % backup)
    else:
        try:
            msg = trash_path(backup)
            plan.update(backup_removed=True, backup_removed_at=now_iso(), backup_removed_how=msg)
            save_plan(plan)
            print("      " + msg)
        except RuntimeError as ex:
            print("      自动清理失败（%s），原始数据保留在 %s，可稍后用 cleanup-backup 清理。" % (ex, backup))
    if manage_apps and relaunch and not args.no_relaunch:
        print("[6/6] 重新启动 WorkBuddy ...")
        relaunch_apps(relaunch)
    else:
        print("[6/6] 跳过自动重启（--no-relaunch 或此前未关闭应用）。")
    print()
    print("✓ 一键迁移完成！")
    print("  %s 现为软链接 -> %s" % (src, dest))
    print("  plan: %s" % path)
    print("  后续所有操作（回滚/恢复）都基于当前实际数据位置 %s，与备份无关。" % dest)
    if not args.keep_backup:
        print("  如需反悔删除: 可从系统废纸篓找回原始数据。")
    else:
        print("  原始数据保留在: %s（确认稳定后用 cleanup-backup 清理）" % backup)
    print("  回滚（数据迁回原位，零丢失）: python3 %s rollback --plan %s" % (os.path.abspath(__file__), path))
    print("  彻底恢复原状: python3 %s restore --plan %s" % (os.path.abspath(__file__), path))


def require_offline(args):
    state, info = detect_app_running()
    if state != "not-running" and not args.allow_app_running:
        if state == "running":
            print("检测到 WorkBuddy/CodeBuddy 正在运行：")
            for pid, name in info[:10]:
                print("  - [%s] %s" % (pid, name))
        else:
            print("无法检测 WorkBuddy 进程状态（受限环境: %s）。" % info)
        print()
        print("为避免数据竞争，本阶段必须在完全退出 WorkBuddy 的环境执行。")
        print("推荐使用一键流程：make-runner --dest-base <目录>（自动关闭/重启 WorkBuddy）。")
        print("在 WorkBuddy 会话内直接执行会被拒绝（exit code 3），这是设计行为。")
        sys.exit(3)
    return state




class _LogStream:
    """同步显示并落盘；不关闭原始终端流。"""
    def __init__(self, console, log):
        self.console, self.log = console, log

    def write(self, text):
        self.log.write(text)
        self.log.flush()
        return self.console.write(text)

    def flush(self):
        self.log.flush()
        self.console.flush()

    def __getattr__(self, name):
        return getattr(self.console, name)


def cmd_run_job(args):
    """Windows runner 的内部入口：参数从 JSON 读取，保留完整输出与退出码。"""
    log_dir = os.path.abspath(os.path.expanduser(args.log_dir))
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    log_path = os.path.join(log_dir, "run-%s.log" % stamp)
    old_out, old_err = sys.stdout, sys.stderr
    rc = 1
    with open(log_path, "w", encoding="utf-8") as log:
        sys.stdout, sys.stderr = _LogStream(old_out, log), _LogStream(old_err, log)
        try:
            print("Started: %s" % now_iso(), flush=True)
            print("Log: %s" % log_path, flush=True)
            print("Python: %s" % sys.executable, flush=True)
            with open(args.args_file, encoding="utf-8") as f:
                job = json.load(f)
            command = job.get("command", "migrate")
            if command == "migrate":
                if not job.get("dest_base"):
                    raise ValueError("Job is missing dest_base")
                print("Target: %s" % os.path.join(job["dest_base"], job.get("name") or ".workbuddy"), flush=True)
                cmd_migrate(argparse.Namespace(
                    dest_base=job["dest_base"], name=job.get("name"), args_file=None,
                    src=None, force_any_dir=False, skip_app_handling=False,
                    no_relaunch=False, keep_backup=False))
            elif command == "restore":
                if not job.get("plan"):
                    raise ValueError("Job is missing plan")
                cmd_restore(argparse.Namespace(plan=job["plan"], keep_backup=False,
                                               hard_delete=False, no_relaunch=False))
            else:
                raise ValueError("Unsupported runner command: %s" % command)
            rc = 0
        except SystemExit as ex:
            rc = ex.code if isinstance(ex.code, int) else (0 if ex.code is None else 1)
            if ex.code is not None and not isinstance(ex.code, int):
                print(str(ex.code), file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            rc = 130
            print("Interrupted. Check directory/link state before retrying.", file=sys.stderr, flush=True)
        except Exception:
            traceback.print_exc()
            rc = 1
        finally:
            try:
                print("\nRESULT: %s (exit code %d)" % ("SUCCESS" if rc == 0 else "FAILED", rc), flush=True)
                print("Finished: %s\nLog: %s" % (now_iso(), log_path), flush=True)
            finally:
                sys.stdout, sys.stderr = old_out, old_err
    raise SystemExit(rc)


def windows_runner_content(job_name):
    """ASCII 启动器；中文路径只来自环境变量或 JSON，cmd /K 保留结果窗口。"""
    return "\r\n".join([
        "@echo off",
        "setlocal DisableDelayedExpansion",
        'if /I "%~1"=="--console" goto run',
        '"%ComSpec%" /D /V:OFF /K ""%~f0" --console"',
        "exit /b",
        ":run",
        "chcp 65001 >nul",
        'title WorkBuddy cache mover',
        'echo WorkBuddy cache mover - preparing to run.',
        'echo WorkBuddy may close automatically. Do not reopen it during the operation.',
        'set "PYEXE="',
        'set "PYFLAG="',
        "for /f \"delims=\" %%P in ('where py.exe 2^>nul') do if not defined PYEXE call :try_python \"%%P\" -3",
        "for /f \"delims=\" %%P in ('where python.exe 2^>nul') do if not defined PYEXE call :try_python \"%%P\"",
        'for /d %%D in ("%USERPROFILE%\\.workbuddy\\binaries\\python\\versions\\*") do if not defined PYEXE call :try_python "%%~fD\\python.exe"',
        'for /d %%D in ("%USERPROFILE%\\.workbuddy\\binaries\\python\\versions\\*") do if not defined PYEXE call :try_python "%%~fD\\bin\\python.exe"',
        "if defined PYEXE goto execute",
        "echo ERROR: No working Python 3.8+ interpreter was found.",
        "echo Install Python 3.8+ from python.org, then regenerate and rerun this launcher.",
        'set "RC=1"',
        "goto finish",
        ":execute",
        'echo Python: "%PYEXE%" %PYFLAG%',
        '"%PYEXE%" %PYFLAG% -X utf8 -u "%~dp0cache_mover.py" run-job --args-file "%~dp0' + job_name + '" --log-dir "%LOCALAPPDATA%\\WorkBuddyCacheMover\\logs"',
        'set "RC=%ERRORLEVEL%"',
        ":finish",
        "echo.",
        'if "%RC%"=="0" (echo RESULT: SUCCESS) else (echo RESULT: FAILED - exit code %RC%)',
        'echo Logs, if the job started: "%LOCALAPPDATA%\\WorkBuddyCacheMover\\logs"',
        'echo This window stays open. Read the result, then close it or type EXIT.',
        "exit /b %RC%",
        ":try_python",
        'if /I "%~dp1"=="%LOCALAPPDATA%\\Microsoft\\WindowsApps\\" exit /b 0',
        '"%~1" %~2 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1',
        "if errorlevel 1 exit /b 0",
        'set "PYEXE=%~1"',
        'set "PYFLAG=%~2"',
        "exit /b 0",
        "",
    ])


def cmd_make_runner(args):
    command = args.command
    os.makedirs(PLAN_DIR, exist_ok=True)
    local_script = os.path.join(PLAN_DIR, "cache_mover.py")
    if os.path.realpath(__file__) != os.path.realpath(local_script):
        shutil.copy2(os.path.abspath(__file__), local_script)
    if command == "migrate":
        if not args.dest_base:
            sys.exit("migrate 模式需要 --dest-base <目录>（将创建 <目录>/.workbuddy）。")
        args_file = os.path.join(PLAN_DIR, "migrate-args.json")
        payload = {"command": "migrate", "dest_base": os.path.abspath(os.path.expanduser(args.dest_base))}
        if args.name:
            payload["name"] = args.name
        with open(args_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        invoke = 'migrate --args-file "%s"' % os.path.join(PLAN_DIR, "migrate-args.json")
        banner = [
            "WorkBuddy cache migration - one-click migrate.",
            "WorkBuddy will be closed automatically, then relaunched when done.",
            "Please do NOT use WorkBuddy until this window says Done.",
        ]
    else:
        plan = load_plan(args.plan)
        plan_rel = os.path.basename(plan["plan_path"])
        invoke = "%s --plan %s" % (command, os.path.join(PLAN_DIR, plan_rel))
        banner = [
            "WorkBuddy cache migration - %s." % command,
            "WorkBuddy will be closed automatically, then relaunched when done.",
            "Please do NOT use WorkBuddy until this window says Done.",
        ]
    if os.name == "nt":
        out = args.out or os.path.join(PLAN_DIR, "run_%s.bat" % command)
        job_name = "migrate-args.json" if command == "migrate" else "runner-restore-args.json"
        if command == "restore":
            with open(os.path.join(PLAN_DIR, job_name), "w", encoding="utf-8") as f:
                json.dump({"command": command, "plan": plan["plan_path"]}, f,
                          ensure_ascii=False, indent=2)
        # --out 可位于其他目录：启动器所需文件也放到该目录。
        out_dir = os.path.dirname(os.path.abspath(os.path.expanduser(out)))
        out = os.path.join(out_dir, os.path.basename(out))
        os.makedirs(out_dir, exist_ok=True)
        if os.path.realpath(out_dir) != os.path.realpath(PLAN_DIR):
            shutil.copy2(local_script, os.path.join(out_dir, "cache_mover.py"))
            shutil.copy2(os.path.join(PLAN_DIR, job_name), os.path.join(out_dir, job_name))
        content = windows_runner_content(job_name)
        if not content.isascii():
            sys.exit("内部错误: bat 内容必须为 ASCII。")
        with open(out, "w", encoding="ascii", newline="") as f:
            f.write(content)
    else:
        out = args.out or os.path.join(PLAN_DIR, "run_%s.command" % command)
        banner_lines = ["#!/bin/bash"]
        for b in banner:
            banner_lines.append("# " + b if b == "pause" or b.startswith(("WorkBuddy", "Please")) else b)
        # pause 仅用于 restore 模式：等待用户确认已退出应用
        pause_lines = ['read -n 1 -s -r -p "Press any key to start..."'] if "pause" in banner else []
        content = "\n".join(banner_lines + [""] + [
            'PY=""',
            'for cand in "$(command -v python3)" "$(command -v python)" \\\n'
            '         /opt/homebrew/bin/python3 /usr/local/bin/python3 \\\n'
            '         "$HOME"/.workbuddy/binaries/python/versions/*/bin/python3; do',
            '  if [ -x "$cand" ]; then PY="$cand"; break; fi',
            "done",
            'if [ -z "$PY" ]; then echo "python3 not found"; read -n 1 -s -r -p "Press any key to close..."; exit 1; fi',
            "",
        ] + pause_lines + [
            "",
            '"$PY" "$HOME/.wb-cache-move/cache_mover.py" %s' % invoke,
            'rc=$?',
            'echo ""',
            'if [ $rc -eq 0 ]; then echo "Done. WorkBuddy can be used again."; else echo "Failed with exit code $rc."; fi',
            'read -n 1 -s -r -p "Press any key to close..."',
            "",
        ])
        with open(out, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(out, 0o755)
    print("✓ 已生成独立运行脚本: %s" % out)
    if os.name == "nt":
        print("  双击运行；无需按键开始。结束后窗口保持打开，显示 SUCCESS 或 FAILED 与退出码。")
        print("  运行日志: %LOCALAPPDATA%\\WorkBuddyCacheMover\\logs（恢复原状后仍保留）")
        print("  更新脚本后必须重新运行 make-runner；旧 .bat 不会自动更新。")
    if command == "migrate":
        print("  使用方式：双击运行，全自动完成：")
        print("  自动关闭 WorkBuddy → 复制+校验 → 建软链接 → 自动重新打开 WorkBuddy")
        print("  迁移目标: %s" % payload["dest_base"])
    else:
        print("  双击运行，自动关闭 WorkBuddy → 最新数据迁回 → 自动重开 → 清理迁移产物。")


def _rmdir_if_empty(path):
    """空目录则删除（非递归，目录非空时静默保留）。"""
    try:
        os.rmdir(path)
        return True
    except OSError:
        return False


def move_dest_back(src, dest):
    """把当前缓存数据（实际位置 dest，含迁移期间全部变更）迁回默认路径 src。
    恢复的数据源永远是 dest——软链接的当前指向，绝不使用 backup 旧快照，
    否则迁移期间的变更会全部丢失。
    同卷: rename 瞬间完成（dest 路径消失）；
    跨卷: 复制 + 严格校验（失败自动恢复软链接，数据无损），dest 保留由用户确认后删除。
    返回 (mode, message)。"""
    parent = os.path.dirname(src)
    os.makedirs(parent, exist_ok=True)
    try:
        same_vol = os.stat(parent).st_dev == os.stat(dest).st_dev
    except OSError:
        same_vol = False
    if same_vol:
        try:
            os.rename(dest, src)
            if os.path.isdir(src):
                _rmdir_if_empty(os.path.dirname(dest))  # 顺手清理空的基准目录
                return "rename", "同卷瞬时移动完成。"
        except OSError:
            pass
    run_copy(dest, src)
    if not verify_strict(dest, src):
        shutil.rmtree(src, ignore_errors=True)
        make_link(dest, src)
        raise RuntimeError(
            "跨卷复制校验失败。已自动恢复软链接，数据无损仍在 %s；请检查目标磁盘后重试。" % dest)
    # 复制校验一致，src 已是最新数据；dest 是多余副本，移入废纸篓（可反悔）
    note = "跨卷复制完成并校验一致。"
    try:
        msg = trash_path(dest)
        _rmdir_if_empty(os.path.dirname(dest))  # 顺手清理空的基准目录
        note += "旧副本%s。" % msg
    except RuntimeError as ex:
        note += "旧副本仍在 %s（%s），确认数据正常后可自行删除。" % (dest, ex)
    return "copy", note


def auto_manage_app(args, action_label):
    """restore/rollback 版的应用管理：检测 WB 运行则自动优雅关闭，
    返回 (manage_apps, relaunch_paths)。与 migrate 的差别：不做 unknown
    拦截前置（进程检测 unknown 时仍允许继续——此时用户在终端/runner 环境，
    检测受限不代表应用在运行；数据一致性由严格校验兜底）。"""
    state, procs = detect_app_running()
    relaunch = []
    if state == "running":
        print("[%s] 检测到 WorkBuddy 运行中，自动优雅退出 ..." % action_label)
        relaunch = graceful_quit_all(procs)
        state2, _ = detect_app_running()
        if state2 != "not-running":
            sys.exit("自动退出 WorkBuddy 失败，请手动退出后重新运行。")
    elif state == "unknown":
        print("[%s] 无法检测 WorkBuddy 进程状态（受限环境），按未运行继续；" % action_label)
        print("      若迁移后校验发现异常会自动中止保护数据。")
    else:
        print("[%s] WorkBuddy 未在运行。" % action_label)
    return relaunch


def cmd_rollback(args):
    """仅回滚：把当前缓存数据放回默认路径，保留旧快照与 plan（便于再次迁移）。
    数据源 = 软链接指向的当前实际位置（含迁移期间全部变更），不用备份快照，零丢失。"""
    plan = load_plan(args.plan)
    src, dest, backup = plan["src"], plan["dest"], plan["backup"]
    cur = link_target(src)
    if cur is None and os.path.isdir(src):
        print("源路径已是真实目录，无需回滚: " + src)
        return
    if cur is None:
        sys.exit("源路径不是软链接: %s，请人工检查。" % src)
    if os.path.normpath(os.path.abspath(cur)) != os.path.normpath(os.path.abspath(dest)):
        sys.exit("软链接指向 %s 与 plan 目标 %s 不符，请人工检查。" % (cur, dest))
    relaunch = auto_manage_app(args, "1/4")
    print("[2/4] 移除软链接: %s" % src)
    remove_link(src)
    if os.path.lexists(src):
        sys.exit("移除软链接失败，原样保留，请人工检查。")
    print("[3/4] 当前缓存数据迁回默认路径（数据源=当前实际位置，含迁移期间全部变更）...")
    mode, msg = move_dest_back(src, dest)
    plan.update(swapped=False, rolled_back_at=now_iso())
    save_plan(plan)
    print("[4/4] 完成。")
    if relaunch and not getattr(args, "no_relaunch", False):
        relaunch_apps(relaunch)
        print("      已重新启动 WorkBuddy。")
    print()
    print("✓ 已回滚，%s 恢复为真实目录（迁移期间全部变更保留，无数据丢失）。" % src)
    if mode == "copy":
        print("  " + msg)
    if os.path.isdir(backup):
        print("  迁移时刻的旧快照仍在: %s（确认后可删除，或让助手用 cleanup-backup 清理）" % backup)


def cmd_cleanup_backup(args):
    """用户主动发起的动作一：迁移稳定后删除原地的历史缓存备份。
    默认移入系统废纸篓（可反悔），需 --yes 显式确认。"""
    plan = load_plan(args.plan)
    src, dest, backup = plan["src"], plan["dest"], plan["backup"]
    if plan.get("backup_removed"):
        print("备份此前已删除(%s)，无需重复操作。" % plan.get("backup_removed_at", ""))
        return
    cur = link_target(src)
    if cur is not None:
        if os.path.normpath(os.path.abspath(cur)) != os.path.normpath(os.path.abspath(dest)):
            sys.exit(
                "当前源路径是指向别处的软链接，状态与 plan 不符，拒绝删除备份。\n"
                "  src=%s\n  当前链接目标=%s\n  plan 目标=%s" % (src, cur, dest))
        if not os.path.isdir(backup):
            print("备份目录不存在（可能已手动删除）: %s" % backup)
            plan.update(backup_removed=True, backup_removed_at=now_iso())
            save_plan(plan)
            return
        b_bytes, b_files, _ = tree_stats(backup)
        print("待清理的迁移备份: %s (%s, %d 个文件)" % (backup, fmt_bytes(b_bytes), b_files))
        print("说明: 删除的是迁移时刻的旧快照，当前数据在迁移目标 %s，不受影响。" % dest)
    elif os.path.isdir(src):
        if not os.path.isdir(backup):
            print("备份目录不存在（可能已手动删除）: %s" % backup)
            plan.update(backup_removed=True, backup_removed_at=now_iso())
            save_plan(plan)
            return
        b_bytes, b_files, _ = tree_stats(backup)
        print("待清理的迁移备份: %s (%s, %d 个文件)" % (backup, fmt_bytes(b_bytes), b_files))
        print("说明: 当前已回滚，最新数据在原位 %s，删除旧快照完全安全。" % src)
    else:
        sys.exit("源路径状态异常（非软链接也非真实目录），拒绝删除备份: " + src)
    if not args.yes:
        sys.exit("未确认。请与用户明确确认后加 --yes 执行。")
    msg = trash_path(backup, hard=args.hard_delete)
    plan.update(backup_removed=True, backup_removed_at=now_iso(), backup_removed_how=msg)
    save_plan(plan)
    print()
    print("✓ 备份清理完成: " + msg)


def cmd_restore(args):
    """用户主动发起的动作二：彻底恢复原状。
    恢复数据源 = 软链接指向的当前实际位置（含迁移期间全部变更），绝不用 backup 旧快照：
      移除软链接 → 当前数据迁回 ~/.workbuddy → 旧快照进废纸篓 → 清理 plan/runner 痕迹。
    与 rollback 的区别：rollback 只迁回数据（快照与 plan 保留）；restore 一切回到迁移前。"""
    plan = load_plan(args.plan)
    src, dest, backup = plan["src"], plan["dest"], plan["backup"]
    cur = link_target(src)
    copy_note = ""
    relaunch = []
    if cur is None and os.path.isdir(src):
        print("源路径已是真实目录（此前已回滚），跳过数据迁回。")
    elif cur is None:
        sys.exit("源路径既不是软链接也不是目录: %s，请人工检查。" % src)
    else:
        if os.path.normpath(os.path.abspath(cur)) != os.path.normpath(os.path.abspath(dest)):
            sys.exit("软链接指向 %s 与 plan 目标 %s 不符，请人工检查。" % (cur, dest))
        relaunch = auto_manage_app(args, "1/4")
        print("[2/4] 移除软链接: %s" % src)
        remove_link(src)
        if os.path.lexists(src):
            sys.exit("移除软链接失败，原样保留，请人工检查。")
        print("[3/4] 当前缓存数据迁回默认路径（数据源=当前实际位置，含迁移期间全部变更）...")
        _mode, copy_note = move_dest_back(src, dest)
        plan.update(swapped=False, restored_at=now_iso())
        save_plan(plan)
        if relaunch and not getattr(args, "no_relaunch", False):
            print("[4/4] 重新启动 WorkBuddy ...")
            relaunch_apps(relaunch)
    print("[%s] 清理迁移产物（旧快照 + plan/runner）..." % ("4/4" if not relaunch else "+"))
    if os.path.isdir(backup):
        if args.keep_backup:
            print("      按要求保留旧快照: %s" % backup)
        else:
            msg = trash_path(backup, hard=args.hard_delete)
            print("      旧快照（迁移时刻的备份）: " + msg)
    if copy_note.startswith("跨卷"):
        print("      " + copy_note)
    removed = []
    for f in ("run_migrate.command", "run_migrate.bat", "migrate-args.json",
              "runner-restore-args.json",
              "run_swap.command", "run_swap.bat",
              "run_restore.command", "run_restore.bat",
              "cache_mover.py", os.path.basename(plan["plan_path"])):
        fp = os.path.join(PLAN_DIR, f)
        if os.path.lexists(fp):
            os.remove(fp)
            removed.append(f)
    try:
        os.rmdir(PLAN_DIR)
        removed.append(PLAN_DIR + " 目录")
    except OSError:
        pass
    print("      已清理: %s" % ("、".join(removed) if removed else "无"))
    print()
    print("✓ 已恢复原状：%s 为真实目录，数据为迁移后最新状态（无变更丢失）。" % src)


def cmd_status(args):
    cmd_detect(args)


def main():
    _utf8_guard()
    p = argparse.ArgumentParser(
        description="WorkBuddy 缓存目录迁移工具（仅迁移 ~/.workbuddy，软链接接管原路径）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("子命令:")[1] if "子命令:" in __doc__ else None,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="查看 ~/.workbuddy 状态/体积分布/磁盘空间")
    d.add_argument("--src", default=None)
    d.set_defaults(func=cmd_detect)

    mg = sub.add_parser("migrate", help="一键迁移：自动关闭 WorkBuddy → 复制 → 校验 → 软链接 → 自动重开")
    mg.add_argument("--dest-base", default=None, help="目标基准目录，将创建 <基准>/.workbuddy")
    mg.add_argument("--args-file", default=None, help="从 JSON 文件读取 dest-base/name（runner 用）")
    mg.add_argument("--name", default=None, help="目标目录名（默认沿用源目录名）")
    mg.add_argument("--src", default=None, help="源目录（默认 ~/.workbuddy）")
    mg.add_argument("--force-any-dir", action="store_true", help="允许操作非默认目录（测试用）")
    mg.add_argument("--skip-app-handling", action="store_true",
                    help="不自动关闭/重开 WorkBuddy（非默认目录自动生效）")
    mg.add_argument("--no-relaunch", action="store_true", help="迁移完成后不自动重新打开 WorkBuddy")
    mg.add_argument("--keep-backup", action="store_true",
                    help="迁移后保留原始数据（默认自动移入废纸篓）")
    mg.set_defaults(func=cmd_migrate)

    m = sub.add_parser("make-runner", help="生成可双击运行的独立脚本（默认一键 migrate 模式）")
    m.add_argument("--command", choices=["migrate", "restore"], default="migrate",
                   help="生成哪种动作的 runner（默认 migrate；restore 恢复原状用）")
    m.add_argument("--dest-base", default=None, help="migrate 模式的目标基准目录")
    m.add_argument("--name", default=None, help="migrate 模式的目标目录名")
    m.add_argument("--plan", default="latest", help="swap/restore 模式使用的 plan（默认 latest）")
    m.add_argument("--out", default=None, help="输出脚本路径")
    m.set_defaults(func=cmd_make_runner)

    job = sub.add_parser("run-job", help="runner 内部入口：读取 JSON 参数并保存运行日志")
    job.add_argument("--args-file", required=True)
    job.add_argument("--log-dir", required=True)
    job.set_defaults(func=cmd_run_job)

    r = sub.add_parser("rollback", help="仅回滚：当前数据迁回原位（含迁移期间变更），保留旧快照与 plan")
    r.add_argument("--plan", required=True, help="plan 文件路径，或 latest")
    r.add_argument("--no-relaunch", action="store_true", help="回滚完成后不自动重新打开 WorkBuddy")
    r.set_defaults(func=cmd_rollback)

    cb = sub.add_parser("cleanup-backup", help="迁移稳定后清理历史备份（默认移入废纸篓，需 --yes）")
    cb.add_argument("--plan", required=True, help="plan 文件路径，或 latest")
    cb.add_argument("--yes", action="store_true", help="确认清理（与用户确认后传入）")
    cb.add_argument("--hard-delete", action="store_true", help="不进废纸篓，永久删除（不建议）")
    cb.set_defaults(func=cmd_cleanup_backup)

    rs = sub.add_parser("restore", help="彻底恢复原状：当前数据迁回原位+清旧快照+清理全部痕迹")
    rs.add_argument("--plan", required=True, help="plan 文件路径，或 latest")
    rs.add_argument("--keep-backup", action="store_true", help="保留迁移时刻的旧快照，仅清理其他产物")
    rs.add_argument("--hard-delete", action="store_true", help="旧快照不进废纸篓，永久删除")
    rs.add_argument("--no-relaunch", action="store_true", help="恢复完成后不自动重新打开 WorkBuddy")
    rs.set_defaults(func=cmd_restore)

    st = sub.add_parser("status", help="等同 detect")
    st.add_argument("--src", default=None)
    st.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
