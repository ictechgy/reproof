"""Tiny, explicitly selected child processes for release-runner fault tests."""
import os
import time
import unittest


class Cases(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(True)

    def test_output_limit(self):
        print("x" * 300000, flush=True)

    @unittest.skip("synthetic unavailable prerequisite")
    def test_skipped(self):
        self.fail("must not execute")

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.fail("synthetic known defect")

    def test_missing_verdict(self):
        print("Ran 1 test in 0.001s", flush=True)
        os._exit(0)

    def test_malformed_verdict(self):
        print("Ran 1 test in 0.001s\n\nOK (unknown=1)", flush=True)
        os._exit(0)

    def test_count_above_bound(self):
        print("Ran 1000000000 tests in 0.001s\n\nOK", flush=True)
        os._exit(0)

    def test_oversized_count(self):
        print("Ran " + "9" * 5000 + " tests in 0.001s\n\nOK", flush=True)
        os._exit(0)

    def test_stdout(self):
        print("ordinary buffered test output")

    def test_timeout(self):
        time.sleep(10)


class Empty(unittest.TestCase):
    pass


class Partial(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(True)

    @unittest.skip("synthetic unavailable prerequisite")
    def test_skipped(self):
        self.fail("must not execute")
