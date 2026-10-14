#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 数据迁移：内容校验、持久阶段记录、独占锁与失败恢复。Python >= 3.8。"""
import argparse
import codecs
import contextlib
import collections
import concurrent.futures
import datetime
import functools
import glob
import hashlib
import json
import ntpath
import os
import platform
import re
import shutil
import signal
import shlex
import stat as statmod
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import uuid

DEFAULT_SRC = os.path.join(os.path.expanduser("~"), ".workbuddy")
PLAN_DIR = os.path.join(os.path.expanduser("~"), ".wb-cache-move")
BACKUP_SUFFIX = ".wb-migration-backup"
WIN_SYMLINK_HINT = "目标需支持目录链接；Windows 本地卷使用 junction，UNC 需 symlink 权限。"
_ACTIVE = None
_WARNINGS = []
DEFAULT_IO_WORKERS = 4 if sys.platform == 'win32' else 1
_IO_WORKERS = DEFAULT_IO_WORKERS
_COPY_RECEIPT = None
NATIVE_WINDOWS = sys.platform == 'win32'
FORCE_SIGNAL = getattr(signal, 'SIGKILL', 9)
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
    target = display_path(os.path.abspath(target))
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

def recycle_precheck(flags):
    # TRANSFER_SOURCE_FLAGS: shell 明确标记可回收时才允许继续，拒绝永久删除降级。
    return 0 if flags & 0x80 else -2147467259


def windows_recycle(path):
    """直接使用 Windows Shell IFileOperation；不依赖 PowerShell/.NET UI 环境。"""
    import ctypes as c
    if sys.getwindowsversion()[:2] < (6, 2):
        raise OSError('系统缺少仅回收模式，旧副本已保留。')
    path = display_path(os.path.abspath(path))
    if path.startswith('\\\\'):
        raise OSError('Windows 不支持将网络路径直接移入本地回收站，旧副本已保留。')
    HRESULT, PTR, UINT = c.c_int32, c.c_void_p, c.c_uint32
    class GUID(c.Structure):
        _fields_ = [('a', c.c_uint32), ('b', c.c_uint16), ('d', c.c_uint16), ('e', c.c_ubyte * 8)]
    def guid(value):
        return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)
    iid_operation = guid('947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8')
    iid_item = guid('43826d1e-e718-42ee-bc55-a1e261c37bfe')
    clsid = guid('3ad05575-8857-4850-9277-11b85bdb8e09')
    iid_sink = guid('04b0f1a7-9490-44bc-96e1-4296a31252e2')
    iid_unknown = guid('00000000-0000-0000-c000-000000000046')
    ole = c.OleDLL('ole32')
    shell = c.WinDLL('shell32')
    ole.CoInitializeEx.argtypes = [PTR, UINT]; ole.CoInitializeEx.restype = HRESULT
    ole.CoCreateInstance.argtypes = [c.POINTER(GUID), PTR, UINT, c.POINTER(GUID), c.POINTER(PTR)]
    ole.CoCreateInstance.restype = HRESULT
    ole.CoTaskMemFree.argtypes = [PTR]
    shell.SHCreateItemFromParsingName.argtypes = [c.c_wchar_p, PTR, c.POINTER(GUID), c.POINTER(PTR)]
    shell.SHCreateItemFromParsingName.restype = HRESULT
    def checked(hr, stage):
        if hr < 0:
            raise OSError('%s失败（HRESULT 0x%08X）；旧副本或剩余内容保留，未尝试永久删除。' % (stage, hr & 0xffffffff))
    def call(ptr, index, types, *args):
        table = c.cast(ptr, c.POINTER(c.POINTER(PTR))).contents
        return c.WINFUNCTYPE(HRESULT, PTR, *types)(table[index])(ptr, *args)
    checked(ole.CoInitializeEx(None, 2), '初始化回收站接口')
    operation, item = PTR(), PTR()
    state = {'references': 1, 'recycled': 0, 'path': None, 'error': None, 'blocked': False}
    callbacks = []
    class Sink(c.Structure):
        _fields_ = [('table', c.POINTER(PTR))]
    sink = Sink()
    def query(this, requested, output):
        c.cast(output, c.POINTER(PTR))[0] = None
        key = c.string_at(requested, 16)
        if key not in (bytes(iid_sink), bytes(iid_unknown)):
            return -2147467262
        c.cast(output, c.POINTER(PTR))[0] = c.addressof(sink)
        state['references'] += 1
        return 0
    def addref(this):
        state['references'] += 1
        return state['references']
    def release(this):
        state['references'] -= 1
        return state['references']
    def before_delete(this, flags, original):
        result = recycle_precheck(flags)
        if result < 0:
            state['blocked'] = True
        return result
    def after_delete(this, flags, original, hr, new_item):
        if hr < 0:
            state['error'] = hr
        elif new_item:
            state['recycled'] += 1
            name = PTR()
            if call(new_item, 5, [UINT, c.POINTER(PTR)], 0x80058000, c.byref(name)) >= 0 and name:
                state['path'] = c.wstring_at(name)
                ole.CoTaskMemFree(name)
        else:
            state['error'] = -2147467259
        return 0
    # 按 SDK 声明顺序构造 IUnknown + IFileOperationProgressSink 的完整 vtable。
    definitions = [([PTR, PTR], query), ([], addref), ([], release),
                   ([], None), ([HRESULT], None), ([UINT, PTR, PTR], None),
                   ([UINT, PTR, PTR, HRESULT, PTR], None), ([UINT, PTR, PTR, PTR], None),
                   ([UINT, PTR, PTR, PTR, HRESULT, PTR], None), ([UINT, PTR, PTR, PTR], None),
                   ([UINT, PTR, PTR, PTR, HRESULT, PTR], None), ([UINT, PTR], before_delete),
                   ([UINT, PTR, HRESULT, PTR], after_delete), ([UINT, PTR, PTR], None),
                   ([UINT, PTR, PTR, PTR, UINT, HRESULT, PTR], None), ([UINT, UINT], None),
                   ([], None), ([], None), ([], None)]
    for types, callback in definitions:
        callbacks.append(c.WINFUNCTYPE(HRESULT, PTR, *types)(callback or (lambda *args: 0)))
    table = (PTR * len(callbacks))(*(c.cast(callback, PTR).value for callback in callbacks))
    sink.table = table
    try:
        checked(ole.CoCreateInstance(c.byref(clsid), None, 1, c.byref(iid_operation), c.byref(operation)), '创建回收站接口')
        # 回收整个目录对象，不递归处理其子条目，避免长子路径触发永久删除降级。
        # RECYCLEONDELETE + ADDUNDORECORD + EARLYFAILURE + NOERRORUI + NORECURSION + NOCONFIRMATION + SILENT。
        checked(call(operation, 5, [UINT], 0x20181414), '设置仅回收模式')
        checked(shell.SHCreateItemFromParsingName(path, None, c.byref(iid_item), c.byref(item)), '读取旧副本目录')
        checked(call(operation, 18, [PTR, PTR], item, c.byref(sink)), '提交回收操作')
        performed = call(operation, 21, [])
        if state['blocked']:
            raise OSError('Windows 要求对部分条目永久删除，已拒绝该降级；旧副本或剩余内容保留。')
        checked(performed, '回收操作')
        aborted = c.c_int32()
        checked(call(operation, 22, [c.POINTER(c.c_int32)], c.byref(aborted)), '核对回收结果')
        if aborted.value or state['error'] is not None or not state['recycled'] or os.path.lexists(native_path(path)):
            raise OSError('系统未确认完成回收，旧副本或剩余内容保留；不会自动永久删除。')
        return state['path']
    finally:
        if item:
            call(item, 2, [])
        if operation:
            call(operation, 2, [])
        ole.CoUninitialize()


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
        windows_recycle(path)
        return "已移入回收站: " + path
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
    return sorted({record['path'] for record in _PROCESS_RECORDS.values()
                   if ntpath.basename(record['path']).lower() == 'workbuddy.exe'})


