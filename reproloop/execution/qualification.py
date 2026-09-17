"""Owned-VM qualification from fixed preinstalled probes and native observations.

There is no deserializer for live qualification authority. Imported reports and
candidate test results cannot call this local trusted composition interface.
"""
from __future__ import annotations

from dataclasses import dataclass
import signal
import subprocess
import threading
import time
import uuid

from reproloop.contracts.versions import bounded_int, digest, exact, require, validate_id
from reproloop.core import ContractError
from .artifacts import ArtifactError, BlobSet, receive_blobs
from .backend import BackendQualification, ExecutionDenied, QualificationAuthority, REQUIRED_PROBES
from .journal import RunDenied, RunStore
from .native import NativeError, NativeVM
from .resources import GuestBundle, HostBuildBundle, HOST_PROBE_RECIPE, ResourceError
from .wire import ProtocolError, bootstrap, decode_json

MODES = ("containment", "hold", "oversize", "forged-report")
HOST_PROBES = ("toolchain-boundary", "process-termination", "cleanup")


class _CombinedCancellation:
    """Expose one fail-closed cancellation view to a VM and its channel."""

    def __init__(self, external, run):
        self.external = external
        self.run = run

    def is_set(self):
        if self.external is not None:
            try:
                value = self.external.is_set()
            except Exception:
                return True
            if type(value) is not bool:
                return True
            if value:
                return True
        try:
            return self.run.cancelled()
        except Exception:
            return True

    def wait(self, timeout=None):
        if timeout is None:
            while not self.is_set():
                time.sleep(.05)
            return True
        deadline = time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.is_set()
            time.sleep(min(.05, remaining))
        return True


def _validate_cancellation(cancellation):
    if cancellation is None:
        return None
    is_set = getattr(cancellation, "is_set", None)
    if not callable(is_set):
        raise ExecutionDenied("Qualification cancellation interface is unavailable")
    try:
        value = is_set()
    except Exception:
        raise ExecutionDenied("Qualification cancellation interface is unavailable") from None
    if type(value) is not bool:
        raise ExecutionDenied("Qualification cancellation interface is invalid")
    return cancellation


def _external_cancelled(cancellation):
    if cancellation is None:
        return False
    try:
        value = cancellation.is_set()
    except Exception:
        return True
    return value is not False


@dataclass(frozen=True, slots=True)
class QualificationOutcome:
    report: dict
    qualification: BackendQualification | None


def _containment(value):
    try:
        exact(value, ("schemaVersion", "mode", "networkDenied", "agentWriteDenied",
                      "toolchainWriteDenied", "detachedChildStarted"))
        return (type(value["schemaVersion"]) is int and value["schemaVersion"] == 1
                and value["mode"] == "containment"
                and all(value[key] is True for key in ("networkDenied", "agentWriteDenied",
                                                       "toolchainWriteDenied", "detachedChildStarted")))
    except ContractError:
        return False


