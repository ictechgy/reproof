"""Root launchd entry point, installed at /Library/ReproofGuest/main.py."""
import ctypes
import os
from pathlib import Path
import socket
import sys
import time

INSTALL_ROOT = Path("/Library/ReproofGuest")
sys.dont_write_bytecode = True
sys.path.insert(0, str(INSTALL_ROOT))

try:
    from reproof.execution.guest import GuestExecutor, assert_guest, serve_one
    from reproof.execution.guest_installation import verify_package
    from reproof.execution.guest_probe import GuestProbe
    from reproof.execution.wire import accept_bootstrap
except Exception:
    print("guest-agent-rejected")
    sys.exit(2)


def main():
    try:
        assert_guest()
        if len(sys.argv) != 3 or sys.argv[1] != "--channel-fd" or not sys.argv[2].isdigit():
            raise ValueError()
        descriptor = int(sys.argv[2])
        if not 3 <= descriptor <= 1000000:
            raise ValueError()
        # Darwin sockaddr_vm has length/family bytes followed by reserved/port/CID.
        address, size = ctypes.create_string_buffer(128), ctypes.c_uint32(128)
        getpeername = ctypes.CDLL(None, use_errno=True).getpeername
        getpeername.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        if getpeername(descriptor, address, ctypes.byref(size)) != 0:
            raise ValueError()
        if size.value < 12 or address.raw[1] != 40 or int.from_bytes(address.raw[8:12], sys.byteorder) != 2:
            raise ValueError()
        policy = verify_package(INSTALL_ROOT, root_owned=True)
        channel = socket.socket(family=40, type=socket.SOCK_STREAM, fileno=descriptor)
        with channel:
            # The host imposes the tighter per-operation deadline in both Python
            # and the native VZ owner; the trusted recipe has its own guest limit.
            wire = accept_bootstrap(channel, deadline=time.monotonic() + 86400)
            executor = GuestExecutor(uid=policy["uid"], gid=policy["gid"])
            serve_one(wire, catalog=policy["catalog"], agent_digest=policy["agentDigest"],
                      executor=executor, probe=GuestProbe())
        return 0
    except Exception:
        # No request, key, path, file content, or exception text reaches launchd.
        print("guest-agent-rejected")
        return 2


if __name__ == "__main__":
    sys.exit(main())