def wait_apps_gone(timeout):
    deadline = time.monotonic() + timeout
    while True:
        state, detail = detect_app_running()
        if state == 'not-running':
            return True
        if state == 'unknown':
            raise SafeAbort('退出后无法核对进程，停止搬迁: ' + str(detail))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1, remaining))


def kill_running(sig):
    state, procs = detect_app_running()
    if state == 'unknown':
        raise SafeAbort('无法核对待退出进程，停止搬迁。')
    if any(int(pid) == os.getpid() for pid, _ in procs):
        raise SafeAbort('启动器属于 WorkBuddy 进程树，请在 WorkBuddy 之外的系统终端执行。')
    if os.name == 'nt':
        # 按已核对身份的 PID 操作；/T 包括子进程，不能按同名映像全局结束。
        pending = {str(pid) for pid, _ in procs}
        roots = [pid for pid in pending if str(_PROCESS_RECORDS.get(pid, {}).get('ppid')) not in pending]
        for pid in roots:
            # 每次重新核对，避免 PID 在前一次查询之后已被其他应用复用。
            previous = _PROCESS_RECORDS.get(pid)
            fresh_state, fresh = detect_app_running()
            if fresh_state == 'unknown':
                raise SafeAbort('结束进程前身份核对失败，停止搬迁。')
            if not previous or _PROCESS_RECORDS.get(pid) != previous or pid not in {str(p) for p, _ in fresh}:
                continue
            command = ['taskkill', '/PID', pid, '/T']
            if sig == FORCE_SIGNAL:
                command.append('/F')
            subprocess.run(command, capture_output=True, timeout=10)
        return
    for pid, _comm in procs:
        previous = _PROCESS_RECORDS.get(str(pid))
        fresh_state, fresh = detect_app_running()
        if fresh_state == 'unknown':
            raise SafeAbort('结束进程前身份核对失败，停止搬迁。')
        if not previous or _PROCESS_RECORDS.get(str(pid)) != previous or str(pid) not in {str(p) for p, _ in fresh}:
            continue
        try:
            os.kill(int(pid), sig)
        except ProcessLookupError:
            pass
        except (PermissionError, ValueError, OSError) as ex:
            warn('无法结束进程 %s: %s' % (pid, ex))


def graceful_quit_all(procs):
    """退出主程序、托盘与已核对归属的子进程，确认全部退出后返回。"""
    if any(int(pid) == os.getpid() for pid, _ in procs):
        raise SafeAbort('启动器属于 WorkBuddy 进程树，请在 WorkBuddy 之外的系统终端执行。')
    relaunch = []
    if sys.platform == 'darwin':
        relaunch = mac_app_bundles(procs)
        for bundle in relaunch:
            script = 'on run argv\n tell application (item 1 of argv) to quit\nend run'
            subprocess.run(['osascript', '-e', script, bundle], capture_output=True,
                           text=True, errors='replace', timeout=10)
    elif os.name == 'nt':
        relaunch = windows_main_exes()
    if sys.platform != 'darwin':
        kill_running(signal.SIGTERM)
    if not wait_apps_gone(8):
        print('      托盘或后台进程仍在运行，结束已核对的 WorkBuddy 进程树 ...', flush=True)
        if sys.platform == 'darwin':
            kill_running(signal.SIGTERM)
        if not wait_apps_gone(3):
            kill_running(FORCE_SIGNAL)
            if not wait_apps_gone(5):
                raise SafeAbort('WorkBuddy 托盘/后台仍未退出，请手动退出后重试；尚未开始复制。')
    print('      已确认 WorkBuddy 主程序、托盘与子进程全部退出。', flush=True)
    return relaunch

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
    return os.path.normcase(display_path(os.path.realpath(native_path(os.path.abspath(os.path.expanduser(path))))))


