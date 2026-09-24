#!/usr/bin/env python3
"""Compile and verify the bounded AVFoundation H.264 segment route."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reproof.live.clock_sync import ClockReading, ClockSynchronizer, RecordingStamp
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore
from reproof.live.recording_session import FramePublication, RecordingStore
from reproof.live.video import (EncoderFrame, VideoFrameSink, VideoLimits,
                                 encode_configuration, encode_frame, encode_finish)


MAX_PROCESS_OUTPUT = 128 * 1024
CORPUS = ROOT / "artifacts/qa-delivery/native-probes/video-motion-corpus/manifest.json"
PARENT_READER = ROOT / "artifacts/qa-delivery/native-probes/verify-video-output"
PARENT_READER_SOURCE = ROOT / "artifacts/qa-delivery/native-probes/verify-video-output.swift"
HELPER_SOURCE = ROOT / "native/macos-video/Sources/ReproVideo/main.swift"
PACKAGE_SOURCE = ROOT / "native/macos-video/Package.swift"


class VerificationFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationFailure(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def owned_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    stat = path.lstat()
    require(path.is_dir() and not path.is_symlink() and stat.st_uid == os.getuid(),
            "output_directory_invalid")
    return path


def stop_group(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_bounded(command, *, cwd, timeout, maximum=MAX_PROCESS_OUTPUT,
                stdin_data=None, environment=None):
    process = subprocess.Popen(
        [str(item) for item in command], cwd=cwd,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, close_fds=True,
        env=environment,
    )
    if stdin_data is not None:
        process.stdin.write(stdin_data)
        process.stdin.close()
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    exceeded = False
    started = time.monotonic()
    with selectors.DefaultSelector() as selector:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map() or process.poll() is None:
            if time.monotonic() - started >= timeout:
                stop_group(process)
                raise VerificationFailure("process_timeout")
            for key, _ in selector.select(0.05):
                body = streams[key.fileobj]
                remaining = maximum + 1 - len(body)
                chunk = os.read(key.fd, min(65536, max(1, remaining)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                body.extend(chunk)
                if len(body) > maximum:
                    exceeded = True
                    stop_group(process)
                    break
            if exceeded:
                break
    process.wait()
    for stream in streams:
        stream.close()
    if exceeded:
        raise VerificationFailure("process_output_limit")
    return process.returncode, bytes(streams[process.stdout]), bytes(streams[process.stderr])


def atomic_json(path, value):
    body = (json.dumps(value, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".part")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


class CorpusClock:
    def __init__(self):
        self.clock_id = "g3-corpus-clock"
        self.boot_digest = "c" * 64
        self.nanoseconds = 1_000_000_000

    def read(self):
        return ClockReading(
            self.clock_id, self.boot_digest, self.nanoseconds, 0,
        )


def project_document():
    with (ROOT / "tests/fixtures/release/project.json").open("rb") as source:
        return json.load(source)


def collection_policy():
    return {
        "schemaVersion": 1,
        "captureMode": "test-data",
        "retentionSeconds": {
            "original": 2_592_000,
            "intermediate": 86_400,
            "derivative": 86_400,
            "export": 604_800,
        },
    }


def preparation():
    return [{
        "receiptId": "prepare_one",
        "recipeId": "seed_account",
        "operation": "prepare",
        "status": "complete",
        "projectId": "checkout",
        "applicationId": "ios_app",
        "startedAtMs": 4_998_000,
        "completedAtMs": 4_999_000,
        "payloadDigest": "2" * 64,
    }]


def compile_helper(output):
    module_cache = output / "module-cache"
    module_cache.mkdir(mode=0o700)
    build_path = output / "swift-build"
    build_environment = os.environ.copy()
    build_environment["CLANG_MODULE_CACHE_PATH"] = str(module_cache)
    build_environment["SWIFTPM_MODULECACHE_OVERRIDE"] = str(module_cache)
    command = [
        "/usr/bin/xcrun", "swift", "build", "--disable-sandbox",
        "-debug-info-format", "none", "--package-path",
        str(ROOT / "native/macos-video"), "--build-path", str(build_path),
        "-c", "release",
    ]
    code, stdout, stderr = run_bounded(
        command, cwd=ROOT, timeout=90, environment=build_environment,
    )
    require(code == 0, "helper_compile_failed")
    candidates = [
        path for path in build_path.rglob("ReproVideo")
        if path.is_file() and not path.is_symlink() and os.access(path, os.X_OK)
    ]
    require(len(candidates) == 1, "helper_output_invalid")
    helper = candidates[0]
    require(helper.is_file() and not helper.is_symlink()
            and helper.stat().st_uid == os.getuid(), "helper_output_invalid")
    version_code, version_out, version_error = run_bounded(
        ["/usr/bin/xcrun", "swiftc", "--version"], cwd=ROOT, timeout=10,
    )
    version_body = version_out + version_error
    require(version_code == 0 and version_body, "compiler_version_unavailable")
    return helper, {
        "command": ["xcrun", "swift", "build", "--disable-sandbox",
                    "-debug-info-format", "none", "--package-path",
                    "native/macos-video", "--build-path",
                    "<owned-output>/swift-build", "-c", "release"],
        "stdoutBytes": len(stdout),
        "stderrBytes": len(stderr),
        "compilerVersion": version_body.decode("utf-8", "replace").strip(),
        "helperSha256": sha256_file(helper),
        "sourceSha256": sha256_file(HELPER_SOURCE),
        "packageSha256": sha256_file(PACKAGE_SOURCE),
    }


def load_corpus():
    raw = CORPUS.read_bytes()
    require(len(raw) <= 128 * 1024, "corpus_manifest_too_large")
    manifest = json.loads(raw)
    require(type(manifest) is dict and manifest.get("schemaVersion") == 1
            and manifest.get("frameCount") == 24
            and type(manifest.get("frames")) is list
            and len(manifest["frames"]) == 24, "corpus_manifest_invalid")
    return manifest


def encode_corpus(output, helper):
    runtime = output / "runtime"
    runtime.mkdir(mode=0o700)
    budget = DiskBudget(
        runtime / "budget", capacity_bytes=192 * 1024 * 1024,
        journal_headroom_bytes=2 * 1024 * 1024,
    )
    evidence = EvidenceStore(runtime / "evidence", budget)
    clock = CorpusClock()
    recordings = RecordingStore(
        runtime / "recordings", evidence, ClockSynchronizer(clock),
        wall_clock_ms=lambda: 5_000_000,
    )
    sink = None
    try:
        registration = recordings.register_project(
            project_document(), collection_policy(),
        )
        session = recordings.begin_recording(
            registration,
            recording_id="recording_g3_corpus",
            session_id="session_g3_corpus",
            application_id="ios_app",
            build_id="original",
            device_identity={
                "bundle": "com.example.app", "artifactDigest": "0" * 64,
            },
            provider_incarnation="provider_g3_corpus",
            preparation_receipts=preparation(),
        )
        limits = VideoLimits(
            segment_duration_ms=10_000,
            max_frame_bytes=1024 * 1024,
            max_decoded_pixels=4_194_304,
            max_queue_frames=16,
            max_queue_bytes=16 * 1024 * 1024,
            max_frames_per_segment=16,
            max_segments=4,
            max_segment_bytes=8 * 1024 * 1024,
            max_total_video_bytes=16 * 1024 * 1024,
            max_active_finalizers=1,
            finalization_timeout_seconds=15,
        )
        sink = VideoFrameSink(
            evidence, runtime / "video", helper=helper, limits=limits,
        )
        session.attach_frame_sink(sink)
        corpus = load_corpus()
        base = clock.nanoseconds
        for sequence, item in enumerate(corpus["frames"], 1):
            source = CORPUS.parent / item["path"]
            body = source.read_bytes()
            require(len(body) == item["bytes"]
                    and hashlib.sha256(body).hexdigest() == item["sha256"],
                    "source_corpus_changed")
            clock.nanoseconds = base + item["captureOffsetMs"] * 1_000_000
            publication = session.record_frame(
                body, item["mimeType"], item["width"], item["height"],
                item["geometry"], acquisition_sequence=sequence,
                timing_source="host-acquired",
            )
            require(publication is not None, "source_frame_not_published")
        result = session.stop()
        manifest = sink.manifest()
        segment_paths = [
            (runtime / "evidence" / segment["path"]).resolve()
            for segment in manifest["segments"]
        ]
        codec_unavailable = any(
            loss["reason"] == "codec-unavailable"
            for loss in manifest["losses"]
        )
        if codec_unavailable:
            return {
                "status": "blocked",
                "reason": "avfoundation-h264-encoder-unavailable-in-worker-sandbox",
                "recordingStatus": result["status"],
                "manifest": manifest,
                "segmentPaths": [],
            }
        require(result["status"] == "frozen-complete", "recording_incomplete")
        require(manifest["status"] == "complete"
                and len(segment_paths) == 2, "video_manifest_incomplete")
        for path in segment_paths:
            require(path.is_file() and not path.is_symlink(),
                    "durable_segment_missing")
        decoder_directory = owned_directory(output / "playable-segments")
        decoder_paths = []
        for index, (path, segment) in enumerate(zip(segment_paths, manifest["segments"]), 1):
            body = path.read_bytes()
            require(len(body) == segment["bytes"]
                    and hashlib.sha256(body).hexdigest() == segment["digest"],
                    "durable_segment_changed")
            copied = decoder_directory / f"segment-{index:03d}.mp4"
            with copied.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            decoder_paths.append(copied)
        code, stdout, stderr = run_bounded(
            [PARENT_READER, CORPUS, *decoder_paths], cwd=ROOT, timeout=30,
        )
        require(code == 0 and not stderr, "independent_decode_failed")
        independent = json.loads(stdout)
        require(independent.get("passed") is True
                and independent.get("decodedFrameCount") == 24,
                "independent_decode_incomplete")
        return {
            "status": "pass",
            "recordingStatus": result["status"],
            "manifest": manifest,
            "segmentPaths": [str(path.relative_to(output)) for path in decoder_paths],
            "independentDecode": independent,
        }
    finally:
        if sink is not None:
            sink.close()
        recordings.close()
        evidence.close()
        budget.close()


def exercise_protocol_rejections(output, helper):
    cases = {
        "truncated-magic": b"RLV",
        "oversized-configuration": b"RLVID001" + struct.pack(">I", 4097),
        "truncated-configuration": b"RLVID001" + struct.pack(">I", 10) + b"{}",
        "unknown-protocol": b"RLVID999" + struct.pack(">I", 2) + b"{}",
    }
    source = load_corpus()["frames"][0]
    body = (CORPUS.parent / source["path"]).read_bytes()
    require(hashlib.sha256(body).hexdigest() == source["sha256"], "source_corpus_changed")
    limits = VideoLimits()
    configuration = encode_configuration(width=source["width"], height=source["height"], limits=limits)
    publication = FramePublication(
        source["sha256"], len(body), source["path"], "image/png",
        source["width"], source["height"], "portrait", 1,
        RecordingStamp(5_000_000, 0, 0, 0, 0), "host-acquired",
    )
    frame = encode_frame(EncoderFrame.from_publication(
        publication, provider_incarnation="provider_protocol", native_incarnation="native_protocol"
    ), body, 0, limits)
    metadata_size = struct.unpack(">I", frame[1:5])[0]
    metadata = frame[9:9 + metadata_size]
    finish = encode_finish()

    def changed(raw, **values):
        return json.dumps(dict(json.loads(raw), **values), sort_keys=True,
                          separators=(",", ":")).encode()

    for name, raw in (
        ("duplicate-config-key", configuration[12:].replace(b'"schemaVersion":1', b'"schemaVersion":1,"schemaVersion":1')),
        ("escaped-duplicate-key", configuration[12:].replace(b'"schemaVersion":1', b'"schemaVersion":1,"\\u0073chemaVersion":1')),
        ("float-schema-version", changed(configuration[12:], schemaVersion=1.0)),
        ("boolean-schema-version", changed(configuration[12:], schemaVersion=True)),
        ("unknown-config-field", changed(configuration[12:], outputPath="other.mp4")),
        ("pixel-limit", changed(configuration[12:], width=4096, height=4096)),
    ):
        cases[name] = configuration[:8] + struct.pack(">I", len(raw)) + raw + frame + finish
    for name, raw in (
        ("duplicate-frame-key", metadata.replace(b'"schemaVersion":1', b'"schemaVersion":1,"schemaVersion":1')),
        ("float-frame-sequence", changed(metadata, acquisitionSequence=1.0)),
        ("excess-pts", changed(metadata, ptsNs=9_000_000_000_000_000_000)),
        ("nonzero-first-pts", changed(metadata, ptsNs=100_000_000)),
        ("wrong-digest", changed(metadata, digest="0" * 64)),
        ("mime-mismatch", changed(metadata, mimeType="image/jpeg")),
    ):
        cases[name] = configuration + b"F" + struct.pack(">II", len(raw), len(body)) + raw + body + finish
    cases.update({
        "truncated-frame": configuration + frame[:-5],
        "oversized-frame-length": configuration + b"F" + struct.pack(">II", 1, 0x7fffffff),
        "duplicate-sequence": configuration + frame + frame + finish,
        "trailing-input": configuration + frame + finish + b"x",
        "empty-segment": configuration + finish,
    })
    results = []
    for name, wire in cases.items():
        directory = output / ("protocol-" + name)
        directory.mkdir(mode=0o700)
        code, stdout, stderr = run_bounded(
            [helper, "--protocol-stdio"], cwd=directory, timeout=5,
            stdin_data=wire,
        )
        expected_acks = 1 if name in {"duplicate-sequence", "trailing-input"} else 0
        acknowledgements = [json.loads(line) for line in stdout.splitlines()]
        require(code != 0 and len(acknowledgements) == expected_acks
                and all(item.get("type") == "accepted" for item in acknowledgements)
                and len(stderr) <= 4096,
                "malformed_protocol_was_accepted")
        require(not (directory / "segment.mp4").exists()
                and not (directory / "segment.partial.mp4").exists(),
                "malformed_protocol_left_output")
        results.append({
            "case": name, "rejected": True, "returncode": code,
            "stderrBytes": len(stderr), "acceptedFrames": len(acknowledgements),
        })
    return results


def exercise_fault_case(output, helper, name, *, fault_mode="none",
                        malformed=False, zero=False, declared_losses=False,
                        storage_fault=False):
    runtime = output / ("fault-" + name)
    runtime.mkdir(mode=0o700)
    storage = {"failNext": False}

    def available_storage():
        if storage["failNext"]:
            storage["failNext"] = False
            return 0
        return 1 << 30

    budget = DiskBudget(
        runtime / "budget", capacity_bytes=64 * 1024 * 1024,
        journal_headroom_bytes=1024 * 1024,
        free_bytes=available_storage,
    )
    evidence = EvidenceStore(runtime / "evidence", budget)
    clock = CorpusClock()
    recordings = RecordingStore(
        runtime / "recordings", evidence, ClockSynchronizer(clock),
        wall_clock_ms=lambda: 5_000_000,
    )
    sink = None
    try:
        registration = recordings.register_project(
            project_document(), collection_policy(),
        )
        session = recordings.begin_recording(
            registration,
            recording_id="recording_" + name.replace("-", "_"),
            session_id="session_" + name.replace("-", "_"),
            application_id="ios_app", build_id="original",
            device_identity={
                "bundle": "com.example.app", "artifactDigest": "0" * 64,
            },
            provider_incarnation="provider_" + name.replace("-", "_"),
            preparation_receipts=preparation(),
        )
        limits = VideoLimits(
            segment_duration_ms=1000,
            max_frame_bytes=1024 * 1024,
            max_queue_frames=4,
            max_queue_bytes=4 * 1024 * 1024,
            max_frames_per_segment=4,
            max_segments=2,
            max_segment_bytes=4 * 1024 * 1024,
            max_total_video_bytes=4 * 1024 * 1024,
            max_active_finalizers=1,
            finalization_timeout_seconds=1,
        )
        sink = VideoFrameSink(
            evidence, runtime / "video", helper=helper, limits=limits,
            fault_mode=fault_mode,
        )
        session.attach_frame_sink(sink)
        if declared_losses:
            sink.declare_loss(
                "native", reason="native-no-sample",
                start_offset_ms=0, end_offset_ms=40,
            )
            sink.declare_loss(
                "transport", reason="transport-drop",
                start_offset_ms=40, end_offset_ms=80,
                acquisition_sequence=1,
            )
            sink.declare_loss(
                "queue", reason="queue-drop",
                start_offset_ms=80, end_offset_ms=120,
                acquisition_sequence=2,
            )
        elif not zero:
            corpus = load_corpus()
            source_items = corpus["frames"][:2]
            base = clock.nanoseconds
            for sequence, item in enumerate(source_items, 1):
                body = (CORPUS.parent / item["path"]).read_bytes()
                if malformed and sequence == 1:
                    body = b"not-a-decodable-png"
                clock.nanoseconds = base + item["captureOffsetMs"] * 1_000_000
                session.record_frame(
                    body, "image/png", item["width"], item["height"],
                    item["geometry"], acquisition_sequence=sequence,
                    timing_source="host-acquired",
                )
            if storage_fault:
                storage["failNext"] = True
        started = time.monotonic()
        frozen = session.stop()
        elapsed = time.monotonic() - started
        manifest = sink.manifest()
        require(elapsed <= 3.0, "fault_finalization_was_unbounded")
        require(frozen["status"] == "frozen-incomplete"
                and manifest["status"] == "incomplete",
                "fault_case_was_not_incomplete")
        work = runtime / "video" / "work"
        require(not list(work.rglob("*.mp4")), "failed_segment_was_not_cleaned")
        return {
            "case": name,
            "elapsedSeconds": round(elapsed, 3),
            "failureReason": manifest["failureReason"],
            "lossClasses": sorted({item["lossClass"]
                                   for item in manifest["losses"]}),
            "lossReasons": sorted({item["reason"]
                                   for item in manifest["losses"]}),
            "segments": len(manifest["segments"]),
        }
    finally:
        if sink is not None:
            sink.close()
        recordings.close()
        evidence.close()
        budget.close()


def exercise_failure_cases(output, helper):
    results = [
        exercise_fault_case(output, helper, "zero", zero=True),
        exercise_fault_case(output, helper, "declared-losses",
                            declared_losses=True),
        exercise_fault_case(output, helper, "killed-encoder",
                            fault_mode="stall-after-first-accept"),
        exercise_fault_case(output, helper, "finalization-timeout",
                            fault_mode="stall-finalization"),
        exercise_fault_case(output, helper, "injected-write-failure",
                            fault_mode="fail-write"),
        exercise_fault_case(output, helper, "injected-disk-full",
                            storage_fault=True),
        exercise_fault_case(output, helper, "malformed-frame", malformed=True),
    ]
    by_name = {item["case"]: item for item in results}
    require(by_name["zero"]["lossClasses"] == ["not-acquired"],
            "zero_frame_loss_class_invalid")
    require(set(by_name["declared-losses"]["lossClasses"])
            == {"not-acquired", "captured-dropped"},
            "declared_loss_classes_invalid")
    require("encoder-accepted-not-durable"
            in by_name["killed-encoder"]["lossClasses"],
            "killed_encoder_acceptance_loss_missing")
    require(by_name["finalization-timeout"]["failureReason"]
            == "finalization-timeout",
            "finalization_timeout_reason_missing")
    require("encoder-accepted-not-durable"
            in by_name["injected-write-failure"]["lossClasses"],
            "write_failure_acceptance_loss_missing")
    require("video-storage-exhausted"
            in by_name["injected-disk-full"]["lossReasons"],
            "disk_full_loss_missing")
    require("captured-dropped"
            in by_name["malformed-frame"]["lossClasses"],
            "malformed_frame_loss_missing")
    return results


def output_directory(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stat = root.lstat()
    require(root.is_dir() and not root.is_symlink() and stat.st_uid == os.getuid(),
            "output_root_invalid")
    name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"
    return owned_directory(root / name)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("avfoundation",), required=True)
    parser.add_argument("--cases", choices=("corpus", "all"), required=True)
    parser.add_argument("--output-root", type=Path,
                        default=ROOT / "artifacts/qa-delivery/g3-video-acceptance")
    parser.add_argument("--output-new", action="store_true")
    args = parser.parse_args(argv)
    if not args.output_new:
        print(json.dumps({"status": "fail", "reason": "output_new_required"},
                         sort_keys=True))
        return 2
    output = None
    try:
        output = output_directory(args.output_root)
        helper, compilation = compile_helper(output)
        corpus = encode_corpus(output, helper)
        status = corpus["status"]
        result = {
            "schemaVersion": 1,
            "status": status,
            "backend": "avfoundation",
            "actualEncoding": (
                "avfoundation-h264-mp4" if status == "pass"
                else "not-produced"
            ),
            "requestedEncoding": "avfoundation-h264-mp4",
            "cases": args.cases,
            "outputDirectory": str(output),
            "compilation": compilation,
            "corpusSha256": sha256_file(CORPUS),
            "independentReader": {
                "binarySha256": sha256_file(PARENT_READER),
                "sourceSha256": sha256_file(PARENT_READER_SOURCE),
            },
            "segments": len(corpus["manifest"]["segments"]),
            "manifest": corpus["manifest"],
            "retainedSegmentPaths": corpus["segmentPaths"],
        }
        if "independentDecode" in corpus:
            result["independentDecode"] = corpus["independentDecode"]
        if "reason" in corpus:
            result["reason"] = corpus["reason"]
        if status == "pass" and args.cases == "all":
            result["protocolRejections"] = exercise_protocol_rejections(
                output, helper
            )
            result["failureCases"] = exercise_failure_cases(output, helper)
        atomic_json(output / "result.json", result)
        print(json.dumps(result, sort_keys=True))
        return 0 if status == "pass" else 3
    except Exception as error:
        result = {
            "schemaVersion": 1,
            "status": "fail",
            "reason": str(error) if type(error) is VerificationFailure
            else "verification_failed",
        }
        if output is not None:
            try:
                atomic_json(output / "result.json", result)
            except Exception:
                pass
        print(json.dumps(result, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
