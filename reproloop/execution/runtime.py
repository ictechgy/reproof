"""Concrete protected VM backend. Native lifecycle is separate from guest reports."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import stat
import time
import subprocess

from reproloop.contracts.versions import digest, require, validate_id
from reproloop.core import ContractError
from .artifacts import ArtifactError, ArtifactValidationAuthority, BlobSet, receive_blobs, send_blobs
from .backend import ExecutionDenied, QualificationAuthority
from .journal import RunDenied, RunStore
from .native import NativeError, NativeVM
from .protocol import validate_execution_request
from .resources import GuestBundle, HostBuildBundle, ResourceError
from .wire import ProtocolError, bootstrap


@dataclass(frozen=True, slots=True)
class GuestExecutionResult:
    operation_id: str
    request_digest: str
    status: str
    reason: str
    input_digest: str
    artifact_digest: str | None
    artifacts: BlobSet | None
    candidate_report: dict | None
    lifecycle: dict
    cleanup_confirmed: bool
    isolation: str

    @property
    def verified(self):
        # Only the G9 trusted validation authority can issue that separate verdict.
        return False

    def public_record(self):
        return {"schemaVersion": 1, "operationId": self.operation_id, "requestDigest": self.request_digest,
                "status": self.status, "reason": self.reason, "inputDigest": self.input_digest,
                "artifactDigest": self.artifact_digest, "candidateReport": dict(self.candidate_report) if self.candidate_report else None,
                "lifecycle": dict(self.lifecycle), "cleanupConfirmed": self.cleanup_confirmed,
                "isolation": self.isolation, "verified": False}


class MacOSVirtualizationBackend:
    isolation = "guest-vm"
    scope_kind = "vm"

    def __init__(self, backend_id, authority, bundle, store, *, artifact_authority=None):
        validate_id(backend_id)
        if (type(authority) is not QualificationAuthority or type(bundle) is not GuestBundle
                or type(store) is not RunStore or store.environment_digest != bundle.environment_digest):
            raise ExecutionDenied("Trusted VM composition required")
        self.backend_id, self.authority, self.bundle, self.store = backend_id, authority, bundle, store
        self.execution_class = bundle.metadata["environment"]["executionClass"]
        if artifact_authority is not None and type(artifact_authority) is not ArtifactValidationAuthority:
            raise ExecutionDenied("Trusted artifact authority required")
        self.artifact_authority = artifact_authority

    def _check(self, request, authorization):
        self.authority.check_authorization(authorization, request, evaluated_at_ms=int(time.time() * 1000))
        if (request["backendId"] != self.backend_id or request["executionClass"] != self.execution_class
                or request["environmentDigest"] != self.bundle.environment_digest):
            raise ExecutionDenied("VM execution binding mismatch")

    def cancel(self, operation_id, request_digest):
        try:
            self.store.cancel(operation_id, request_digest)
        except (RunDenied, OSError):
            raise ExecutionDenied("VM cancellation rejected") from None

    def reconcile(self, operation_id, request_digest):
        try:
            with self.store.machine_lease(self.bundle.machine_digest):
                return self.store.reconcile(operation_id, request_digest)
        except (RunDenied, OSError):
            raise ExecutionDenied("VM recovery remains quarantined") from None

    def execute(self, request, authorization, inputs):
        try:
            request = validate_execution_request(request)
            self._check(request, authorization)
            if request["inputKind"] == "validated-artifact":
                require(self.artifact_authority is not None, "Validated artifact authority required")
                inputs = self.artifact_authority.require_input(inputs, input_digest=request["inputDigest"],
                    project_digest=request["projectDigest"], execution_class=request["executionClass"])
            require(type(inputs) is BlobSet and request["inputDigest"] == inputs.digest,
                    "Sealed execution input required")
            recipe = self.bundle.recipe(request["recipeId"])
            require(recipe["executionClass"] == self.execution_class
                    and recipe["artifactPolicyId"] == request["artifactPolicyId"]
                    and recipe["cleanupPolicyId"] == request["cleanupPolicyId"], "Guest recipe binding mismatch")
            self.bundle.verify()
            self._check(request, authorization)
            with self.store.machine_lease(self.bundle.machine_digest):
                with self.store.admit(request["operationId"], digest(request), disk_bytes=self.bundle.overlay_bytes) as run:
                    return self._execute_owned(request, authorization, inputs, recipe, run)
        except (ContractError, ResourceError, ArtifactError, RunDenied, OSError):
            raise ExecutionDenied("Protected VM execution rejected") from None

    def _execute_owned(self, request, authorization, inputs, recipe, run):
        vm, report, artifacts = None, None, None
        stopped, outcome = True, "failed"
        reason = "overlay-preparation-failed"
        lifecycle = {"configured": False, "started": False, "connected": False, "stopped": False}
        try:
            self.bundle.create_overlays(run.directory)
            reason = "authorization-denied"
            self._check(request, authorization)
            if run.cancelled():
                raise ExecutionDenied("VM execution cancelled")
            remaining = min(self.bundle.metadata["environment"]["resources"]["timeoutMs"] / 1000,
                            (authorization.expires_at_ms - int(time.time() * 1000)) / 1000)
            deadline = time.monotonic() + remaining
            # Once construction starts, missing owner/stop evidence is uncertain.
            stopped = False
            reason = "native-start-failed"
            vm = NativeVM(self.bundle, run, deadline=deadline, cancel=run)
            vm.wait_ready()
            channel = bootstrap(vm.channel, run_id=request["operationId"], deadline=deadline, cancel=run)
            kind, ready = channel.receive()
            reason = "guest-identity-mismatch"
            metadata = self.bundle.metadata
            require(kind == "ready" and ready["agentDigest"] == metadata["agentDigest"]
                    and ready["catalogDigest"] == digest(metadata["catalog"]), "Guest readiness identity mismatch")
            self._check(request, authorization)
            reason = "input-transfer-failed"
            send_blobs(channel, inputs, prefix="input")
            reason = "authorization-denied"
            self._check(request, authorization)
            reason = "candidate-execution-failed"
            channel.send("run", {"recipeId": recipe["id"], "inputDigest": inputs.digest})
            first = channel.receive()
            if first[0] == "artifact-start":
                reason = "artifact-rejected"
                artifacts = receive_blobs(channel, prefix="artifact", first_message=first,
                                          max_bytes=recipe["maxOutputBytes"], allowed_paths=recipe["outputPaths"])
                first = channel.receive()
            require(first[0] == "result", "Guest result missing")
            report = first[1]
            reason = ("candidate-exit-nonzero" if report["exitCode"] != 0 else
                      "candidate-output-truncated" if report["outputTruncated"] else "artifact-missing")
            if report["exitCode"] == 0 and not report["outputTruncated"] and artifacts is not None:
                outcome = "succeeded"
                reason = "candidate-output"
        except (ExecutionDenied, ContractError, ResourceError, ArtifactError, ProtocolError, NativeError, OSError):
            outcome = "failed"
        finally:
            if vm is not None:
                try:
                    stopped = vm.stop()
                except (NativeError, OSError, subprocess.SubprocessError):
                    stopped = False
                lifecycle = vm.evidence
            if not all(lifecycle.values()):
                outcome = "failed"
                if reason == "candidate-output":
                    reason = "native-lifecycle-unconfirmed"
            run.finish(outcome, stopped=stopped)
        state = self.store.status(request["operationId"])["state"]
        status = "candidate-output" if state == "succeeded" else state
        if state == "quarantined":
            reason = "vm-stop-unconfirmed" if not stopped else "overlay-cleanup-unconfirmed"
        elif state == "cancelled":
            reason = "cancelled"
        published = artifacts if status == "candidate-output" else None
        return GuestExecutionResult(request["operationId"], digest(request), status, reason, inputs.digest,
                                    published.digest if published is not None else None, published,
                                    report, lifecycle, state != "quarantined", isolation="guest-vm")


def _host_group_alive(process):
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_host_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _remove_tree_fd(root_fd, depth=0):
    """fd-기준 재귀 삭제: root symlink 교체로 다른 경로를 지우지 못하게 한다.

    깊이를 제한한다 — 후보가 만든 무한 중첩이 RecursionError로 전파되지 않고
    정리 실패(격리)로 떨어지게 한다. fd는 레벨당 하나만 잡는다.
    """
    if depth > 64:
        return False
    for name in os.listdir(root_fd):
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                if not _remove_tree_fd(child, depth + 1):
                    return False
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=root_fd)
        else:
            os.unlink(name, dir_fd=root_fd)
    return True


def _discard_run_tree(directory, expected=None):
    """Remove every entry the run created so journal cleanup can empty the dir.

    The run directory is candidate-writable in host mode: it must be opened
    with O_NOFOLLOW and traversed by descriptor so a swapped symlink cannot
    redirect deletion outside the journal tree. ``expected``가 주어지면
    admit 시점에 기록된 (st_dev, st_ino)와 열린 fd를 비교한다 — 부모
    디렉터리까지 교체된 경우 다른 inode를 지우는 일이 없다.
    """
    try:
        root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        info = os.fstat(root_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or (expected is not None and (info.st_dev, info.st_ino) != expected)):
            return False
        return _remove_tree_fd(root_fd)
    except (OSError, RecursionError):
        return False
    finally:
        os.close(root_fd)


def _host_environment(work):
    return {"HOME": str(work), "TMPDIR": str(work), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


def run_host_recipe(bundle, recipe, inputs, run, deadline, *, cancel=None, started=None):
    """Stage sealed inputs and run one pinned host recipe in its own process group.

    Shared by the trusted backend and qualification probes; never reachable
    from candidate code. The run directory is emptied before returning so the
    journal can finalize it. A cancellation or deadline observed before launch
    prevents the process from starting at all; a stop that cannot be confirmed
    returns ``stopped: False`` so the journal quarantines instead of looping.
    ``started``(threading.Event)가 주어지면 프로세스 시작 직후 set된다 —
    probe가 launch를 관측한 뒤 취소할 수 있게 한다.
    """
    lifecycle = {"configured": False, "started": False, "connected": False, "stopped": False}
    process, artifacts, exit_code = None, None, None
    stopped, kill_sent, cleaned = True, False, False
    try:
        try:
            inputs.write_new(run.directory / "input")
            work = run.directory / "work"
            work.mkdir(mode=0o700)
            lifecycle["configured"] = True
            argv = list(recipe["argv"])
            require(argv[0] in bundle.tool_paths, "Host recipe must run a pinned host tool")
            # Launch은 마지막으로 확인한다: 이미 취소되거나 기한이 지난 run은
            # 프로세스를 시작하지 않는다.
            if (not run.cancelled() and not (cancel is not None and cancel.is_set())
                    and time.monotonic() < deadline):
                process = subprocess.Popen(argv, cwd=work, env=_host_environment(work),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                stopped = False
                lifecycle["started"] = True
                if started is not None:
                    started.set()
                kill_deadline = None
                while process.poll() is None:
                    cancelled = run.cancelled() or (cancel is not None and cancel.is_set())
                    if cancelled or time.monotonic() >= deadline:
                        if kill_deadline is None:
                            _kill_host_group(process)
                            kill_deadline = time.monotonic() + 10
                        elif time.monotonic() >= kill_deadline:
                            break  # 종료를 확인할 수 없다 — 저널이 격리로 처리한다.
                    time.sleep(0.05)
                kill_sent = kill_deadline is not None
                exit_code = process.returncode
                if exit_code == 0:
                    try:
                        artifacts = BlobSet.from_directory(work, recipe["outputPaths"],
                                                           max_bytes=recipe["maxOutputBytes"])
                    except ArtifactError:
                        artifacts = None
                lifecycle["connected"] = exit_code is not None
        except (ContractError, ResourceError, ArtifactError, OSError):
            # 시작 전/스테이징 실패도 구조화된 결과로 반환해 lifecycle·종료
            # 증거가 호출자에 도달하게 한다. 프로세스가 없으면 stopped=True다.
            pass
    finally:
        if process is not None:
            if process.poll() is None:
                _kill_host_group(process)
            stopped = not _host_group_alive(process)
        lifecycle["stopped"] = stopped
        cleaned = _discard_run_tree(run.directory,
                                    expected=getattr(run, "directory_identity", None))
    return {"lifecycle": lifecycle, "exitCode": exit_code, "artifacts": artifacts,
            "stopped": stopped, "killSent": kill_sent, "cleaned": cleaned}


class HostBuildBackend:
    """Non-isolated build backend; pinned recipes run as host child processes.

    This backend does not isolate candidate code: the sealed input runs with
    ordinary host network and filesystem access. Every result it returns is
    labelled ``isolation: "host"`` so it cannot be confused with a guest-VM
    build.
    """

    isolation = "host"
    scope_kind = "host"

    def __init__(self, backend_id, authority, bundle, store, *, artifact_authority=None):
        validate_id(backend_id)
        if (type(authority) is not QualificationAuthority or type(bundle) is not HostBuildBundle
                or type(store) is not RunStore or store.environment_digest != bundle.environment_digest):
            raise ExecutionDenied("Trusted host build composition required")
        self.backend_id, self.authority, self.bundle, self.store = backend_id, authority, bundle, store
        self.execution_class = "host-build"
        if artifact_authority is not None and type(artifact_authority) is not ArtifactValidationAuthority:
            raise ExecutionDenied("Trusted artifact authority required")
        self.artifact_authority = artifact_authority

    def _check(self, request, authorization):
        self.authority.check_authorization(authorization, request, evaluated_at_ms=int(time.time() * 1000))
        if (request["backendId"] != self.backend_id or request["executionClass"] != self.execution_class
                or request["environmentDigest"] != self.bundle.environment_digest):
            raise ExecutionDenied("Host build execution binding mismatch")

    def cancel(self, operation_id, request_digest):
        try:
            self.store.cancel(operation_id, request_digest)
        except (RunDenied, OSError):
            raise ExecutionDenied("Host build cancellation rejected") from None

    def reconcile(self, operation_id, request_digest):
        # 호스트 run에는 신뢰할 수 있는 종료 증거가 없다: termination.json과
        # 저널 파일은 같은 UID의 후보 프로세스가 쓸 수 있다. 위조된 정지 증거로
        # 격리를 풀 수 없게 복구를 거절한다 — 운영자가 저널을 직접 재설정한다.
        raise ExecutionDenied("Host build recovery requires operator journal reset")

    def execute(self, request, authorization, inputs):
        try:
            request = validate_execution_request(request)
            self._check(request, authorization)
            if request["inputKind"] == "validated-artifact":
                require(self.artifact_authority is not None, "Validated artifact authority required")
                inputs = self.artifact_authority.require_input(inputs, input_digest=request["inputDigest"],
                    project_digest=request["projectDigest"], execution_class=request["executionClass"])
            require(type(inputs) is BlobSet and request["inputDigest"] == inputs.digest,
                    "Sealed execution input required")
            recipe = self.bundle.recipe(request["recipeId"])
            require(recipe["executionClass"] == self.execution_class
                    and recipe["artifactPolicyId"] == request["artifactPolicyId"]
                    and recipe["cleanupPolicyId"] == request["cleanupPolicyId"], "Host recipe binding mismatch")
            self.bundle.verify()
            self._check(request, authorization)
            with self.store.machine_lease(self.bundle.machine_digest, kind='host'):
                with self.store.admit(request["operationId"], digest(request),
                                      disk_bytes=self.bundle.overlay_bytes) as run:
                    return self._execute_owned(request, authorization, inputs, recipe, run)
        except (ContractError, ResourceError, ArtifactError, RunDenied, OSError):
            raise ExecutionDenied("Protected host execution rejected") from None

    def _execute_owned(self, request, authorization, inputs, recipe, run):
        outcome, reason = "failed", "authorization-denied"
        lifecycle = {"configured": False, "started": False, "connected": False, "stopped": False}
        artifacts = report = None
        # launch 전에는 프로세스가 존재하지 않는다 — stopped=True가 정직한 초기값이다.
        # run_host_recipe가 반환하면 실제 관측값으로 교체된다.
        stopped = True
        try:
            self._check(request, authorization)
            if run.cancelled():
                raise ExecutionDenied("Host build execution cancelled")
            remaining = min(recipe["timeoutMs"] / 1000,
                            (authorization.expires_at_ms - int(time.time() * 1000)) / 1000)
            require(remaining > 0, "Host build authorization expired")
            reason = "candidate-execution-failed"
            outcome_run = run_host_recipe(self.bundle, recipe, inputs, run,
                                          time.monotonic() + remaining)
            lifecycle = outcome_run["lifecycle"]
            stopped = outcome_run["stopped"]
            exit_code = outcome_run["exitCode"]
            artifacts = outcome_run["artifacts"]
            report = {"exitCode": exit_code if exit_code is not None else -1, "outputTruncated": False}
            reason = ("candidate-exit-nonzero" if exit_code != 0 else "artifact-missing")
            if exit_code == 0 and artifacts is not None:
                outcome = "succeeded"
                reason = "candidate-output"
        except (ExecutionDenied, ContractError, ResourceError, ArtifactError, OSError):
            outcome = "failed"
        finally:
            if not all(lifecycle.values()):
                outcome = "failed"
                if reason == "candidate-output":
                    reason = "native-lifecycle-unconfirmed"
            run.finish(outcome, stopped=stopped)
        state = self.store.status(request["operationId"])["state"]
        status = "candidate-output" if state == "succeeded" else state
        if state == "quarantined":
            reason = "process-stop-unconfirmed" if not stopped else "work-cleanup-unconfirmed"
        elif state == "cancelled":
            reason = "cancelled"
        published = artifacts if status == "candidate-output" else None
        return GuestExecutionResult(request["operationId"], digest(request), status, reason, inputs.digest,
                                    published.digest if published is not None else None, published,
                                    report, lifecycle, state != "quarantined", isolation="host")
