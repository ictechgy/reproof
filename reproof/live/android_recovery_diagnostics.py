"""Private bounded stream diagnostics. Public summaries never contain raw text."""
import hashlib
import re
import threading


_PATTERNS = (
    ('unsupported-option', re.compile(br'(?:unknown|unrecognized) (?:feature|option)', re.I)),
    ('permission-denied', re.compile(br'operation not permitted|permission denied|\b(?:EPERM|EACCES)\b', re.I)),
    ('fatal', re.compile(br'\b(?:panic|fatal)\b', re.I)),
    ('error', re.compile(br'\berror\b', re.I)),
    ('warning', re.compile(br'\bwarn(?:ing)?\b', re.I)),
)


class IncrementalCollector:
    """Read available pipe bytes, not a buffered request for a full 8 KiB."""
    def __init__(self, stream, maximum=65536):
        self.stream, self.maximum = stream, maximum
        self.data = bytearray()
        self.oversized = False
        self.read_failed = False
        self._lock = threading.Lock()
        self._tail = b''
        self._codes = set()
        self.alert = threading.Event()
        self.thread = threading.Thread(target=self._read, name='reproof-private-output', daemon=True)

    def _read(self):
        try:
            # Popen PIPE is a BufferedReader. read1 performs at most one raw
            # read, so a short warning is visible while its writer stays alive.
            reader = self.stream.read1
            while True:
                chunk = reader(8192)
                if not chunk:
                    return
                with self._lock:
                    if len(self.data) < self.maximum+1:
                        self.data.extend(chunk[:self.maximum+1-len(self.data)])
                    self.oversized = len(self.data) > self.maximum
                    scan = self._tail+chunk
                    self._codes.update(code for code, pattern in _PATTERNS if pattern.search(scan))
                    self._tail = scan[-64:]
                    if self._codes or self.oversized:
                        self.alert.set()
        except (OSError, ValueError, AttributeError):
            with self._lock:
                self.read_failed = self.oversized = True
                self.alert.set()
        finally:
            try:
                self.stream.close()
            except (OSError, ValueError):
                pass

    def snapshot(self):
        with self._lock:
            raw = bytes(self.data)
            return {'bytesCaptured': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                    'truncated': self.oversized, 'readFailed': self.read_failed,
                    'classifications': sorted(self._codes),
                    'complete': (self.thread.ident is not None and not self.thread.is_alive()
                                 and not self.read_failed and not self.oversized)}


def child_summary(child):
    streams = [collector.snapshot() for collector in getattr(child, 'collectors', ())[:2]
               if type(collector) is IncrementalCollector]
    process = getattr(child, 'process', None)
    code = getattr(process, 'returncode', None)
    code = code if type(code) is int and -4096 <= code <= 4096 else None
    role = getattr(child, '_diagnostic_role', 'other')
    return {'role': role if role in ('server','emulator','initial-install') else 'other',
            'returncode': code, 'streams': streams}


def output_alert(children):
    return any(collector.alert.is_set() for child in children
               for collector in getattr(child, 'collectors', ())
               if type(collector) is IncrementalCollector)


class OutputCancellation:
    """Read-only composite; never changes the caller's cancellation event."""
    def __init__(self, cancellation, children):
        self.cancellation, self.children = cancellation, children

    def is_set(self):
        return self.cancellation.is_set() or output_alert(self.children)


def inventory_state(raw, serial):
    wanted = serial.encode('ascii')
    if raw == b'':
        return 'empty'
    if raw == wanted+b'\tdevice\n':
        return 'selected-device'
    if raw == wanted+b'\toffline\n':
        return 'selected-offline'
    return 'unexpected'


def birth_summary(owner):
    # Only enums/counters and hashes. Never a regex-redacted raw line: keys,
    # certificates, paths, serials and unknown future log formats cannot escape.
    inventory = getattr(owner, '_inventory_state', 'not-observed')
    boot = getattr(owner, '_boot_state', 'not-queried')
    stage = getattr(owner, '_stage', 'unknown')
    query = getattr(owner, '_inventory_query', 'not-started')
    digest = getattr(owner, '_inventory_digest', None)
    count = getattr(owner, '_inventory_bytes', None)
    return {'schemaVersion': 1, 'kind': 'reproof-private-birth-diagnostics',
            'stage': stage if stage in ('prepare','server-start','emulator-start','callbacks',
                'inventory','identity','install-sample','install-helper','ready') else 'unknown',
            'inventoryQuery': query if query in ('not-started','pending','failed','returned') else 'unknown',
            'inventoryState': inventory if inventory in
                ('not-observed','empty','selected-device','selected-offline','unexpected') else 'unexpected',
            'inventorySha256': digest if type(digest) is str and re.fullmatch('[0-9a-f]{64}', digest) else None,
            'inventoryBytes': count if type(count) is int and 0 <= count <= 65536 else None,
            'inventoryPolls': min(10000, max(0, getattr(owner, '_inventory_polls', 0))),
            'bootState': boot if boot in ('not-queried','pending','query-failed','not-complete','complete','unexpected') else 'unexpected',
            'outputAlert': output_alert(getattr(owner, '_children', ())),
            'processes': [child_summary(child) for child in getattr(owner, '_children', ())[:4]],
            'causeConfirmed': False}
