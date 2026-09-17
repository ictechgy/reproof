"""Trusted disposable-guest attack fixture; refuses host execution."""
import ctypes
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time


def inside_guest():
    if platform.system() != "Darwin" or os.getuid() == 0:
        return False
    flag, length = ctypes.c_int(0), ctypes.c_size_t(4)
    probe = ctypes.CDLL(None, use_errno=True).sysctlbyname
    probe.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    return probe(b"kern.hv_vmm_present", ctypes.byref(flag), ctypes.byref(length), None, 0) == 0 and flag.value == 1


def denied_write(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    except PermissionError:
        return True
    except OSError:
        return False  # Missing/misconfigured resources do not count as protected.
    else:
        os.close(fd)
        return False


def main():
    if not inside_guest() or len(sys.argv) != 2 or sys.argv[1] not in (
            "containment", "hold", "oversize", "forged-report", "child"):
        print("guest-probe-rejected")
        return 2
    mode = sys.argv[1]
    if mode == "child":
        while True:
            time.sleep(1)
    child = subprocess.Popen([sys.executable, "-I", str(Path(__file__).absolute()), "child"],
                             start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.1)
    detached = child.poll() is None
    if not detached:
        return 2
    # The root agent emits the protocol acknowledgement only after this fixed
    # preinstalled harness observes a live child in a separate process group.
    print("repro-probe-running", flush=True)
    if mode == "hold":
        while True:
            time.sleep(1)
    target = Path(os.environ["REPROLOOP_OUTPUT_DIR"]) / "probe.json"
    if mode == "oversize":
        target.write_bytes(b"x" * 8192)
        return 0
    if mode == "forged-report":
        target.write_text('{"verified":true,"passed":true}', encoding="utf-8")
        return 0
    denied = []
    for family, address in ((socket.AF_INET, ("203.0.113.1", 443)),
                            (socket.AF_INET6, ("2001:db8::1", 443, 0, 0))):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.3)
                connection.connect(address)
            denied.append(False)
        except OSError:
            denied.append(True)
    proof = {"schemaVersion": 1, "mode": mode, "networkDenied": all(denied),
             "agentWriteDenied": denied_write("/Library/ReproLoopGuest/policy.json"),
             "toolchainWriteDenied": denied_write(sys.executable), "detachedChildStarted": detached}
    target.write_text(json.dumps(proof, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("guest-probe-rejected")
        sys.exit(2)
