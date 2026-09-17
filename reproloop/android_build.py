"""Create and validate immutable Android replay build directories."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import tempfile

from .core import ContractError, digest, require
from .resources import resource_root
from .repair import build_android, copy_source, snapshot_source
from .storage import MAX_APK, read_json, sha_file, write_json


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAMPLE_TASK = {
    "buggy": (":sample:assembleBuggyDebug", "sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk"),
    "fixed": (":sample:assembleFixedDebug", "sample/build/outputs/apk/fixed/debug/sample-fixed-debug.apk"),
}
_DRIVER_TASK = ":driver:assembleDebug"
_DRIVER_APK = "driver/build/outputs/apk/debug/driver-debug.apk"


def _input_policy(profile):
    return ('explicit-public-android-inputs-v2' if 'sourceInputs' in profile.data
            else 'isolated-public-kotlin-dsl-v1')


def _copy_artifact(source: Path, destination: Path) -> str:
    require(source.is_file() and not source.is_symlink(), "Build artifact is missing or linked")
    require(0 < source.stat().st_size <= MAX_APK, "Build artifact is invalid")
    with source.open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    destination.chmod(0o600)
    return sha_file(destination)


def create_protected_build(source, output, *, gradle, java_home, sdk_home,
                           variant="buggy", timeout=300, app_profile=None):
    """Build the sample and driver, then freeze both APKs with one receipt.

    The output directory is created atomically and is never reused.  The
    receipt's source proof is taken before either build and both build proofs
    must refer to that exact source snapshot.
    """
    if app_profile is not None:
        require(variant == 'buggy', 'An app profile chooses its own fixed build task')
        return _create_profile_build(source, output, app_profile, gradle=gradle,
                                     java_home=java_home, sdk_home=sdk_home, timeout=timeout)
    source = Path(source)
    output = Path(output)
    require(variant in _SAMPLE_TASK, "Unsupported Android sample variant")
    require(source.is_dir() and not source.is_symlink(), "Android source directory is invalid")
    source = source.resolve()
    require(not output.exists() and not output.is_symlink(), "Protected build output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)

    source_files = snapshot_source(source)
    source_digest = digest(source_files)
    sample_task, sample_relative = _SAMPLE_TASK[variant]
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=str(output.parent)))
    try:
        sample_apk, sample_proof = build_android(
            source, gradle=gradle, java_home=java_home, sdk_home=sdk_home,
            task=sample_task, apk_relative=sample_relative, timeout=timeout,
        )
        driver_apk, driver_proof = build_android(
            source, gradle=gradle, java_home=java_home, sdk_home=sdk_home,
            task=_DRIVER_TASK, apk_relative=_DRIVER_APK, timeout=timeout,
        )
        require(snapshot_source(source) == source_files, "Build modified protected source")
        for proof in (sample_proof, driver_proof):
            require(proof.get("buildCompleted") is True
                    and proof.get("sourceDigest") == source_digest
                    and proof.get("sourceFiles") == source_files,
                    "Build proof does not match frozen source")

        original_sha = _copy_artifact(Path(sample_apk), staging / "original.apk")
        driver_sha = _copy_artifact(Path(driver_apk), staging / "driver.apk")
        require(sample_proof.get("apkSha256") == original_sha, "Sample APK digest changed during freeze")
        require(driver_proof.get("apkSha256") == driver_sha, "Driver APK digest changed during freeze")
        receipt = {
            "schemaVersion": 1,
            "platform": "android",
            "variant": variant,
            "buildCompleted": True,
            "sourceDigest": source_digest,
            "sourceFiles": source_files,
            "buildTask": sample_task,
            "apkSha256": original_sha,
            "driverSha256": driver_sha,
            "driverProof": driver_proof,
            "sampleProof": sample_proof,
            "artifacts": {"original.apk": original_sha, "driver.apk": driver_sha},
        }
        write_json(staging / "receipt.json", receipt)
        os.replace(staging, output)
        return receipt
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_protected_build(directory, *, source=None, app_profile=None):
    """Validate an immutable build directory and return its receipt."""
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), "Protected build directory is invalid")
    directory = directory.resolve()
    receipt = read_json(directory / "receipt.json")
    if receipt.get('schemaVersion') == 2:
        return _validate_profile_build(directory, receipt, source, app_profile)
    require(app_profile is None, 'A legacy build has no selected app profile')
    require(isinstance(receipt, dict) and receipt.get("schemaVersion") == 1
            and receipt.get("platform") == "android"
            and receipt.get("buildCompleted") is True,
            "Unsupported Android protected build receipt")
    source_files = receipt.get("sourceFiles")
    source_digest = receipt.get("sourceDigest")
    require(isinstance(source_files, dict) and source_files and source_digest == digest(source_files),
            "Invalid Android source proof")
    require(isinstance(receipt.get("buildTask"), str) and receipt["buildTask"],
            "Missing Android sample build task")
    driver_proof = receipt.get("driverProof")
    require(isinstance(driver_proof, dict)
            and driver_proof.get("buildCompleted") is True
            and driver_proof.get("sourceDigest") == source_digest
            and driver_proof.get("sourceFiles") == source_files
            and driver_proof.get("buildTask") == _DRIVER_TASK,
            "Invalid Android driver build proof")

    for name, field in (("original.apk", "apkSha256"), ("driver.apk", "driverSha256")):
        artifact = directory / name
        require(artifact.is_file() and not artifact.is_symlink(), f"Missing protected artifact: {name}")
        expected = receipt.get(field)
        require(isinstance(expected, str) and _SHA256.fullmatch(expected)
                and sha_file(artifact) == expected,
                f"Protected artifact digest mismatch: {name}")
    require(driver_proof.get("apkSha256") == receipt["driverSha256"],
            "Driver proof digest mismatch")
    if "sampleProof" in receipt:
        sample_proof = receipt["sampleProof"]
        require(isinstance(sample_proof, dict)
                and sample_proof.get("sourceDigest") == source_digest
                and sample_proof.get("apkSha256") == receipt["apkSha256"],
                "Sample build proof digest mismatch")
    if "artifacts" in receipt:
        require(receipt["artifacts"] == {
            "original.apk": receipt["apkSha256"], "driver.apk": receipt["driverSha256"]},
                "Protected artifact index mismatch")

    if source is not None:
        current = snapshot_source(Path(source))
        require(current == source_files and digest(current) == source_digest,
                "Android source differs from protected build receipt")
    return receipt


def _create_profile_build(source, output, profile, **toolchain):
    source, output = Path(source).resolve(), Path(output).resolve()
    require(source.is_dir() and not output.exists() and not output.is_relative_to(source),
            'Use a new protected output outside the app source directory')
    instrumentation = None
    if profile.data.get('captureMode') == 'debug_receiver':
        from .instrumentation import validate_instrumented_source
        instrumentation = validate_instrumented_source(source, profile)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.' + output.name + '.', dir=output.parent))
    try:
        # Both original and candidate builds use the same public input policy.
        inputs = profile.data.get('sourceInputs')
        source_files = copy_source(source, staging / 'app-source', source_inputs=inputs)
        require(profile.data['edit']['path'] in source_files, 'Configured product edit file is not in the public build inputs')
        runner_source = resource_root() / 'android'
        runner_files = copy_source(runner_source, staging / 'driver-source')
        build = profile.data['build']
        original, proof = build_android(staging / 'app-source', task=build['task'], app_profile=profile,
                                        apk_relative=build['apk'], **toolchain)
        driver, driver_proof = build_android(staging / 'driver-source', task=_DRIVER_TASK,
                                            apk_relative=_DRIVER_APK, **toolchain)
        require(proof['sourceFiles'] == source_files and driver_proof['sourceFiles'] == runner_files
                and snapshot_source(source, source_inputs=inputs) == source_files,
                'Protected original or runner source changed during build')
        apk_sha = _copy_artifact(original, staging / 'original.apk')
        driver_sha = _copy_artifact(driver, staging / 'driver.apk')
        require(apk_sha == proof['apkSha256'] and driver_sha == driver_proof['apkSha256'],
                'Build artifacts changed before freezing')
        write_json(staging / 'app-profile.json', profile.data)
        receipt = {'schemaVersion': 2, 'platform': 'android', 'buildCompleted': True,
            'package': profile.data['package'], 'appProfileDigest': profile.digest,
            'nativeProfileDigest': profile.native_digest, 'buildInputPolicy': _input_policy(profile),
            'sourceFiles': source_files, 'sourceDigest': digest(source_files),
            'buildTask': build['task'], 'apkSha256': apk_sha, 'driverSha256': driver_sha,
            'driverProof': driver_proof, 'appProof': proof,
            'artifacts': {'original.apk': apk_sha, 'driver.apk': driver_sha,
                          'app-profile.json': sha_file(staging / 'app-profile.json')}}
        if instrumentation is not None:
            write_json(staging / 'instrumentation.json', instrumentation)
            receipt['instrumentationReceiptDigest'] = digest(instrumentation)
            receipt['artifacts']['instrumentation.json'] = sha_file(staging / 'instrumentation.json')
        from .build_instrumentation import is_build_instrumented, validate_bytecode_artifacts
        if is_build_instrumented(profile):
            bytecode = validate_bytecode_artifacts(staging / 'app-source', profile)
            require(proof.get('bytecodeInstrumentation') == bytecode, 'Bytecode proof changed before freezing')
            write_json(staging / 'bytecode-report.json', bytecode['report'])
            receipt['artifacts']['bytecode-report.json'] = sha_file(staging / 'bytecode-report.json')
        write_json(staging / 'receipt.json', receipt)
        os.replace(staging, output)
        return receipt
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_profile_build(directory, receipt, source, profile):
    require(profile is not None, 'Select the trusted app profile for this build')
    require(receipt.get('platform') == 'android' and receipt.get('buildCompleted') is True
            and receipt.get('appProfileDigest') == profile.digest
            and receipt.get('nativeProfileDigest') == profile.native_digest
            and receipt.get('package') == profile.data['package']
            and receipt.get('buildTask') == profile.data['build']['task']
            and receipt.get('buildInputPolicy') == _input_policy(profile),
            'Build does not match the selected app profile')
    require(read_json(directory / 'app-profile.json') == profile.data, 'Frozen app profile changed')
    files = receipt.get('sourceFiles')
    require(isinstance(files, dict) and bool(files) and receipt.get('sourceDigest') == digest(files)
            and snapshot_source(directory / 'app-source', source_inputs=profile.data.get('sourceInputs'), isolated=True) == files,
            'Frozen original build inputs changed')
    if source is not None:
        require(snapshot_source(source, source_inputs=profile.data.get('sourceInputs')) == files,
                'App source differs from its protected build')
    driver_proof = receipt.get('driverProof', {})
    require(driver_proof.get('buildCompleted') is True and driver_proof.get('buildTask') == _DRIVER_TASK
            and driver_proof.get('sourceFiles') == snapshot_source(directory / 'driver-source')
            and driver_proof.get('sourceDigest') == digest(driver_proof.get('sourceFiles')),
            'Frozen platform runner build inputs changed')
    proof = receipt.get('appProof', {})
    require(proof.get('sourceFiles') == files and proof.get('sourceDigest') == digest(files)
            and proof.get('buildCompleted') is True and proof.get('buildTask') == receipt['buildTask']
            and proof.get('apkSha256') == receipt.get('apkSha256')
            and driver_proof.get('apkSha256') == receipt.get('driverSha256'), 'Invalid protected build proof')
    expected = {'original.apk': receipt.get('apkSha256'), 'driver.apk': receipt.get('driverSha256'),
                'app-profile.json': sha_file(directory / 'app-profile.json')}
    if profile.data.get('captureMode') == 'debug_receiver':
        instrumentation = read_json(directory / 'instrumentation.json')
        require(receipt.get('instrumentationReceiptDigest') == digest(instrumentation)
                and instrumentation.get('instrumentedFiles') == files
                and instrumentation.get('instrumentedSourceDigest') == digest(files)
                and instrumentation.get('appProfileDigest') == profile.digest,
                'Instrumented build no longer matches its source transformation')
        if source is not None:
            from .instrumentation import validate_instrumented_source
            require(validate_instrumented_source(source, profile) == instrumentation,
                    'Instrumentation receipt changed after the protected build')
        expected['instrumentation.json'] = sha_file(directory / 'instrumentation.json')
    from .build_instrumentation import is_build_instrumented, validate_bytecode_artifacts
    if is_build_instrumented(profile):
        bytecode = validate_bytecode_artifacts(directory / 'app-source', profile)
        require(proof.get('bytecodeInstrumentation') == bytecode
                and read_json(directory / 'bytecode-report.json') == bytecode['report'],
                'Frozen bytecode instrumentation proof changed')
        expected['bytecode-report.json'] = sha_file(directory / 'bytecode-report.json')
    require(receipt.get('artifacts') == expected, 'Protected build artifact index changed')
    for name, checksum in expected.items():
        path = directory / name
        require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= MAX_APK
                and isinstance(checksum, str) and _SHA256.fullmatch(checksum)
                and sha_file(path) == checksum, 'Protected app build artifact changed')
    return receipt
