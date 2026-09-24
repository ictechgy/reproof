import unittest
from pathlib import Path
import plistlib
from types import SimpleNamespace
from unittest.mock import patch, Mock
from reproof.live.iphone import device_from_details,public_device_status,validate_tunnel_address
from reproof.live.model import LiveError


class IphoneDiscoveryTests(unittest.TestCase):
    def details(self,**changes):
        value={'identifier':'11111111-2222-3333-4444-555555555555',
          'hardwareProperties':{'udid':'00000000-0000000000000000','deviceType':'iPhone','platform':'iOS','marketingName':'iPhone Test'},
          'deviceProperties':{'name':'PRIVATE OWNER PHONE','developerModeStatus':'enabled','ddiServicesAvailable':True,'osVersionNumber':'27.0'},
          'connectionProperties':{'transportType':'wired','tunnelState':'connected','tunnelIPAddress':'fd00::2','pairingState':'paired'}}
        value.update(changes);return value
    def test_public_discovery_does_not_expose_personal_device_name_or_identifiers(self):
        device=device_from_details(self.details());value=public_device_status(device)
        self.assertTrue(value['ready']);self.assertEqual(value['platform'],'ios-physical')
        self.assertNotIn('PRIVATE OWNER',str(value));self.assertNotIn('00000000-0000000000000000',str(value));self.assertNotIn('fd00::2',str(value))
    def test_missing_pairing_developer_mode_or_usb_tunnel_is_not_ready(self):
        for section,key,bad in [('connectionProperties','pairingState','unpaired'),('connectionProperties','tunnelState','disconnected'),('connectionProperties','transportType','localNetwork'),('deviceProperties','developerModeStatus','disabled')]:
            data=self.details();data[section][key]=bad
            self.assertFalse(public_device_status(device_from_details(data))['ready'])
    def test_bridge_rejects_wildcard_public_lan_and_loopback_addresses(self):
        self.assertEqual(validate_tunnel_address('fd00::2'),'fd00::2')
        for address in ['::','::1','2001:4860:4860::8888','192.168.1.2','127.0.0.1','example.com']:
            with self.subTest(address=address):
                with self.assertRaises(LiveError):validate_tunnel_address(address)
    def test_start_refreshes_tunnel_after_install_before_launching_helper(self):
        from reproof.live.iphone import PhysicalIosProvider
        old=device_from_details(self.details())
        details=self.details();details['connectionProperties']['tunnelIPAddress']='fd00::3'
        fresh=device_from_details(details)
        identity={'bundle':'io.reproof.sample.ios','artifactDigest':'a'*64}
        provider=PhysicalIosProvider(old,Path('products'),Path('sample.app'),identity)
        def config(products,target,path):
            path.write_bytes(plistlib.dumps({'__xctestrun_metadata__':{'FormatVersion':1},
                'ReproLiveTests':{'BlueprintName':'ReproLiveTests'}}))
            return path
        process=Mock(returncode=0)
        with patch('reproof.live.iphone.Lease'),patch('reproof.live.iphone.validate_signed_products',return_value=identity),\
             patch('reproof.live.iphone._devicectl') as install,patch('reproof.live.iphone.select_iphone',return_value=fresh) as refresh,\
             patch('reproof.live.iphone.prepare_xctestrun',side_effect=config),\
             patch('reproof.live.iphone.subprocess.Popen',return_value=process),patch('reproof.live.iphone.threading.Thread'):
            provider.start({'id':'sample-session'},Mock())
            try:
                self.assertTrue(install.called)
                refresh.assert_called_once_with(old.public_id)
                self.assertEqual(provider.transport.address,'fd00::3')
                document=plistlib.loads((Path(provider.temp.name)/'live.xctestrun').read_bytes())
                self.assertEqual(document['ReproLiveTests']['EnvironmentVariables']['REPRO_LIVE_LISTEN_HOST'],'fd00::3')
            finally:provider.close()

if __name__=='__main__':unittest.main()