def windows_path(path):
    """Win32 Unicode 文件接口路径；不依赖注册表的 LongPathsEnabled 设置。"""
    path = os.fsdecode(path)
    if path.startswith('\\\\?\\'):
        tail = path[4:]
        if not (tail.startswith('UNC\\') or (len(tail) >= 3 and tail[0].isalpha() and tail[1:3] == ':\\')):
            raise SafeAbort('不支持设备命名空间作为迁移目录。')
        return path[:4] + ntpath.normpath(tail)
    if path.startswith('\\\\.\\'):
        raise SafeAbort('不支持设备路径作为迁移目录。')
    path = ntpath.abspath(path)
    if path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path


def native_path(path):
    return windows_path(path) if NATIVE_WINDOWS else os.fspath(path)


def display_path(path):
    if path.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path[8:]
    if path.startswith('\\\\?\\'):
        return path[4:]
    return path


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
        global _IO_WORKERS
        previous = _IO_WORKERS
        _IO_WORKERS = worker_count(getattr(args, 'workers', DEFAULT_IO_WORKERS))
        try:
            with operation_lock():
                return func(args)
        finally:
            _IO_WORKERS = previous
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

    def report(self, final=False, force=False):
        with self.mutex:
            count, size = self.files, self.bytes
        elapsed = time.monotonic() - self.start
        text = self.label
        if self.total_bytes is not None:
            fraction = min(1, size / self.total_bytes) if self.total_bytes else (min(1, count / self.total_files) if self.total_files else 1)
            filled = int(fraction * 12)
            text += ' | [%s%s] %.1f%% | %s/%s' % ('#' * filled, '-' * (12 - filled), fraction * 100, fmt_bytes(size), fmt_bytes(self.total_bytes))
        elif size:
            text += ' | 已读取 ' + fmt_bytes(size)
        if self.total_files is not None:
            text += ' | %d/%d 项' % (count, self.total_files)
        elif count:
            text += ' | %d 项' % count
        text += ' | 已用时 %.0fs' % elapsed
        if self.total_bytes is not None and elapsed > 1 and size:
            text += ' | %s/s' % fmt_bytes(size / elapsed)
        writer = getattr(sys.stdout, 'progress', None)
        if writer:
            writer(text, final=final, force=force)
        elif sys.stdout.isatty():
            line = fit_line(text)
            width = cell_width(line)
            previous = getattr(self, 'line_width', 0)
            sys.stdout.write('\r' + line + ' ' * max(0, previous - width) + ('\n' if final else ''))
            sys.stdout.flush()
            self.line_width = 0 if final else width
        elif final or force:
            print(text, flush=True)

    def worker(self):
        while not self.stop.wait(self.interval):
            self.report()

    def __enter__(self):
        self.start = time.monotonic()
        self.report(force=True)
        self.thread = threading.Thread(target=self.worker, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        self.report(final=True)


def signature(st):
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode]


def identity(path):
    st = os.stat(path)
    return [st.st_dev, st.st_ino]


