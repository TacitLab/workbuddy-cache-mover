"""Runner 回归验证。仅使用临时目录与替身动作，不迁移真实 WorkBuddy 数据。"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "cache_mover", os.path.join(os.path.dirname(__file__), "cache_mover.py"))
mover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mover)


class RunnerTests(unittest.TestCase):
    def logged_job(self, job, action):
        with tempfile.TemporaryDirectory(prefix="wb-runner-") as root:
            args_file = os.path.join(root, "任务.json")
            log_dir = os.path.join(root, "日志")
            with open(args_file, "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False)
            console = io.StringIO()
            with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
                with mock.patch.object(mover, "cmd_migrate", side_effect=action):
                    with self.assertRaises(SystemExit) as result:
                        mover.cmd_run_job(argparse.Namespace(args_file=args_file, log_dir=log_dir))
                self.assertIs(mover.sys.stdout, console)
                self.assertIs(mover.sys.stderr, console)
            names = os.listdir(log_dir)
            self.assertEqual(len(names), 1)
            with open(os.path.join(log_dir, names[0]), encoding="utf-8") as f:
                contents = f.read()
            return result.exception.code, console.getvalue(), contents

    def test_success_preserves_unicode_output_and_target(self):
        seen = []
        def success(args):
            seen.append(args.dest_base)
            print("复制完成：中文文件")
        rc, console, log = self.logged_job({"dest_base": "D:\\数据盘\\缓存"}, success)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["D:\\数据盘\\缓存"])
        self.assertIn("复制完成：中文文件", log)
        self.assertIn("RESULT: SUCCESS", console)
        self.assertEqual(console, log)

    def test_failure_preserves_reason_and_exit_code(self):
        rc, console, log = self.logged_job({"dest_base": "D:\\缓存"}, SystemExit("目标空间不足"))
        self.assertEqual(rc, 1)
        self.assertIn("目标空间不足", log)
        self.assertIn("RESULT: FAILED", console)
        rc, _, log = self.logged_job({"dest_base": "D:\\缓存"}, SystemExit(3))
        self.assertEqual(rc, 3)
        self.assertIn("exit code 3", log)

    def test_exception_and_interrupt_are_logged(self):
        rc, _, log = self.logged_job({"dest_base": "D:\\缓存"}, OSError("磁盘断开"))
        self.assertEqual(rc, 1)
        self.assertIn("Traceback", log)
        self.assertIn("磁盘断开", log)
        rc, _, log = self.logged_job({"dest_base": "D:\\缓存"}, KeyboardInterrupt())
        self.assertEqual(rc, 130)
        self.assertIn("Interrupted", log)

    def test_bad_job_never_calls_migration(self):
        action = mock.Mock()
        rc, _, log = self.logged_job({"dest_base": "D:\\缓存", "command": "delete"}, action)
        self.assertEqual(rc, 1)
        action.assert_not_called()
        self.assertIn("Unsupported runner command", log)

    def test_windows_generation_with_unicode_and_custom_output(self):
        with tempfile.TemporaryDirectory(prefix="wb-gen-") as root:
            plan_dir = os.path.join(root, "中文 用户", ".wb-cache-move")
            out = os.path.join(root, "桌面", "运行.bat")
            args = argparse.Namespace(command="migrate", dest_base="D:\\用户数据\\缓存",
                                      name=None, plan="latest", out=out)
            with mock.patch.object(mover, "PLAN_DIR", plan_dir), mock.patch.object(mover.os, "name", "nt"):
                with contextlib.redirect_stdout(io.StringIO()):
                    mover.cmd_make_runner(args)
            with open(out, "rb") as f:
                launcher = f.read()
            self.assertTrue(launcher.isascii())
            self.assertNotIn(b"\n", launcher.replace(b"\r\n", b""))
            self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(out), "cache_mover.py")))
            with open(os.path.join(os.path.dirname(out), "migrate-args.json"), encoding="utf-8") as f:
                job = json.load(f)
            self.assertIn("用户数据", job["dest_base"])

    def test_restore_generation_preserves_unicode_plan_in_json(self):
        with tempfile.TemporaryDirectory(prefix="wb-restore-") as root:
            plan_dir = os.path.join(root, "中文 用户", ".wb-cache-move")
            os.makedirs(plan_dir)
            plan_path = os.path.join(plan_dir, "plan-中文.json")
            with open(plan_path, "w", encoding="utf-8") as f:
                json.dump({"src": "unused", "dest": "unused", "backup": "unused"}, f)
            args = argparse.Namespace(command="restore", dest_base=None,
                                      name=None, plan=plan_path, out=None)
            with mock.patch.object(mover, "PLAN_DIR", plan_dir), mock.patch.object(mover.os, "name", "nt"):
                with contextlib.redirect_stdout(io.StringIO()):
                    mover.cmd_make_runner(args)
            with open(os.path.join(plan_dir, "run_restore.bat"), "rb") as f:
                self.assertTrue(f.read().isascii())
            with open(os.path.join(plan_dir, "runner-restore-args.json"), encoding="utf-8") as f:
                job = json.load(f)
            self.assertEqual(job["plan"], plan_path)
            self.assertEqual(job["command"], "restore")

    def test_restore_dispatch_and_deleted_job_still_leave_result_log(self):
        with tempfile.TemporaryDirectory(prefix="wb-restore-job-") as root:
            args_file = os.path.join(root, "restore.json")
            with open(args_file, "w", encoding="utf-8") as f:
                json.dump({"command": "restore", "plan": "中文 plan.json"}, f)
            log_dir = os.path.join(root, "logs")
            def restore(args):
                self.assertEqual(args.plan, "中文 plan.json")
                os.remove(args_file)
                print("已恢复")
            with contextlib.redirect_stdout(io.StringIO()):
                with mock.patch.object(mover, "cmd_restore", side_effect=restore):
                    with self.assertRaises(SystemExit) as result:
                        mover.cmd_run_job(argparse.Namespace(args_file=args_file, log_dir=log_dir))
            self.assertEqual(result.exception.code, 0)
            with open(os.path.join(log_dir, os.listdir(log_dir)[0]), encoding="utf-8") as f:
                self.assertIn("RESULT: SUCCESS", f.read())

    def test_cli_failure_exit_code_and_log(self):
        with tempfile.TemporaryDirectory(prefix="wb-cli-") as root:
            args_file = os.path.join(root, "参数.json")
            with open(args_file, "w", encoding="utf-8") as f:
                f.write("invalid json")
            log_dir = os.path.join(root, "日志")
            result = subprocess.run([
                sys.executable, "-X", "utf8", "-u", mover.__file__, "run-job",
                "--args-file", args_file, "--log-dir", log_dir,
            ], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 1)
            self.assertIn("RESULT: FAILED", result.stdout)
            with open(os.path.join(log_dir, os.listdir(log_dir)[0]), encoding="utf-8") as f:
                self.assertIn("JSONDecodeError", f.read())

    def test_robocopy_output_and_result_codes(self):
        with tempfile.TemporaryDirectory(prefix="wb-copy-") as root:
            with mock.patch.object(mover.shutil, "which", return_value=None), mock.patch.object(mover.os, "name", "nt"):
                for rc in (3, 8):
                    proc = mock.Mock(stdout=io.StringIO("复制状态\n"))
                    proc.wait.return_value = rc
                    console = io.StringIO()
                    with mock.patch.object(mover.subprocess, "Popen", return_value=proc), contextlib.redirect_stdout(console):
                        if rc >= 8:
                            with self.assertRaises(RuntimeError):
                                mover.run_copy(root, os.path.join(root, "dest"))
                        else:
                            self.assertEqual(mover.run_copy(root, os.path.join(root, "dest")), "robocopy")
                    self.assertIn("复制状态", console.getvalue())


if __name__ == "__main__":
    unittest.main()
