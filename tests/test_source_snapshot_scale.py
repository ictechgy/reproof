"""Real large synthetic local byte round trips; no native execution or source upload."""
import hashlib
from pathlib import Path
import tempfile
import time
import tracemalloc
import unittest


class SourceSnapshotScaleTests(unittest.TestCase):
    def test_enterprise_sized_sources_have_bounded_python_memory(self):
        from reproof.source_snapshot import SnapshotBudget, capture, verify, materialize
        block = b'synthetic source bytes only\n'
        raw = (block * (4096//len(block)+1))[:4096]
        expected = hashlib.sha256(raw).hexdigest()
        for count in (22000,32768):
            with self.subTest(files=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve();source = root/'source';source.mkdir()
                names = [f'part{index//256:03d}/file{index:05d}.swift' for index in range(count)]
                for name in names:
                    path = source/name;path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
                budget = SnapshotBudget(max_files=32768,max_bytes=256*1024*1024)
                snapshot = root/'snapshot';output = root/'output'
                started = time.monotonic()
                tracemalloc.start()
                try:
                    captured = capture(source,names,snapshot,budgets=budget)
                    verified = verify(snapshot,budgets=budget)
                    restored = materialize(snapshot,output,budgets=budget)
                    _,peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertEqual(captured['digest'],verified['digest'])
                self.assertEqual(captured['digest'],restored['digest'])
                # Bound Python allocations, not OS filesystem cache or process RSS.
                self.assertLess(peak,96*1024*1024)
                self.assertGreater(count*len(raw),64*1024*1024)
                self.assertEqual(sum(1 for path in output.rglob('*') if path.is_file()),count)
                for name in names:
                    self.assertEqual(hashlib.sha256((output/name).read_bytes()).hexdigest(),expected)
                print(f'SOURCE_SNAPSHOT_SCALE files={count} bytes={count*len(raw)} '
                      f'pythonPeakBytes={peak} elapsedSeconds={time.monotonic()-started:.3f}',flush=True)


if __name__ == '__main__':unittest.main()
