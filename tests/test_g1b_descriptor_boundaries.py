"""Physical providers cannot opt out by omitting canonical authority metadata."""

from pathlib import Path
import tempfile
import unittest

from reproof.core import ContractError
from reproof.live.model import Lab, LiveError


class StartupProbe:
    def __init__(self):
        self.starts = 0

    def start(self, session, lab):
        self.starts += 1
        lab.publish_frame(session['id'], b'<svg/>', 'image/svg+xml', 400, 800)

    def close(self):
        pass


class DescriptorAuthorityBoundaries(unittest.TestCase):
    def _open(self, kind):
        provider = StartupProbe()
        descriptor = {
            'id': 'unbound-provider', 'name': 'Synthetic startup probe',
            'kind': kind, 'platform': 'android' if kind != 'demo' else 'demo',
            'capabilities': {'actions': ['tap'], 'media': 'demo-svg'},
            'factory': lambda: provider,
        }
        with tempfile.TemporaryDirectory(prefix='g1b-descriptor-probe-') as directory:
            lab = None
            try:
                lab = Lab([descriptor], Path(directory))
                try:
                    lab.create_session(descriptor['id'], 'owner', 'controller')
                except (LiveError, ContractError):
                    pass
            except (LiveError, ContractError):
                pass
            finally:
                if lab is not None:
                    lab.close_all()
        return provider.starts

    def test_native_descriptor_without_authority_is_denied_before_startup(self):
        self.assertEqual(self._open('android-live'), 0)

    def test_synthetic_descriptor_remains_usable(self):
        self.assertEqual(self._open('demo'), 1)


if __name__ == '__main__':
    unittest.main()