def worker_count(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 8:
        raise SafeAbort('并发数需为 1–8；机械盘可用 --workers 1。')
    return value


def bounded_results(function, items, workers=None):
    """任务队列最多 2×并发数；异常时停止提交，等待在途写入退出。"""
    workers = worker_count(_IO_WORKERS if workers is None else workers)
    if workers == 1:
        for item in items:
            yield function(item)
        return
    iterator = iter(items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = collections.deque()
        try:
            for _ in range(workers * 2):
                item = next(iterator, None)
                if item is None:
                    break
                pending.append(pool.submit(function, item))
            while pending:
                yield pending.popleft().result()
                item = next(iterator, None)
                if item is not None:
                    pending.append(pool.submit(function, item))
        finally:
            for future in pending:
                future.cancel()


def file_hash(path, record, progress):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        if signature(os.fstat(f.fileno()))[:2] != record['signature'][:2]:
            raise SafeAbort('读取前文件发生变化: ' + display_path(path))
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            progress.update(size=len(chunk))
        if signature(os.fstat(f.fileno()))[:2] != record['signature'][:2]:
            raise SafeAbort('读取期间文件发生变化: ' + display_path(path))
    if signature(os.lstat(path)) != record['signature']:
        raise SafeAbort('读取后文件发生变化: ' + display_path(path))
    return digest.hexdigest()


def manifest(path, hashes=False, label='扫描目录'):
    """目录链接不跟随；用扫描项自带 stat，避免每个普通文件重复系统查询。"""
    path = native_path(path)
    if not os.path.isdir(path):
        raise SafeAbort('数据目录不可访问: ' + display_path(path))
    root_stat = os.stat(path)
    out = {'.': {'kind': 'dir', 'mode': statmod.S_IMODE(root_stat.st_mode), 'signature': signature(root_stat)}}
    with Progress(label + ('（扫描）' if hashes else '')) as progress:
        def visit(folder):
            with os.scandir(folder) as iterator:
                entries = sorted(iterator, key=lambda e: e.name)
            for entry in entries:
                rel = os.path.relpath(entry.path, path)
                # Windows 不同 Python 版本的目录枚举与句柄 stat 对 ctime 的语义不同。
                st = os.lstat(entry.path) if NATIVE_WINDOWS else entry.stat(follow_symlinks=False)
                if statmod.S_ISLNK(st.st_mode):
                    record = {'kind': 'link', 'target': os.readlink(entry.path),
                              'directory': bool(getattr(st, 'st_file_attributes', 0) & 0x10) if os.name == 'nt' else False}
                elif statmod.S_ISDIR(st.st_mode) and bool(getattr(st, 'st_file_attributes', 0) & 0x400):
                    record = {'kind': 'junction', 'target': display_path(os.path.realpath(entry.path))}
                elif statmod.S_ISDIR(st.st_mode):
                    record = {'kind': 'dir', 'mode': statmod.S_IMODE(st.st_mode)}
                    visit(entry.path)
                elif statmod.S_ISREG(st.st_mode):
                    record = {'kind': 'file', 'size': st.st_size, 'mode': statmod.S_IMODE(st.st_mode)}
                else:
                    raise SafeAbort('不支持的特殊文件（如 socket/FIFO）: ' + display_path(entry.path))
                record['signature'] = signature(st)
                out[rel] = record
                progress.update(count=1)
        visit(path)
    if hashes:
        files = [(name, record) for name, record in out.items() if record['kind'] == 'file']
        with Progress(label, sum(record['size'] for _, record in files), len(files)) as progress:
            def hash_one(item):
                name, record = item
                digest = file_hash(tree_path(path, name), record, progress)
                progress.update(count=1)
                return name, digest
            for name, digest in bounded_results(hash_one, files):
                out[name]['sha256'] = digest
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


def tree_path(root, relative):
    # manifest 的 '.' 表示目录本身；Win32 扩展路径不接受尾随的 \ . 组件。
    return root if relative == '.' else os.path.join(root, relative)


def run_copy(src, dest, records=None):
    global _COPY_RECEIPT
    _COPY_RECEIPT = None
    """通用流式复制，四个平台采用相同计数与校验语义，无外部复制工具依赖。"""
    src, dest = native_path(src), native_path(dest)
    records = records if records is not None else manifest(src, label='[2/6] 扫描待复制数据')
    total = sum(r.get('size', 0) for r in records.values())
    os.makedirs(dest, mode=0o700, exist_ok=True)
    # 调用方只传入本工具创建并记录的 staging；清理上次中断的多余副本条目。
    previous = manifest(dest, label='检查临时副本')
    for rel, record in sorted(previous.items(), key=lambda item: item[0].count(os.sep)):
        if record['kind'] == 'dir':
            path = tree_path(dest, rel)
            os.chmod(path, record['mode'] | statmod.S_IRUSR | statmod.S_IWUSR | statmod.S_IXUSR)
    for rel in sorted(set(previous) - set(records), key=lambda p: p.count(os.sep), reverse=True):
        path = tree_path(dest, rel)
        if previous[rel]['kind'] in ('link', 'junction'):
            remove_link(path)
        elif previous[rel]['kind'] == 'dir':
            os.rmdir(path)
        else:
            os.unlink(path)
    print('复制与 SHA-256 源摘要合并执行；%d 路并发，保留逐文件落盘。' % _IO_WORKERS, flush=True)
    files = []
    with Progress('[2/6] 复制并计算源摘要', total, len(records)) as progress:
        for rel, record in sorted(records.items(), key=lambda item: (item[0].count(os.sep), item[0])):
            d = tree_path(dest, rel)
            kind = record['kind']
            if kind == 'file':
                files.append((rel, record))
                continue
            if kind == 'dir':
                if link_target(d) is not None:
                    raise SafeAbort('拒绝写入临时副本内的目录链接: ' + display_path(d))
                os.makedirs(d, mode=0o700, exist_ok=True)
            else:
                if os.path.lexists(d):
                    if link_target(d) is None:
                        raise SafeAbort('临时副本中已有非链接条目: ' + display_path(d))
                    remove_link(d)
                if kind == 'junction':
                    make_link(record['target'], d)
                else:
                    os.symlink(record['target'], d, target_is_directory=record['directory'])
            progress.update(count=1)
        def copy_one(item):
            rel, record = item
            s, d = tree_path(src, rel), tree_path(dest, rel)
            if os.path.islink(d) or is_reparse_dir(d):
                raise SafeAbort('拒绝写入临时副本内的链接: ' + display_path(d))
            temporary = os.path.join(os.path.dirname(d), '.wb-file-' + uuid.uuid4().hex + '.tmp')
            digest = hashlib.sha256()
            try:
                with open(s, 'rb') as source, open(temporary, 'xb') as target:
                    if signature(os.fstat(source.fileno()))[:2] != record['signature'][:2]:
                        raise SafeAbort('复制前源文件发生变化: ' + display_path(s))
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                        digest.update(chunk)
                        progress.update(size=len(chunk))
                    target.flush()
                    os.fsync(target.fileno())
                    if signature(os.fstat(source.fileno()))[:2] != record['signature'][:2]:
                        raise SafeAbort('复制期间源文件发生变化: ' + display_path(s))
                if signature(os.lstat(s)) != record['signature']:
                    raise SafeAbort('复制后源文件发生变化: ' + display_path(s))
                shutil.copystat(s, temporary, follow_symlinks=False)
                os.replace(temporary, d)
            except OSError as ex:
                raise SafeAbort('复制文件失败: %s\n源文件: %s\n目标文件: %s' %
                                (ex, display_path(s), display_path(d))) from ex
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            progress.update(count=1)
            return rel, digest.hexdigest()
        for rel, digest in bounded_results(copy_one, files):
            records[rel]['sha256'] = digest
        for rel, record in sorted(records.items(), key=lambda item: -1 if item[0] == '.' else item[0].count(os.sep), reverse=True):
            if record['kind'] == 'dir':
                shutil.copystat(tree_path(src, rel), tree_path(dest, rel), follow_symlinks=False)
    _COPY_RECEIPT = dict(src=canonical(src), dest=canonical(dest), source_id=identity(src),
                         dest_id=identity(dest), records=records)
    return 'python-stream'


def verify_strict(src, dest):
    global _COPY_RECEIPT
    receipt, _COPY_RECEIPT = _COPY_RECEIPT, None
    if (receipt and receipt['src'] == canonical(src) and receipt['dest'] == canonical(dest)
            and receipt['source_id'] == identity(src) and receipt['dest_id'] == identity(dest)):
        source = receipt['records']
        snapshot = {name: {k: v for k, v in record.items() if k != 'sha256'} for name, record in source.items()}
        if manifest(src, label='[3/6] 核对源目录状态') != snapshot:
            raise SafeAbort('复制后源目录发生变化，已停止。')
        print('使用本次复制读取时的源 SHA-256，避免再次完整读取源盘。', flush=True)
    else:
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
    for key in ('dest', 'data', 'old_target'):
        path = plan.get(key)
        if path:
            if not isinstance(path, str) or not os.path.isabs(path):
                raise SafeAbort('记录中的数据位置无效: ' + key)
            guard_common(path)
    backup = plan['backup']
    if backup != plan['src'] + BACKUP_SUFFIX and not re.fullmatch(re.escape(plan['src'] + BACKUP_SUFFIX) + '-[0-9a-f]{32}', backup):
        raise SafeAbort('备份路径与原目录不符，拒绝清理。')
    if is_within(plan['src'], plan['dest']) or is_within(plan['dest'], plan['src']):
        # 已迁移时 src 实际指向 dest，不能按 realpath 判断是否嵌套。
        if resolved_link(plan['src']) is None or not same_path(plan['src'], plan['dest']):
            raise SafeAbort('记录中数据路径存在嵌套关系。')


_PROCESS_IDENTITIES = {}
_PROCESS_RECORDS = {}


def process_snapshot():
    """只读取进程名、可执行路径与启动时间；不采集可能含隐私的命令行。"""
    records = {}
    if os.name == 'nt':
        ps = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                          'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        script = ("$ErrorActionPreference='Stop'; "
                  "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding; "
                  "@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,"
                  "@{Name='Started';Expression={if ($_.CreationDate) {$_.CreationDate.ToUniversalTime().Ticks.ToString()}}}) "
                  "| ConvertTo-Json -Compress")
        result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
        if result.returncode != 0:
            raise SafeAbort('Windows 进程查询失败: ' + (result.stderr or str(result.returncode))[:200])
        rows = json.loads(result.stdout.lstrip('\ufeff'))
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            pid = str(int(row['ProcessId']))
            records[pid] = dict(pid=pid, ppid=str(int(row['ParentProcessId'])),
                                name=row['Name'], path=row.get('ExecutablePath') or '',
                                started=row.get('Started') or '')
    else:
        result = subprocess.run(['ps', '-axo', 'pid=,ppid=,lstart=,comm='],
                                capture_output=True, text=True, errors='replace', timeout=10,
                                env=dict(os.environ, LC_ALL='C'))
        if result.returncode != 0:
            raise SafeAbort('POSIX 进程查询失败: ' + (result.stderr or str(result.returncode))[:200])
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 7)
            if len(parts) != 8 or not parts[0].isdigit() or not parts[1].isdigit():
                raise SafeAbort('进程查询返回无法识别的数据。')
            pid, ppid = parts[:2]
            comm = parts[7]
            path = comm
            if sys.platform.startswith('linux'):
                try:
                    path = os.readlink('/proc/%s/exe' % pid)
                except OSError:
                    pass
            records[pid] = dict(pid=pid, ppid=ppid, name=os.path.basename(comm),
                                path=path, started=' '.join(parts[2:7]))
    if not records:
        raise SafeAbort('进程列表为空，无法确认应用已退出。')
    return records


