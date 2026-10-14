"""权限修复授权、范围与 Windows 原生 ACL；仅操作本用例临时数据。"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('permission_mover', os.path.join(os.path.dirname(__file__), 'cache_mover.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
SID = bytes.fromhex('010100000000000501000000')


def ace(sid=SID, mask=0x10040, flags=0, kind=1):
    return struct.pack('<BBHI', kind, flags, 8 + len(sid), mask) + sid


def acl(*entries):
    body = b''.join(entries)
    return struct.pack('<BBHHH', 2, 0, 8 + len(body), len(entries), 0) + body


class Terminal(io.StringIO):
    def isatty(self): return True


class FakeACL:
    sid, account, elevated = SID, 'TEST\\User', True
    def __init__(self, paths):
        self.rules = dict(paths); self.repaired = []
    def read(self, path):
        st = os.stat(path)
        return dict(acl=self.rules.get(path, acl()).hex(), identity=[st.st_dev, st.st_ino],
                    attributes=0x10 if os.path.isdir(path) else 0x20)
    def repair(self, path, expected):
        if self.read(path) != expected:
            raise OSError('changed object')
        self.rules[path] = m.without_delete_denies(self.rules[path], self.sid)
        self.repaired.append(path)


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wb-permission-')
        self.root = self.tmp.name
        self.src = os.path.join(self.root, '.workbuddy')
        self.dest = os.path.join(self.root, 'new-data'); os.mkdir(self.dest)
        self.backup = self.src + m.BACKUP_SUFFIX + '-' + 'a' * 32
        self.day = os.path.join(self.backup, 'logs', '2026-10-10'); os.makedirs(self.day)
        self.file = os.path.join(self.day, '日志.log')
        with open(self.file, 'wb') as f: f.write(b'old log')
        m.make_link(self.dest, self.src)
        self.api = FakeACL({self.day: acl(ace(mask=0x10042, flags=3), ace(kind=0, mask=0x1f01ff)),
                            self.file: acl(ace(mask=0x10042, flags=16), ace(kind=0, mask=0x1f01ff))})
        self.plan_dir = os.path.join(self.root, 'state')
        self.plan = dict(id='a' * 32, src=self.src, dest=self.dest, backup=self.backup,
                         verified=True, stage='complete', action='migrate',
                         plan_path=os.path.join(self.plan_dir, 'plan.json'))
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(m, '_ACTIVE', self.plan))
        self.stack.enter_context(mock.patch.object(m, 'PLAN_DIR', self.plan_dir))
        self.stack.enter_context(mock.patch.object(m, '_PERMISSION_REPAIR', 'ask'))
        self.stack.enter_context(mock.patch.object(m, 'WindowsACL', return_value=self.api))
        self.output = Terminal()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(mock.patch.object(m.sys, 'stdin', Terminal()))
        self.failure = m.RecycleError(0x80270021, '回收操作', self.backup)
    def tearDown(self):
        self.stack.close(); m._PROMPTING.clear()
        if m.link_target(self.src) is not None: m.remove_link(self.src)
        self.tmp.cleanup()
    def test_remove_only_delete_bits_preserves_other_accounts_flags_and_rights(self):
        other = bytes.fromhex('010100000000000502000000')
        raw = acl(ace(mask=0x10042, flags=3), ace(other), ace(kind=0, mask=0x1f01ff))
        self.assertEqual(m.without_delete_denies(raw, SID),
                         acl(ace(mask=2, flags=3), ace(other), ace(kind=0, mask=0x1f01ff)))
        self.assertEqual(m.without_delete_denies(acl(ace(kind=6)), SID), acl(ace(kind=6)))
        with self.assertRaises(OSError): m.without_delete_denies(b'bad', SID)
    def test_cancel_never_changes_acl_or_retries(self):
        with mock.patch.object(m, 'windows_recycle', side_effect=self.failure) as recycle, mock.patch('builtins.input', return_value=''):
            with self.assertRaises(OSError): m.recycle_with_permissions(self.backup)
        self.assertFalse(self.api.repaired); self.assertEqual(recycle.call_count, 1)
        with open(self.plan['permission_reports'][0], encoding='utf-8') as f:
            self.assertEqual(json.load(f)['status'], 'kept_without_authorization')
    def test_confirm_records_acl_before_writing_and_retries_once(self):
        original = self.api.repair
        def checked(path, expected):
            with open(self.plan['permission_reports'][0], encoding='utf-8') as f: record = json.load(f)
            self.assertEqual(record['status'], 'authorized')
            self.assertTrue(any(c['path'] == path and c['before'] == expected for c in record['changes']))
            original(path, expected)
        with mock.patch.object(m, 'windows_recycle', side_effect=[self.failure, 'test recycle path']) as recycle, mock.patch('builtins.input', return_value='REPAIR'), mock.patch.object(self.api, 'repair', side_effect=checked):
            self.assertEqual(m.recycle_with_permissions(self.backup), 'test recycle path')
        self.assertEqual(recycle.call_count, 2)
        self.assertEqual(set(self.api.repaired), {self.day, self.file})
        self.assertEqual(struct.unpack_from('<I', m.acl_entries(self.api.rules[self.file])[1][0], 4)[0], 2)
        with open(self.plan['permission_reports'][0], encoding='utf-8') as f: self.assertEqual(json.load(f)['status'], 'recycled')
    def test_pipe_and_off_modes_never_prompt_or_write(self):
        for mode in ('pipe', 'off'):
            with self.subTest(mode=mode), mock.patch.object(m, 'windows_recycle', side_effect=self.failure), mock.patch('builtins.input') as ask, mock.patch.object(m.sys, 'stdin', io.StringIO() if mode == 'pipe' else Terminal()), mock.patch.object(m, '_PERMISSION_REPAIR', 'off' if mode == 'off' else 'ask'):
                with self.assertRaises(OSError): m.recycle_with_permissions(self.backup)
                ask.assert_not_called(); self.assertFalse(self.api.repaired)
    def test_no_owned_plan_and_live_data_are_never_modified(self):
        with mock.patch.object(m, 'windows_recycle', side_effect=self.failure), mock.patch.object(m, '_ACTIVE', None), mock.patch('builtins.input') as ask:
            with self.assertRaises(OSError): m.recycle_with_permissions(self.backup)
            ask.assert_not_called()
        with self.assertRaises(OSError): m.permission_scope(self.dest)
    def test_cancel_on_eof_and_keyboard_interrupt(self):
        for error in (EOFError, KeyboardInterrupt):
            with mock.patch.object(m, 'windows_recycle', side_effect=self.failure), mock.patch('builtins.input', side_effect=error):
                with self.assertRaises(OSError): m.recycle_with_permissions(self.backup)
                self.assertFalse(self.api.repaired); self.assertFalse(m._PROMPTING.is_set())
    def test_changed_object_after_confirmation_is_preserved(self):
        def answer(prompt):
            self.api.rules[self.file] = acl(ace(mask=0x10041, flags=16))
            return 'REPAIR'
        with mock.patch.object(m, 'windows_recycle', side_effect=self.failure) as recycle, mock.patch('builtins.input', side_effect=answer):
            with self.assertRaises(OSError): m.recycle_with_permissions(self.backup)
        self.assertEqual(recycle.call_count, 1)
        self.assertNotIn(self.file, self.api.repaired)
    def test_failed_permission_backup_prevents_prompt_and_mutation(self):
        with mock.patch.object(m, 'windows_recycle', side_effect=self.failure), mock.patch.object(m, 'atomic_json', side_effect=OSError('disk full')), mock.patch('builtins.input') as ask:
            with self.assertRaises(OSError): m.recycle_with_permissions(self.backup)
            ask.assert_not_called(); self.assertFalse(self.api.repaired)
    def test_second_recycle_failure_does_not_repeat_authorization(self):
        with mock.patch.object(m, 'windows_recycle', side_effect=[self.failure, self.failure]) as recycle, mock.patch('builtins.input', return_value='REPAIR') as ask:
            with self.assertRaises(OSError): m.recycle_with_permissions(self.backup)
        self.assertEqual(recycle.call_count, 2); self.assertEqual(ask.call_count, 1)
    def test_links_do_not_expand_the_modification_scope(self):
        link = os.path.join(self.day, 'external-link'); m.make_link(self.dest, link)
        try:
            with self.assertRaises(OSError): m.guarded_permission_path(self.backup, os.path.join(link, 'anything'))
            changes = m.permission_changes(self.backup, [self.day], self.api)
            self.assertEqual({c['path'] for c in changes}, {self.day, self.file})
            with self.assertRaises(OSError): m.permission_changes(self.backup, [self.day], self.api, limit=1)
        finally: m.remove_link(link)
    def test_permission_prompt_is_not_overwritten_by_progress(self):
        console, log = Terminal(), io.StringIO()
        stream = m._LogStream(console, log, __import__('threading').Lock())
        m._PROMPTING.set()
        stream.progress('unrelated refresh', lines=['progress'], force=True)
        self.assertEqual(console.getvalue(), '')


@unittest.skipUnless(os.name == 'nt', 'Native Windows ACL fixtures')
class NativePermissionTests(unittest.TestCase):
    def test_inherited_denies_are_repaired_without_propagating_to_junction_target(self):
        with tempfile.TemporaryDirectory(prefix='wb-native-acl-') as root:
            source = os.path.join(root, 'old'); day = os.path.join(source, 'logs', '日期'); os.makedirs(day)
            file = os.path.join(day, '日志.log')
            with open(file, 'wb') as f: f.write(b'payload')
            external = os.path.join(root, 'external'); os.mkdir(external)
            api = m.WindowsACL()
            try:
                # 普通 WRITE_DAC 句柄在夹具中建立真实可继承的拒绝规则。
                with api.opened(day, 0x60000) as (handle, info):
                    original = api.read_handle(handle, info)
                    header, entries = m.acl_entries(bytes.fromhex(original['acl']))
                    body = ace(api.sid, mask=0x10042, flags=3) + b''.join(entries)
                    raw = struct.pack('<BBHHH', header[0], header[1], 8 + len(body), len(entries) + 1, header[2]) + body
                    api.set_handle(handle, raw)
                inherited = m.acl_entries(bytes.fromhex(api.read(file)['acl']))[1]
                self.assertTrue(any(m.delete_deny(a, api.sid) and a[1] & 16 for a in inherited))
                link = os.path.join(day, 'junction'); m.make_link(external, link)
                external_before = api.read(external)
                changes = m.permission_changes(source, [day], api)
                self.assertEqual({c['path'] for c in changes}, {day, file})
                for change in changes:
                    api.repair(change['path'], change['before'])
                self.assertEqual(api.read(external), external_before)
                for path in (day, file):
                    entries = m.acl_entries(bytes.fromhex(api.read(path)['acl']))[1]
                    self.assertFalse(any(m.delete_deny(a, api.sid) for a in entries))
                    self.assertTrue(any(a[0] == 1 and struct.unpack_from('<I', a, 4)[0] == 2 for a in entries))
                m.remove_link(link)
                recycled = m.windows_recycle(source)
                try:
                    with open(m.native_path(os.path.join(recycled, 'logs', '日期', '日志.log')), 'rb') as f:
                        self.assertEqual(f.read(), b'payload')
                finally: shutil.rmtree(m.native_path(recycled))
            finally:
                # 清理失败时也只修复本夹具，避免影响测试账户与用户目录。
                link = os.path.join(day, 'junction')
                if m.link_target(link) is not None: m.remove_link(link)
                for path in (day, file):
                    if os.path.exists(path):
                        item = api.read(path)
                        if item['acl']: api.repair(path, item)

    def test_hardlinked_file_is_never_modified(self):
        with tempfile.TemporaryDirectory(prefix='wb-acl-hardlink-') as root:
            original = os.path.join(root, 'file'); alias = os.path.join(root, 'alias')
            with open(original, 'wb') as f: f.write(b'keep')
            os.link(original, alias)
            with self.assertRaises(OSError): m.WindowsACL().read(alias)


if __name__ == '__main__': unittest.main()
