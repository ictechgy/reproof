"""Fixed preinstalled qualification harness. It accepts no candidate payload."""
from .artifacts import BlobSet, read_regular
from .guest import GuestError, INSTALL_ROOT, assert_guest
from .wire import PROBE_MODES


class GuestProbe:
    def prepare(self, mode):
        assert_guest()
        if mode not in PROBE_MODES:
            raise GuestError("Unknown guest probe")
        source = BlobSet((("probe.py", read_regular(INSTALL_ROOT, "probe.py", maximum=64 * 1024)),))
        recipe = {"id": "qualification-probe", "executionClass": "build-guest",
                  "argv": [str(INSTALL_ROOT / "python/bin/python3"), "-I", "probe.py", mode],
                  "artifactPolicyId": "probe-output", "cleanupPolicyId": "dispose-overlay",
                  "outputPaths": ["probe.json"], "maxOutputBytes": 4096, "timeoutMs": 30000}
        return source, recipe
