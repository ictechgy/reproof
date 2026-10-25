"""Finite, repository-owned Swift app-logic experiment, not mobile verification.

The native compiler executes exact pinned sample sources, with only one Boolean
return expression allowed to change. The original XCTest file is never edited.
This is not a sandbox, AI integration, SDK capture, or enterprise acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from . import contracts
from .app_logs import APP_LOG_MIME
from .execution.artifacts import ArtifactError, BlobSet, read_regular
from .ios_core import validate_swift_expression
from .issue_package import build_archive, canonical
from .project_repair import RepairError, RepairSource
from .storage import _unique_object
from .ticket_intake import RegisteredCapsule, intake_ticket

PRODUCT = 'ios/Sample/SubmissionLogic.swift'
ORACLE = 'ios/LogicTests/LogicTests.swift'
CALLSITE = 'ios/Sample/CounterViewController.swift'
FIXTURE = 'examples/native-logic-repair/'
PACKAGE = FIXTURE + 'Package.swift'
RECIPE = FIXTURE + 'recipe.json'
PROJECT = FIXTURE + 'project.json'
CASE = 'ReproLogicTests.SubmissionLogicTests/testDuplicateIsRejected'
OBJC_CASE = '-[ReproLogicTests.SubmissionLogicTests testDuplicateIsRejected]'
FILTER = r'^ReproLogicTests\.SubmissionLogicTests/testDuplicateIsRejected$'
PINS = {
    'ios/Sample/CounterLogic.swift': 'd4cc9a590b7456e12d220a200a25a247780b08f2ae6f6559444fef437cc1f2ea',
    PRODUCT: 'a97f4bf31df61dfdb0fd499c372d627174836e4104d450b631bb74ed04fb1083',
    'ios/Sample/ResetLogic.swift': '61f4135b1200f1beb34cd4571fbe71e48a007e38411539abf18df1f43bfdd523',
    ORACLE: '0c56508f9b35b4679766f5d9913384da7d80c3d814cebd07f920e087e0c81dcd',
    CALLSITE: '4fe7ace535850c681d68f495c720bde10e768a29f219fb639f0c7eaad68cae0d',
}
SOURCE_PATHS = tuple(PINS) + (PACKAGE, RECIPE)
MAPPING = {path: 'Sources/' + Path(path).name for path in PINS if path not in (ORACLE, CALLSITE)}
MAPPING.update({ORACLE: 'Tests/LogicTests.swift', PACKAGE: 'Package.swift'})
RECIPE_VALUE = {
    'schemaVersion': 1, 'kind': 'host-swift-sample-logic', 'testCase': CASE,
    'testFilter': FILTER, 'testCount': 1, 'assertionCount': 2,
    'oracleFile': ORACLE, 'editableFile': PRODUCT,
    'candidateExpressions': ['true', 'false', '!submitted'],
    'failureAssertions': {'baseline': {'line': 13, 'assertion': 'XCTAssertFalse'},
                          'wrong': {'line': 12, 'assertion': 'XCTAssertTrue'}},
    'buildTimeoutSeconds': 90, 'testTimeoutSeconds': 30, 'maxOutputBytes': 2097152,
}
# The manifest is executable Swift. Pin it independently of editable descriptors.
PACKAGE_SHA256 = '31e62f4a70ab30c1ee23fed3720915f3f918f2eb5e99546f6fe2b7778c1bf7d7'
DEVELOPER_DIR = '/Applications/Xcode_16.4.app/Contents/Developer'
EXPECTED_XCODE = 'Xcode 16.4\nBuild version 16F6'
MAX_OUTPUT = 2 * 1024 * 1024
SESSION = '00000000-0000-4000-8000-000000000011'
RUN = '00000000-0000-4000-8000-000000000012'
PROFILE = hashlib.sha256(b'repository-synthetic-duplicate-submission-v1').hexdigest()


class NativeLogicError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(value, code):
    if not value:
        raise NativeLogicError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise NativeLogicError('fixture_json') from None


def _project(source_digest):
    return {
        'schemaVersion': 1, 'id': 'native_submission_demo', 'revision': 'repository-fixture-v1',
        'trustGroup': 'local_demo',
        'applications': [{'id': 'sample_ios_source', 'platform': 'ios', 'bundle': 'io.reproof.sample.ios'}],
        'builds': [{'id': 'original', 'applicationId': 'sample_ios_source',
                    'revision': 'pinned-swift-source-not-installed-ios-build',
                    'artifactDigest': PINS[PRODUCT], 'sourceDigest': source_digest,
                    'provenance': 'operator-attestation'}],
        'variables': [], 'fixtures': [], 'observations': ['first_submission', 'duplicate_submission'],
        'evidencePolicy': {'pixels': False, 'text': False, 'accessibility': False,
                           'logs': True, 'fixtures': True, 'unknownSensitive': 'deny', 'aiEligible': False},
        'editablePaths': [PRODUCT],
        'recipes': [{'id': 'native_submission_regression', 'kind': 'regression',
                     'productFile': RECIPE, 'operation': 'regression'}],
        'executionClasses': ['host-build'],
    }


def _load_source(root):
    raw = BlobSet.from_directory(root, SOURCE_PATHS, max_bytes=128 * 1024)
    entries = dict(raw.entries)
    _require(all(_sha(entries[path]) == checksum for path, checksum in PINS.items()), 'source_pin')
    _require(_sha(entries[PACKAGE]) == PACKAGE_SHA256, 'recipe_pin')
    _require(entries[RECIPE] == (json.dumps(RECIPE_VALUE, indent=2, sort_keys=True) + '\n').encode(), 'recipe_pin')
    project = contracts.validate_project_revision(_json(read_regular(root, PROJECT, maximum=16384)))
    _require(canonical(project) == canonical(_project(raw.digest)), 'project_policy')
    source = RepairSource(project, root, source_paths=SOURCE_PATHS,
                          protected_paths=tuple(path for path in SOURCE_PATHS if path != PRODUCT),
                          original_artifact_root=root, original_artifact_paths=(PRODUCT,))
    return source, source.freeze('original')


def _ticket_material(project, oracle_digest):
    """Explicit synthetic correlation only: no recorded UI action is dispatched."""
    app, build = project['applications'][0], project['builds'][0]
    log = {'schemaVersion': 1, 'platform': 'ios', 'applicationId': app['bundle'],
        'runId': RUN, 'sessionId': SESSION, 'profileDigest': PROFILE, 'startedAtMs': 1000,
        'endSequence': 1, 'truncated': False, 'lostEvents': False,
        'events': [{'seq': 1, 'elapsedMs': 0, 'type': 'lifecycle', 'name': 'attached',
                    'component': 'application', 'componentId': 'app', 'target': None}]}
    raw_log = canonical(log)
    action = {'action': 'launch', 'parameters': {'applicationId': app['id']}}
    original = {'schemaVersion': 1, 'recordingId': 'synthetic_submission_recording',
        'projectId': project['id'], 'projectRevision': project['revision'],
        'applicationId': app['id'], 'buildId': build['id'], 'startedAtMs': 1000, 'endSequence': 1,
        'events': [{'id': 'event_one', 'operationId': 'operation_one', 'generation': 1,
            'sequence': 1, 'offsetMs': 0, 'input': action, 'dispatch': 'injected',
            'provenance': {'kind': 'injected', 'source': 'synthetic-repository-fixture-not-device'},
            'receipt': {'operationId': 'operation_one', 'generation': 1, 'status': 'injected',
                        'providerIncarnation': 'synthetic_not_device', 'observedAtMs': 1000}}],
        'preparation': [], 'observations': [{'id': 'synthetic_log', 'digest': _sha(raw_log),
            'path': 'logs/synthetic.json', 'bytes': len(raw_log), 'mimeType': APP_LOG_MIME}],
        'media': [], 'clock': {'source': 'monotonic', 'uncertaintyMs': 0},
        'interruptions': [], 'unknowns': [], 'sealed': True}
    assertions = []
    for identifier, role, observation, value in (
            ('duplicate_defect', 'defect', 'duplicate_submission', True),
            ('first_accepted', 'expected', 'first_submission', True),
            ('duplicate_rejected', 'expected', 'duplicate_submission', False)):
        assertions.append({'id': identifier, 'role': role,
            'predicate': {'kind': 'property', 'observationId': observation,
                          'property': 'accepted', 'operator': 'equals', 'value': value},
            'coverage': {'class': 'snapshot', 'windowMs': {'start': 0, 'end': 0},
                        'maxUncertaintyMs': 0, 'maxAgeMs': 0, 'scope': 'root',
                        'properties': ['accepted']}, 'windowMs': 0, 'stabilityMs': 0})
    assertions = [assertions[0], {**assertions[1], 'id': 'submission_contract',
        'predicate': {'kind': 'all', 'children': [assertions[1]['predicate'], assertions[2]['predicate']]}}]
    specification = {'schemaVersion': 1, 'id': 'synthetic_submission_specification', 'revision': 1,
        'originalRecordingDigest': contracts.digest(original),
        'actions': [{'eventId': 'event_one', **action}], 'waits': [], 'bindings': [],
        'fixtures': [], 'assertions': assertions,
        'provenance': {'kind': 'authored', 'source': 'repository-synthetic-duplicate-submission-oracle',
                      'author': 'fixture_author_not_user_approval', 'revision': '1', 'digest': oracle_digest}}
    contracts.validate_specification(specification)
    recording = {'original': original, 'recordingDigest': contracts.digest(original),
                 'status': 'frozen-complete', 'lifecycleReceipts': []}
    capsule = build_archive(recording, specification, lambda _: raw_log,
                            package_id='synthetic_submission_capsule')
    entry = RegisteredCapsule('local_demo', project, capsule, _sha(raw_log), RUN, SESSION, PROFILE)
    ticket = {'schemaVersion': 1, 'ticketId': 'synthetic_duplicate_submission', 'tenantId': 'local_demo',
        'projectId': project['id'], 'applicationId': app['id'], 'buildId': build['id'],
        'sourceDigest': build['sourceDigest'], 'sessionId': SESSION,
        'recordingDigest': contracts.digest(original),
        'text': 'Synthetic sample ticket: accept the first submission; reject a repeated submission.'}
    return ticket, entry, specification


def _draft(source, frozen):
    ticket, entry, specification = _ticket_material(source.project, PINS[ORACLE])
    draft = intake_ticket(canonical(ticket), tenant_id='local_demo', registry=(entry,))
    expected = {'sourceDigest': frozen.digest, 'projectDigest': source.project_digest,
                'specificationDigest': contracts.digest(specification),
                'recordingDigest': ticket['recordingDigest'], 'capsuleDigest': _sha(entry.capsule),
                'appLogDigest': entry.app_log_digest}
    _require(draft.get('status') == 'review-required' and draft.get('approved') is False
             and draft.get('executionAuthority') == 'none' and draft.get('aiTransfer') is False
             and draft.get('references') == expected
             and specification['provenance']['digest'] == PINS[ORACLE], 'ticket_binding')
    return draft


def _stop(process):
    # poll() may already have reaped a naturally completed leader. Never signal
    # that numeric PGID again: it could now belong to a different process.
    if process.returncode is None:
        # No wait/poll between these signals: the unreaped leader reserves its
        # PID while we terminate the process group we created with it.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            if sig == signal.SIGTERM:
                time.sleep(0.1)
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            raise NativeLogicError('cleanup_unconfirmed') from None
    deadline = time.monotonic() + 0.5
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        _require(time.monotonic() < deadline, 'cleanup_unconfirmed')
        time.sleep(0.02)


def _command(argv, cwd, log_path, home, timeout):
    """Internal fixed argv only. No shell, inherited secrets, or arbitrary provider."""
    environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(home),
                   'TMPDIR': str(home), 'LANG': 'C', 'LC_ALL': 'C',
                   'DEVELOPER_DIR': DEVELOPER_DIR}
    cancelled = False
    def cancel(signum, frame):
        nonlocal cancelled
        cancelled = True
    # A flag avoids interrupting Popen between spawn and ownership assignment,
    # or interrupting the bounded cleanup on a repeated termination signal.
    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, cancel)
        with log_path.open('xb') as log:
            _require(not cancelled, 'command_cancelled')
            try:
                process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError:
                raise NativeLogicError('tool_start_failed') from None
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    _require(not cancelled, 'command_cancelled')
                    _require(time.monotonic() < deadline, 'command_timeout')
                    _require(os.fstat(log.fileno()).st_size <= MAX_OUTPUT, 'output_limit')
                    time.sleep(0.05)
                _require(not cancelled, 'command_cancelled')
                _require(os.fstat(log.fileno()).st_size <= MAX_OUTPUT, 'output_limit')
                _require(time.monotonic() < deadline, 'command_timeout')
                code = process.returncode
            finally:
                _stop(process)
        _require(not cancelled, 'command_cancelled')
        raw = read_regular(log_path.parent, log_path.name, maximum=MAX_OUTPUT)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    _require(not cancelled, 'command_cancelled')
    return code, raw


def _validate_swift_version(value):
    # Xcode may print a driver prefix on stderr before the compiler version.
    # Record the observed string; do not substitute a presumed compiler build.
    _require(type(value) is str and re.fullmatch(
        r'(?:swift-driver version: [0-9]+(?:\.[0-9]+)*(?: |\n))?'
        r'Apple Swift version [0-9]+(?:\.[0-9]+){1,2} '
        r'\(swiftlang-[0-9.]+ clang-[0-9.]+\)\n'
        r'Target: (?:arm64|x86_64)-apple-macosx[0-9]+(?:\.[0-9]+){0,2}', value) is not None,
        'native_toolchain_version')


def _toolchain(output):
    _require(sys.platform == 'darwin' and Path('/usr/bin/xcrun').is_file()
             and Path(DEVELOPER_DIR).is_dir(), 'native_toolchain_unavailable')
    home = output / 'tool-home'
    home.mkdir(mode=0o700)
    commands = (['/usr/bin/xcodebuild', '-version'], ['/usr/bin/xcrun', 'swift', '--version'])
    results = []
    for number, command in enumerate(commands):
        code, raw = _command(command, output, output / f'toolchain-{number}.log', home, 15)
        _require(code == 0, 'native_toolchain_unavailable')
        results.append(raw.decode('utf-8', 'strict').strip())
    _require(results[0] == EXPECTED_XCODE, 'native_toolchain_version')
    _validate_swift_version(results[1])
    host = os.uname()
    return {'xcode': results[0], 'swift': results[1], 'platform': sys.platform,
            'host': {key: getattr(host, key) for key in ('sysname', 'release', 'version', 'machine')},
            'developerDirectory': DEVELOPER_DIR, 'invocations': list(commands)}


def _validate_result(xml_raw, log_raw, returncode, mode, oracle_path):
    """Check SwiftPM's xUnit AND XCTest's exact native assertion, never JSON claims.

    SwiftPM 6.1.2 emits xUnit only with --parallel, and its failure text is generic.
    The worker count is one and success output is enabled with --verbose.
    """
    _require(mode in ('baseline', 'wrong', 'fixed'), 'result_mode')
    _require(type(xml_raw) is bytes and 0 < len(xml_raw) <= 16384
             and b'<!' not in xml_raw and type(log_raw) is bytes and len(log_raw) <= MAX_OUTPUT, 'result_invalid')
    try:
        element = ET.fromstring(xml_raw)
        log = log_raw.decode('utf-8', 'strict')
    except (ET.ParseError, UnicodeError, ValueError):
        raise NativeLogicError('result_invalid') from None
    _require(all(not (node.text or '').strip() and not (node.tail or '').strip()
                 for node in element.iter()), 'result_invalid')
    failing = mode != 'fixed'
    failures = int(failing)
    _require(type(returncode) is int and returncode == failures, 'test_exit_mismatch')
    _require(element.tag == 'testsuites' and not element.attrib and len(element) == 1, 'result_invalid')
    suite = element[0]
    _require(suite.tag == 'testsuite' and set(suite.attrib) == {'name', 'errors', 'tests', 'failures', 'time'}
             and suite.get('name') == 'TestResults' and suite.get('errors') == '0'
             and suite.get('tests') == '1' and suite.get('failures') == str(failures)
             and len(suite) == 1, 'test_count_mismatch')
    case = suite[0]
    _require(case.tag == 'testcase' and set(case.attrib) == {'classname', 'name', 'time'}
             and case.get('classname') == 'ReproLogicTests.SubmissionLogicTests'
             and case.get('name') == 'testDuplicateIsRejected' and len(case) == failures, 'test_identity_mismatch')
    for node in (suite, case):
        _require(re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:e-[0-9]+)?', node.get('time', '')) is not None,
                 'result_invalid')
    if failing:
        _require(case[0].tag == 'failure' and case[0].attrib == {'message': 'failed'}
                 and len(case[0]) == 0 and not (case[0].text or '').strip(), 'result_invalid')
    _require(not re.search(r'\bskipped\b|XCTSkip|unexpected signal|fatal error:|\bsignal\s+(?:SIG[A-Z]+|[0-9]+)|'
                           r'Test run with [0-9]+ tests? passed|Testing Library Version:|backtracing|program crashed|'
                           r'runtime (?:trap|error|failure)|segmentation fault|'
                           r'illegal instruction|bus error|abort trap|stack dump|precondition failed|assertion failed', log, re.IGNORECASE), 'test_incomplete')
    case_lines = re.findall(r"^Test Case '([^']+)' (started\.|(?:passed|failed) \([0-9]+(?:\.[0-9]+)? seconds\)\.)$", log, re.MULTILINE)
    expected_end = 'failed' if failing else 'passed'
    _require(len(case_lines) == 2 and all(name == OBJC_CASE for name, _ in case_lines)
             and case_lines[0][1] == 'started.' and case_lines[1][1].startswith(expected_end + ' '), 'test_log_identity')
    # Reject unparsed, duplicate or truncated test-case rows as well.
    _require(len(re.findall(r'^Test Case ', log, re.MULTILINE)) == 2, 'test_log_identity')
    summaries = re.findall(r'^\s*Executed (\d+) tests?, with (\d+) failures? \((\d+) unexpected\) in [0-9]+(?:\.[0-9]+)? \([0-9]+(?:\.[0-9]+)?\) seconds$', log, re.MULTILINE)
    _require(len(summaries) == 3 and all(row == ('1', str(failures), '0') for row in summaries)
             and len(summaries) == len(re.findall(r'^\s*Executed ', log, re.MULTILINE)), 'test_log_count')
    # Exact Xcode 16.4 host XCTest hierarchy, captured by the native CI fixture.
    # The bundle wrapper has its own summary; it is not another testcase.
    suite_lines = re.findall(r"^Test Suite '([^']+)' (started|passed|failed) at [^\n]+$", log, re.MULTILINE)
    hierarchy = ('Selected tests', 'ReproofNativeLogicPackageTests.xctest', 'SubmissionLogicTests')
    expected_suites = [(name, 'started') for name in hierarchy]
    expected_suites += [(name, expected_end) for name in reversed(hierarchy)]
    _require(suite_lines == expected_suites
             and len(re.findall(r'^Test Suite ', log, re.MULTILINE)) == 6, 'test_log_incomplete')
    # A valid prefix or reordered rows are not a complete native test run.
    suite_rows = list(re.finditer(r'^Test Suite ', log, re.MULTILINE))
    case_rows = list(re.finditer(r'^Test Case ', log, re.MULTILINE))
    summary_rows = list(re.finditer(r'^\s*Executed ', log, re.MULTILINE))
    positions = [suite_rows[0].start(), suite_rows[1].start(), suite_rows[2].start(),
                 case_rows[0].start(), case_rows[1].start(), suite_rows[3].start(),
                 summary_rows[0].start(), suite_rows[4].start(), summary_rows[1].start(),
                 suite_rows[5].start(), summary_rows[2].start()]
    _require(positions == sorted(set(positions)), 'test_log_incomplete')
    errors = [line for line in log.splitlines() if 'error:' in line or 'XCTAssert' in line]
    if failing:
        expectation = RECIPE_VALUE['failureAssertions'][mode]
        exact = (f"{oracle_path}:{expectation['line']}: error: {OBJC_CASE} : "
                 f"{expectation['assertion']} failed")
        _require(len(errors) == 1 and errors[0].rstrip() == exact
                 and case_rows[0].start() < log.index(errors[0]) < case_rows[1].start(),
                 'expected_assertion_missing')
    else:
        _require(not errors, 'unexpected_test_error')
    return {'testCase': CASE, 'tests': 1, 'assertionsInFrozenTest': 2, 'failures': failures,
            'skipped': 0, 'expectedAssertion': None if not failing else dict(RECIPE_VALUE['failureAssertions'][mode]),
            'resultDigest': _sha(xml_raw), 'logDigest': _sha(log_raw), 'returncode': returncode}


def _observe(frozen, directory, mode):
    entries = dict(frozen.entries)
    package = BlobSet(tuple((destination, entries[path]) for path, destination in MAPPING.items()))
    directory.mkdir(mode=0o700)
    workspace = directory / 'package'
    package.write_new(workspace)
    home = directory / 'home'
    home.mkdir(mode=0o700)
    build = ['/usr/bin/xcrun', 'swift', 'build', '--package-path', str(workspace),
             '--disable-automatic-resolution', '--build-tests']
    code, build_log = _command(build, workspace, directory / 'build.log', home, 90)
    _require(code == 0, 'native_build_failed')
    _require(BlobSet.from_directory(workspace, tuple(MAPPING.values())).digest == package.digest, 'build_inputs_changed')
    executables = sorted(workspace.glob('.build/*/debug/ReproofNativeLogicPackageTests.xctest/Contents/MacOS/ReproofNativeLogicPackageTests'))
    _require(len(executables) == 1, 'native_artifact_missing')
    artifact_path = executables[0].relative_to(workspace).as_posix()
    artifact = read_regular(workspace, artifact_path, maximum=32 * 1024 * 1024)
    result_path = directory / 'result.xml'
    _require(not result_path.exists(), 'result_preexists')
    test = ['/usr/bin/xcrun', 'swift', 'test', '--package-path', str(workspace),
            '--disable-automatic-resolution', '--skip-build', '--verbose',
            '--enable-xctest', '--disable-swift-testing', '--parallel', '--num-workers', '1',
            '--filter', FILTER, '--xunit-output', str(result_path)]
    code, log = _command(test, workspace, directory / 'test.log', home, 30)
    _require(BlobSet.from_directory(workspace, tuple(MAPPING.values())).digest == package.digest
             and read_regular(workspace, artifact_path, maximum=32 * 1024 * 1024) == artifact, 'build_inputs_changed')
    xml = read_regular(directory, 'result.xml', maximum=16384)
    observed = _validate_result(xml, log, code, mode, workspace / 'Tests/LogicTests.swift')
    return {**observed, 'buildCompleted': True, 'buildReturncode': 0, 'buildLogDigest': _sha(build_log),
            'compiledTestArtifactDigest': _sha(artifact), 'buildInputDigest': package.digest,
            'oracleDigest': _sha(entries[ORACLE]), 'recipeDigest': _sha(entries[RECIPE]),
            'packageDigest': _sha(entries[PACKAGE]), 'invocations': [build, test]}


def _claims():
    return {'verificationTier': 'host-native-logic-exercise', 'execution': 'swift-xctest-on-macos-host',
            'candidateGenerator': 'scripted-not-ai', 'syntheticTicketEvidence': True,
            'sdkCapture': False, 'scenarioReplay': False, 'mobileUI': False, 'deviceExecution': False,
            'actualAI': False, 'externalAI': False, 'companyAcceptance': False,
            'protectedVerification': False, 'sandbox': False, 'credentialsUsed': False}


def _run_demo(root, output, candidate):
    _require(candidate in ('fixed', 'wrong', 'noop'), 'candidate_selection')
    source, frozen = _load_source(root)
    entries = dict(frozen.entries)
    draft = _draft(source, frozen)
    # The evaluator stays outside all candidate workspaces. These hashes detect
    # accidental mutation, not a security boundary against a hostile host.
    evaluator_paths = ('reproof/native_logic_repair_demo.py', 'reproof/project_repair.py',
                       'reproof/ios_core.py', 'reproof/ios_cases.py', 'reproof/ticket_intake.py')
    evaluator = BlobSet.from_directory(root, evaluator_paths)
    toolchain = _toolchain(output)
    baseline = _observe(frozen, output / 'baseline', 'baseline')
    source.require_original(frozen, 'original')
    report = {'schemaVersion': 1, 'kind': 'native-sample-logic-repair-demo', **_claims(),
        'status': 'rejected', 'approval': 'not-granted-demo-only', 'candidateSelection': candidate,
        'ticketIntake': draft, 'sourceDigest': frozen.digest, 'projectDigest': source.project_digest,
        'sourceFileDigest': _sha(entries[PRODUCT]), 'oracleDigest': _sha(entries[ORACLE]),
        'recipeDigest': _sha(entries[RECIPE]), 'packageDigest': _sha(entries[PACKAGE]),
        'assertionLocations': {key: dict(value) for key, value in RECIPE_VALUE['failureAssertions'].items()},
        'evaluatorDigest': evaluator.digest, 'sourceManifest': frozen.manifest,
        'originalArtifactKind': 'pinned-swift-source-not-installed-ios-build',
        'baselineDefectReproduced': True, 'baseline': baseline, 'candidate': None,
        'candidateDigest': None, 'candidateFileDigest': None, 'candidateOracleDigest': None,
        'protectedInputsUnchanged': True, 'toolchain': toolchain, 'protectedEditRejections': []}
    for path in (ORACLE, PACKAGE, RECIPE, PROJECT, CALLSITE, 'reproof/native_logic_repair_demo.py'):
        try:
            source.apply(frozen, [{'path': path, 'old': 'expected', 'new': 'bypassed'}])
        except RepairError as error:
            _require(error.code == 'protected_path', 'protected_policy_failed')
            report['protectedEditRejections'].append({'path': path, 'code': error.code})
        else:
            raise NativeLogicError('protected_policy_failed')
    replacement = {'fixed': '!submitted', 'wrong': 'false', 'noop': 'true'}[candidate]
    edit = {'path': PRODUCT, 'old': 'return true', 'new': 'return ' + replacement}
    try:
        changed = source.apply(frozen, [edit])
    except RepairError as error:
        if candidate != 'noop' or error.code not in ('patch_invalid', 'patch_noop'):
            raise
        report['error'] = {'code': error.code, 'reason': 'no-op rejected before candidate compilation'}
    else:
        changed_entries = dict(changed.entries)
        validate_swift_expression(entries[PRODUCT].decode(), changed_entries[PRODUCT].decode(), 'duplicate-submit')
        _require(changed.digest != frozen.digest and all(changed_entries[path] == raw
                 for path, raw in frozen.entries if path != PRODUCT), 'protected_inputs_changed')
        observed = _observe(changed, output / 'candidate', candidate)
        report.update(status='host_logic_repair_passed' if candidate == 'fixed' else 'candidate_failed',
            candidate=observed, candidateDigest=changed.digest,
            candidateFileDigest=_sha(changed_entries[PRODUCT]),
            candidateOracleDigest=_sha(changed_entries[ORACLE]), edit=edit)
    source.require_original(frozen, 'original')
    _require(BlobSet.from_directory(root, evaluator_paths).digest == evaluator.digest, 'evaluator_changed')
    return report


def run_demo(candidate='fixed', *, output=None):
    """Only checked-in sources and named finite candidates; no source/tool arguments."""
    root = Path(__file__).absolute().parents[1]
    if output is None:
        with tempfile.TemporaryDirectory(prefix='reproof-native-logic-') as temporary:
            return _run_demo(root, Path(temporary).resolve(), candidate)
    output = Path(output).absolute()
    _require(not output.exists(), 'output_exists')
    # Resolve only the output parent; all workspace inputs use no-follow readers.
    output = output.parent.resolve() / output.name
    output.mkdir(mode=0o700)
    return _run_demo(root, output, candidate)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Bounded host Swift logic proof, not mobile UI/SDK/AI verification.')
    parser.add_argument('--candidate', choices=('fixed', 'wrong', 'noop'), default='fixed')
    parser.add_argument('--output', type=Path, help='New local directory for native logs and build artifacts')
    args = parser.parse_args(argv)
    try:
        report = run_demo(args.candidate, output=args.output)
    except (NativeLogicError, RepairError, ArtifactError, contracts.ContractError, OSError, UnicodeError) as error:
        report = {'schemaVersion': 1, 'kind': 'native-sample-logic-repair-demo', **_claims(),
                  'status': 'rejected', 'baselineDefectReproduced': False,
                  'error': {'code': getattr(error, 'code', 'native_evidence_unavailable')}}
    print(json.dumps(report, indent=2, sort_keys=True))
    return {'host_logic_repair_passed': 0, 'candidate_failed': 1}.get(report['status'], 2)


if __name__ == '__main__':
    raise SystemExit(main())
