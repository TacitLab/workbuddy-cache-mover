"""原生 shell 启动与窗口/退出码检查；输入故意损坏，绝不执行真实迁移。"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location('native_mover', os.path.join(os.path.dirname(__file__), 'cache_mover.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class NativeRunnerTests(unittest.TestCase):
    def setup_job(self, root):
        folder = os.path.join(root, '中文 路径 & 测试!')
        os.makedirs(folder)
        shutil.copy2(m.__file__, os.path.join(folder, 'cache_mover.py'))
        with open(os.path.join(folder, 'broken.json'), 'w', encoding='utf-8') as f:
            f.write('invalid JSON')
        environment = dict(os.environ)
        environment['HOME'] = os.path.join(root, 'home')
        environment['USERPROFILE'] = environment['HOME']
        environment['LOCALAPPDATA'] = os.path.join(root, 'local')
        environment['XDG_STATE_HOME'] = os.path.join(root, 'state')
        environment['PATH'] = os.path.dirname(sys.executable) + os.pathsep + environment.get('PATH', '')
        os.makedirs(environment['HOME'])
        return folder, environment

    @unittest.skipIf(os.name == 'nt', 'POSIX shell only')
    def test_posix_native_launcher_keeps_failure_output_and_exit_code(self):
        with tempfile.TemporaryDirectory(prefix='wb-native-') as root:
            folder, env = self.setup_job(root)
            path = os.path.join(folder, 'run.sh')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(m.posix_runner_content('broken.json'))
            result = subprocess.run(['sh', path], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('RESULT: FAILED', result.stdout)
            self.assertIn('JSONDecodeError', result.stdout + result.stderr)
            logs = []
            for parent, _, files in os.walk(root):
                logs.extend(os.path.join(parent, f) for f in files if f.endswith('.log'))
            self.assertEqual(len(logs), 1)
            with open(logs[0], encoding='utf-8') as f:
                self.assertIn('JSONDecodeError', f.read())

    @unittest.skipUnless(os.name == 'nt', 'Windows cmd only')
    def test_windows_native_launcher_stays_open_until_explicit_exit(self):
        with tempfile.TemporaryDirectory(prefix='wb-native-') as root:
            folder, env = self.setup_job(root)
            path = os.path.join(folder, 'run.bat')
            with open(path, 'w', encoding='ascii', newline='') as f:
                f.write(m.windows_runner_content('broken.json'))
            command = [os.environ['COMSPEC'], '/D', '/V:OFF', '/C', 'run.bat']
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                       errors='replace', env=env, cwd=folder, bufsize=1)
            lines, ready = [], threading.Event()
            def read_until_result():
                for line in process.stdout:
                    lines.append(line)
                    if 'This window stays open.' in line:
                        ready.set()
                        break
            reader = threading.Thread(target=read_until_result, daemon=True)
            reader.start()
            try:
                self.assertTrue(ready.wait(30), ''.join(lines))
                reader.join(timeout=2)
                self.assertIn('RESULT: FAILED', ''.join(lines))
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)
                process.communicate('exit 0\r\n', timeout=10)
                logs = []
                for parent, _, files in os.walk(root):
                    logs.extend(os.path.join(parent, f) for f in files if f.endswith('.log'))
                self.assertEqual(len(logs), 1)
            finally:
                if process.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                    process.wait(timeout=10)
                reader.join(timeout=2)
                process.stdin.close()
                process.stdout.close()


if __name__ == '__main__':
    unittest.main()
