"""Execute the production retry state machine without needing a Simulator."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class IOSAppLogRetryTests(unittest.TestCase):
    def test_events_and_status_changes_each_reopen_one_bounded_retry_burst(self):
        source=(ROOT/'reproof/ios_instrumentation_templates/RLAutomaticRecorder.swift').read_text()
        self.assertTrue('struct RLAppLogRetryPolicy' in source, 'Production retry policy is missing')
        start=source.index('struct RLAppLogRetryPolicy');opening=source.index('{',start);end=opening+1;depth=1
        while depth:
            depth += (source[end]=='{')-(source[end]=='}');end+=1
        policy=source[start:end]
        program=policy+'''
var retry = RLAppLogRetryPolicy()
retry.changed()
for _ in 0..<3 {
    precondition(retry.canSchedule())
    retry.beginAttempt()
    retry.finished(success: false)
}
precondition(!retry.canSchedule())
// A loss/truncation flag is a new revision even when event count cannot grow.
retry.changed()
precondition(retry.canSchedule())
retry.beginAttempt()
retry.finished(success: true)
precondition(retry.failures == 0)
for _ in 0..<3 {
    precondition(retry.canSchedule())
    retry.beginAttempt()
    retry.finished(success: false)
}
precondition(!retry.canSchedule())
retry.changed()
precondition(retry.canSchedule())
retry.beginAttempt()
retry.finished(success: true)
print("retry policy passed")
'''
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'main.swift';path.write_text(program)
            result=subprocess.run(['/usr/bin/xcrun','swift',str(path)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('retry policy passed',result.stdout)

if __name__=='__main__':unittest.main()