def process_identity(record):
    return record['name'], record['path'], record['started']


def owns_app_process(record):
    name = record['name'].lower()
    if name in ('workbuddy', 'workbuddy.exe', 'workbuddytray.exe', 'workbuddy-tray.exe') or name.startswith('workbuddy helper'):
        return True
    path = record['path'].replace('\\', '/').lower()
    # 独立运行的 CodeBuddy 不属于本工具；仅确认 WorkBuddy 安装目录中的引擎。
    installed = any(part in ('workbuddy', 'workbuddy.app', '.workbuddy') for part in path.split('/')[:-1])
    return installed and name in ('codebuddy', 'codebuddy.exe', 'electron', 'electron.exe')


def detect_app_running():
    global _PROCESS_RECORDS, _PROCESS_IDENTITIES
    try:
        records = process_snapshot()
        owned = {pid for pid, record in records.items() if owns_app_process(record) or
                 (record['started'] and _PROCESS_IDENTITIES.get(pid) == process_identity(record))}
        # 保留退出主程序后变为孤儿的助手身份，直到其退出或 PID 被复用。
        changed = True
        while changed:
            changed = False
            for pid, record in records.items():
                if pid not in owned and record['ppid'] in owned:
                    parent = records[record['ppid']]
                    # 不能把新进程复用的父 PID 当作历史父子关系。
                    if os.name == 'nt' and record['started'] and parent['started']:
                        if int(record['started']) < int(parent['started']):
                            continue
                    elif record['started'] and parent['started']:
                        fmt = '%a %b %d %H:%M:%S %Y'
                        if time.strptime(record['started'], fmt) < time.strptime(parent['started'], fmt):
                            continue
                    owned.add(pid)
                    changed = True
        for pid, record in records.items():
            if pid not in owned and record['name'].lower() in ('codebuddy', 'codebuddy.exe', 'electron', 'electron.exe'):
                if not record['path'] or not (ntpath.isabs(record['path']) if os.name == 'nt' else os.path.isabs(record['path'])):
                    raise SafeAbort('无法核对后台引擎的可执行路径，不能确认 WorkBuddy 已退出。')
        if any(not records[pid]['started'] for pid in owned):
            raise SafeAbort('无法核对 WorkBuddy 进程启动身份。')
        _PROCESS_RECORDS = {pid: records[pid] for pid in owned}
        _PROCESS_IDENTITIES = {pid: process_identity(record) for pid, record in _PROCESS_RECORDS.items()}
        hits = [(pid, record['path'] or record['name']) for pid, record in _PROCESS_RECORDS.items()]
        return ('running' if hits else 'not-running'), hits
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, SafeAbort) as ex:
        return 'unknown', str(ex)


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
        source_records = manifest(data, label='扫描待复制目录')
        size = sum(r.get('size', 0) for r in source_records.values())
        count = len(source_records) - 1
        print('数据: %s / %d 项；目标: %s' % (fmt_bytes(size), count, dest), flush=True)
        if not reuse and shutil.disk_usage(os.path.dirname(dest)).free < size * 1.02:
            raise SafeAbort('目标剩余空间不足（需要数据体积加 2% 余量）。')
        candidate = dest if reuse else plan['staging']
        if not reuse:
            run_copy(data, candidate, records=source_records)
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
    source_records = manifest(dest, label='扫描待恢复目录')
    size = sum(r.get('size', 0) for r in source_records.values())
    if shutil.disk_usage(os.path.dirname(src)).free < size * 1.02:
        raise SafeAbort('原磁盘空间不足；原链接未改变。')
    transition(plan, 'copying')
    run_copy(dest, stage, records=source_records)
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
    transition(plan, 'committed', source_usable=True, swapped=False, restored_identity=identity(src))
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
            assert_offline()
            with Progress('[5/6] 清理旧位置副本'):
                print(trash_path(dest), flush=True)
        except Exception as ex:
            warn('数据已恢复，旧位置副本清理失败，保留在 %s: %s' % (dest, ex))
        if full and not getattr(args, 'keep_backup', False):
            # 历次二次搬迁可能保留多个旧副本，只清理该源路径的可核实快照。
            snapshots = {old['backup']: old.get('source_digest')}
            for filename in sorted(glob.glob(os.path.join(PLAN_DIR, 'plan-*.json'))):
                history = load_plan(filename)
                if history.get('action') == 'migrate' and os.path.normcase(history['src']) == os.path.normcase(src):
                    try:
                        validate_plan(history, args)
                    except SafeAbort as ex:
                        warn('历史记录路径无效，跳过该记录的副本清理: ' + str(ex))
                        continue
                    for path in (history['backup'], history.get('old_target')):
                        if path and os.path.isdir(path) and not same_path(path, src):
                            snapshots[path] = history.get('source_digest')
            for path, digest in snapshots.items():
                if not os.path.isdir(path):
                    continue
                try:
                    if not digest or manifest_digest(manifest(path, hashes=True, label='确认旧快照身份')) != digest:
                        raise SafeAbort('旧快照身份无法确认或内容已变化，保留供人工检查: ' + path)
                    with Progress('[5/6] 清理旧快照'):
                        print(trash_path(path, hard=getattr(args, 'hard_delete', False)), flush=True)
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
    relaunch = auto_manage_app(args)
    target = resolved_link(src)
    stage = plan.get('stage')
    if plan['action'] == 'migrate':
        if target and same_path(target, dest) and os.path.isdir(dest):
            transition(plan, 'complete', swapped=True, source_usable=True,
                       warnings=['中断后已确认链接与新数据位置；旧副本保留，需检查后清理。'])
            warn('已确认迁移切换完成；旧副本保留。请检查应用后清理。')
            if relaunch and not args.no_relaunch:
                relaunch_apps(relaunch)
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
            if relaunch and not args.no_relaunch:
                relaunch_apps(relaunch)
            return
    else:
        if not os.path.lexists(src) and os.path.isdir(data):
            make_link(data, src)
            target = resolved_link(src)
        if target and same_path(target, data) and os.path.isdir(src):
            transition(plan, 'aborted', source_usable=True)
            print('原链接已恢复；最新数据仍在原实际位置，可重新执行恢复。', flush=True)
            if relaunch and not args.no_relaunch:
                relaunch_apps(relaunch)
            return
        if resolved_link(src) is None and os.path.isdir(src) and stage in ('switching', 'committed', 'cleanup'):
            if plan.get('source_digest') and manifest_digest(manifest(src, hashes=True, label='核对已恢复数据')) == plan['source_digest']:
                transition(plan, 'complete', swapped=False, source_usable=True)
                warn('已确认恢复完成；旧位置副本保留。')
                if relaunch and not args.no_relaunch:
                    relaunch_apps(relaunch)
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
    restored = plan.get('action') in ('restore', 'rollback')
    obsolete = plan['dest'] if restored else (plan.get('old_target') or plan['backup'])
    if restored and (target is not None or plan.get('stage') != 'complete'):
        raise SafeAbort('尚未确认恢复完成，拒绝清理旧位置。')
    if restored and plan.get('restored_identity') and identity(src) != plan['restored_identity']:
        raise SafeAbort('已恢复目录身份发生变化，拒绝清理旧位置。')
    if os.path.isdir(obsolete):
        if same_path(obsolete, src) or (not restored and same_path(obsolete, dest)):
            raise SafeAbort('待清理位置仍是当前数据，拒绝删除。')
        if not plan.get('source_digest') or manifest_digest(manifest(obsolete, hashes=True, label='确认旧副本身份')) != plan['source_digest']:
            raise SafeAbort('旧副本身份无法确认或内容已变化，拒绝自动清理。请人工检查。')
        with Progress('[5/6] 仅清理旧副本'):
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


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1) for c in text)


