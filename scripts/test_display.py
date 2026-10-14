"""动态面板、日志与传统终端显示的兼容检查。"""
import contextlib
import importlib.util
import io
import os
import threading
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location('display_mover',os.path.join(os.path.dirname(__file__),'cache_mover.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Terminal(io.StringIO):
    def isatty(self): return True

class DisplayTests(unittest.TestCase):
    def test_panel_redraws_in_place_and_logs_no_control_sequences(self):
        console,log=Terminal(),io.StringIO()
        with mock.patch.object(m,'enable_terminal',return_value=True):stream=m._LogStream(console,log,threading.Lock())
        for i in range(5):stream.progress('复制 %d'%i,lines=['阶段','进度','速度'])
        stream.progress('阶段完成',lines=['完成','100%','耗时'],final=True)
        self.assertIn('\x1b[3A',console.getvalue())
        self.assertNotIn('\x1b',log.getvalue());self.assertNotIn('\r',log.getvalue())
        self.assertEqual(log.getvalue().count('\n'),6)
        self.assertFalse(stream.progress_visible)
    def test_warning_clears_panel_and_is_preserved(self):
        console,log=Terminal(),io.StringIO()
        with mock.patch.object(m,'enable_terminal',return_value=True):stream=m._LogStream(console,log,threading.Lock())
        stream.progress('复制中',lines=['1','2','3']);stream.write('提醒: 原目录保留\n')
        self.assertFalse(stream.progress_visible);self.assertEqual(stream.progress_rows,0)
        self.assertTrue(console.getvalue().endswith('提醒: 原目录保留\n'))
    def test_plain_output_never_contains_cursor_controls(self):
        console,log=Terminal(),io.StringIO()
        stream=m._LogStream(console,log,threading.Lock())
        with mock.patch.object(m,'_DISPLAY','plain'):
            stream.progress('开始',force=True,lines=['1','2','3'])
            stream.progress('中间',lines=['1','2','3'])
            stream.progress('结束',final=True,lines=['1','2','3'])
        self.assertEqual(console.getvalue(),'开始\n结束\n')
    def test_no_color_preserves_cursor_updates_without_color(self):
        console,log=Terminal(),io.StringIO()
        with mock.patch.object(m,'enable_terminal',return_value=True):stream=m._LogStream(console,log,threading.Lock())
        with mock.patch.dict(os.environ,{'NO_COLOR':'1'}):stream.progress('复制中',lines=['1','2','3'])
        self.assertNotIn('\x1b[36m',console.getvalue());self.assertIn('1\n2\n3\n',console.getvalue())
    def test_panels_fit_narrow_terminal_and_unknown_progress_is_honest(self):
        with mock.patch.object(m.shutil,'get_terminal_size',return_value=os.terminal_size((40,24))):
            lines=m.progress_panel('[2/6] 复制并计算源摘要',30,400,1000,1000,2000,.5,30,33,False,False)
            self.assertTrue(all(m.cell_width(line)<=38 for line in lines))
            unknown=m.progress_panel('扫描目录',10,40,0,None,None,None,0,None,False,False)
        self.assertNotIn('%',''.join(unknown));self.assertNotIn('预计剩余',''.join(unknown))
    def test_completed_wait_does_not_keep_showing_waiting(self):
        lines=m.progress_panel('退出应用',15,0,0,None,None,None,0,None,True,False)
        self.assertNotIn('等待完成',''.join(lines));self.assertNotIn('正在处理',''.join(lines))
        self.assertIn('本阶段已完成',''.join(lines))

    def test_exception_marks_progress_stopped(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(OSError):
                with m.Progress('复制',100,10):raise OSError('disk failure')
        self.assertIn('已停止',output.getvalue())
        self.assertNotIn('阶段完成',output.getvalue())

if __name__=='__main__':unittest.main()
