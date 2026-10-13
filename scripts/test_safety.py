"""真实临时目录上的迁移/恢复与故障注入；从不操作用户 WorkBuddy 数据。"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('mover_safety', os.path.join(os.path.dirname(__file__), 'cache_mover.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wb-safety-')
        self.root = self.tmp.name
        self.src = os.path.join(self.root, '用户 数据', '.workbuddy')
        self.base = os.path.join(self.root, '目标 数据')
        self.dest = os.path.join(self.base, '.workbuddy')
        os.makedirs(os.path.join(self.src, 'empty'))
        with open(os.path.join(self.src, '文件.bin'), 'wb') as f:
            f.write(b'original payload')
        os.chmod(os.path.join(self.src, '文件.bin'), 0o755)
        self.plan_dir = os.path.join(self.root, 'state')
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(m, 'PLAN_DIR', self.plan_dir))
        self.stack.enter_context(mock.patch.object(m, 'DEFAULT_SRC', self.src))
        self.stack.enter_context(mock.patch.object(m, 'detect_app_running', return_value=('not-running', [])))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        m._ACTIVE, m._WARNINGS = None, []

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def migrate_args(self, **changes):
        data = dict(src=self.src, force_any_dir=False, skip_app_handling=False,
                    dest_base=self.base, name=None, args_file=None,
                    no_relaunch=True, keep_backup=True)
        data.update(changes)
        return argparse.Namespace(**data)

    def restore_args(self, **changes):
        data = dict(plan='latest', force_any_dir=False, no_relaunch=True,
                    keep_backup=True, hard_delete=False, yes=True)
        data.update(changes)
        return argparse.Namespace(**data)

    def fake_trash(self, path, hard=False):
        target = os.path.join(self.root, 'trash-' + str(time.time_ns()))
        shutil.move(path, target)
        return 'moved to test trash'

    def migrate(self):
        m.cmd_migrate(self.migrate_args())
        return m.load_plan('latest')

    def test_migrate_and_restore_keep_latest_changes(self):
        self.migrate()
        self.assertEqual(m.canonical(self.src), m.canonical(self.dest))
        with open(os.path.join(self.src, 'new.txt'), 'w', encoding='utf-8') as f:
            f.write('迁移后新数据')
        with mock.patch.object(m, 'trash_path', side_effect=self.fake_trash):
            m.cmd_restore(self.restore_args())
        self.assertIsNone(m.resolved_link(self.src))
        with open(os.path.join(self.src, 'new.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read(), '迁移后新数据')
        self.assertTrue(os.path.isdir(os.path.join(self.src, 'empty')))
        self.assertFalse(m.unfinished(self.src))

    def test_nonempty_destination_is_never_overwritten(self):
        os.makedirs(self.dest)
        path = os.path.join(self.dest, '文件.bin')
        with open(path, 'wb') as f:
            f.write(b'unrelated data')
        with self.assertRaises(m.SafeAbort):
            m.cmd_migrate(self.migrate_args())
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), b'unrelated data')
        self.assertIsNone(m.resolved_link(self.src))

    def test_copy_failure_keeps_restore_link_and_retry_works(self):
        self.migrate()
        def fail(source, target):
            os.mkdir(target)
            with open(os.path.join(target, 'partial'), 'w') as f:
                f.write('partial')
            raise OSError('simulated disk disconnect')
        with mock.patch.object(m, 'run_copy', side_effect=fail):
            with self.assertRaises(OSError):
                m.cmd_restore(self.restore_args())
        self.assertIsNotNone(m.resolved_link(self.src))
        self.assertTrue(os.path.isfile(os.path.join(self.src, '文件.bin')))
        self.assertEqual(m.load_plan('latest')['stage'], 'aborted')
        with mock.patch.object(m, 'trash_path', side_effect=self.fake_trash):
            m.cmd_restore(self.restore_args())
        self.assertIsNone(m.resolved_link(self.src))

    def test_switch_failure_restores_original_link(self):
        self.migrate()
        rename = m.os.rename
        def fail_stage(source, target):
            if '.wb-restore-' in source:
                raise PermissionError('file locked')
            return rename(source, target)
        with mock.patch.object(m.os, 'rename', side_effect=fail_stage):
            with self.assertRaises(PermissionError):
                m.cmd_rollback(self.restore_args())
        self.assertTrue(m.same_path(self.src, self.dest))
        self.assertEqual(m.load_plan('latest')['stage'], 'aborted')

    def test_initial_link_failure_restores_real_directory(self):
        create_link = m.make_link
        def fail_final(target, path):
            if path == self.src:
                raise PermissionError('link denied')
            return create_link(target, path)
        with mock.patch.object(m, 'make_link', side_effect=fail_final):
            with self.assertRaises(PermissionError):
                m.cmd_migrate(self.migrate_args())
        self.assertIsNone(m.resolved_link(self.src))
        self.assertTrue(os.path.isfile(os.path.join(self.src, '文件.bin')))
        # 本工具发布的校验副本可以在重跑时复用，不合并外部文件。
        m.cmd_migrate(self.migrate_args())
        self.assertTrue(m.same_path(self.src, self.dest))

    def test_recover_crash_after_backup_rename(self):
        os.makedirs(self.base)
        plan = m.new_operation('migrate', self.src, self.dest, self.src)
        m.transition(plan, 'switching')
        os.rename(self.src, plan['backup'])
        m.cmd_recover(self.restore_args())
        self.assertTrue(os.path.isfile(os.path.join(self.src, '文件.bin')))
        self.assertIsNone(m.resolved_link(self.src))
        self.assertEqual(m.load_plan('latest')['stage'], 'aborted')

    def test_recover_crash_after_restore_link_removed(self):
        self.migrate()
        origin = m.load_plan('latest')
        plan = m.new_operation('restore', self.src, self.dest, self.dest, origin['backup'])
        m.transition(plan, 'switching')
        m.remove_link(self.src)
        m.cmd_recover(self.restore_args())
        self.assertTrue(m.same_path(self.src, self.dest))
        self.assertEqual(m.load_plan('latest')['stage'], 'aborted')

    def test_recover_committed_migration_keeps_latest_data(self):
        origin = self.migrate()
        with open(os.path.join(self.dest, 'new.txt'), 'w') as f:
            f.write('latest')
        m.transition(origin, 'committed')
        m.cmd_recover(self.restore_args())
        self.assertTrue(os.path.isfile(os.path.join(self.src, 'new.txt')))
        self.assertEqual(m.load_plan('latest')['stage'], 'complete')

    def test_recovery_does_not_overwrite_unknown_directory(self):
        os.makedirs(self.base)
        plan = m.new_operation('migrate', self.src, self.dest, self.src)
        m.transition(plan, 'switching')
        os.rename(self.src, plan['backup'])
        os.mkdir(self.src)
        with open(os.path.join(self.src, 'unknown'), 'w') as f:
            f.write('user created')
        with self.assertRaises(m.RecoveryRequired):
            m.cmd_recover(self.restore_args())
        self.assertTrue(os.path.isfile(os.path.join(self.src, 'unknown')))
        self.assertTrue(os.path.isfile(os.path.join(plan['backup'], '文件.bin')))

    def test_unknown_process_state_stops_before_switch(self):
        with mock.patch.object(m, 'detect_app_running', return_value=('unknown', 'denied')):
            with self.assertRaises(m.SafeAbort):
                m.cmd_migrate(self.migrate_args())
        self.assertIsNone(m.resolved_link(self.src))
        self.assertFalse(os.path.exists(self.dest))

    def test_failed_process_command_is_unknown(self):
        result = subprocess.CompletedProcess([], 1, stdout='', stderr='denied')
        # setUp 的 mock 临时移开，验证真实检测函数。
        func_spec = importlib.util.spec_from_file_location('fresh_detection', m.__file__)
        fresh = importlib.util.module_from_spec(func_spec)
        func_spec.loader.exec_module(fresh)
        with mock.patch.object(fresh.subprocess, 'run', return_value=result):
            self.assertEqual(fresh.detect_app_running()[0], 'unknown')

    def test_atomic_record_failure_preserves_previous_file(self):
        os.makedirs(self.plan_dir)
        path = os.path.join(self.plan_dir, 'test.json')
        m.atomic_json(path, {'state': 'old'})
        with mock.patch.object(m.json, 'dump', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                m.atomic_json(path, {'state': 'new'})
        with open(path) as f:
            self.assertEqual(json.load(f), {'state': 'old'})

    def test_kernel_lock_blocks_another_process_and_releases(self):
        code = '''import importlib.util, sys
s = importlib.util.spec_from_file_location('m', sys.argv[1]); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.PLAN_DIR = sys.argv[2]
try:
    with m.operation_lock(): pass
except m.BusyError: sys.exit(4)
'''
        command = [sys.executable, '-c', code, m.__file__, self.plan_dir]
        with m.operation_lock():
            result = subprocess.run(command, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(subprocess.run(command, capture_output=True, timeout=15).returncode, 0)

    def test_interpreter_inside_source_is_rejected(self):
        with mock.patch.object(m.sys, 'executable', os.path.join(self.src, 'python')):
            with self.assertRaises(m.SafeAbort):
                m.cmd_migrate(self.migrate_args())
        self.assertIsNone(m.resolved_link(self.src))

    def test_harmony_identified_and_refuses_migration(self):
        with mock.patch.object(m.sys, 'platform', 'ohos'), mock.patch.object(m.platform, 'system', return_value='HarmonyOS'):
            self.assertEqual(m.platform_tag(), 'harmony')
            with self.assertRaises(m.UnsupportedEnvironment):
                m.cmd_migrate(self.migrate_args())
            m.cmd_detect(argparse.Namespace(src=None))
        self.assertIsNone(m.resolved_link(self.src))

    def test_redirect_preserves_changes_and_can_restore(self):
        self.migrate()
        with open(os.path.join(self.src, 'latest'), 'w') as f:
            f.write('latest')
        second = os.path.join(self.root, '第二块 数据')
        with mock.patch.object(m, 'trash_path', side_effect=self.fake_trash):
            m.cmd_migrate(self.migrate_args(dest_base=second, keep_backup=False))
            self.assertTrue(os.path.isfile(os.path.join(self.src, 'latest')))
            m.cmd_rollback(self.restore_args())
        self.assertIsNone(m.resolved_link(self.src))
        self.assertTrue(os.path.isfile(os.path.join(self.src, 'latest')))

    def test_restore_cleans_verified_snapshots_from_previous_moves(self):
        first = self.migrate()
        with open(os.path.join(self.src, 'latest'), 'w') as f:
            f.write('latest')
        second = os.path.join(self.root, 'second')
        m.cmd_migrate(self.migrate_args(dest_base=second))
        with mock.patch.object(m, 'trash_path', side_effect=self.fake_trash):
            m.cmd_restore(self.restore_args(keep_backup=False))
        self.assertFalse(os.path.exists(first['backup']))
        self.assertFalse(os.path.exists(self.dest))
        self.assertTrue(os.path.isfile(os.path.join(self.src, 'latest')))

    @unittest.skipIf(os.name == 'nt', 'POSIX directory permissions')
    def test_root_private_permissions_are_preserved(self):
        os.chmod(self.src, 0o700)
        self.migrate()
        self.assertEqual(os.stat(self.dest).st_mode & 0o777, 0o700)

    def test_cleanup_failure_is_completed_with_warnings(self):
        with mock.patch.object(m, 'trash_path', side_effect=OSError('trash denied')):
            rc = m.execute_logged(lambda: m.cmd_migrate(self.migrate_args(keep_backup=False)), os.path.join(self.root, 'logs'))
        self.assertEqual(rc, 2)
        self.assertTrue(m.same_path(self.src, self.dest))
        self.assertEqual(m.load_plan('latest')['stage'], 'complete')

    def test_pending_operation_blocks_new_migration(self):
        os.makedirs(self.base)
        m.new_operation('migrate', self.src, self.dest, self.src)
        with self.assertRaises(m.RecoveryRequired):
            m.cmd_migrate(self.migrate_args())
        self.assertIsNone(m.resolved_link(self.src))

    def test_read_error_is_not_silently_ignored(self):
        with mock.patch.object(m.os, 'scandir', side_effect=PermissionError('no access')):
            with self.assertRaises(PermissionError):
                m.manifest(self.src, hashes=True)

    def test_progress_reports_during_wait(self):
        output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        with m.Progress('slow operation', interval=0.02):
            time.sleep(0.07)
        self.assertGreaterEqual(output.getvalue().count('已用时'), 2)

    def test_symlink_target_changes_are_detected(self):
        original = os.path.join(self.src, 'linked')
        try:
            os.symlink('文件.bin', original)
        except OSError:
            self.skipTest('file symlink privilege not available')
        target = os.path.join(self.root, 'copy')
        m.run_copy(self.src, target)
        self.assertTrue(m.verify_strict(self.src, target))
        os.unlink(os.path.join(target, 'linked'))
        os.symlink('other.bin', os.path.join(target, 'linked'))
        self.assertFalse(m.verify_strict(self.src, target))


if __name__ == '__main__':
    unittest.main()