def fit_line(text):
    limit = max(10, shutil.get_terminal_size((100, 24)).columns - 2)
    if cell_width(text) <= limit:
        return text
    out, width = [], 0
    for char in text:
        amount = cell_width(char)
        if width + amount > limit - 3:
            break
        out.append(char)
        width += amount
    return ''.join(out) + '...'


class _LogStream:
    def __init__(self, console, log, mutex):
        self.console, self.log, self.mutex = console, log, mutex
        self.progress_width = 0
        self.progress_visible = False
        self.normal_line_open = False

    def write(self, text):
        with self.mutex:
            self.log.write(text)
            self.log.flush()
            if self.progress_visible:
                self.console.write('\r' + ' ' * self.progress_width + '\r')
                self.progress_visible = False
                self.progress_width = 0
            result = self.console.write(text)
            self.console.flush()
            if text:
                self.normal_line_open = not text.endswith('\n')
            return result

    def progress(self, text, final=False, force=False):
        with self.mutex:
            # 日志保留每次采样，终端只更新当前行；控制字符不进入日志。
            self.log.write(text + '\n')
            self.log.flush()
            if self.console.isatty():
                if self.normal_line_open and not final:
                    return
                line = fit_line(text)
                width = cell_width(line)
                self.console.write('\r' + line + ' ' * max(0, self.progress_width - width))
                if final:
                    self.console.write('\n')
                self.progress_width = 0 if final else width
                self.progress_visible = not final
            elif final or force:
                self.console.write(text + '\n')
            self.console.flush()

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
    if rc == 2:
        print('数据操作已完成；待处理提醒如下：', flush=True)
        for message in _WARNINGS:
            print('  - ' + message, flush=True)
        if _ACTIVE and _ACTIVE.get('action') in ('restore', 'rollback'):
            print('无需重跑恢复；仅清理旧位置可用新版 cleanup-backup --plan "%s" --yes。' % _ACTIVE['plan_path'], flush=True)
    print('结束: %s\n日志: %s' % (now_iso(), log_path), flush=True)
    return rc


