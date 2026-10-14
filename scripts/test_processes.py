"""退出托盘与助手时的进程归属检查；不结束本机真实进程。"""
import contextlib
import importlib.util
import io
import os
import subprocess
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('process_mover', os.path.join(os.path.dirname(__file__), 'cache_mover.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def process(pid, parent, name, path='', started='100'):
    return dict(pid=str(pid), ppid=str(parent), name=name, path=path, started=started)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        m._PROCESS_RECORDS.clear()
        m._PROCESS_IDENTITIES.clear()

    def test_main_children_tray_and_orphans_are_owned_but_unrelated_engine_is_not(self):
        records = {
            '11': process(11, 1, 'WorkBuddy.exe', r'C:\Apps\WorkBuddy\WorkBuddy.exe'),
            '12': process(12, 11, 'CodeBuddy.exe', r'C:\Apps\WorkBuddy\engine\CodeBuddy.exe'),
            '13': process(13, 12, 'node.exe', r'C:\Apps\WorkBuddy\node.exe'),
            '14': process(14, 1, 'electron.exe', r'C:\Apps\WorkBuddy\electron.exe'),
            '99': process(99, 1, 'CodeBuddy.exe', r'C:\Apps\CodeBuddy\CodeBuddy.exe')}
        with mock.patch.object(m.os, 'name', 'nt'), mock.patch.object(m, 'process_snapshot', return_value=records):
            state, hits = m.detect_app_running()
            self.assertEqual(state, 'running')
            self.assertEqual({pid for pid, _ in hits}, {'11', '12', '13', '14'})
            records.pop('11')
            records['12']['ppid'] = '1'
            self.assertEqual({pid for pid, _ in m.detect_app_running()[1]}, {'12', '13', '14'})
            records['13'] = process(13, 1, 'unrelated.exe', r'C:\Other\unrelated.exe', '200')
            self.assertNotIn('13', {pid for pid, _ in m.detect_app_running()[1]})

    def test_reused_parent_pid_does_not_adopt_older_unrelated_child(self):
        records = {'11': process(11, 1, 'WorkBuddy.exe', started='200'),
                   '12': process(12, 11, 'other.exe', started='100')}
        with mock.patch.object(m.os, 'name', 'nt'), mock.patch.object(m, 'process_snapshot', return_value=records):
            self.assertEqual({pid for pid, _ in m.detect_app_running()[1]}, {'11'})

    def test_missing_start_identity_fails_closed(self):
        with mock.patch.object(m, 'process_snapshot', return_value={'11': process(11, 1, 'workbuddy', started='')}):
            self.assertEqual(m.detect_app_running()[0], 'unknown')

    def test_current_runner_in_app_tree_is_never_killed(self):
        procs = [(str(os.getpid()), 'python')]
        with mock.patch.object(m, 'detect_app_running', return_value=('running', procs)), mock.patch.object(m.os, 'kill') as kill:
            with self.assertRaises(m.SafeAbort):
                m.kill_running(m.FORCE_SIGNAL)
            kill.assert_not_called()

    def test_windows_force_closes_verified_tree_by_pid(self):
        records = {'11': process(11, 1, 'WorkBuddy.exe'), '12': process(12, 11, 'node.exe')}
        with mock.patch.object(m.os, 'name', 'nt'), mock.patch.object(m, 'process_snapshot', return_value=records), mock.patch.object(m.subprocess, 'run') as run:
            m.kill_running(m.FORCE_SIGNAL)
            self.assertEqual(run.call_args[0][0], ['taskkill', '/PID', '11', '/T', '/F'])
            self.assertEqual(run.call_count, 1)

    def test_windows_pid_reuse_between_queries_is_not_killed(self):
        first = {'11': process(11, 1, 'WorkBuddy.exe')}
        second = {'11': process(11, 1, 'unrelated.exe', started='200')}
        with mock.patch.object(m.os, 'name', 'nt'), mock.patch.object(m, 'process_snapshot', side_effect=[first, second]), mock.patch.object(m.subprocess, 'run') as run:
            m.kill_running(m.FORCE_SIGNAL)
            run.assert_not_called()

    def test_tray_timeout_uses_force_and_verifies_final_exit(self):
        with mock.patch.object(m.os, 'name', 'nt'), mock.patch.object(m.sys, 'platform', 'win32'), mock.patch.object(m, 'windows_main_exes', return_value=['WB.exe']), mock.patch.object(m, 'kill_running') as kill, mock.patch.object(m, 'wait_apps_gone', side_effect=[False, False, True]) as wait, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.graceful_quit_all([('11', 'WorkBuddy.exe')]), ['WB.exe'])
            self.assertEqual(kill.call_args_list, [mock.call(m.signal.SIGTERM), mock.call(m.FORCE_SIGNAL)])
            self.assertEqual(wait.call_count, 3)

    def test_background_process_still_running_aborts(self):
        with mock.patch.object(m.os, 'name', 'nt'), mock.patch.object(m.sys, 'platform', 'win32'), mock.patch.object(m, 'windows_main_exes', return_value=[]), mock.patch.object(m, 'kill_running'), mock.patch.object(m, 'wait_apps_gone', return_value=False), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(m.SafeAbort):
                m.graceful_quit_all([('11', 'WorkBuddy.exe')])

    def test_unknown_during_wait_stops_without_copying(self):
        with mock.patch.object(m, 'detect_app_running', return_value=('unknown', 'query failed')):
            with self.assertRaises(m.SafeAbort):
                m.wait_apps_gone(1)

    def test_native_process_snapshot_is_read_only_and_has_current_pid(self):
        try:
            records = m.process_snapshot()
        except PermissionError as ex:
            self.skipTest('当前沙箱禁止进程查询: ' + str(ex))
        self.assertIn(str(os.getpid()), records)
        self.assertTrue(records[str(os.getpid())]['started'])


if __name__ == '__main__':
    unittest.main()