def _probe(bundle, store, mode, *, cancellation=None):
    operation_id = "qual-" + uuid.uuid4().hex
    request_digest = digest({"operationId": operation_id, "environmentDigest": bundle.environment_digest, "mode": mode})
    vm = None
    stopped, passed = True, False
    lifecycle = {"configured": False, "started": False, "connected": False, "stopped": False}
    with store.admit(operation_id, request_digest, disk_bytes=bundle.overlay_bytes) as run:
        combined = _CombinedCancellation(cancellation, run)
        try:
            if not combined.is_set():
                bundle.create_overlays(run.directory)
                require(not combined.is_set(), "Qualification cancelled")
                deadline = time.monotonic() + min(600, bundle.metadata["environment"]["resources"]["timeoutMs"] / 1000)
                stopped = False
                vm = NativeVM(bundle, run, deadline=deadline, cancel=combined)
                vm.wait_ready()
                channel = bootstrap(vm.channel, run_id=operation_id, deadline=deadline, cancel=combined)
                kind, ready = channel.receive()
                require(kind == "ready" and ready["agentDigest"] == bundle.metadata["agentDigest"]
                        and ready["catalogDigest"] == digest(bundle.metadata["catalog"]), "Probe identity mismatch")
                channel.send("probe", {"mode": mode})
                require(channel.receive() == ("probe-started", {"mode": mode}), "Live probe process not observed")
                if mode == "hold":
                    store.cancel(operation_id, request_digest)
                    passed = run.cancelled()
                else:
                    first = channel.receive()
                    if mode == "oversize":
                        passed = first == ("error", {"code": "output-rejected"})
                    else:
                        blobs = receive_blobs(channel, prefix="artifact", first_message=first,
                                              max_bytes=4096, allowed_paths=("probe.json",))
                        kind, result = channel.receive()
                        require(kind == "result" and result["exitCode"] == 0
                                and result["outputTruncated"] is False, "Trusted probe execution failed")
                        value = decode_json(blobs.entries[0][1])
                        passed = (_containment(value) if mode == "containment" else
                                  value == {"verified": True, "passed": True} and not _containment(value))
        except (ContractError, NativeError, ProtocolError, ArtifactError, ResourceError, OSError):
            passed = False
        finally:
            if _external_cancelled(cancellation):
                try:
                    store.cancel(operation_id, request_digest)
                except (RunDenied, OSError):
                    pass
                passed = False
            if vm is not None:
                try:
                    stopped = vm.stop()
                except (NativeError, OSError, subprocess.SubprocessError):
                    stopped = False
                lifecycle = vm.evidence
            passed = passed and all(lifecycle.values())
            run.finish("succeeded" if passed else "failed", stopped=stopped)
    record = store.status(operation_id)
    clean = record["reservedBytes"] == 0 and record["state"] != "quarantined"
    passed = passed and clean and all(lifecycle.values()) and (record["state"] == ("cancelled" if mode == "hold" else "succeeded"))
    return {"operationId": operation_id, "mode": mode, "passed": passed,
            "lifecycle": lifecycle, "state": record["state"], "cleanupConfirmed": clean}


def qualify_backend(backend_id, authority, bundle, store, *, ttl_ms=3600000, cancellation=None):
    cancellation = _validate_cancellation(cancellation)
    if (type(authority) is not QualificationAuthority or type(bundle) is not GuestBundle
            or type(store) is not RunStore or bundle.environment_digest != store.environment_digest):
        raise ExecutionDenied("Trusted VM qualification composition required")
    validate_id(backend_id)
    bounded_int(ttl_ms, "qualification lifetime", 1000, 24 * 60 * 60 * 1000)
    execution_class = bundle.metadata["environment"]["executionClass"]
    probes, qualification = [], None
    try:
        bundle.verify()
        with store.machine_lease(bundle.machine_digest):
            # New qualification invalidates the old observation set before any
            # probe runs. Failure cannot leave old authorizations usable.
            authority.revoke_backend(backend_id, execution_class, bundle.environment_digest)
            for mode in MODES:
                if _external_cancelled(cancellation):
                    break
                probes.append(_probe(bundle, store, mode, cancellation=cancellation))
                if not probes[-1]["passed"]:
                    break
            if (len(probes) == len(MODES) and all(item["passed"] for item in probes)
                    and not _external_cancelled(cancellation)):
                bundle.verify()  # All immutable VM/toolchain/agent resources survived the probes.
                if not _external_cancelled(cancellation):
                    now = int(time.time() * 1000)
                    probe_ids = sorted(REQUIRED_PROBES[execution_class])
                    receipts = []
                    for probe_id in probe_ids:
                        if _external_cancelled(cancellation):
                            break
                        receipts.append(authority.record_probe(probe_id=probe_id, backend_id=backend_id,
                            execution_class=execution_class, environment_digest=bundle.environment_digest,
                            outcome="pass", evidence_digest=digest({"probeId": probe_id, "runs": probes}),
                            observed_at_ms=now))
                    if len(receipts) == len(probe_ids) and not _external_cancelled(cancellation):
                        qualification = authority.issue_backend_qualification({"schemaVersion": 1,
                            "id": "qualified-" + uuid.uuid4().hex, "backendId": backend_id,
                            "executionClass": execution_class, "environmentDigest": bundle.environment_digest,
                            "issuedAtMs": now, "expiresAtMs": now + ttl_ms, "probeIds": probe_ids},
                            receipts, evaluated_at_ms=now)
                        if _external_cancelled(cancellation):
                            authority.revoke_backend(backend_id, execution_class, bundle.environment_digest)
                            qualification = None
    except (ContractError, ExecutionDenied, RunDenied, ResourceError, OSError):
        qualification = None
    report = {"schemaVersion": 1, "status": "qualified" if qualification is not None else "blocked-unqualified",
              "backendId": backend_id, "executionClass": execution_class, "environmentDigest": bundle.environment_digest,
              "actualVM": any(item["lifecycle"]["started"] for item in probes),
              "qualified": qualification is not None, "probes": probes,
              "authority": "process-local" if qualification is not None else "none"}
    return QualificationOutcome(report, qualification)


