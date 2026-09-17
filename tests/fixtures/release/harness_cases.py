"""Tiny, explicitly selected child processes for release-runner fault tests."""
import time
import unittest


class Cases(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(True)

    def test_output_limit(self):
        print("x" * 300000, flush=True)

    def test_timeout(self):
        time.sleep(10)


class Empty(unittest.TestCase):
    pass