def execute_logged(action, log_dir=None):
    global _ACTIVE, _WARNINGS, _COPY_RECEIPT
    _ACTIVE, _WARNINGS, _COPY_RECEIPT = None, [], None
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
            if ex.__cause__ is not None:
                traceback.print_exc(file=log)
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
        common = dict(no_relaunch=False, force_any_dir=False, workers=worker_count(job.get('workers', DEFAULT_IO_WORKERS)))
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


def resolve_runner_python(explicit=None):
    """选择并实际验证解释器；支持现有便携 Python，不交付已知不能启动的入口。"""
    candidates = [(os.path.abspath(os.path.expanduser(explicit)), [])] if explicit else [(sys.executable, [])]
    if not explicit:
        for name in ('python3', 'python', 'py'):
            path = shutil.which(name)
            if path:
                candidates.append((path, ['-3'] if name == 'py' else []))
    probe = 'import json,sys; print(json.dumps({"version":list(sys.version_info[:3]),"executable":sys.executable,"prefix":sys.prefix}))'
    errors = []
    for path, flags in candidates:
        try:
            if not os.path.isfile(native_path(path)):
                continue
            if NATIVE_WINDOWS and '\\windowsapps\\' in path.lower():
                continue
            result = subprocess.run([path] + flags + ['-X', 'utf8', '-c', probe], capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=15)
            if result.returncode:
                raise ValueError(result.stderr.strip() or '解释器无法启动')
            info = json.loads(result.stdout)
            if tuple(info['version']) < (3, 8):
                raise ValueError('需要 Python 3.8+')
            if any(is_within(DEFAULT_SRC, info[key]) for key in ('executable', 'prefix')):
                raise ValueError('解释器位于待迁移数据目录中')
            selected = os.path.abspath(display_path(info['executable']))
            if NATIVE_WINDOWS and any(char in selected for char in ('"', '\r', '\n')):
                raise ValueError('解释器路径包含无效字符')
            return selected
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as ex:
            errors.append('%s: %s' % (path, ex))
    raise UnsupportedEnvironment('未找到数据目录之外的可用 Python。可用 --python 指定已有便携解释器。'
                                 '未生成新的启动脚本。\n' + '\n'.join(errors[-3:]))


