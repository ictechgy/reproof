import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from reproof.repair import CommandError,run_command


class ProcessBoundaryTests(unittest.TestCase):
    def test_timeout_reaps_descendant_that_ignores_term(self):
        with tempfile.TemporaryDirectory() as directory:
            ready=Path(directory)/'child-ready'
            child_code=('import os,signal,time; from pathlib import Path; '
                        'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                        f'Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(30)')
            parent_code=f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child_code!r}]); time.sleep(30)'
            child=None
            try:
                with self.assertRaises(CommandError):
                    run_command([sys.executable,'-c',parent_code],directory,timeout=1)
                self.assertTrue(ready.exists(),'child must have started for this regression')
                child=int(ready.read_text())
                for _ in range(20):
                    try:os.kill(child,0)
                    except ProcessLookupError:break
                    time.sleep(.05)
                else:self.fail('descendant survived the timed-out command group')
            finally:
                if ready.exists():
                    child=int(ready.read_text())
                    try:os.kill(child,signal.SIGKILL)
                    except ProcessLookupError:pass

if __name__=='__main__':unittest.main()
