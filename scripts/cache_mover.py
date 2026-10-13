#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 数据迁移：内容校验、持久阶段记录、独占锁与失败恢复。Python >= 3.8。"""
import argparse
import codecs
import contextlib
import csv
import datetime
import functools
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat as statmod
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid

DEFAULT_SRC = os.path.join(os.path.expanduser("~"), ".workbuddy")
PLAN_DIR = os.path.join(os.path.expanduser("~"), ".wb-cache-move")
BACKUP_SUFFIX = ".wb-migration-backup"
NAME_PAT = re.compile(r"workbuddy", re.IGNORECASE)
WIN_SYMLINK_HINT = "目标需支持目录链接；Windows 本地卷使用 junction，UNC 需 symlink 权限。"
_ACTIVE = None
_WARNINGS = []
STAGES = {'prepared': '准备', 'copying': '复制数据', 'verifying': '内容校验',
          'verified': '校验完成', 'publishing': '发布已校验副本', 'published': '副本就绪',
          'switching': '切换原路径', 'committed': '数据切换完成', 'cleanup': '清理旧副本',
          'complete': '完成', 'aborted': '已停止，原路径仍可用', 'recovery_required': '需要恢复确认'}


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
            # 外置卷交给 Finder 的原生废纸篓，不复制回系统盘。
            script = 'on run argv\n tell application "Finder" to delete (POSIX file (item 1 of argv))\nend run'
            r = subprocess.run(['osascript', '-e', script, path], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError('移入目标卷废纸篓失败，原数据保留: ' + (r.stderr or '')[:200])
            return '已移入目标卷废纸篓: ' + path
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
            capture_output=True, text=True, errors="replace", timeout=20)
        return [line.strip() for line in (r.stdout or "").splitlines()
                if line.strip() and os.path.basename(line.strip()).lower() == "workbuddy.exe"]
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
                capture_output=True, text=True, errors="replace", timeout=20)
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
            subprocess.run(["taskkill", "/IM", n], capture_output=True, timeout=20)
        if not wait_apps_gone(20):
            for n in names:
                subprocess.run(["taskkill", "/IM", n, "/F"], capture_output=True, timeout=20)
            if not wait_apps_gone(10):
                raise RuntimeError("无法结束 WorkBuddy 进程，请手动退出后重试")
        return relaunch
    kill_running(signal.SIGTERM)
    if not wait_apps_gone(15):
        kill_running(signal.SIGKILL)
        if not wait_apps_gone(5):
            raise RuntimeError("无法结束 WorkBuddy 进程，请手动退出后重试")
    return []

class SafeAbort(RuntimeError):
    code = 1


class RecoveryRequired(SafeAbort):
    code = 3


class BusyError(SafeAbort):
    code = 4


class UnsupportedEnvironment(SafeAbort):
    code = 5


def platform_tag():
    name = (sys.platform + ' ' + platform.system() + ' ' + platform.release()).lower()
    if any(word in name for word in ('harmony', 'openharmony', 'ohos')):
        return 'harmony'
    if os.name == 'nt':
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    if sys.platform.startswith('linux'):
        return 'linux'
    return 'unsupported'


def require_supported():
    tag = platform_tag()
    if tag not in ('windows', 'macos', 'linux'):
        raise UnsupportedEnvironment(
            '鸿蒙电脑版/当前平台暂不能迁移：尚无已验证的本地数据路径、Python 与目录链接接口。'
            '请勿用 Linux 的目录和命令替代。可以使用 detect 输出环境报告。')


def canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def same_path(a, b):
    return canonical(a) == canonical(b)


def is_within(parent, child):
    p, c = canonical(parent), canonical(child)
    try:
        return os.path.commonpath([p, c]) == p
    except ValueError:
        return False


def resolved_link(path):
    target = link_target(path)
    if target is None:
        return None
    return os.path.abspath(os.path.join(os.path.dirname(path), target))


def warn(message):
    _WARNINGS.append(message)
    print('提醒: ' + message, flush=True)


