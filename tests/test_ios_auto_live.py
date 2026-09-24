from pathlib import Path
import plistlib
import tempfile
import unittest

from reproof.core import digest
from reproof.ios_storage import tree_manifest
from reproof.live.model import Lab, LiveError
from reproof.live.providers import demo_device
from reproof.live.repair_jobs import LiveRepairJobs
from reproof.storage import write_json


class SimulatorRepairProjectTests(unittest.TestCase):
    def test_simulator_project_accepts_matching_build_and_rejects_environment_relabel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'Fixture.swift').write_text('let fixture = true\n')
            build = root / 'build'
            products = build / 'DerivedData/Build/Products'
            app = products / 'Debug-iphonesimulator/ReproSample.app'
            app.mkdir(parents=True)
            (app / 'Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': 'io.reproof.sample.ios', 'ReproBuildID': 'fixture-build'}))
            receipt = {'executionEnvironment': 'simulator', 'buildCompleted': True,
                'buildId': 'fixture-build', 'sourceDigest': digest(tree_manifest(source, True)),
                'productsDigest': digest(tree_manifest(products)), 'appRelative': 'Debug-iphonesimulator/ReproSample.app'}
            write_json(build / 'receipt.json', receipt)
            lab = Lab([demo_device()], root / 'live')
            repairs = None
            try:
                repairs = LiveRepairJobs(lab, source, build)
                self.assertEqual(repairs._validate_project()[0]['executionEnvironment'], 'simulator')
                receipt.update(executionEnvironment='physical-iphone', signed=True)
                write_json(build / 'receipt.json', receipt)
                with self.assertRaises(LiveError):repairs._validate_project()
            finally:
                if repairs:repairs.close()
                lab.close_all()


if __name__ == '__main__':unittest.main()
