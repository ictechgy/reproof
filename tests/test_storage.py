import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from reproof.core import ContractError
from reproof.storage import create_bundle, load_bundle, Lease, read_json
from tests.test_core import capture, oracle


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.apk = self.root / 'sample.apk'; self.apk.write_bytes(b'fixture-apk')
    def tearDown(self): self.temp.cleanup()

    def test_bundle_integrity_and_scenario_roundtrip(self):
        b = create_bundle(capture(), oracle(), self.apk, self.root/'bundle')
        self.assertEqual(load_bundle(b['path'])['scenario'], b['scenario'])
        (b['path']/'capture.json').write_text('{}')
        with self.assertRaises(ContractError): load_bundle(b['path'])

    def test_linked_apk_and_traversal_manifest_rejected(self):
        b=create_bundle(capture(),oracle(),self.apk,self.root/'bundle')
        f=b['path']/'original.apk'; f.unlink(); f.symlink_to(self.apk)
        with self.assertRaises(ContractError): load_bundle(b['path'])
        m=b['manifest']; m['files']['../outside']=m['files'].pop('original.apk')
        (b['path']/'manifest.json').write_text(json.dumps(m))
        with self.assertRaises(ContractError):load_bundle(b['path'])

    def test_duplicate_json_keys_rejected(self):
        f=self.root/'bad.json';f.write_text('{"a":1,"a":2}')
        with self.assertRaises(ContractError):read_json(f)

    def test_exclusive_lease_releases_on_exception(self):
        directory=self.root/'leases'
        with self.assertRaisesRegex(RuntimeError,'test'):
            with Lease('synthetic-device',directory):
                with self.assertRaises(ContractError):
                    with Lease('synthetic-device',directory):pass
                raise RuntimeError('test')
        with Lease('synthetic-device',directory):pass

    def test_lease_refuses_linked_lock_and_boolean_cutover_version(self):
        directory=self.root/'strict-leases';directory.mkdir()
        serial='strict-device';key=hashlib.sha256(serial.encode()).hexdigest()
        victim=self.root/'preserved';victim.write_bytes(b'preserve me')
        (directory/(key+'.lock')).symlink_to(victim)
        with self.assertRaises((ContractError,OSError)):
            with Lease(serial,directory):pass
        self.assertEqual(victim.read_bytes(),b'preserve me')
        (directory/(key+'.lock')).unlink()
        with Lease(serial,directory,authority_root='a'*64) as lease:
            lease.mark_authority('shared')
        marker=directory/(key+'.authority.json')
        document=json.loads(marker.read_text());document['version']=True
        marker.write_text(json.dumps(document))
        with self.assertRaises(ContractError):
            with Lease(serial,directory,authority_root='a'*64):pass

if __name__=='__main__':unittest.main()
