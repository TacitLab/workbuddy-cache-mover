"""开发用固定小文件基准；全部在临时目录内，不触碰真实 WorkBuddy。"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-ref', default='168a3697c09c4b10adb40516cffe43540745d0e5')
    parser.add_argument('--count', type=int, default=1000 if os.name == 'nt' else 8000)
    parser.add_argument('--output')
    args = parser.parse_args()
    baseline_code = subprocess.check_output(['git', 'show', args.baseline_ref + ':scripts/cache_mover.py'])
    results = []
    with tempfile.TemporaryDirectory(prefix='wb-io-bench-') as td:
        root = Path(td); source = root/'source'; source.mkdir()
        old_path = root/'baseline.py'; old_path.write_bytes(baseline_code)
        old = load(old_path, 'io_old'); new = load(Path(__file__).with_name('cache_mover.py'), 'io_new')
        data = bytes(range(256)) * 48
        for index in range(args.count):
            parent = source/('dir-%03d' % (index//100)); parent.mkdir(exist_ok=True)
            (parent/('file-%05d.bin' % index)).write_bytes(data)
        for index in range(4): (source/('large-%d.bin'%index)).write_bytes(data*342)
        methods = {'old': (old, 1), 'pipeline1': (new, 1), 'pipeline4': (new, 4)}
        for round_no, order in enumerate((('old','pipeline1','pipeline4'), ('pipeline4','pipeline1','old')), 1):
            for name in order:
                module, workers = methods[name]; module._IO_WORKERS = workers; module._ACTIVE = None
                target = root/('%s-%d' % (name, round_no))
                start = time.perf_counter()
                with contextlib.redirect_stdout(io.StringIO()):
                    module.run_copy(str(source), str(target))
                    assert module.verify_strict(str(source), str(target)), 'copy verification failed'
                row = dict(method=name, round=round_no, seconds=time.perf_counter()-start)
                results.append(row); print(json.dumps(row), flush=True)
        summary = dict(files=args.count+4, bytes=(args.count+4*342)*len(data), rounds=results,
                       averages={name:statistics.mean(row['seconds'] for row in results if row['method']==name) for name in methods},
                       limitation='临时目录、同卷热缓存；不代表用户 Windows 跨盘速度；保留 fsync 和完整副本校验。')
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if args.output: Path(args.output).write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__': main()
