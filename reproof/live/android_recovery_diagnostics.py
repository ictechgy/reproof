"""Private bounded stream diagnostics. Public summaries never contain raw text."""
import hashlib
import re
import threading
import time


_PATTERNS = (
    ('jdwp-unavailable', re.compile(re.escape(b'jdwp port creation fails, Icebox will not work.'))),
    ('unsupported-feature', re.compile(re.escape(
        b'Please update the emulator to one that supports the feature(s):'))),
    ('unsupported-option', re.compile(br'(?:unknown|unrecognized) (?:feature|option)', re.I)),
    ('permission-denied', re.compile(br'operation not permitted|permission denied|\b(?:EPERM|EACCES)\b', re.I)),
    ('fatal', re.compile(br'\b(?:panic|fatal)\b', re.I)),
    ('error', re.compile(br'\berror\b', re.I)),
    ('warning', re.compile(br'\bwarn(?:ing)?\b', re.I)),
)


# Exact public SDK diagnostic, matched locally against complete retained output.
# This identifies one advisory; it is not a claim of runtime compatibility.
_VULKAN_ADVISORY = (b'WARNING      | Please update the emulator to one that supports '
                    b'the feature(s): VulkanVirtualQueue\n')
_JDWP_ADVISORY = b'WARNING      | jdwp port creation fails, Icebox will not work.\n'
_ADVISORY_PREFIX_SECONDS = 0.25


class IncrementalCollector:
    """Read available pipe bytes, not a buffered request for a full 8 KiB."""
    def __init__(self, stream, maximum=65536, *, evidence=None, _vulkan_advisory=False, _jdwp_advisory=False):
        if type(_vulkan_advisory) is not bool or type(_jdwp_advisory) is not bool:
            raise ValueError('Private advisory option must be boolean')
        self.stream, self.maximum = stream, maximum
        self._evidence = evidence
        self.evidence_failed = False
        self.data = bytearray()
        self.oversized = False
        self.read_failed = False
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._advisory_enabled = _vulkan_advisory
        self._advisory_count = 0
        self._jdwp_enabled = _jdwp_advisory
        self._jdwp_count = 0
        self._candidate = b''
        self._candidate_deadline = None
        self._line_start = True
        self._watchdog_stop = False
        self._watchdog = (threading.Thread(target=self._watch_prefix,
            name='reproof-private-advisory-deadline', daemon=True) if _vulkan_advisory or _jdwp_advisory else None)
        self._tail = b''
        self._codes = set()
        self.alert = threading.Event()
        self.thread = threading.Thread(target=self._read, name='reproof-private-output', daemon=True)

    def _scan(self, chunk):
        scan = self._tail+chunk
        self._codes.update(code for code, pattern in _PATTERNS if pattern.search(scan))
        self._tail = scan[-64:]
        if self._codes or self.oversized:
            self.alert.set()

    def _reject_candidate(self):
        # Called with the collector lock held. Never clear an existing alert.
        self._scan(self._candidate)
        self._codes.add('advisory-incomplete')
        self.alert.set()
        self._candidate = b''
        self._candidate_deadline = None
        self._condition.notify_all()

    def _watch_prefix(self):
        with self._condition:
            while not self._watchdog_stop:
                if self._candidate_deadline is None:
                    self._condition.wait()
                else:
                    remaining = self._candidate_deadline-time.monotonic()
                    if remaining <= 0:
                        self._reject_candidate()
                    else:
                        self._condition.wait(remaining)

    def _remaining_advisories(self):
        # Fixed, independently requested allowances; never caller-supplied text.
        return tuple(line for line, enabled, count in (
            (_VULKAN_ADVISORY, self._advisory_enabled, self._advisory_count),
            (_JDWP_ADVISORY, self._jdwp_enabled, self._jdwp_count)) if enabled and not count)

    def _classify(self, chunk):
        if not self._remaining_advisories() or self.alert.is_set():
            self._scan(chunk)
            return
        # Only exact line-prefix candidates are held, never arbitrary output.
        # Other bytes are scanned immediately. A watchdog bounds a stalled writer.
        for value in chunk:
            byte = bytes((value,))
            if self._candidate_deadline is not None and time.monotonic() >= self._candidate_deadline:
                self._reject_candidate()
            if self.alert.is_set() or not self._remaining_advisories():
                self._scan(byte)
                continue
            if self._candidate:
                candidate = self._candidate+byte
                if not any(line.startswith(candidate) for line in self._remaining_advisories()):
                    if self._candidate.startswith(b'WARNING'):
                        self._codes.add('advisory-mismatch')
                        self.alert.set()
                    self._candidate = b''
                    self._candidate_deadline = None
                    self._condition.notify_all()
                    self._scan(candidate)
                    self._line_start = value == 10
                elif candidate in self._remaining_advisories():
                    self._candidate = b''
                    self._candidate_deadline = None
                    self._condition.notify_all()
                    if candidate == _VULKAN_ADVISORY:
                        self._advisory_count = 1
                    else:
                        self._jdwp_count = 1
                    # Preserve a token boundary without scanning the permitted
                    # warning again through the rolling tail on the next read.
                    self._scan(b'\n')
                    self._line_start = True
                else:
                    self._candidate = candidate
            elif self._line_start and byte == _VULKAN_ADVISORY[:1]:
                self._candidate = byte
                self._candidate_deadline = time.monotonic()+_ADVISORY_PREFIX_SECONDS
                self._line_start = False
                self._condition.notify_all()
            else:
                self._scan(byte)
                self._line_start = value == 10

    def auxiliary_alive(self):
        return self._watchdog is not None and self._watchdog.is_alive()

    def _read(self):
        try:
            if self._watchdog is not None:
                self._watchdog.start()
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
                    if self.oversized:
                        self.alert.set()
                    self._classify(chunk)
                # Classify/alert before disk I/O so a slow filesystem cannot
                # delay observation of a warning. Only the bounded prefix persists.
                if self._evidence is not None and not self._evidence.append(chunk):
                    self.evidence_failed = True
                    self.alert.set()
        except (OSError, ValueError, AttributeError, RuntimeError):
            with self._lock:
                self.read_failed = self.oversized = True
                self.alert.set()
        finally:
            with self._condition:
                if self._candidate:
                    self._reject_candidate()
                self._watchdog_stop = True
                self._condition.notify_all()
            if self._watchdog is not None and self._watchdog.ident is not None:
                self._watchdog.join(1)
                if self._watchdog.is_alive():
                    self.alert.set()
            if self._evidence is not None and not self._evidence.finish():
                self.evidence_failed = True
                self.alert.set()
            try:
                self.stream.close()
            except (OSError, ValueError):
                pass

    def snapshot(self):
        with self._lock:
            raw = bytes(self.data)
            return {'bytesCaptured': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                    'truncated': self.oversized, 'readFailed': self.read_failed,
                    'privateEvidence': self._evidence.summary() if self._evidence is not None else None,
                    'classifications': sorted(self._codes),
                    'vulkanAdvisoryEnabled': self._advisory_enabled,
                    'vulkanAdvisoriesAccepted': self._advisory_count,
                    'jdwpAdvisoryEnabled': self._jdwp_enabled,
                    'jdwpAdvisoriesAccepted': self._jdwp_count,
                    'complete': (self.thread.ident is not None and not self.thread.is_alive()
                                 and not self.read_failed and not self.oversized and not self.evidence_failed
                                 and not self.auxiliary_alive())}


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
