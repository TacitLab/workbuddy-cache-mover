"""长路径、单行显示与单入口启动器回归检查。"""
import argparse
import builtins
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('usability_mover', os.path.join(os.path.dirname(__file__), 'cache_mover.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Terminal(io.StringIO):
    def isatty(self):
        return True


class UsabilityTests(unittest.TestCase):
    def test_windows_drive_and_unc_namespaces_are_idempotent(self):
        for plain, expected in [(r'C:\Users\A\folder', '\\\\?\\C:\\Users\\A\\folder'),
                                (r'\\server\share\folder', '\\\\?\\UNC\\server\\share\\folder')]:
            self.assertEqual(m.windows_path(plain), expected)
            self.assertEqual(m.windows_path(expected), expected)
            self.assertEqual(m.display_path(expected), plain)
            self.assertEqual(m.windows_path(expected + '\\.'), expected)
        with self.assertRaises(m.SafeAbort):
            m.windows_path('\\\\?\\GLOBALROOT\\Device\\Harddisk0')

    def test_terminal_refreshes_one_line_while_log_keeps_all_samples(self):
        console, log = Terminal(), io.StringIO()
        stream = m._LogStream(console, log, threading.Lock())
        for count in range(5):
            stream.progress('复制 [######------] %d%%' % (count * 20))
        stream.progress('复制 [############] 100%', final=True)
        self.assertEqual(console.getvalue().count('\n'), 1)
        self.assertEqual(log.getvalue().count('\n'), 6)
        self.assertIn('\r', console.getvalue())
        self.assertNotIn('\r', log.getvalue())
        self.assertIn('100%', log.getvalue())

    def test_redirected_output_keeps_start_and_finish_only(self):
        console, log = io.StringIO(), io.StringIO()
        stream = m._LogStream(console, log, threading.Lock())
        stream.progress('开始', force=True)
        stream.progress('中间')
        stream.progress('结束', final=True)
        self.assertEqual(console.getvalue(), '开始\n结束\n')
        self.assertEqual(log.getvalue(), '开始\n中间\n结束\n')

    def test_warning_clears_progress_line_and_is_not_overwritten(self):
        console, log = Terminal(), io.StringIO()
        stream = m._LogStream(console, log, threading.Lock())
        stream.progress('当前进度')
        stream.write('警告: 磁盘变化\n')
        self.assertFalse(stream.progress_visible)
        stream.progress('当前进度', final=True)
        self.assertIn('警告: 磁盘变化\n', console.getvalue())
        self.assertNotIn('\r', log.getvalue())

    def test_cjk_line_is_fitted_to_terminal_width(self):
        with mock.patch.object(m.shutil, 'get_terminal_size', return_value=os.terminal_size((40, 24))):
            self.assertLessEqual(m.cell_width(m.fit_line('正在复制 中文文件 ' * 20)), 38)

    def test_long_basename_does_not_gain_a_temporary_suffix(self):
        with tempfile.TemporaryDirectory(prefix='wb-long-name-') as root, contextlib.redirect_stdout(io.StringIO()):
            source, target = os.path.join(root, 'source'), os.path.join(root, 'target')
            os.mkdir(source)
            name = 'x' * 230 + '.txt'
            with open(m.native_path(os.path.join(source, name)), 'wb') as f:
                f.write(b'long filename payload')
            m.run_copy(source, target)
            self.assertTrue(m.verify_strict(source, target))
            self.assertEqual(os.listdir(m.native_path(target)), [name])

    @unittest.skipUnless(os.name == 'nt', 'Native Windows long path regression')
    def test_deep_paths_use_extended_namespace_even_if_legacy_open_rejects_them(self):
        with tempfile.TemporaryDirectory(prefix='wb-long-path-') as root, contextlib.redirect_stdout(io.StringIO()):
            source, target = os.path.join(root, 'source'), os.path.join(root, '.workbuddy.wb-restore-' + 'a' * 32)
            deep = os.path.join(source, *(['directory-' + 'x' * 55] * 4))
            os.makedirs(m.native_path(deep))
            file = os.path.join(deep, 'payload.txt')
            self.assertGreater(len(file), 260)
            with open(m.native_path(file), 'wb') as f:
                f.write(b'deep path contents')
            real_open = builtins.open
            seen = []
            def legacy_guard(path, *args, **kwargs):
                if isinstance(path, str) and len(m.display_path(path)) >= 260:
                    if not path.startswith('\\\\?\\'):
                        raise FileNotFoundError('legacy MAX_PATH limit', path)
                    seen.append(path)
                return real_open(path, *args, **kwargs)
            with mock.patch.object(builtins, 'open', side_effect=legacy_guard):
                m.run_copy(source, target)
                self.assertTrue(m.verify_strict(source, target))
            self.assertTrue(seen)
            shutil.rmtree(m.native_path(source))
            shutil.rmtree(m.native_path(target))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows junction long path regression')
    def test_junction_inside_deep_copy_keeps_target_and_can_be_removed(self):
        with tempfile.TemporaryDirectory(prefix='wb-junction-') as root, contextlib.redirect_stdout(io.StringIO()):
            source, target, external = [os.path.join(root, name) for name in ('source', 'target', 'external')]
            deep = os.path.join(source, *(['nested-' + 'x' * 60] * 4))
            os.makedirs(m.native_path(deep))
            os.mkdir(external)
            link = m.native_path(os.path.join(deep, 'linked'))
            m.make_link(external, link)
            try:
                m.run_copy(source, target)
                self.assertTrue(m.verify_strict(source, target))
                copied = m.native_path(os.path.join(target, os.path.relpath(m.display_path(link), source)))
                m.remove_link(copied)
                self.assertTrue(os.path.isdir(external))
            finally:
                m.remove_link(link)
                shutil.rmtree(m.native_path(source))
                if os.path.isdir(m.native_path(target)):
                    shutil.rmtree(m.native_path(target))

    def test_explicit_python_outside_data_is_accepted(self):
        with tempfile.TemporaryDirectory(prefix='wb-python-') as root:
            with mock.patch.object(m, 'DEFAULT_SRC', os.path.join(root, '.workbuddy')):
                selected = m.resolve_runner_python(sys.executable)
                self.assertTrue(m.same_path(selected, sys.executable))

    def test_no_usable_python_does_not_generate_a_broken_runner(self):
        with tempfile.TemporaryDirectory(prefix='wb-no-python-') as root:
            args = argparse.Namespace(command='migrate', dest_base=os.path.join(root, 'dest'),
                                      name=None, plan='latest', out=None)
            with mock.patch.object(m, 'PLAN_DIR', root), mock.patch.object(m, 'resolve_runner_python', side_effect=m.UnsupportedEnvironment('not available')):
                with self.assertRaises(m.UnsupportedEnvironment):
                    m.cmd_make_runner(args)
            self.assertFalse(any(name.endswith(('.bat', '.cmd', '.sh', '.command')) for name in os.listdir(root)))

    def test_generator_creates_exactly_one_entry_using_selected_interpreter(self):
        with tempfile.TemporaryDirectory(prefix='wb-one-entry-') as root, contextlib.redirect_stdout(io.StringIO()):
            args = argparse.Namespace(command='migrate', dest_base=os.path.join(root, 'dest'), name=None,
                                      plan='latest', out=None, python_executable=sys.executable)
            with mock.patch.object(m, 'PLAN_DIR', os.path.join(root, 'runner')), mock.patch.object(m, 'DEFAULT_SRC', os.path.join(root, '.workbuddy')):
                m.cmd_make_runner(args)
            folder = os.path.join(root, 'runner')
            entries = [name for name in os.listdir(folder) if name.endswith(('.bat', '.cmd', '.sh', '.command'))]
            self.assertEqual(len(entries), 1)
            with open(os.path.join(folder, entries[0]), encoding='utf-8') as f:
                text = f.read()
            self.assertNotIn('where py.exe', text)
            self.assertNotIn('command -v python', text)


if __name__ == '__main__':
    unittest.main()