def windows_runner_content(job_name, python_executable=None):
    bootstrap = [
        'set "PYEXE="', 'set "PYFLAG="',
        'for /f "delims=" %%P in (\'where py.exe 2^>nul\') do if not defined PYEXE call :try_python "%%P" -3',
        'for /f "delims=" %%P in (\'where python.exe 2^>nul\') do if not defined PYEXE call :try_python "%%P"',
    ]
    if python_executable:
        bootstrap = ['set "PYEXE=' + python_executable.replace('%', '%%') + '"', 'set "PYFLAG="']
    return '\r\n'.join([
        '@echo off', 'setlocal DisableDelayedExpansion',
        'chcp 65001 >nul', 'title WorkBuddy cache mover',
        'echo Preparing. Do not reopen WorkBuddy during the operation.',
    ] + bootstrap + [
        'if defined PYEXE goto execute',
        'echo No working external Python 3.8+ found. Install system Python from python.org.',
        'set "RC=5"', 'goto finish', ':execute',
        '"%PYEXE%" %PYFLAG% -X utf8 -u "%~dp0cache_mover.py" run-job --args-file "%~dp0' + job_name + '"',
        'set "RC=%ERRORLEVEL%"', ':finish', 'echo.',
        'echo Exit code: %RC% - 0=success, 2=completed with warnings, 3=recovery required, 4=busy, 5=environment unsupported.',
        'echo Logs: "%LOCALAPPDATA%\\WorkBuddyCacheMover\\logs"',
        'echo This window stays open. Read the result, then close it or type EXIT.',
        '"%ComSpec%" /D /Q /V:OFF /K', 'exit /b %RC%',
        ':try_python', 'if /I "%~dp1"=="%LOCALAPPDATA%\\Microsoft\\WindowsApps\\" exit /b 0',
        '"%~1" %~2 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1',
        'if errorlevel 1 exit /b 0', 'set "PYEXE=%~1"', 'set "PYFLAG=%~2"', 'exit /b 0', ''
    ])


def posix_runner_content(job_name, python_executable=None):
    bootstrap = [
        'PY=""', 'rc=5',
        'for cand in "$(command -v python3)" "$(command -v python)" /opt/homebrew/bin/python3 /usr/local/bin/python3; do',
        '  if [ -x "$cand" ] && "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >/dev/null 2>&1; then PY="$cand"; break; fi',
        'done',
    ]
    if python_executable:
        bootstrap = ['PY=' + shlex.quote(python_executable), 'rc=5']
    return '\n'.join([
        '#!/bin/sh', 'SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$0")" && pwd) || exit 1',
    ] + bootstrap + [
        'if [ -n "$PY" ]; then',
        '  "$PY" -X utf8 -u "$SCRIPT_DIR/cache_mover.py" run-job --args-file "$SCRIPT_DIR/' + job_name + '"',
        '  rc=$?', 'else', '  echo "No working external Python 3.8+ found. Install system Python."', 'fi',
        'echo "Exit code: $rc (0=success, 2=completed with warnings, 3=recovery required, 4=busy, 5=environment unsupported)"',
        'if [ -t 0 ]; then printf "Read the result, then press Enter to close..."; read -r answer; fi',
        'exit "$rc"', ''
    ])


@locked
def cmd_make_runner(args):
    command = args.command
    python_executable = resolve_runner_python(getattr(args, 'python_executable', None))
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
    payload['python_executable'] = python_executable
    payload['workers'] = worker_count(getattr(args, 'workers', DEFAULT_IO_WORKERS))
    suffix = '.bat' if os.name == 'nt' else ('.command' if sys.platform == 'darwin' else '.sh')
    out = os.path.abspath(os.path.expanduser(args.out or os.path.join(PLAN_DIR, 'run_' + command + suffix)))
    folder = os.path.dirname(out)
    os.makedirs(folder, exist_ok=True)
    local = os.path.join(folder, 'cache_mover.py')
    if not same_path(__file__, local):
        shutil.copy2(__file__, local)
    atomic_json(os.path.join(folder, job_name), payload)
    content = windows_runner_content(job_name, python_executable) if os.name == 'nt' else posix_runner_content(job_name, python_executable)
    with open(out, 'w', encoding='utf-8', newline='') as f:
        f.write(content)
    if os.name != 'nt':
        os.chmod(out, 0o755)
    print('已生成: ' + out, flush=True)
    print('唯一启动入口: %s\n已验证的解释器: %s' % (out, python_executable), flush=True)
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
    mg.add_argument('--workers', type=int, choices=range(1, 9), default=DEFAULT_IO_WORKERS)
    mg.set_defaults(func=cmd_migrate)
    runner = sub.add_parser('make-runner')
    runner.add_argument('--command', choices=['migrate', 'restore', 'rollback', 'recover'], default='migrate')
    runner.add_argument('--dest-base')
    runner.add_argument('--name')
    runner.add_argument('--plan', default='latest')
    runner.add_argument('--out')
    runner.add_argument('--python', dest='python_executable', help='指定数据目录之外的现有系统/便携 Python，生成唯一入口')
    runner.add_argument('--keep-backup', action='store_true')
    runner.add_argument('--workers', type=int, choices=range(1, 9), default=DEFAULT_IO_WORKERS, help='复制/校验并发数；机械盘建议从 1 开始')
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
        parser.add_argument('--workers', type=int, choices=range(1, 9), default=DEFAULT_IO_WORKERS)
        parser.set_defaults(func=func)
    args = p.parse_args()
    if args.cmd == 'run-job':
        args.func(args)
    else:
        raise SystemExit(execute_logged(lambda: args.func(args)))


if __name__ == '__main__':
    main()
