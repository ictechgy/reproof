"""Suspend-inclusive host time and conservative cross-clock translation.

Clock mappings are process-local capabilities.  Wire timestamps and a mapping
document are inert until a trusted host composition root records the exchange
through :class:`ClockSynchronizer`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time

from reproof.core import ContractError


MAX_NS = 2 ** 63 - 1
MAX_UNCERTAINTY_NS = 60_000_000_000
MAX_DRIFT_PPM = 1_000_000
DEFAULT_MAX_MAPPING_UNCERTAINTY_NS = 250_000_000
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _identifier(value, field_name):
    _require(type(value) is str and _ID.fullmatch(value) is not None,
             f"Invalid {field_name}")
    return value


def _digest(value, field_name):
    _require(type(value) is str and _DIGEST.fullmatch(value) is not None,
             f"Invalid {field_name}")
    return value


def _integer(value, field_name, low=0, high=MAX_NS):
    _require(type(value) is int and low <= value <= high, f"Invalid {field_name}")
    return value


def _fingerprint(value):
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ContractError("Invalid clock mapping") from None
    return hashlib.sha256(encoded).hexdigest()


def _clock_locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


@dataclass(frozen=True, slots=True)
class ClockReading:
    clock_id: str
    boot_digest: str
    nanoseconds: int
    uncertainty_ns: int = 0

    def __post_init__(self):
        _identifier(self.clock_id, "clock identity")
        _digest(self.boot_digest, "boot incarnation digest")
        _integer(self.nanoseconds, "clock value")
        _integer(self.uncertainty_ns, "clock uncertainty", 0, MAX_UNCERTAINTY_NS)


@dataclass(frozen=True, slots=True)
class TrustedClockSample:
    reading: ClockReading
    _issuer: object = field(repr=False, compare=False)

    @property
    def clock_id(self):
        return self.reading.clock_id

    @property
    def boot_digest(self):
        return self.reading.boot_digest

    @property
    def nanoseconds(self):
        return self.reading.nanoseconds

    @property
    def uncertainty_ns(self):
        return self.reading.uncertainty_ns


@dataclass(frozen=True, slots=True)
class ClockInterval:
    earliest_ns: int
    latest_ns: int

    def __post_init__(self):
        _integer(self.earliest_ns, "translated clock lower bound")
        _integer(self.latest_ns, "translated clock upper bound")
        _require(self.earliest_ns <= self.latest_ns, "Invalid translated clock interval")

    @property
    def uncertainty_ns(self):
        return self.latest_ns - self.earliest_ns


@dataclass(frozen=True, slots=True)
class ClockMapping:
    coordinator_clock_id: str
    host_clock_id: str
    host_boot_digest: str
    coordinator_send_ns: int
    coordinator_receive_ns: int
    host_receive_ns: int
    host_send_ns: int
    offset_lower_ns: int
    offset_upper_ns: int
    max_drift_ppm: int
    mapping_id: str
    _issuer: object = field(repr=False, compare=False)
    measurement: str = "peer-exchange"

    def translate(self, coordinator_ns):
        value = _integer(coordinator_ns, "coordinator clock value")
        horizon = max(
            abs(value - self.coordinator_send_ns),
            abs(value - self.coordinator_receive_ns),
        )
        drift = (horizon * self.max_drift_ppm + 999_999) // 1_000_000
        lower = value + self.offset_lower_ns - drift
        upper = value + self.offset_upper_ns + drift
        _require(0 <= lower <= upper <= MAX_NS, "Clock translation is out of range")
        return ClockInterval(lower, upper)

    def require_compatible(self, sample):
        _require(type(sample) is TrustedClockSample and sample._issuer is self._issuer,
                 "Clock mapping is incompatible")
        reading = sample.reading
        _require(
            reading.clock_id == self.host_clock_id
            and reading.boot_digest == self.host_boot_digest
            and reading.nanoseconds >= max(self.host_send_ns, self.host_receive_ns),
            "Clock mapping is incompatible",
        )
        return reading


class UnavailableClock:
    """Explicitly unavailable clock used to keep authority fail closed."""

    def read(self):
        raise ContractError("Suspend-inclusive clock is unavailable")


class _MachTimebaseInfo(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


def _darwin_sysctl(name, maximum=1024):
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    function = library.sysctlbyname
    function.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    function.restype = ctypes.c_int
    length = ctypes.c_size_t()
    encoded = name.encode("ascii")
    if function(encoded, None, ctypes.byref(length), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "sysctl unavailable")
    if not 0 < length.value <= maximum:
        raise OSError("sysctl result is outside bounds")
    buffer = ctypes.create_string_buffer(length.value)
    if function(encoded, buffer, ctypes.byref(length), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "sysctl unavailable")
    return bytes(buffer.raw[:length.value]).rstrip(b"\x00")


class SuspendInclusiveClock:
    """A monotonic clock that advances while the machine is suspended.

    macOS uses ``mach_continuous_time``.  Linux uses ``CLOCK_BOOTTIME`` when
    present.  Unsupported platforms fail construction instead of silently
    substituting a suspend-exclusive clock.
    """

    def __init__(self):
        try:
            if sys.platform == "darwin":
                self._configure_darwin()
            elif sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
                self._configure_linux()
            else:
                raise OSError("unsupported platform")
        except (AttributeError, OSError, ValueError):
            raise ContractError("Suspend-inclusive clock is unavailable") from None

    def _configure_darwin(self):
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        continuous = library.mach_continuous_time
        continuous.argtypes = []
        continuous.restype = ctypes.c_uint64
        timebase = library.mach_timebase_info
        timebase.argtypes = [ctypes.POINTER(_MachTimebaseInfo)]
        timebase.restype = ctypes.c_int
        info = _MachTimebaseInfo()
        if timebase(ctypes.byref(info)) != 0 or info.numer == 0 or info.denom == 0:
            raise OSError("mach timebase unavailable")
        boot_value = _darwin_sysctl("kern.bootsessionuuid", 256).strip()
        if not boot_value:
            raise OSError("boot identity unavailable")
        self.clock_id = f"mach-continuous-{info.numer}-{info.denom}"
        _identifier(self.clock_id, "clock identity")
        self.boot_digest = hashlib.sha256(b"darwin-boot\0" + boot_value).hexdigest()
        self._continuous = continuous
        self._numer = info.numer
        self._denom = info.denom
        self._uncertainty = (info.numer + info.denom - 1) // info.denom
        self._read_ns = self._read_darwin

    def _read_darwin(self):
        return int(self._continuous()) * self._numer // self._denom

    def _configure_linux(self):
        boot_path = Path("/proc/sys/kernel/random/boot_id")
        boot_value = boot_path.read_bytes()
        if not 0 < len(boot_value) <= 256:
            raise OSError("boot identity unavailable")
        self.clock_id = "clock-boottime"
        self.boot_digest = hashlib.sha256(b"linux-boot\0" + boot_value.strip()).hexdigest()
        self._uncertainty = 1
        self._read_ns = lambda: time.clock_gettime_ns(time.CLOCK_BOOTTIME)

    def read(self):
        return ClockReading(
            self.clock_id,
            self.boot_digest,
            self._read_ns(),
            self._uncertainty,
        )


class ClockSynchronizer:
    """Trusted process-local owner of samples and conservative mappings."""

    def __init__(self, clock=None, *, max_mapping_uncertainty_ns=DEFAULT_MAX_MAPPING_UNCERTAINTY_NS):
        self._clock = SuspendInclusiveClock() if clock is None else clock
        self._issuer = object()
        self._lock = threading.RLock()
        self._last_reading = None
        self.max_mapping_uncertainty_ns = _integer(
            max_mapping_uncertainty_ns,
            "maximum mapping uncertainty",
            0,
            MAX_UNCERTAINTY_NS,
        )

    @_clock_locked
    def sample(self):
        try:
            reading = self._clock.read()
            _require(type(reading) is ClockReading, "Suspend-inclusive clock is unavailable")
        except Exception:
            self._issuer = object()
            self._last_reading = None
            raise ContractError("Suspend-inclusive clock is unavailable") from None
        previous = self._last_reading
        if previous is not None and (
            reading.clock_id != previous.clock_id
            or reading.boot_digest != previous.boot_digest
            or reading.nanoseconds < previous.nanoseconds
        ):
            # A new reading may establish a new exchange, but it can never
            # make any sample or mapping from the previous epoch valid again.
            self._issuer = object()
        self._last_reading = reading
        return TrustedClockSample(reading, self._issuer)

    @_clock_locked
    def record_exchange(
        self,
        *,
        coordinator_clock_id,
        coordinator_send_ns,
        host_received,
        host_sent,
        coordinator_receive_ns,
        coordinator_uncertainty_ns=0,
        max_drift_ppm=100,
    ):
        coordinator_id = _identifier(coordinator_clock_id, "coordinator clock identity")
        sent_ns = _integer(coordinator_send_ns, "coordinator send time")
        receive_ns = _integer(coordinator_receive_ns, "coordinator receive time")
        coordinator_uncertainty = _integer(
            coordinator_uncertainty_ns,
            "coordinator clock uncertainty",
            0,
            MAX_UNCERTAINTY_NS,
        )
        drift = _integer(max_drift_ppm, "clock drift bound", 0, MAX_DRIFT_PPM)
        _require(
            type(host_received) is TrustedClockSample
            and type(host_sent) is TrustedClockSample
            and host_received._issuer is self._issuer
            and host_sent._issuer is self._issuer,
            "Trusted local clock samples required",
        )
        first = host_received.reading
        second = host_sent.reading
        _require(
            sent_ns <= receive_ns
            and first.clock_id == second.clock_id
            and first.boot_digest == second.boot_digest
            and first.nanoseconds <= second.nanoseconds,
            "Clock exchange is inconsistent",
        )

        offset_lower = (
            second.nanoseconds - second.uncertainty_ns
            - (receive_ns + coordinator_uncertainty)
        )
        offset_upper = (
            first.nanoseconds + first.uncertainty_ns
            - (sent_ns - coordinator_uncertainty)
        )
        _require(offset_lower <= offset_upper, "Clock exchange is inconsistent")
        base_uncertainty = offset_upper - offset_lower
        _require(
            base_uncertainty <= self.max_mapping_uncertainty_ns,
            "Clock mapping uncertainty is excessive",
        )
        fields = {
            "coordinatorClockId": coordinator_id,
            "hostClockId": first.clock_id,
            "hostBootDigest": first.boot_digest,
            "coordinatorSendNs": sent_ns,
            "coordinatorReceiveNs": receive_ns,
            "hostReceiveNs": first.nanoseconds,
            "hostSendNs": second.nanoseconds,
            "offsetLowerNs": offset_lower,
            "offsetUpperNs": offset_upper,
            "maxDriftPpm": drift,
        }
        return ClockMapping(
            coordinator_clock_id=coordinator_id,
            host_clock_id=first.clock_id,
            host_boot_digest=first.boot_digest,
            coordinator_send_ns=sent_ns,
            coordinator_receive_ns=receive_ns,
            host_receive_ns=first.nanoseconds,
            host_send_ns=second.nanoseconds,
            offset_lower_ns=offset_lower,
            offset_upper_ns=offset_upper,
            max_drift_ppm=drift,
            mapping_id=_fingerprint(fields),
            _issuer=self._issuer,
        )

    @_clock_locked
    def record_probe(self, *, coordinator_clock_id, coordinator_ns,
                     host_sent, host_received, coordinator_uncertainty_ns=0,
                     max_drift_ppm=100):
        """Map a native reading taken inside a host-initiated round trip.

        This is the opposite message direction to ``record_exchange``.  The
        native point lies between the trusted host send and receive samples;
        neither network leg is assumed symmetric or zero latency.
        """
        native_id = _identifier(coordinator_clock_id, "coordinator clock identity")
        native_ns = _integer(coordinator_ns, "coordinator clock value")
        uncertainty = _integer(coordinator_uncertainty_ns,
                               "coordinator clock uncertainty", 0, MAX_UNCERTAINTY_NS)
        drift = _integer(max_drift_ppm, "clock drift bound", 0, MAX_DRIFT_PPM)
        _require(type(host_sent) is TrustedClockSample
                 and type(host_received) is TrustedClockSample
                 and host_sent._issuer is self._issuer
                 and host_received._issuer is self._issuer,
                 "Trusted local clock samples required")
        first, last = host_sent.reading, host_received.reading
        _require(first.clock_id == last.clock_id
                 and first.boot_digest == last.boot_digest
                 and first.nanoseconds <= last.nanoseconds,
                 "Clock probe is inconsistent")
        lower = first.nanoseconds - first.uncertainty_ns - native_ns - uncertainty
        upper = last.nanoseconds + last.uncertainty_ns - native_ns + uncertainty
        _require(upper - lower <= self.max_mapping_uncertainty_ns,
                 "Clock mapping uncertainty is excessive")
        fields = {
            "coordinatorClockId": native_id,
            "hostClockId": first.clock_id, "hostBootDigest": first.boot_digest,
            "coordinatorSendNs": native_ns, "coordinatorReceiveNs": native_ns,
            "hostReceiveNs": last.nanoseconds, "hostSendNs": first.nanoseconds,
            "offsetLowerNs": lower, "offsetUpperNs": upper,
            "maxDriftPpm": drift, "measurement": "host-probe",
        }
        return ClockMapping(
            native_id, first.clock_id, first.boot_digest, native_ns, native_ns,
            last.nanoseconds, first.nanoseconds, lower, upper, drift,
            _fingerprint(fields), self._issuer, "host-probe")

    @_clock_locked
    def require_mapping(self, mapping):
        fields = {
            "coordinatorClockId": getattr(mapping, "coordinator_clock_id", None),
            "hostClockId": getattr(mapping, "host_clock_id", None),
            "hostBootDigest": getattr(mapping, "host_boot_digest", None),
            "coordinatorSendNs": getattr(mapping, "coordinator_send_ns", None),
            "coordinatorReceiveNs": getattr(mapping, "coordinator_receive_ns", None),
            "hostReceiveNs": getattr(mapping, "host_receive_ns", None),
            "hostSendNs": getattr(mapping, "host_send_ns", None),
            "offsetLowerNs": getattr(mapping, "offset_lower_ns", None),
            "offsetUpperNs": getattr(mapping, "offset_upper_ns", None),
            "maxDriftPpm": getattr(mapping, "max_drift_ppm", None),
        }
        if getattr(mapping, "measurement", None) != "peer-exchange":
            fields["measurement"] = getattr(mapping, "measurement", None)
        _require(type(mapping) is ClockMapping and mapping._issuer is self._issuer
                 and mapping.measurement in {"peer-exchange", "host-probe"}
                 and mapping.mapping_id == _fingerprint(fields),
                 "Trusted clock mapping required")
        sample = self.sample()
        mapping.require_compatible(sample)
        return sample


class RecordingClockError(ContractError):
    """A recording time segment can no longer be mapped conservatively."""


@dataclass(frozen=True, slots=True)
class RecordingStamp:
    display_ms: int
    offset_ms: int
    earliest_offset_ms: int
    latest_offset_ms: int
    uncertainty_ns: int
    provider_clock_id: str | None = None
    provider_boot_digest: str | None = None
    native_incarnation: str | None = None

    @property
    def presentation_offset_ms(self):
        return self.offset_ms


@dataclass(frozen=True, slots=True)
class ProviderClockBinding:
    mapping: ClockMapping
    provider_boot_digest: str
    native_incarnation: str
    _issuer: object = field(repr=False, compare=False)


class RecordingTimeAnchor:
    """Stable display-time anchor backed only by suspend-inclusive elapsed time.

    Wall time is sampled once for display compatibility.  It is never sampled
    again to order input, receipts, or provider frames.
    """

    def __init__(self, synchronizer, *, wall_clock_ms=None):
        _require(type(synchronizer) is ClockSynchronizer,
                 "Trusted clock synchronizer required")
        self.synchronizer = synchronizer
        self._issuer = object()
        self._provider_issuer = object()
        self._lock = threading.RLock()
        self._provider_identity = None
        self._provider_last: dict[str, int] = {}
        self._anchor = synchronizer.sample()
        source = (lambda: time.time_ns() // 1_000_000) if wall_clock_ms is None else wall_clock_ms
        try:
            started = source()
        except Exception:
            raise RecordingClockError("Recording display clock is unavailable") from None
        try:
            self.started_at_ms = _integer(started, "recording display time", 0,
                                          32_503_680_000_000)
        except ContractError:
            raise RecordingClockError("Recording display clock is unavailable") from None

    def _current(self):
        try:
            sample = self.synchronizer.sample()
            _require(sample._issuer is self._anchor._issuer
                     and sample.clock_id == self._anchor.clock_id
                     and sample.boot_digest == self._anchor.boot_digest
                     and sample.nanoseconds >= self._anchor.nanoseconds,
                     "Recording clock segment is invalid")
            return sample
        except ContractError:
            raise RecordingClockError("Recording clock segment is invalid") from None

    @staticmethod
    def _ceil_ms(nanoseconds):
        return (nanoseconds + 999_999) // 1_000_000

    def stamp(self):
        sample = self._current()
        elapsed = sample.nanoseconds - self._anchor.nanoseconds
        offset = elapsed // 1_000_000
        uncertainty = sample.uncertainty_ns + self._anchor.uncertainty_ns
        earliest = max(0, (elapsed - uncertainty) // 1_000_000)
        latest = self._ceil_ms(elapsed + uncertainty)
        return RecordingStamp(
            display_ms=self.started_at_ms + offset,
            offset_ms=offset,
            earliest_offset_ms=earliest,
            latest_offset_ms=latest,
            uncertainty_ns=uncertainty,
        )

    def bind_provider(self, mapping, *, provider_boot_digest, native_incarnation):
        try:
            self.synchronizer.require_mapping(mapping)
            _digest(provider_boot_digest, "provider boot incarnation digest")
            _identifier(native_incarnation, "native incarnation")
            identity = (mapping.coordinator_clock_id, provider_boot_digest,
                        native_incarnation)
            with self._lock:
                _require(self._provider_identity in {None, identity},
                         "Provider clock incarnation changed")
                self._provider_identity = identity
                issuer = self._provider_issuer
        except ContractError:
            raise RecordingClockError("Provider clock mapping is invalid") from None
        return ProviderClockBinding(mapping, provider_boot_digest,
                                    native_incarnation, issuer)

    def invalidate_provider_mappings(self):
        with self._lock:
            self._provider_issuer = object()
            self._provider_identity = None
            self._provider_last.clear()

    def stamp_provider(self, binding, provider_nanoseconds, *, capture_start_ns=None):
        try:
            _integer(provider_nanoseconds, "provider clock value")
            start = (provider_nanoseconds if capture_start_ns is None
                     else _integer(capture_start_ns, "provider capture start"))
            _require(0 <= provider_nanoseconds - start <= 10_000_000_000,
                     "Provider capture interval is invalid")
            with self._lock:
                identity = (
                    binding.mapping.coordinator_clock_id,
                    binding.provider_boot_digest,
                    binding.native_incarnation,
                ) if type(binding) is ProviderClockBinding else None
                _require(type(binding) is ProviderClockBinding
                         and binding._issuer is self._provider_issuer
                         and identity == self._provider_identity,
                         "Provider clock mapping is invalid")
                previous = self._provider_last.get(binding.native_incarnation)
                _require(previous is None or start >= previous,
                         "Provider clock moved backward")
                current = self.synchronizer.require_mapping(binding.mapping)
                _require(current._issuer is self._anchor._issuer,
                         "Recording clock segment is invalid")
                interval = binding.mapping.translate(provider_nanoseconds)
                start_interval = binding.mapping.translate(start)
                earliest_ns = (start_interval.earliest_ns - self._anchor.nanoseconds
                               - self._anchor.uncertainty_ns)
                latest_ns = (interval.latest_ns - self._anchor.nanoseconds
                             + self._anchor.uncertainty_ns)
                total_uncertainty = latest_ns - earliest_ns
                _require(
                    max(interval.uncertainty_ns, start_interval.uncertainty_ns)
                    + 2 * self._anchor.uncertainty_ns
                    <= self.synchronizer.max_mapping_uncertainty_ns,
                    "Provider clock uncertainty is excessive",
                )
                _require(latest_ns >= 0, "Provider timestamp predates recording")
                self._provider_last[binding.native_incarnation] = provider_nanoseconds
        except ContractError:
            raise RecordingClockError("Provider clock mapping is invalid") from None
        earliest_ms = max(0, earliest_ns // 1_000_000)
        latest_ms = max(earliest_ms, self._ceil_ms(latest_ns))
        presentation = (earliest_ms + latest_ms) // 2
        uncertainty = total_uncertainty
        return RecordingStamp(
            display_ms=self.started_at_ms + presentation,
            offset_ms=presentation,
            earliest_offset_ms=earliest_ms,
            latest_offset_ms=latest_ms,
            uncertainty_ns=uncertainty,
            provider_clock_id=binding.mapping.coordinator_clock_id,
            provider_boot_digest=binding.provider_boot_digest,
            native_incarnation=binding.native_incarnation,
        )


__all__ = [
    "ClockInterval",
    "ClockMapping",
    "ClockReading",
    "ClockSynchronizer",
    "ProviderClockBinding",
    "RecordingClockError",
    "RecordingStamp",
    "RecordingTimeAnchor",
    "SuspendInclusiveClock",
    "TrustedClockSample",
    "UnavailableClock",
]
