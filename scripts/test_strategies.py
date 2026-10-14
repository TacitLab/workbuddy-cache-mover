"""环境适配与原生工具验证，均使用隔离临时文件。"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location('strategy_mover',os.path.join(os.path.dirname(__file__),'cache_mover.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class StrategyTests(unittest.TestCase):
    def test_auto_keeps_compatible_python_engine(self):
        self.assertEqual(m.resolve_copy_engine('auto'),('python',None))
    def test_native_return_codes_are_interpreted_before_checksum(self):
        for code in range(8): self.assertTrue(m.tool_success('robocopy',code))
        for code in (8,16,-1): self.assertFalse(m.tool_success('robocopy',code))
        self.assertTrue(m.tool_success('rsync',0));self.assertFalse(m.tool_success('rsync',23))
    def test_templates_never_mirror_move_or_follow_links(self):
        cmd=m.copy_command('robocopy','robocopy.exe','source','target')
        self.assertIn('/SL',cmd);self.assertIn('/SJ',cmd)
        self.assertFalse(set(cmd)&{'/MIR','/PURGE','/MOVE','/MOV','/256'})
        cmd=m.copy_command('rsync','/usr/bin/rsync','source','target')
        self.assertIn('--',cmd);self.assertNotIn('--delete',cmd);self.assertNotIn('--copy-links',cmd)
    def test_missing_requested_tool_stops(self):
        with mock.patch.object(m.os,'name','posix'),mock.patch.object(m.shutil,'which',return_value=None):
            with self.assertRaises(m.UnsupportedEnvironment):m.resolve_copy_engine('rsync')
    def test_invalid_settings_stop_without_actions(self):
        for data in ({'copy_engine':'shell rm'},{'display':'unknown'},{'cleanup':'delete'}):
            with self.assertRaises(m.SafeAbort):m.validate_settings(argparse.Namespace(**data))
    def test_generator_records_configuration_and_single_entry(self):
        with tempfile.TemporaryDirectory() as root,contextlib.redirect_stdout(io.StringIO()):
            args=argparse.Namespace(command='migrate',dest_base=os.path.join(root,'destination'),name=None,out=None,plan='latest',copy_engine='python',cleanup='keep',display='compact',workers=2)
            with mock.patch.object(m,'PLAN_DIR',os.path.join(root,'runner')):
                m.cmd_make_runner(args)
            folder=os.path.join(root,'runner')
            with open(os.path.join(folder,'migrate-args.json'),encoding='utf8') as f:job=json.load(f)
            self.assertEqual((job['copy_engine'],job['cleanup'],job['display'],job['workers']),('python','keep','compact',2))
            self.assertIn('capability_report',job)
            self.assertEqual(sum(name.endswith(('.sh','.command','.bat','.cmd')) for name in os.listdir(folder)),1)
    def test_unvalidated_native_engine_does_not_generate_runner(self):
        with tempfile.TemporaryDirectory() as root:
            args=argparse.Namespace(command='migrate',dest_base=os.path.join(root,'destination'),name=None,out=None,plan='latest',copy_engine='rsync')
            with mock.patch.object(m,'PLAN_DIR',os.path.join(root,'runner')),mock.patch.object(m,'probe_copy_engine',side_effect=m.SafeAbort('fixture failed')):
                with self.assertRaises(m.SafeAbort):m.cmd_make_runner(args)
            self.assertFalse(os.path.isfile(os.path.join(root,'runner','migrate-args.json')))
    def test_native_copy_rejects_staging_links_before_launch(self):
        with tempfile.TemporaryDirectory() as root,contextlib.redirect_stdout(io.StringIO()):
            src,dst,external=[os.path.join(root,name) for name in ('src','dst','external')]
            for path in (src,dst,external):os.mkdir(path)
            os.mkdir(os.path.join(src,'child'));m.make_link(external,os.path.join(dst,'child'))
            with mock.patch.object(m.subprocess,'Popen') as run:
                with self.assertRaises(m.SafeAbort):m.native_copy(src,dst,m.manifest(src),'rsync','rsync')
                run.assert_not_called()
            m.remove_link(os.path.join(dst,'child'))
    def test_keep_never_invokes_recycle_or_delete(self):
        with tempfile.TemporaryDirectory() as root,mock.patch.object(m,'_CLEANUP_ENGINE','keep'),mock.patch.object(m,'windows_recycle') as recycle,mock.patch.object(m.shutil,'rmtree') as delete:
            self.assertIn('保留',m.trash_path(root))
            recycle.assert_not_called();delete.assert_not_called();self.assertTrue(os.path.isdir(root))
    def test_native_tool_probe_is_real_and_preserves_links_and_readonly_files(self):
        engine='robocopy' if os.name=='nt' else 'rsync'
        if not shutil.which(engine):self.skipTest('optional native tool unavailable')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIn('SHA-256',m.probe_copy_engine(engine))
        self.assertEqual(m._COPY_ENGINE,'auto')


if __name__=='__main__':unittest.main()