def _host_probe_toolchain(bundle):
    """Every declared tool must still match its pinned digest and permissions."""
    metadata = bundle.metadata
    tools = metadata["tools"]
    catalog = metadata["catalog"]
    evidence = {"toolCount": len(tools), "toolsVerified": True, "probeRecipePresent": False,
                "recipesPinned": True}
    try:
        bundle.verify()
        paths = bundle.tool_paths
        evidence["probeRecipePresent"] = any(
            recipe["id"] == HOST_PROBE_RECIPE and recipe["executionClass"] == "host-build"
            for recipe in catalog)
        evidence["recipesPinned"] = all(recipe["argv"][0] in paths for recipe in catalog)
    except ResourceError:
        evidence["toolsVerified"] = False
    passed = (evidence["toolsVerified"] and evidence["probeRecipePresent"]
              and evidence["recipesPinned"] and evidence["toolCount"] >= 1)
    return {"probeId": "toolchain-boundary", "passed": passed, "evidence": evidence}


def _host_probe_termination(bundle, store, cancellation):
    """Cancel a live host recipe run; the process group must be fully reaped."""
    from .runtime import run_host_recipe
    operation_id = "qual-host-" + uuid.uuid4().hex
    request_digest = digest({"operationId": operation_id, "mode": "process-termination"})
    evidence = {"started": False, "killObserved": False, "groupReaped": False,
                "runCancelled": False, "reservedReleased": False}
    recipe = bundle.recipe(HOST_PROBE_RECIPE)
    outcome = None
    with store.admit(operation_id, request_digest, disk_bytes=bundle.overlay_bytes) as run:
        outcome_box = {}
        started = threading.Event()

        def target():
            try:
                outcome_box["result"] = run_host_recipe(
                    bundle, recipe, BlobSet((("probe.seed", b"host-qualification"),)), run,
                    time.monotonic() + recipe["timeoutMs"] / 1000, cancel=cancellation,
                    started=started)
            except Exception as error:  # probe만 실패로 기록하고 자격은 부여하지 않는다.
                outcome_box["error"] = error

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        # launch를 관측한 뒤에만 취소한다 — 취소가 실제로 살아 있는 프로세스를
        # 죽였는지가 이 probe의 측정 대상이다.
        started.wait(timeout=10)
        store.cancel(operation_id, request_digest)
        # kill 확인 창(10s)보다 충분히 긴 join — 진행 중인 수거를 실패로 오인하지 않는다.
        worker.join(timeout=40)
        # join 이후에도 살아 있는 worker는 종료 미확정으로 취급한다.
        finished = not worker.is_alive()
        outcome = outcome_box.get("result") if finished else None
        run.finish("failed", stopped=finished and outcome is not None and outcome["stopped"])
    if outcome is not None and finished:
        evidence["started"] = outcome["lifecycle"]["started"]
        # 실제로 SIGKILL이 발송됐고 프로세스가 시그널로 종료했는지 요구한다 —
        # 취소 전에 스스로 종료된 recipe는 종료를 측정한 것이 아니다.
        evidence["killObserved"] = (outcome["killSent"] is True
                                    and outcome["exitCode"] == -signal.SIGKILL)
        evidence["groupReaped"] = outcome["stopped"] is True and outcome["lifecycle"]["stopped"]
    record = store.status(operation_id)
    evidence["runCancelled"] = record["state"] == "cancelled"
    evidence["reservedReleased"] = record["reservedBytes"] == 0
    passed = all(evidence.values())
    return {"probeId": "process-termination", "passed": passed, "evidence": evidence}


