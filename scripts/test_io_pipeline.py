"""复制摘要复用、并发异常与原生回收；所有写入仅针对测试自己的临时目录。"""
import argparse
import builtins
import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('pipeline_mover', os.path.join(os.path.dirname(__file__), 'cache_mover.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wb-pipeline-')
        self.src = os.path.join(self.tmp.name, 'source')
        self.dst = os.path.join(self.tmp.name, 'target')
        os.mkdir(self.src)
        self.file = os.path.join(self.src, 'file.txt')
        with open(self.file, 'wb') as f: f.write(b'payload')
        m._ACTIVE, m._COPY_RECEIPT = None, None
        self.output = contextlib.redirect_stdout(io.StringIO()); self.output.__enter__()
    def tearDown(self):
        self.output.__exit__(None, None, None); self.tmp.cleanup()
    def test_source_is_read_once_but_destination_is_independently_hashed(self):
        real_open = builtins.open; reads = []; mutex = threading.Lock()
        def record(path, mode='r', *args, **kwargs):
            if mode == 'rb':
                with mutex: reads.append(m.canonical(path))
            return real_open(path, mode, *args, **kwargs)
        with mock.patch.object(builtins, 'open', side_effect=record):
            m.run_copy(self.src, self.dst)
            self.assertTrue(m.verify_strict(self.src, self.dst))
        self.assertEqual(reads.count(m.canonical(self.file)), 1)
        self.assertEqual(reads.count(m.canonical(os.path.join(self.dst, 'file.txt'))), 1)
        self.assertIsNone(m._COPY_RECEIPT)
    def test_copied_file_corruption_is_detected_even_with_same_size(self):
        m.run_copy(self.src, self.dst)
        with open(os.path.join(self.dst, 'file.txt'), 'wb') as f: f.write(b'PAYLOAD')
        self.assertFalse(m.verify_strict(self.src, self.dst))
    def test_source_changed_after_copy_is_rejected(self):
        m.run_copy(self.src, self.dst)
        with open(self.file, 'wb') as f: f.write(b'new data')
        with self.assertRaises(m.SafeAbort): m.verify_strict(self.src, self.dst)
    def test_unrelated_destination_does_not_use_receipt(self):
        m.run_copy(self.src, self.dst)
        other = os.path.join(self.tmp.name, 'other'); shutil.copytree(self.src, other)
        with mock.patch.object(m, 'manifest', wraps=m.manifest) as scan:
            self.assertTrue(m.verify_strict(self.src, other))
        self.assertTrue(any(call.kwargs.get('hashes') and call.args[0] == self.src for call in scan.call_args_list))
    def test_changed_destination_directory_identity_does_not_use_receipt(self):
        m.run_copy(self.src, self.dst)
        os.rename(self.dst, self.dst+'-old'); shutil.copytree(self.src, self.dst)
        with mock.patch.object(m, 'manifest', wraps=m.manifest) as scan:
            self.assertTrue(m.verify_strict(self.src, self.dst))
        self.assertTrue(any(call.kwargs.get('hashes') and call.args[0] == self.src for call in scan.call_args_list))
    def test_sync_failure_never_produces_a_valid_receipt(self):
        with mock.patch.object(m.os, 'fsync', side_effect=OSError('disk flush failed')):
            with self.assertRaises(m.SafeAbort): m.run_copy(self.src, self.dst)
        self.assertIsNone(m._COPY_RECEIPT)
        self.assertTrue(os.path.isfile(self.file))
        self.assertFalse(any(name.startswith('.wb-file-') for name in os.listdir(self.dst)))
    def test_workers_are_bounded_and_really_overlap(self):
        active = peak = 0; lock = threading.Lock()
        def work(number):
            nonlocal active, peak
            with lock: active += 1; peak = max(peak, active)
            time.sleep(0.01)
            with lock: active -= 1
            return number
        self.assertEqual(list(m.bounded_results(work, range(30), workers=4)), list(range(30)))
        self.assertGreater(peak, 1); self.assertLessEqual(peak, 4)
    def test_worker_error_is_propagated_and_pending_work_is_joined(self):
        def work(number):
            if number == 0: raise OSError('worker failed')
            time.sleep(0.01); return number
        with self.assertRaises(OSError): list(m.bounded_results(work, range(10000), workers=4))
    def test_invalid_worker_count_is_rejected(self):
        for value in (0, 9, True, '4'):
            with self.assertRaises(m.SafeAbort): m.worker_count(value)
    def test_recycle_sink_refuses_non_recyclable_delete(self):
        self.assertLess(m.recycle_precheck(0), 0)
        self.assertEqual(m.recycle_precheck(0x80), 0)
    def test_windows_trash_failure_never_falls_back_to_permanent_delete(self):
        with mock.patch.object(m.os, 'name', 'nt'), mock.patch.object(m.sys, 'platform', 'win32'), mock.patch.object(m, 'windows_recycle', side_effect=OSError('not supported')), mock.patch.object(m.shutil, 'rmtree') as remove:
            with self.assertRaises(OSError): m.trash_path(self.src)
            remove.assert_not_called()
        self.assertTrue(os.path.isfile(self.file))
    @unittest.skipUnless(os.name == 'nt', 'Native Windows Shell recycle fixture')
    def test_native_windows_recycle_preserves_the_fixture_in_recycle_bin(self):
        recycled = m.windows_recycle(self.src)
        self.assertFalse(os.path.lexists(self.src))
        self.assertTrue(recycled, 'Shell did not supply recycled filesystem path')
        self.assertIn('$recycle.bin', recycled.lower())
        self.assertTrue(os.path.isdir(m.native_path(recycled)))
        try:
            with open(m.native_path(os.path.join(recycled, 'file.txt')), 'rb') as f: self.assertEqual(f.read(), b'payload')
        finally:
            # 仅清理本用例刚创建、已核对内容的回收站目录，不能清空用户回收站。
            shutil.rmtree(m.native_path(recycled))


if __name__ == '__main__': unittest.main()