def atomic_json(path, data):
    """持久化后原子替换；写入失败不损坏已有记录。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.wb-json-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            try:
                d = os.open(os.path.dirname(path), os.O_RDONLY)
                try:
                    os.fsync(d)
                finally:
                    os.close(d)
            except OSError:
                pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def operation_lock():
    """内核锁崩溃后自动释放；锁文件不删除，避免并发进程锁住不同文件。"""
    os.makedirs(PLAN_DIR, exist_ok=True)
    path = os.path.join(PLAN_DIR, 'operation.lock')
    with open(path, 'a+b') as f:
        if os.fstat(f.fileno()).st_size == 0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        acquired = False
        try:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                raise BusyError('已有任务正在运行。请保留第一个窗口，等待它结束；不要重复双击。')
            yield
        finally:
            if acquired:
                f.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def locked(func):
    @functools.wraps(func)
    def wrapped(args):
        require_supported()
        with operation_lock():
            return func(args)
    return wrapped


class Progress:
    """准确计数；未知总量只显示阶段与耗时，长任务每两秒更新。"""
    def __init__(self, label, total_bytes=None, total_files=None, interval=2):
        self.label, self.total_bytes, self.total_files = label, total_bytes, total_files
        self.bytes, self.files = 0, 0
        self.interval = interval
        self.stop = threading.Event()
        self.mutex = threading.Lock()

    def update(self, count=0, size=0):
        with self.mutex:
            self.files += count
            self.bytes += size

    def report(self):
        with self.mutex:
            count, size = self.files, self.bytes
        elapsed = time.monotonic() - self.start
        text = '%s | 已用时 %.0fs | %d 项' % (self.label, elapsed, count)
        if self.total_files is not None:
            text += '/%d' % self.total_files
        if self.total_bytes is not None:
            text += ' | %s/%s' % (fmt_bytes(size), fmt_bytes(self.total_bytes))
            if self.total_bytes:
                text += ' (%.1f%%)' % min(100, size * 100 / self.total_bytes)
            if elapsed > 1 and size:
                text += ' | %s/s' % fmt_bytes(size / elapsed)
        print(text, flush=True)

    def worker(self):
        while not self.stop.wait(self.interval):
            self.report()

    def __enter__(self):
        self.start = time.monotonic()
        print(self.label + ' ...', flush=True)
        self.thread = threading.Thread(target=self.worker, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        self.report()


def signature(st):
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode]


def identity(path):
    st = os.stat(path)
    return [st.st_dev, st.st_ino]


def manifest(path, hashes=False, label='扫描目录'):
    """不跟随目录链接；目录、链接、内容均纳入校验，读取失败一律报错。"""
    out = {}
    if not os.path.isdir(path):
        raise SafeAbort('数据目录不可访问: ' + path)
    with Progress(label) as progress:
        def visit(folder):
            with os.scandir(folder) as iterator:
                entries = sorted(iterator, key=lambda e: e.name)
            for entry in entries:
                rel = os.path.relpath(entry.path, path)
                st = os.lstat(entry.path)
                if entry.is_symlink():
                    record = {'kind': 'link', 'target': os.readlink(entry.path),
                              'directory': bool(getattr(st, 'st_file_attributes', 0) & 0x10) if os.name == 'nt' else False}
                elif is_reparse_dir(entry.path):
                    record = {'kind': 'junction', 'target': os.path.realpath(entry.path)}
                elif statmod.S_ISDIR(st.st_mode):
                    record = {'kind': 'dir', 'mode': statmod.S_IMODE(st.st_mode)}
                    visit(entry.path)
                elif statmod.S_ISREG(st.st_mode):
                    record = {'kind': 'file', 'size': st.st_size, 'mode': statmod.S_IMODE(st.st_mode)}
                    if hashes:
                        digest = hashlib.sha256()
                        with open(entry.path, 'rb') as f:
                            while True:
                                chunk = f.read(4 * 1024 * 1024)
                                if not chunk:
                                    break
                                digest.update(chunk)
                                progress.update(size=len(chunk))
                        record['sha256'] = digest.hexdigest()
                        if signature(os.lstat(entry.path)) != signature(st):
                            raise SafeAbort('校验时文件发生变化: ' + entry.path)
                else:
                    raise SafeAbort('不支持的特殊文件（如 socket/FIFO）: ' + entry.path)
                record['signature'] = signature(st)
                out[rel] = record
                progress.update(count=1)
        visit(path)
    return out


def content_manifest(records):
    return {name: {k: v for k, v in record.items() if k != 'signature'}
            for name, record in records.items()}


def manifest_digest(records):
    data = json.dumps(content_manifest(records), ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def relative_file_set(path):
    return manifest(path)


def tree_stats(path):
    data = manifest(path)
    return sum(r.get('size', 0) for r in data.values()), sum(r['kind'] != 'dir' for r in data.values()), 0


def run_copy(src, dest):
    """通用流式复制，四个平台采用相同计数与校验语义，无外部复制工具依赖。"""
    records = manifest(src, label='[2/6] 扫描待复制数据')
    total = sum(r.get('size', 0) for r in records.values())
    os.makedirs(dest, exist_ok=True)
    # 调用方只传入本工具创建并记录的 staging；清理上次中断的多余副本条目。
    previous = manifest(dest, label='检查临时副本')
    for rel, record in sorted(previous.items(), key=lambda item: item[0].count(os.sep)):
        if record['kind'] == 'dir':
            path = os.path.join(dest, rel)
            os.chmod(path, record['mode'] | statmod.S_IRUSR | statmod.S_IWUSR | statmod.S_IXUSR)
    for rel in sorted(set(previous) - set(records), key=lambda p: p.count(os.sep), reverse=True):
        path = os.path.join(dest, rel)
        if previous[rel]['kind'] in ('link', 'junction'):
            remove_link(path)
        elif previous[rel]['kind'] == 'dir':
            os.rmdir(path)
        else:
            os.unlink(path)
    with Progress('[2/6] 正在复制', total, len(records)) as progress:
        for rel, record in sorted(records.items(), key=lambda item: (item[0].count(os.sep), item[0])):
            s, d = os.path.join(src, rel), os.path.join(dest, rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            kind = record['kind']
            if kind == 'dir':
                os.makedirs(d, exist_ok=True)
            elif kind in ('link', 'junction'):
                if os.path.lexists(d):
                    if link_target(d) is None:
                        raise SafeAbort('临时副本中已有非链接条目: ' + d)
                    remove_link(d)
                if kind == 'junction':
                    make_link(record['target'], d)
                else:
                    os.symlink(record['target'], d, target_is_directory=record['directory'])
            else:
                if os.path.islink(d) or is_reparse_dir(d):
                    raise SafeAbort('拒绝写入临时副本内的链接: ' + d)
                temporary = d + '.wb-file-' + uuid.uuid4().hex
                try:
                    with open(s, 'rb') as source, open(temporary, 'xb') as target:
                        while True:
                            chunk = source.read(4 * 1024 * 1024)
                            if not chunk:
                                break
                            target.write(chunk)
                            progress.update(size=len(chunk))
                        target.flush()
                        os.fsync(target.fileno())
                    if signature(os.lstat(s)) != record['signature']:
                        raise SafeAbort('复制期间源文件发生变化: ' + s)
                    shutil.copystat(s, temporary, follow_symlinks=False)
                    os.replace(temporary, d)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            progress.update(count=1)
        for rel, record in sorted(records.items(), key=lambda item: item[0].count(os.sep), reverse=True):
            if record['kind'] == 'dir':
                shutil.copystat(os.path.join(src, rel), os.path.join(dest, rel), follow_symlinks=False)
    return 'python-stream'


def verify_strict(src, dest):
    source = manifest(src, hashes=True, label='[3/6] 校验源文件内容')
    target = manifest(dest, hashes=True, label='[3/6] 校验副本内容')
    s, d = content_manifest(source), content_manifest(target)
    missing, extra = set(s) - set(d), set(d) - set(s)
    diff = [name for name in set(s) & set(d) if s[name] != d[name]]
    if missing or extra or diff:
        print('校验失败: 缺失 %d 项，多出 %d 项，内容/类型/权限不同 %d 项。' % (len(missing), len(extra), len(diff)), flush=True)
        for name in sorted(missing | extra | set(diff))[:8]:
            print('  ' + name, flush=True)
        return False
    if manifest(src, label='[3/6] 复查源目录是否变化') != {n: {k: v for k, v in r.items() if k != 'sha256'} for n, r in source.items()}:
        raise SafeAbort('校验期间源目录发生变化，已停止。')
    if _ACTIVE is not None:
        _ACTIVE['source_digest'] = manifest_digest(source)
    print('校验通过: 文件内容 SHA-256、目录、链接目标与权限一致。', flush=True)
    return True


def write_plan(plan):
    plan['plan_path'] = os.path.join(PLAN_DIR, 'plan-%s-%s.json' % (datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'), plan.get('id', uuid.uuid4().hex)))
    save_plan(plan)
    return plan['plan_path']


def save_plan(plan):
    atomic_json(plan['plan_path'], plan)


def load_plan(ref):
    if ref == 'latest':
        files = sorted(glob.glob(os.path.join(PLAN_DIR, 'plan-*.json')))
        if not files:
            raise SafeAbort('没有迁移记录，请先 detect 确认当前目录状态。')
        ref = files[-1]
    with open(os.path.abspath(os.path.expanduser(ref)), encoding='utf-8') as f:
        plan = json.load(f)
    plan['plan_path'] = os.path.abspath(os.path.expanduser(ref))
    for key in ('src', 'dest', 'backup'):
        if not isinstance(plan.get(key), str) or not os.path.isabs(plan[key]):
            raise SafeAbort('迁移记录路径无效: ' + key)
    return plan


def transition(plan, stage, **values):
    plan.update(values, stage=stage, updated_at=now_iso())
    save_plan(plan)
    print('[阶段] ' + STAGES.get(stage, stage), flush=True)


def unfinished(src):
    found = []
    for filename in sorted(glob.glob(os.path.join(PLAN_DIR, 'plan-*.json'))):
        plan = load_plan(filename)
        if plan.get('version', 1) >= 2 and plan.get('stage') not in ('complete', 'aborted') and os.path.normcase(os.path.abspath(plan['src'])) == os.path.normcase(os.path.abspath(src)):
            found.append(plan)
    return found


def resolve_src(args_src, force_any_dir=False):
    src = os.path.abspath(os.path.expanduser(args_src or DEFAULT_SRC))
    # 原路径可能已是链接：此处比较路径文字，不跟随链接。
    if os.path.normcase(src) != os.path.normcase(os.path.abspath(DEFAULT_SRC)) and not force_any_dir:
        raise SafeAbort('本工具仅操作默认目录 ' + DEFAULT_SRC)
    guard_common(src)
    return src


def guard_common(src):
    home = os.path.expanduser('~')
    if canonical(src) in {canonical(home), canonical(os.path.dirname(home)), canonical(os.path.abspath(os.sep))}:
        raise SafeAbort('拒绝操作系统根目录或用户主目录。')
    if is_within(src, PLAN_DIR) or is_within(PLAN_DIR, src):
        raise SafeAbort('数据目录与迁移记录目录存在嵌套关系。')


def validate_plan(plan, args):
    resolve_src(plan['src'], getattr(args, 'force_any_dir', False))
    backup = plan['backup']
    if backup != plan['src'] + BACKUP_SUFFIX and not re.fullmatch(re.escape(plan['src'] + BACKUP_SUFFIX) + '-[0-9a-f]{32}', backup):
        raise SafeAbort('备份路径与原目录不符，拒绝清理。')
    if is_within(plan['src'], plan['dest']) or is_within(plan['dest'], plan['src']):
        # 已迁移时 src 实际指向 dest，不能按 realpath 判断是否嵌套。
        if resolved_link(plan['src']) is None or not same_path(plan['src'], plan['dest']):
            raise SafeAbort('记录中数据路径存在嵌套关系。')


def detect_app_running():
    hits = []
    try:
        if os.name == 'nt':
            r = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True, errors='replace', timeout=20)
            if r.returncode != 0 or not r.stdout.strip():
                return 'unknown', 'tasklist 失败: ' + (r.stderr or r.stdout or str(r.returncode))[:200]
            for row in csv.reader(r.stdout.splitlines()):
                if len(row) < 2 or not row[1].isdigit():
                    return 'unknown', 'tasklist 返回无法识别的数据。'
                if NAME_PAT.search(row[0]):
                    hits.append((row[1], row[0]))
        else:
            r = subprocess.run(['ps', '-axo', 'pid=,comm='], capture_output=True, text=True, errors='replace', timeout=20)
            if r.returncode != 0 or not r.stdout.strip():
                return 'unknown', 'ps 失败: ' + (r.stderr or str(r.returncode))[:200]
            for line in r.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) != 2 or not parts[0].isdigit():
                    return 'unknown', 'ps 返回无法识别的数据。'
                pid, comm = parts
                basename = os.path.basename(comm).lower()
                if NAME_PAT.search(basename) and not basename.startswith(('python', 'sh', 'bash', 'zsh', 'node', 'rg')):
                    hits.append((pid, comm))
    except (OSError, subprocess.SubprocessError) as ex:
        return 'unknown', str(ex)
    return ('running' if hits else 'not-running'), hits


def assert_offline(manage=True):
    if manage:
        state, info = detect_app_running()
        if state != 'not-running':
            raise SafeAbort('无法确认 WorkBuddy 已完全退出，已停止。状态: %s；详情: %s' % (state, info))


def auto_manage_app(args, label='[1/6]'):
    if getattr(args, 'skip_app_handling', False):
        if os.path.normcase(getattr(args, 'src', '') or '') == os.path.normcase(DEFAULT_SRC):
            raise SafeAbort('真实 WorkBuddy 目录禁止跳过进程检测。')
        return []
    state, procs = detect_app_running()
    if state == 'unknown':
        raise SafeAbort('无法判断 WorkBuddy 是否运行。请在独立终端重试；不会把 unknown 当作已退出。')
    paths = []
    if state == 'running':
        print(label + ' 正在关闭 WorkBuddy ...', flush=True)
        if platform_tag() == 'linux':
            for pid, comm in procs:
                if 'workbuddy' in os.path.basename(comm).lower():
                    try:
                        executable = os.readlink('/proc/%s/exe' % pid)
                        if executable not in paths:
                            paths.append(executable)
                    except OSError:
                        pass
            with Progress('[1/6] 等待应用退出'):
                graceful_quit_all(procs)
        else:
            with Progress('[1/6] 等待应用退出'):
                paths = graceful_quit_all(procs)
        if not paths:
            warn('无法确定应用重开路径，完成后请手动打开 WorkBuddy。')
        assert_offline()
    else:
        print(label + ' WorkBuddy 已退出。', flush=True)
    return paths


def relaunch_apps(paths):
    if not paths:
        return
    for path in paths:
        try:
            if sys.platform == 'darwin':
                r = subprocess.run(['open', '-a', path], capture_output=True, text=True, timeout=20)
                if r.returncode:
                    raise RuntimeError(r.stderr)
            else:
                p = subprocess.Popen([path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    rc = p.wait(timeout=0.5)
                    if rc:
                        raise RuntimeError('启动进程退出码 %s' % rc)
                except subprocess.TimeoutExpired:
                    pass
        except (OSError, RuntimeError, subprocess.SubprocessError) as ex:
            warn('数据操作已完成，但重新打开应用失败，请手动打开: %s (%s)' % (path, ex))


def preflight(data, dest, src):
    require_supported()
    if not os.path.isdir(data):
        raise SafeAbort('当前实际数据目录不可访问: ' + data)
    if is_within(data, dest) or is_within(dest, data):
        raise SafeAbort('源与目标相同或存在嵌套关系。')
    if is_within(dest, PLAN_DIR) or is_within(PLAN_DIR, dest):
        raise SafeAbort('目标与迁移记录目录存在嵌套关系。')
    for path in (sys.executable, sys.prefix, __file__):
        if is_within(data, path):
            raise SafeAbort('执行脚本或 Python 环境位于待迁移目录内。请使用独立系统 Python 和 make-runner 生成的脚本。')
    if link_target(dest) is not None:
        raise SafeAbort('目标本身是链接，拒绝写入: ' + dest)
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    fd, test = tempfile.mkstemp(prefix='.wb-write-', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(b'WorkBuddy probe')
            f.flush()
            os.fsync(f.fileno())
    finally:
        os.unlink(test)
    # 在与原路径相同的卷上验证链接能力，目标放在目标卷。
    probe_data = tempfile.mkdtemp(prefix='.wb-link-target-', dir=parent)
    probe_link = os.path.join(os.path.dirname(src), '.wb-link-probe-' + uuid.uuid4().hex)
    try:
        make_link(probe_data, probe_link)
        if not os.path.isdir(probe_link) or not same_path(probe_link, probe_data):
            raise SafeAbort('目录链接能力检查失败。')
    finally:
        if link_target(probe_link) is not None:
            remove_link(probe_link)
        os.rmdir(probe_data)
    return os.stat(parent).st_dev


def check_volume(dest, volume):
    if os.stat(os.path.dirname(dest)).st_dev != volume:
        raise SafeAbort('目标磁盘发生变化，已停止；请检查磁盘连接后使用 recover 确认状态。')


def new_operation(action, src, dest, data, backup=None):
    global _ACTIVE
    op_id = uuid.uuid4().hex
    plan = {'version': 2, 'id': op_id, 'action': action, 'method': action,
            'created_at': now_iso(), 'platform': platform_tag(), 'src': src, 'dest': dest,
            'data': data, 'backup': backup or src + BACKUP_SUFFIX + '-' + op_id,
            'staging': dest + '.wb-partial-' + op_id if action == 'migrate' else src + '.wb-restore-' + op_id,
            'stage': 'prepared', 'swapped': False, 'source_identity': identity(data),
            'source_usable': True}
    _ACTIVE = plan
    write_plan(plan)
    return plan


def owned_destination(src, dest):
    for filename in reversed(sorted(glob.glob(os.path.join(PLAN_DIR, 'plan-*.json')))):
        plan = load_plan(filename)
        if plan.get('action') == 'migrate' and plan.get('stage') == 'aborted' and plan.get('published') and os.path.normcase(plan['src']) == os.path.normcase(src) and same_path(plan['dest'], dest):
            return plan
    return None


def owned_staging(src, data, dest):
    for filename in reversed(sorted(glob.glob(os.path.join(PLAN_DIR, 'plan-*.json')))):
        plan = load_plan(filename)
        if (plan.get('action') == 'migrate' and plan.get('stage') == 'aborted'
                and os.path.normcase(plan['src']) == os.path.normcase(src)
                and same_path(plan['dest'], dest) and same_path(plan.get('data', src), data)
                and plan.get('source_identity') == identity(data)):
            expected = plan['dest'] + '.wb-partial-' + plan['id']
            if plan.get('staging') == expected and os.path.isdir(expected) and link_target(expected) is None:
                return expected
    return None


def ensure_no_pending(src):
    pending = unfinished(src)
    if pending:
        raise RecoveryRequired('存在未完成操作，先运行 recover --plan "%s"。源与目标都会保留。' % pending[-1]['plan_path'])


def destination_name(name, default='.workbuddy'):
    name = name or default
    if name in ('.', '..') or os.path.basename(name) != name or '/' in name or '\\' in name:
        raise SafeAbort('目标名称必须是单个目录名。')
    return name


@locked
def cmd_migrate(args):
    src = resolve_src(args.src, args.force_any_dir)
    ensure_no_pending(src)
    data = resolved_link(src) or src
    dest_base, name = args.dest_base, args.name
    if args.args_file:
        with open(args.args_file, encoding='utf-8') as f:
            payload = json.load(f)
        dest_base, name = payload.get('dest_base'), payload.get('name', name)
    if not dest_base:
        raise SafeAbort('需要 --dest-base 指定目标目录。')
    dest = os.path.join(os.path.abspath(os.path.expanduser(dest_base)), destination_name(name, os.path.basename(src)))
    if resolved_link(src) and same_path(data, dest):
        print('已经迁移到该位置，无需重复执行。', flush=True)
        return
    if getattr(args, 'skip_app_handling', False) and os.path.normcase(src) == os.path.normcase(DEFAULT_SRC):
        raise SafeAbort('真实 WorkBuddy 目录禁止跳过进程检测。')
    volume = preflight(data, dest, src)
    reuse = None
    if os.path.lexists(dest):
        if not os.path.isdir(dest) or os.listdir(dest):
            reuse = owned_destination(src, dest)
            if reuse is None:
                raise SafeAbort('目标已有内容且不是本工具记录的副本，拒绝合并/覆盖: ' + dest)
    relaunch = auto_manage_app(args)
    manage = not getattr(args, 'skip_app_handling', False)
    plan = new_operation('migrate', src, dest, data)
    previous_stage = owned_staging(src, data, dest)
    if previous_stage:
        plan['staging'] = previous_stage
        print('复用本工具记录的临时副本；会按当前源数据重新复制与校验。', flush=True)
    old_target = resolved_link(src)
    plan['old_target'] = old_target
    plan['volume'] = volume
    try:
        transition(plan, 'copying')
        size, count, _ = tree_stats(data)
        print('数据: %s / %d 项；目标: %s' % (fmt_bytes(size), count, dest), flush=True)
        if not reuse and shutil.disk_usage(os.path.dirname(dest)).free < size * 1.02:
            raise SafeAbort('目标剩余空间不足（需要数据体积加 2% 余量）。')
        candidate = dest if reuse else plan['staging']
        if not reuse:
            run_copy(data, candidate)
        transition(plan, 'verifying')
        if not verify_strict(data, candidate):
            raise SafeAbort('内容校验失败。原始数据保持完整，副本保留供检查。')
        assert_offline(manage)
        check_volume(dest, volume)
        transition(plan, 'verified', verified=True)
        if not reuse:
            transition(plan, 'publishing')
            if os.path.isdir(dest) and not os.listdir(dest):
                os.rmdir(dest)
            if os.path.lexists(dest):
                raise SafeAbort('目标在执行期间被创建，拒绝覆盖。')
            os.rename(candidate, dest)
        transition(plan, 'published', published=True)
        assert_offline(manage)
        transition(plan, 'switching')
        try:
            if old_target:
                remove_link(src)
            else:
                os.rename(src, plan['backup'])
            make_link(dest, src)
            if not same_path(src, dest):
                raise RecoveryRequired('创建后的链接指向异常。')
        except BaseException:
            if link_target(src) is not None:
                remove_link(src)
            if not os.path.lexists(src):
                if old_target:
                    make_link(old_target, src)
                elif os.path.isdir(plan['backup']):
                    os.rename(plan['backup'], src)
            raise
        transition(plan, 'committed', swapped=True, source_usable=True)
        print('[4/6] 原路径已切换到新数据位置。', flush=True)
        transition(plan, 'cleanup')
        obsolete = data if old_target else plan['backup']
        if not args.keep_backup:
            try:
                assert_offline(manage)
                with Progress('[5/6] 清理旧副本'):
                    print(trash_path(obsolete), flush=True)
                plan['backup_removed'] = True
                plan['obsolete_removed'] = True
            except Exception as ex:
                warn('迁移已完成，旧副本清理失败，保留在 %s: %s' % (obsolete, ex))
        else:
            print('[5/6] 已保留旧副本: ' + obsolete, flush=True)
        if relaunch and not args.no_relaunch:
            print('[6/6] 正在重新打开 WorkBuddy ...', flush=True)
            relaunch_apps(relaunch)
        transition(plan, 'complete', warnings=list(_WARNINGS))
    except BaseException as ex:
        usable = os.path.isdir(src)
        # 实际切换成功但记录失败时不要将已迁移的状态误报为已回退。
        committed = resolved_link(src) is not None and same_path(src, dest) and os.path.isdir(dest)
        try:
            transition(plan, 'committed' if committed else ('aborted' if usable else 'recovery_required'),
                       source_usable=usable, error=str(ex), swapped=committed,
                       failed_at_stage=plan['stage'])
        except OSError:
            pass
        if committed:
            raise RecoveryRequired('数据切换已完成，但后处理/记录未完成。请先 recover 确认状态。记录: ' + plan['plan_path']) from ex
        if not usable:
            raise RecoveryRequired('原路径暂不可用。数据位置: %s；记录: %s；请运行 recover。' % (data, plan['plan_path'])) from ex
        raise


def move_dest_back(src, dest, plan=None, manage=True):
    """先完整复制与校验，再切换。原链接在复制失败/中断时保持可用。"""
    if plan is None:
        plan = new_operation('restore', src, dest, dest)
    stage = plan['staging']
    size, _, _ = tree_stats(dest)
    if shutil.disk_usage(os.path.dirname(src)).free < size * 1.02:
        raise SafeAbort('原磁盘空间不足；原链接未改变。')
    transition(plan, 'copying')
    run_copy(dest, stage)
    transition(plan, 'verifying')
    if not verify_strict(dest, stage):
        raise SafeAbort('恢复副本校验失败；原链接未改变。')
    assert_offline(manage)
    transition(plan, 'verified', verified=True)
    if resolved_link(src) is None or not same_path(src, dest):
        raise RecoveryRequired('原路径状态在恢复期间发生变化，已停止。')
    transition(plan, 'switching')
    try:
        remove_link(src)
        os.rename(stage, src)
    except BaseException:
        if not os.path.lexists(src):
            make_link(dest, src)
        raise
    transition(plan, 'committed', source_usable=True, swapped=False)
    return 'copy', '最新数据已校验并迁回；原实际位置仍保留到清理阶段。'


def restore_data(args, full):
    old = load_plan(args.plan)
    validate_plan(old, args)
    src, dest = old['src'], old['dest']
    ensure_no_pending(src)
    target = resolved_link(src)
    if target is None:
        if not os.path.isdir(src):
            raise RecoveryRequired('原路径既不是链接也不是目录，请先 recover。')
        print('原路径已是真实目录。', flush=True)
        return
    if not same_path(target, dest):
        raise SafeAbort('当前链接与迁移记录不符；先 detect 确认当前记录。')
    # 恢复目的路径 src 本身就是链接，预检使用独立同卷 staging 路径。
    probe_dest = src + '.wb-preflight-' + uuid.uuid4().hex
    volume = preflight(dest, probe_dest, src)
    relaunch = auto_manage_app(args)
    plan = new_operation('restore' if full else 'rollback', src, dest, dest, old['backup'])
    plan['origin_plan'] = old['plan_path']
    try:
        check_volume(probe_dest, volume)
        move_dest_back(src, dest, plan)
        old.update(swapped=False, restored_at=now_iso())
        save_plan(old)
        transition(plan, 'cleanup')
        try:
            with Progress('[5/6] 清理旧位置副本'):
                print(trash_path(dest), flush=True)
        except Exception as ex:
            warn('数据已恢复，旧位置副本清理失败，保留在 %s: %s' % (dest, ex))
        if full and not getattr(args, 'keep_backup', False) and os.path.isdir(old['backup']):
            try:
                if not old.get('source_digest') or manifest_digest(manifest(old['backup'], hashes=True, label='确认旧快照身份')) != old['source_digest']:
                    raise SafeAbort('旧快照身份无法确认或内容已变化，保留供人工检查。')
                with Progress('[5/6] 清理旧快照'):
                    print(trash_path(old['backup'], hard=getattr(args, 'hard_delete', False)), flush=True)
            except Exception as ex:
                warn('旧快照清理失败，已保留: ' + str(ex))
        if relaunch and not args.no_relaunch:
            relaunch_apps(relaunch)
        if full:
            # 内核锁、阶段记录与日志保留，避免恢复中删除正在执行的 Windows .bat。
            plan['runner_cleanup_pending'] = True
            print('恢复完成。运行脚本与阶段记录保留作诊断；确认后可手动删除 .wb-cache-move。', flush=True)
        transition(plan, 'complete', warnings=list(_WARNINGS))
    except BaseException as ex:
        usable = os.path.isdir(src)
        committed = resolved_link(src) is None and os.path.isdir(src)
        try:
            transition(plan, 'committed' if committed else ('aborted' if usable else 'recovery_required'), source_usable=usable, error=str(ex), failed_at_stage=plan['stage'])
        except OSError:
            pass
        if committed:
            raise RecoveryRequired('数据已迁回，但后处理/记录未完成。请先 recover 确认。记录: ' + plan['plan_path']) from ex
        if not usable:
            raise RecoveryRequired('原路径暂不可用，请运行 recover。记录: ' + plan['plan_path']) from ex
        raise


@locked
def cmd_restore(args):
    restore_data(args, True)


@locked
def cmd_rollback(args):
    restore_data(args, False)


@locked
def cmd_recover(args):
    global _ACTIVE
    plan = load_plan(args.plan)
    validate_plan(plan, args)
    _ACTIVE = plan
    src, dest, data = plan['src'], plan['dest'], plan.get('data', plan['dest'])
    if plan.get('version', 1) < 2:
        raise SafeAbort('旧记录没有阶段信息，请先 detect；不能自动推断中断状态。')
    auto_manage_app(args)
    target = resolved_link(src)
    stage = plan.get('stage')
    if plan['action'] == 'migrate':
        if target and same_path(target, dest) and os.path.isdir(dest):
            transition(plan, 'complete', swapped=True, source_usable=True,
                       warnings=['中断后已确认链接与新数据位置；旧副本保留，需检查后清理。'])
            warn('已确认迁移切换完成；旧副本保留。请检查应用后清理。')
            return
        if not os.path.lexists(src):
            if plan.get('old_target') and os.path.isdir(data):
                make_link(data, src)
            elif os.path.isdir(plan['backup']) and identity(plan['backup']) == plan['source_identity']:
                os.rename(plan['backup'], src)
            else:
                raise RecoveryRequired('无法确认原始数据位置，拒绝自动覆盖。请检查磁盘连接与记录。')
            target = resolved_link(src)
        if ((target and same_path(target, data)) or (resolved_link(src) is None and identity(src) == plan['source_identity'])) and os.path.isdir(src):
            transition(plan, 'aborted', swapped=False, source_usable=True,
                       published=bool(plan.get('verified') and os.path.isdir(dest)))
            print('原路径已恢复可用，临时副本保留。可重新执行迁移。', flush=True)
            return
    else:
        if not os.path.lexists(src) and os.path.isdir(data):
            make_link(data, src)
            target = resolved_link(src)
        if target and same_path(target, data) and os.path.isdir(src):
            transition(plan, 'aborted', source_usable=True)
            print('原链接已恢复；最新数据仍在原实际位置，可重新执行恢复。', flush=True)
            return
        if resolved_link(src) is None and os.path.isdir(src) and stage in ('switching', 'committed', 'cleanup'):
            if plan.get('source_digest') and manifest_digest(manifest(src, hashes=True, label='核对已恢复数据')) == plan['source_digest']:
                transition(plan, 'complete', swapped=False, source_usable=True)
                warn('已确认恢复完成；旧位置副本保留。')
                return
    raise RecoveryRequired('当前目录状态与记录无法确定对应关系。已保留全部数据，请人工检查；不要删除副本。')


@locked
def cmd_cleanup_backup(args):
    plan = load_plan(args.plan)
    validate_plan(plan, args)
    ensure_no_pending(plan['src'])
    if not args.yes:
        raise SafeAbort('需要明确确认后加 --yes。')
    src, dest = plan['src'], plan['dest']
    target = resolved_link(src)
    if not os.path.isdir(src) or (target and not same_path(target, dest)):
        raise SafeAbort('当前原路径状态与记录不符，拒绝清理。')
    obsolete = plan.get('old_target') or plan['backup']
    if os.path.isdir(obsolete):
        if same_path(obsolete, src) or same_path(obsolete, dest):
            raise SafeAbort('待清理位置仍是当前数据，拒绝删除。')
        if not plan.get('source_digest') or manifest_digest(manifest(obsolete, hashes=True, label='确认旧副本身份')) != plan['source_digest']:
            raise SafeAbort('旧副本身份无法确认或内容已变化，拒绝自动清理。请人工检查。')
        print(trash_path(obsolete, hard=args.hard_delete), flush=True)
    else:
        print('旧副本已不存在。', flush=True)
    plan.update(backup_removed=True, obsolete_removed=True)
    save_plan(plan)


def cmd_detect(args):
    src = os.path.abspath(os.path.expanduser(args.src or DEFAULT_SRC))
    print('系统: %s；架构: %s；Python: %s' % (platform_tag(), platform.machine(), sys.version.split()[0]), flush=True)
    if platform_tag() in ('harmony', 'unsupported'):
        print('暂不支持自动迁移：本地数据目录和系统链接/进程接口尚未验证。请勿假定 ~/.workbuddy 是该平台的数据路径。', flush=True)
        return
    guard_common(src)
    target = resolved_link(src)
    print('原路径: %s\n实际位置: %s' % (src, target or src), flush=True)
    if os.path.isdir(src):
        total, files, _ = tree_stats(src)
        print('状态: %s；数据: %s / %d 项' % ('已迁移（链接）' if target else '普通目录', fmt_bytes(total), files), flush=True)
    else:
        print('原路径不可访问，请检查磁盘连接和未完成记录。', flush=True)
    print('进程检测: %s' % (detect_app_running(),), flush=True)
    for plan in unfinished(src):
        print('未完成操作: %s；阶段: %s\n  记录: %s\n  下一步: recover --plan "%s"' % (plan['action'], plan['stage'], plan['plan_path'], plan['plan_path']), flush=True)


def default_log_dir():
    tag = platform_tag()
    if tag == 'windows':
        return os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'WorkBuddyCacheMover', 'logs')
    if tag == 'macos':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Logs', 'WorkBuddyCacheMover')
    return os.path.join(os.environ.get('XDG_STATE_HOME', os.path.join(os.path.expanduser('~'), '.local', 'state')), 'WorkBuddyCacheMover', 'logs')


class _LogStream:
    def __init__(self, console, log, mutex):
        self.console, self.log, self.mutex = console, log, mutex

    def write(self, text):
        with self.mutex:
            self.log.write(text)
            self.log.flush()
            result = self.console.write(text)
            self.console.flush()
            return result

    def flush(self):
        with self.mutex:
            self.log.flush()
            self.console.flush()

    def __getattr__(self, name):
        return getattr(self.console, name)


def report_result(rc, log_path, failure=None):
    if rc == 0 and _WARNINGS:
        rc = 2
    result = {0: 'SUCCESS', 2: 'COMPLETED_WITH_WARNINGS', 3: 'RECOVERY_REQUIRED',
              4: 'BUSY', 5: 'UNSUPPORTED_ENVIRONMENT', 130: 'INTERRUPTED'}.get(rc, 'FAILED_SAFE' if _ACTIVE and _ACTIVE.get('source_usable') else 'FAILED')
    print('\nRESULT: %s (exit code %d)' % (result, rc), flush=True)
    if failure:
        print('原因: ' + str(failure), flush=True)
    if _ACTIVE:
        src = _ACTIVE['src']
        actual = resolved_link(src) or (src if os.path.isdir(src) else _ACTIVE.get('data', _ACTIVE['dest']))
        target = src if _ACTIVE.get('action') in ('restore', 'rollback') else _ACTIVE['dest']
        stage = _ACTIVE.get('failed_at_stage', _ACTIVE['stage']) if rc not in (0, 2) else _ACTIVE['stage']
        print('停止/完成阶段: %s\n原路径: %s\n当前实际数据: %s\n目标: %s\n记录: %s' % (STAGES.get(stage, stage), src, actual, target, _ACTIVE['plan_path']), flush=True)
        print('原路径当前可访问: %s' % os.path.isdir(_ACTIVE['src']), flush=True)
        if rc not in (0, 2):
            print('下一步: 先 detect；未完成记录使用 recover。不要删除源目录或副本。', flush=True)
    print('结束: %s\n日志: %s' % (now_iso(), log_path), flush=True)
    return rc


def execute_logged(action, log_dir=None):
    global _ACTIVE, _WARNINGS
    _ACTIVE, _WARNINGS = None, []
    log_dir = os.path.abspath(os.path.expanduser(log_dir or default_log_dir()))
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, 'run-%s-%s.log' % (datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'), os.getpid()))
    out, err = sys.stdout, sys.stderr
    with open(path, 'w', encoding='utf-8') as log:
        mutex = threading.Lock()
        sys.stdout, sys.stderr = _LogStream(out, log, mutex), _LogStream(err, log, mutex)
        rc, failure = 0, None
        try:
            print('开始: %s\n平台: %s\nPython: %s\n日志: %s' % (now_iso(), platform_tag(), sys.executable, path), flush=True)
            action()
        except SafeAbort as ex:
            rc, failure = ex.code, ex
        except SystemExit as ex:
            rc = ex.code if isinstance(ex.code, int) else (0 if ex.code is None else 1)
            failure = ex.code if not isinstance(ex.code, int) else None
        except KeyboardInterrupt as ex:
            rc, failure = 130, 'Interrupted. 已保留数据，请先检查阶段记录。'
        except Exception as ex:
            rc, failure = 1, ex
            traceback.print_exc()
        finally:
            try:
                rc = report_result(rc, path, failure)
            finally:
                sys.stdout, sys.stderr = out, err
    return rc


def cmd_run_job(args):
    def action():
        with open(args.args_file, encoding='utf-8') as f:
            job = json.load(f)
        command = job.get('command', 'migrate')
        common = dict(no_relaunch=False, force_any_dir=False)
        if command == 'migrate':
            if not job.get('dest_base'):
                raise ValueError('Job is missing dest_base')
            print('迁移目标: ' + os.path.join(job['dest_base'], destination_name(job.get('name'))), flush=True)
            cmd_migrate(argparse.Namespace(dest_base=job['dest_base'], name=job.get('name'), args_file=None,
                       src=None, skip_app_handling=False, keep_backup=job.get('keep_backup', False), **common))
        elif command in ('restore', 'rollback', 'recover'):
            if not job.get('plan'):
                raise ValueError('Job is missing plan')
            method = {'restore': cmd_restore, 'rollback': cmd_rollback, 'recover': cmd_recover}[command]
            method(argparse.Namespace(plan=job['plan'], keep_backup=job.get('keep_backup', False), hard_delete=False, **common))
        else:
            raise ValueError('Unsupported runner command: ' + command)
    raise SystemExit(execute_logged(action, getattr(args, 'log_dir', None)))


def windows_runner_content(job_name):
    return '\r\n'.join([
        '@echo off', 'setlocal DisableDelayedExpansion',
        'if /I "%~1"=="--console" goto run',
        '"%ComSpec%" /D /V:OFF /K ""%~f0" --console"', 'exit /b', ':run',
        'chcp 65001 >nul', 'title WorkBuddy cache mover',
        'echo Preparing. Do not reopen WorkBuddy during the operation.',
        'set "PYEXE="', 'set "PYFLAG="',
        'for /f "delims=" %%P in (\'where py.exe 2^>nul\') do if not defined PYEXE call :try_python "%%P" -3',
        'for /f "delims=" %%P in (\'where python.exe 2^>nul\') do if not defined PYEXE call :try_python "%%P"',
        'if defined PYEXE goto execute',
        'echo No working external Python 3.8+ found. Install system Python from python.org.',
        'set "RC=5"', 'goto finish', ':execute',
        '"%PYEXE%" %PYFLAG% -X utf8 -u "%~dp0cache_mover.py" run-job --args-file "%~dp0' + job_name + '"',
        'set "RC=%ERRORLEVEL%"', ':finish', 'echo.',
        'echo Exit code: %RC% - 0=success, 2=completed with warnings, 3=recovery required, 4=busy, 5=environment unsupported.',
        'echo Logs: "%LOCALAPPDATA%\\WorkBuddyCacheMover\\logs"',
        'echo This window stays open. Read the result, then close it or type EXIT.', 'exit /b %RC%',
        ':try_python', 'if /I "%~dp1"=="%LOCALAPPDATA%\\Microsoft\\WindowsApps\\" exit /b 0',
        '"%~1" %~2 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1',
        'if errorlevel 1 exit /b 0', 'set "PYEXE=%~1"', 'set "PYFLAG=%~2"', 'exit /b 0', ''
    ])


def posix_runner_content(job_name):
    return '\n'.join([
        '#!/bin/sh', 'SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$0")" && pwd) || exit 1',
        'PY=""', 'rc=5',
        'for cand in "$(command -v python3)" "$(command -v python)" /opt/homebrew/bin/python3 /usr/local/bin/python3; do',
        '  if [ -x "$cand" ] && "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >/dev/null 2>&1; then PY="$cand"; break; fi',
        'done', 'if [ -n "$PY" ]; then',
        '  "$PY" -X utf8 -u "$SCRIPT_DIR/cache_mover.py" run-job --args-file "$SCRIPT_DIR/' + job_name + '"',
        '  rc=$?', 'else', '  echo "No working external Python 3.8+ found. Install system Python."', 'fi',
        'echo "Exit code: $rc (0=success, 2=completed with warnings, 3=recovery required, 4=busy, 5=environment unsupported)"',
        'if [ -t 0 ]; then printf "Read the result, then press Enter to close..."; read -r answer; fi',
        'exit "$rc"', ''
    ])


@locked
def cmd_make_runner(args):
    command = args.command
    if command == 'migrate':
        if not args.dest_base:
            raise SafeAbort('需要 --dest-base。')
        payload = {'command': command, 'dest_base': os.path.abspath(os.path.expanduser(args.dest_base))}
        if args.name:
            payload['name'] = destination_name(args.name)
        job_name = 'migrate-args.json'
    else:
        plan = load_plan(args.plan)
        payload = {'command': command, 'plan': plan['plan_path']}
        job_name = 'runner-%s-args.json' % command
    payload['keep_backup'] = getattr(args, 'keep_backup', False)
    suffix = '.bat' if os.name == 'nt' else ('.command' if sys.platform == 'darwin' else '.sh')
    out = os.path.abspath(os.path.expanduser(args.out or os.path.join(PLAN_DIR, 'run_' + command + suffix)))
    folder = os.path.dirname(out)
    os.makedirs(folder, exist_ok=True)
    local = os.path.join(folder, 'cache_mover.py')
    if not same_path(__file__, local):
        shutil.copy2(__file__, local)
    atomic_json(os.path.join(folder, job_name), payload)
    content = windows_runner_content(job_name) if os.name == 'nt' else posix_runner_content(job_name)
    with open(out, 'w', encoding='ascii' if os.name == 'nt' else 'utf-8', newline='') as f:
        f.write(content)
    if os.name != 'nt':
        os.chmod(out, 0o755)
    print('已生成: ' + out, flush=True)
    print('Windows/macOS 可双击；Linux 在终端执行 sh "脚本路径"。日志、阶段与退出码统一。', flush=True)
    print('更新 Skill 后必须重新生成启动脚本。独立系统 Python 必須位于数据目录之外。', flush=True)


def main():
    _utf8_guard()
    p = argparse.ArgumentParser(description='WorkBuddy 数据迁移：内容校验、阶段记录、独占锁与恢复。')
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('detect', 'status'):
        parser = sub.add_parser(name)
        parser.add_argument('--src')
        parser.set_defaults(func=cmd_detect)
    mg = sub.add_parser('migrate')
    mg.add_argument('--dest-base')
    mg.add_argument('--name')
    mg.add_argument('--args-file')
    mg.add_argument('--src')
    mg.add_argument('--force-any-dir', action='store_true', help='仅用于临时目录测试')
    mg.add_argument('--skip-app-handling', action='store_true', help='仅允许非默认测试目录')
    mg.add_argument('--no-relaunch', action='store_true')
    mg.add_argument('--keep-backup', action='store_true')
    mg.set_defaults(func=cmd_migrate)
    runner = sub.add_parser('make-runner')
    runner.add_argument('--command', choices=['migrate', 'restore', 'rollback', 'recover'], default='migrate')
    runner.add_argument('--dest-base')
    runner.add_argument('--name')
    runner.add_argument('--plan', default='latest')
    runner.add_argument('--out')
    runner.add_argument('--keep-backup', action='store_true')
    runner.set_defaults(func=cmd_make_runner)
    job = sub.add_parser('run-job')
    job.add_argument('--args-file', required=True)
    job.add_argument('--log-dir')
    job.set_defaults(func=cmd_run_job)
    for name, func in [('restore', cmd_restore), ('rollback', cmd_rollback), ('recover', cmd_recover), ('cleanup-backup', cmd_cleanup_backup)]:
        parser = sub.add_parser(name)
        parser.add_argument('--plan', default='latest')
        parser.add_argument('--force-any-dir', action='store_true', help='仅用于临时目录测试')
        parser.add_argument('--no-relaunch', action='store_true')
        parser.add_argument('--keep-backup', action='store_true')
        parser.add_argument('--hard-delete', action='store_true')
        parser.add_argument('--yes', action='store_true')
        parser.set_defaults(func=func)
    args = p.parse_args()
    if args.cmd == 'run-job':
        args.func(args)
    else:
        raise SystemExit(execute_logged(lambda: args.func(args)))


if __name__ == '__main__':
    main()
