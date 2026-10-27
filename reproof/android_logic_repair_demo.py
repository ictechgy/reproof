"""Bounded real Android sample Kotlin/JUnit proof, not SDK or device verification.

Only repository-owned pinned sources and three finite candidate selections run.
The synthetic ticket is correlation metadata, not captured application evidence.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from . import contracts
from .app_logs import APP_LOG_MIME
from .execution.artifacts import ArtifactError, BlobSet, read_regular
from .issue_package import build_archive, canonical
from .project_repair import RepairError, RepairSource
from .ticket_intake import RegisteredCapsule, intake_ticket
from .native_logic_repair_demo import (NativeLogicError, _require, _sha, _json,
    _command, MAX_OUTPUT, SESSION, RUN)

PROFILE = _sha(b'repository-synthetic-android-counter-v1')

PRODUCT = 'android/sample/src/main/java/io/reproof/sample/CounterLogic.kt'
CALLSITE = 'android/sample/src/main/java/io/reproof/sample/MainActivity.kt'
ORACLE = 'android/sample/src/test/java/io/reproof/sample/CounterLogicTest.kt'
PIN_FILE = 'examples/android-logic-repair/source-pins.json'
PIN_SHA256 = '087a859557735b85d4e03178453edfdc3df1b041bac2ebb0ea6009c52c061ac2'
RECIPE = 'examples/android-logic-repair/recipe.json'
CASE = 'io.reproof.sample.CounterLogicTest.addingOneItemIncrementsCountByOne'
TASK = ':sample:testBuggyDebugUnitTest'
RESULT = 'android/sample/build/test-results/testBuggyDebugUnitTest/TEST-io.reproof.sample.CounterLogicTest.xml'
RECIPE_VALUE = {
    'schemaVersion': 1, 'kind': 'host-android-sample-logic', 'task': TASK,
    'testCase': CASE, 'testCount': 1, 'oracleFile': ORACLE, 'editableFile': PRODUCT,
    'flavor': 'buggy', 'expectedValue': 1, 'baselineActual': 2, 'wrongActual': 3,
    'gradle': '8.14.5', 'agp': '8.13.2', 'kotlin': '2.2.10', 'javaMajor': 17,
    'compileSdk': 35, 'buildTools': '35.0.0', 'timeoutSeconds': 240,
    'candidateExpressions': ['2', '3', '1'], 'maxOutputBytes': MAX_OUTPUT,
}


def _load_source(root):
    raw = read_regular(root, PIN_FILE, maximum=32768)
    _require(_sha(raw) == PIN_SHA256, 'source_manifest_pin')
    pins = _json(raw)
    paths = tuple(pins) + (RECIPE,)
    frozen = BlobSet.from_directory(root, paths)
    entries = dict(frozen.entries)
    _require(all(_sha(entries[path]) == digest for path, digest in pins.items()), 'source_pin')
    _require(entries[RECIPE] == (json.dumps(RECIPE_VALUE, indent=2, sort_keys=True) + '\n').encode(), 'recipe_pin')
    project = {
        'schemaVersion': 1, 'id': 'android_counter_demo', 'revision': 'repository-fixture-v1',
        'trustGroup': 'local_demo',
        'applications': [{'id': 'sample_android_source', 'platform': 'android', 'bundle': 'io.reproof.sample'}],
        'builds': [{'id': 'original', 'applicationId': 'sample_android_source',
            'revision': 'pinned-kotlin-source-not-installed-android-build',
            'artifactDigest': pins[PRODUCT], 'sourceDigest': frozen.digest,
            'provenance': 'operator-attestation'}],
        'variables': [], 'fixtures': [], 'observations': ['counter_increment'],
        'evidencePolicy': {'pixels': False, 'text': False, 'accessibility': False,
            'logs': True, 'fixtures': True, 'unknownSensitive': 'deny', 'aiEligible': False},
        'editablePaths': [PRODUCT],
        'recipes': [{'id': 'android_counter_regression', 'kind': 'regression',
            'productFile': RECIPE, 'operation': 'regression'}],
        'executionClasses': ['host-build'],
    }
    source = RepairSource(project, root, source_paths=paths,
        protected_paths=tuple(path for path in paths if path != PRODUCT),
        original_artifact_root=root, original_artifact_paths=(PRODUCT,))
    return source, source.freeze('original')


def _ticket_material(project, oracle_digest):
    """Explicit synthetic correlation only: no recorded UI action is dispatched."""
    app, build = project['applications'][0], project['builds'][0]
    log = {'schemaVersion': 1, 'platform': 'android', 'applicationId': app['bundle'],
        'runId': RUN, 'sessionId': SESSION, 'profileDigest': PROFILE, 'startedAtMs': 1000,
        'endSequence': 1, 'truncated': False, 'lostEvents': False,
        'events': [{'seq': 1, 'elapsedMs': 0, 'type': 'lifecycle', 'name': 'attached',
                    'component': 'application', 'componentId': 'app', 'target': None}]}
    raw_log = canonical(log)
    action = {'action': 'launch', 'parameters': {'applicationId': app['id']}}
    original = {'schemaVersion': 1, 'recordingId': 'synthetic_counter_recording',
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
            ('extra_defect', 'defect', 'counter_increment', 2),
            ('one_increment', 'expected', 'counter_increment', 1)):
        assertions.append({'id': identifier, 'role': role,
            'predicate': {'kind': 'property', 'observationId': observation,
                          'property': 'increment', 'operator': 'equals', 'value': value},
            'coverage': {'class': 'snapshot', 'windowMs': {'start': 0, 'end': 0},
                        'maxUncertaintyMs': 0, 'maxAgeMs': 0, 'scope': 'root',
                        'properties': ['increment']}, 'windowMs': 0, 'stabilityMs': 0})
    specification = {'schemaVersion': 1, 'id': 'synthetic_counter_specification', 'revision': 1,
        'originalRecordingDigest': contracts.digest(original),
        'actions': [{'eventId': 'event_one', **action}], 'waits': [], 'bindings': [],
        'fixtures': [], 'assertions': assertions,
        'provenance': {'kind': 'authored', 'source': 'repository-synthetic-extra-counter-oracle',
                      'author': 'fixture_author_not_user_approval', 'revision': '1', 'digest': oracle_digest}}
    contracts.validate_specification(specification)
    recording = {'original': original, 'recordingDigest': contracts.digest(original),
                 'status': 'frozen-complete', 'lifecycleReceipts': []}
    capsule = build_archive(recording, specification, lambda _: raw_log,
                            package_id='synthetic_counter_capsule')
    entry = RegisteredCapsule('local_demo', project, capsule, _sha(raw_log), RUN, SESSION, PROFILE)
    ticket = {'schemaVersion': 1, 'ticketId': 'synthetic_extra_counter', 'tenantId': 'local_demo',
        'projectId': project['id'], 'applicationId': app['id'], 'buildId': build['id'],
        'sourceDigest': build['sourceDigest'], 'sessionId': SESSION,
        'recordingDigest': contracts.digest(original),
        'text': 'Synthetic sample ticket: one add action must increment by one, not two.'}
    return ticket, entry, specification


def _draft(source, frozen):
    ticket, entry, specification = _ticket_material(source.project, _sha(dict(frozen.entries)[ORACLE]))
    draft = intake_ticket(canonical(ticket), tenant_id='local_demo', registry=(entry,))
    expected = {'sourceDigest': frozen.digest, 'projectDigest': source.project_digest,
                'specificationDigest': contracts.digest(specification),
                'recordingDigest': ticket['recordingDigest'], 'capsuleDigest': _sha(entry.capsule),
                'appLogDigest': entry.app_log_digest}
    _require(draft.get('status') == 'review-required' and draft.get('approved') is False
             and draft.get('executionAuthority') == 'none' and draft.get('aiTransfer') is False
             and draft.get('references') == expected
             and specification['provenance']['digest'] == _sha(dict(frozen.entries)[ORACLE]), 'ticket_binding')
    return draft


def _toolchain(output):
    """Use the explicit host JDK/SDK only; never inherit Gradle init scripts/secrets."""
    java = Path(os.environ.get('JAVA_HOME', '/nonexistent')).resolve()
    sdk = Path(os.environ.get('ANDROID_HOME', '/nonexistent')).resolve()
    _require((java / 'bin/java').is_file() and (java / 'bin/javac').is_file()
        and (sdk / 'platforms/android-35/android.jar').is_file()
        and (sdk / 'build-tools/35.0.0').is_dir(), 'android_toolchain_unavailable')
    home = output / 'tool-home'
    home.mkdir(mode=0o700)
    environment = _environment(java, sdk, home)
    code, raw = _command([str(java / 'bin/java'), '-version'], output,
                        output / 'java-version.log', home, 15, environment=environment)
    version = raw.decode('utf-8', 'strict')
    _require(code == 0 and re.search(r'(?:openjdk|java) version "17\.', version), 'android_toolchain_version')
    return {'javaVersion': version.strip(), 'javaHome': str(java), 'androidHome': str(sdk),
            'platform': os.uname().sysname, 'compileSdk': 35, 'buildTools': '35.0.0'}


def _environment(java, sdk, home):
    return {'PATH': f'{java}/bin:/usr/bin:/bin', 'HOME': str(home), 'TMPDIR': str(home),
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'JAVA_HOME': str(java),
        'ANDROID_HOME': str(sdk), 'ANDROID_USER_HOME': str(home / 'android'),
        'GRADLE_USER_HOME': str(home / 'gradle')}


def _validate_result(xml_raw, log_raw, returncode, mode):
    _require(mode in ('baseline', 'wrong', 'fixed'), 'result_mode')
    _require(type(xml_raw) is bytes and 0 < len(xml_raw) <= 65536 and b'<!DOCTYPE' not in xml_raw.upper() and b'<!ENTITY' not in xml_raw.upper()
        and type(log_raw) is bytes and len(log_raw) <= MAX_OUTPUT, 'result_invalid')
    try:
        suite = ET.fromstring(xml_raw)
        log = log_raw.decode('utf-8', 'strict')
    except (ET.ParseError, UnicodeError, ValueError):
        raise NativeLogicError('result_invalid') from None
    failures = int(mode != 'fixed')
    actual = {'baseline': 2, 'wrong': 3, 'fixed': None}[mode]
    _require(type(returncode) is int and returncode == failures, 'test_exit_mismatch')
    _require(set(suite.attrib) == {'name', 'tests', 'skipped', 'failures', 'errors', 'timestamp', 'hostname', 'time'}
        and suite.tag == 'testsuite' and suite.get('name') == 'io.reproof.sample.CounterLogicTest'
        and suite.get('tests') == '1' and suite.get('failures') == str(failures)
        and suite.get('errors') == '0' and suite.get('skipped') == '0', 'test_count_mismatch')
    _require([node.tag for node in suite] == ['properties', 'testcase', 'system-out', 'system-err']
        and not suite[0].attrib and len(suite[0]) == 0 and not (suite[0].text or '').strip(), 'result_invalid')
    case = suite[1]
    _require(case.get('classname') == 'io.reproof.sample.CounterLogicTest'
        and case.get('name') == 'addingOneItemIncrementsCountByOne'
        and set(case.attrib) == {'classname', 'name', 'time'} and len(case) == failures,
        'test_identity_mismatch')
    for node in (suite, case):
        _require(re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', node.get('time', '')) is not None, 'result_invalid')
    _require(not (case.text or '').strip() and all(not (node.tail or '').strip() for node in suite.iter())
        and not (suite.text or '').strip(), 'result_invalid')
    _require(all(not node.attrib and len(node) == 0 and not (node.text or '').strip()
        for node in suite[2:]), 'unexpected_test_output')
    if failures:
        failure = case[0]
        message = f'java.lang.AssertionError: expected:<1> but was:<{actual}>'
        _require(failure.tag == 'failure' and failure.attrib == {
            'message': message, 'type': 'java.lang.AssertionError'} and len(failure) == 0
            and (failure.text or '').startswith(message + '\n')
            and (failure.text or '').count('at io.reproof.sample.CounterLogicTest.addingOneItemIncrementsCountByOne(CounterLogicTest.kt:9)') == 1,
            'expected_assertion_missing')
        frames = (failure.text or '').splitlines()[1:]
        _require(bool(frames) and all(re.fullmatch(r'\s+at \S+\([^\n]+\)', frame) for frame in frames),
            'unexpected_failure_detail')
    # Fresh execution, not UP-TO-DATE / FROM-CACHE / NO-SOURCE / SKIPPED.
    for task in (':sample:compileBuggyDebugKotlin', ':sample:compileBuggyDebugUnitTestKotlin'):
        rows = re.findall(r'^> Task ' + re.escape(task) + r'([^\n]*)$', log, re.MULTILINE)
        _require(rows == [''], 'native_build_incomplete')
    rows = re.findall(r'^> Task ' + re.escape(TASK) + r'([^\n]*)$', log, re.MULTILINE)
    # Gradle plain output can print a start row before the terminal FAILED row
    # (observed in hosted run 40205773342). Accept only this ordered pair or
    # the single terminal row; all exact XML, exit and build checks still apply.
    _require(rows in ([' FAILED'], ['', ' FAILED']) if failures else rows == [''],
             'test_task_incomplete')
    failed_tasks = re.findall(r'^> Task (\S+) FAILED$', log, re.MULTILINE)
    _require(failed_tasks == ([TASK] if failures else []), 'unexpected_task_failure')
    _require(len(re.findall(r'^BUILD (?:SUCCESSFUL|FAILED) in .+$', log, re.MULTILINE)) == 1
        and re.search(r'^BUILD ' + ('FAILED' if failures else 'SUCCESSFUL') + r' in .+$', log, re.MULTILINE), 'build_summary_missing')
    # Kotlin's configuration-error diagnostic tasks are legitimately skipped
    # when no configuration errors exist (observed in hosted run 40203462168).
    # Permit only these exact metadata rows, once per module. Compilation,
    # test execution and JUnit skipped counts remain strictly checked above.
    execution_log = log
    for module in ('sample', 'sdk'):
        row = f'> Task :{module}:checkKotlinGradlePluginConfigurationErrors SKIPPED\n'
        _require(execution_log.count(row) <= 1, 'test_incomplete')
        execution_log = re.sub('^' + re.escape(row), '', execution_log, flags=re.MULTILINE)
    _require(not re.search(r'^e: |Compilation error|Could not resolve|daemon disappeared|OutOfMemoryError|No tests found|\bSKIPPED\b', execution_log, re.MULTILINE), 'test_incomplete')
    if failures:
        _require(log.count("Execution failed for task ':sample:testBuggyDebugUnitTest'.") == 1
            and 'There were failing tests.' in log
            and re.findall(r'^(\d+) tests? completed, (\d+) failed(?:, (\d+) skipped)?$', log, re.MULTILINE) == [('1', '1', '')], 'expected_test_failure_missing')
    return {'testCase': CASE, 'tests': 1, 'failures': failures, 'skipped': 0,
        'expectedActual': actual, 'resultDigest': _sha(xml_raw), 'logDigest': _sha(log_raw),
        'returncode': returncode, 'buildCompleted': True}


def _observe(frozen, directory, mode, toolchain):
    directory.mkdir(mode=0o700)
    workspace = directory / 'workspace'
    frozen.write_new(workspace)
    home = directory / 'home'
    home.mkdir(mode=0o700)
    result = workspace / RESULT
    _require(not result.exists(), 'result_preexists')
    command = ['/bin/sh', './gradlew', '--no-daemon', '--no-build-cache', '--no-configuration-cache',
        '--console=plain', '--rerun-tasks', '-Pkotlin.compiler.execution.strategy=in-process',
        TASK, '--tests', CASE]
    code, log = _command(command, workspace / 'android', directory / 'gradle.log', home, 240,
        environment=_environment(toolchain['javaHome'], toolchain['androidHome'], home))
    _require(BlobSet.from_directory(workspace, tuple(path for path, _ in frozen.entries)).digest == frozen.digest,
        'build_inputs_changed')
    results = list(result.parent.glob('TEST-*.xml'))
    _require(results == [result], 'test_result_set')
    xml = read_regular(workspace, RESULT, maximum=65536)
    observed = _validate_result(xml, log, code, mode)
    # Actual compiler output, not just task text. Each workspace was created new.
    artifacts = {}
    for relative in ('android/sample/build/tmp/kotlin-classes/buggyDebug/io/reproof/sample/CounterLogic.class',
                     'android/sample/build/tmp/kotlin-classes/buggyDebugUnitTest/io/reproof/sample/CounterLogicTest.class'):
        raw = read_regular(workspace, relative, maximum=1024 * 1024)
        _require(raw.startswith(b'\xca\xfe\xba\xbe'), 'native_artifact_invalid')
        artifacts[relative] = _sha(raw)
    entries = dict(frozen.entries)
    return {**observed, 'compiledArtifactDigests': artifacts, 'buildInputDigest': frozen.digest,
        'oracleDigest': _sha(entries[ORACLE]), 'recipeDigest': _sha(entries[RECIPE]), 'invocation': command}


def _claims():
    return {'verificationTier': 'host-native-logic-exercise', 'execution': 'android-gradle-kotlin-junit-host',
        'candidateGenerator': 'scripted-not-ai', 'syntheticTicketEvidence': True,
        'sdkCapture': False, 'scenarioReplay': False, 'mobileUI': False, 'deviceExecution': False,
        'actualAI': False, 'externalAI': False, 'companyAcceptance': False,
        'protectedVerification': False, 'sandbox': False, 'credentialsUsed': False}


def _run_demo(root, output, candidate):
    _require(candidate in ('fixed', 'wrong', 'noop'), 'candidate_selection')
    source, frozen = _load_source(root)
    entries = dict(frozen.entries)
    draft = _draft(source, frozen)
    evaluator_paths = ('reproof/android_logic_repair_demo.py', 'reproof/native_logic_repair_demo.py',
        'reproof/project_repair.py', 'reproof/ticket_intake.py', PIN_FILE)
    evaluator = BlobSet.from_directory(root, evaluator_paths)
    report = {'schemaVersion': 1, 'kind': 'android-sample-logic-repair-demo', **_claims(),
        'status': 'rejected', 'approval': 'not-granted-demo-only', 'candidateSelection': candidate,
        'ticketIntake': draft, 'sourceDigest': frozen.digest, 'projectDigest': source.project_digest,
        'sourceFileDigest': _sha(entries[PRODUCT]), 'oracleDigest': _sha(entries[ORACLE]),
        'recipeDigest': _sha(entries[RECIPE]), 'evaluatorDigest': evaluator.digest,
        'baselineDefectReproduced': False, 'baseline': None, 'candidate': None,
        'candidateDigest': None, 'protectedInputsUnchanged': True}
    replacement = {'fixed': '1', 'wrong': '3', 'noop': '2'}[candidate]
    edit = {'path': PRODUCT, 'old': 'if (BuildConfig.BUGGY) 2 else 1',
            'new': f'if (BuildConfig.BUGGY) {replacement} else 1'}
    try:
        changed = source.apply(frozen, [edit])
    except RepairError as error:
        if candidate != 'noop' or error.code not in ('patch_invalid', 'patch_noop'):
            raise
        report['error'] = {'code': error.code, 'reason': 'no-op rejected before any compilation'}
    else:
        changed_entries = dict(changed.entries)
        _require(changed.digest != frozen.digest and all(changed_entries[path] == raw
            for path, raw in frozen.entries if path != PRODUCT), 'protected_inputs_changed')
        toolchain = _toolchain(output)
        baseline = _observe(frozen, output / 'baseline', 'baseline', toolchain)
        observed = _observe(changed, output / 'candidate', candidate, toolchain)
        report.update(status='host_logic_repair_passed' if candidate == 'fixed' else 'candidate_failed',
            baselineDefectReproduced=True, baseline=baseline, candidate=observed,
            candidateDigest=changed.digest, toolchain=toolchain, edit=edit)
    source.require_original(frozen, 'original')
    _require(BlobSet.from_directory(root, evaluator_paths).digest == evaluator.digest, 'evaluator_changed')
    return report


def run_demo(candidate='fixed', *, output=None):
    _require(candidate in ('fixed', 'wrong', 'noop'), 'candidate_selection')
    root = Path(__file__).absolute().parents[1]
    if output is None:
        _require(candidate == 'noop', 'evidence_output_required')
        with tempfile.TemporaryDirectory(prefix='reproof-android-logic-') as temporary:
            return _run_demo(root, Path(temporary).resolve(), candidate)
    output = Path(output).absolute()
    _require(not output.exists(), 'output_exists')
    output = output.parent.resolve() / output.name
    output.mkdir(mode=0o700)
    return _run_demo(root, output, candidate)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Host Android Kotlin/JUnit proof, not mobile/SDK/AI verification.')
    parser.add_argument('--candidate', choices=('fixed', 'wrong', 'noop'), default='fixed')
    parser.add_argument('--output', type=Path, required=True, help='New directory retaining raw Gradle/JUnit evidence')
    args = parser.parse_args(argv)
    try:
        report = run_demo(args.candidate, output=args.output)
    except (NativeLogicError, RepairError, ArtifactError, contracts.ContractError, OSError, UnicodeError) as error:
        report = {'schemaVersion': 1, 'kind': 'android-sample-logic-repair-demo', **_claims(),
            'status': 'rejected', 'baselineDefectReproduced': False,
            'error': {'code': getattr(error, 'code', 'android_evidence_unavailable')}}
    print(json.dumps(report, indent=2, sort_keys=True))
    return {'host_logic_repair_passed': 0, 'candidate_failed': 1}.get(report['status'], 2)


if __name__ == '__main__':
    raise SystemExit(main())