def _host_probe_cleanup(bundle, store):
    """A run directory with stray files must end empty, released and removed."""
    operation_id = "qual-host-" + uuid.uuid4().hex
    request_digest = digest({"operationId": operation_id, "mode": "cleanup"})
    evidence = {"scratchRemoved": False, "directoryRemoved": False, "reservedReleased": False,
                "stateTerminal": False}
    with store.admit(operation_id, request_digest, disk_bytes=bundle.overlay_bytes) as run:
        from .runtime import _discard_run_tree
        scratch = run.directory / "scratch" / "nested"
        scratch.mkdir(parents=True)
        (scratch / "leftover.bin").write_bytes(b"probe")
        (run.directory / "stray").write_bytes(b"probe")
        evidence["scratchRemoved"] = _discard_run_tree(
            run.directory, expected=getattr(run, "directory_identity", None))
        run.finish("succeeded", stopped=True)
    record = store.status(operation_id)
    evidence["directoryRemoved"] = not run.directory.exists()
    evidence["reservedReleased"] = record["reservedBytes"] == 0
    evidence["stateTerminal"] = record["state"] in ("succeeded", "cancelled", "failed")
    passed = all(evidence.values())
    return {"probeId": "cleanup", "passed": passed, "evidence": evidence}


def qualify_host_build(backend_id, authority, bundle, store, *, ttl_ms=3600000, cancellation=None):
    """Measure a pinned host toolchain and issue a ``host-build`` qualification.

    This never implies isolation: the report states ``isolation: "host"`` and
    the issued capability only carries the host-build probe set.
    """
    cancellation = _validate_cancellation(cancellation)
    if (type(authority) is not QualificationAuthority or type(bundle) is not HostBuildBundle
            or type(store) is not RunStore or bundle.environment_digest != store.environment_digest):
        raise ExecutionDenied("Trusted host qualification composition required")
    validate_id(backend_id)
    bounded_int(ttl_ms, "qualification lifetime", 1000, 24 * 60 * 60 * 1000)
    execution_class = "host-build"
    probes, qualification = [], None
    try:
        with store.machine_lease(bundle.machine_digest, kind='host'):
            # 재측정이 받아들여지면 이전 qualification을 먼저 폐기한다 —
            # toolchain 변조로 측정이 실패해도 낡은 자격이 남지 않는다.
            authority.revoke_backend(backend_id, execution_class, bundle.environment_digest)
            bundle.verify()
            probes.append(_host_probe_toolchain(bundle))
            if probes[-1]["passed"] and not _external_cancelled(cancellation):
                probes.append(_host_probe_termination(bundle, store, cancellation))
            if probes[-1]["passed"] and not _external_cancelled(cancellation):
                probes.append(_host_probe_cleanup(bundle, store))
            if ([item["probeId"] for item in probes] == list(HOST_PROBES)
                    and all(item["passed"] for item in probes)
                    and not _external_cancelled(cancellation)):
                bundle.verify()  # 고정 toolchain이 probe 실행 동안에도 그대로였는지 재확인한다.
                if not _external_cancelled(cancellation):
                    now = int(time.time() * 1000)
                    probe_ids = sorted(REQUIRED_PROBES[execution_class])
                    receipts = []
                    for probe_id in probe_ids:
                        if _external_cancelled(cancellation):
                            break
                        receipts.append(authority.record_probe(probe_id=probe_id, backend_id=backend_id,
                            execution_class=execution_class, environment_digest=bundle.environment_digest,
                            outcome="pass", evidence_digest=digest({"probeId": probe_id, "runs": probes}),
                            observed_at_ms=now))
                    if len(receipts) == len(probe_ids) and not _external_cancelled(cancellation):
                        qualification = authority.issue_backend_qualification({"schemaVersion": 1,
                            "id": "qualified-" + uuid.uuid4().hex, "backendId": backend_id,
                            "executionClass": execution_class, "environmentDigest": bundle.environment_digest,
                            "issuedAtMs": now, "expiresAtMs": now + ttl_ms, "probeIds": probe_ids},
                            receipts, evaluated_at_ms=now)
                        if _external_cancelled(cancellation):
                            authority.revoke_backend(backend_id, execution_class, bundle.environment_digest)
                            qualification = None
    except (ContractError, ExecutionDenied, RunDenied, ResourceError, OSError):
        qualification = None
    report = {"schemaVersion": 1, "status": "qualified" if qualification is not None else "blocked-unqualified",
              "backendId": backend_id, "executionClass": execution_class,
              "environmentDigest": bundle.environment_digest, "isolation": "host", "actualVM": False,
              "qualified": qualification is not None, "probes": probes,
              "authority": "process-local" if qualification is not None else "none"}
    return QualificationOutcome(report, qualification)
