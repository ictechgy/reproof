"""Administrator-selected Android app contracts; recordings cannot set policy."""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re

from .core import SAFE_TEST_VALUES, digest, identifier, require
from .ios_profile import _validate_runtime_document


def relative_path(value, suffix=None):
    require(isinstance(value, str) and 0 < len(value) <= 240, 'Invalid app-relative path')
    path = PurePosixPath(value)
    require(bool(path.parts) and not path.is_absolute() and str(path) == value and '..' not in path.parts
            and all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in path.parts)
            and not any(part.startswith('.') for part in path.parts), 'Unsafe app-relative path')
    if suffix:
        require(path.suffix == suffix, 'Unsupported app artifact type')
    return value


@dataclass(frozen=True)
class AndroidAppProfile:
    _json: str

    @property
    def data(self):
        return json.loads(self._json)

    @property
    def digest(self):
        return digest(self.data)

    def native(self):
        data = self.data
        return {key: data[key] for key in ('package', 'activity', 'fixture', 'startState', 'targets')}

    @property
    def native_digest(self):
        return digest(self.native())

    @property
    def component_name(self):
        package, activity = self.data['package'], self.data['activity']
        qualified = package + activity if activity.startswith('.') else activity if '.' in activity else package + '.' + activity
        short = qualified[len(package):] if qualified.startswith(package + '.') else qualified
        return package + '/' + short

    def oracle(self):
        oracle = self.data['oracle']
        bug, expected = oracle['bugCondition'], oracle['expectedCondition']
        return dict(oracle, actual=f"Observed {bug['target']} = {bug['text']}",
                    expected=f"Expected {expected['target']} = {expected['text']}")


def validate_app_profile(document):
    required = {'schemaVersion', 'id', 'package', 'activity', 'fixture', 'startState', 'targets', 'oracle', 'build', 'edit'}
    require(isinstance(document, dict) and required <= set(document)
            and set(document) <= required | {'captureMode', 'instrumentation', 'screenTargets', 'appLogs', 'sourceInputs'},
        'Unsupported Android app profile fields')
    capture_mode = document.get('captureMode', 'report_view')
    require(isinstance(capture_mode, str) and capture_mode in {'report_view', 'debug_receiver'}, 'Unsupported SDK capture transport')
    require(capture_mode == 'debug_receiver' or 'instrumentation' not in document,
            'Automatic instrumentation requires its debug receiver capture transport')
    if 'appLogs' in document:
        require(type(document['appLogs']) is int and document['appLogs'] == 1 and capture_mode == 'debug_receiver',
                'Automatic app logs require an instrumented debug build')
    if 'screenTargets' in document:
        screens = document['screenTargets']
        require(isinstance(screens, dict) and len(screens) <= 32, 'Invalid screen observation targets')
        for target, screen in screens.items():
            identifier(target);identifier(screen)
        require(len(set(screens.values())) == len(screens), 'Screen names must identify one root view')
    require(type(document['schemaVersion']) is int and document['schemaVersion'] == 1, 'Unsupported app profile version')
    identifier(document['id'])
    package = document['package']
    require(isinstance(package, str) and len(package) <= 180
            and re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)+', package)
            and package not in {'io.reproloop.live', 'io.reproloop.driver'}, 'Invalid target app package')
    activity = document['activity']
    require(isinstance(activity, str) and len(activity) <= 240
            and re.fullmatch(r'\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*', activity),
            'Invalid target activity')
    fixture = document['fixture']
    require(isinstance(fixture, dict) and set(fixture) == {'id', 'version', 'inputs'}
            and type(fixture['version']) is int and 1 <= fixture['version'] <= 1000 and fixture['inputs'] == {},
            'App profile requires a closed fixture without executable inputs')
    identifier(fixture['id'])
    targets = document['targets']
    require(isinstance(targets, dict) and set(targets) == {'tap', 'text', 'numeric', 'scroll', 'back', 'report'},
            'Invalid target policy')
    for key in ('tap', 'text', 'numeric'):
        values = targets[key]
        require(isinstance(values, list) and len(values) <= 64 and all(isinstance(v, str) for v in values)
                and len(set(values)) == len(values), 'Invalid or duplicate target IDs')
        for value in values: identifier(value)
    require(bool(targets['numeric']) and not set(targets['text']) & set(targets['numeric']),
            'Text and numeric observation targets must be distinct')
    identifier(targets['back'])
    if targets['report'] is None:
        require(capture_mode == 'debug_receiver', 'Report view ID is required for view capture')
    else:
        identifier(targets['report'])
    require(isinstance(targets['scroll'], dict) and len(targets['scroll']) <= 16, 'Invalid scroll targets')
    for container, ends in targets['scroll'].items():
        identifier(container)
        require(isinstance(ends, list) and 0 < len(ends) <= 32, 'Missing scroll end targets')
        for end in ends: identifier(end)
    start = document['startState']
    require(isinstance(start, dict) and set(start) == {'screen', 'nodes'} and isinstance(start['nodes'], dict)
            and bool(start['nodes']), 'Missing app start state')
    identifier(start['screen'])
    require(set(start['nodes']) == set(targets['text']) | set(targets['numeric']),
            'Start state must specify every text and numeric observation target')
    for target, value in start['nodes'].items():
        identifier(target)
        require(isinstance(value, str) and (
            target in targets['text'] and value in SAFE_TEST_VALUES or
            target in targets['numeric'] and re.fullmatch(r'[0-9]{1,9}', value)), 'Start value outside observation policy')
    oracle = document['oracle']
    require(isinstance(oracle, dict) and set(oracle) == {'bugCondition', 'expectedCondition'}, 'Invalid app oracle')
    for condition in oracle.values():
        require(isinstance(condition, dict) and set(condition) == {'target', 'text'}
                and condition['target'] in targets['numeric'] and isinstance(condition['text'], str)
                and re.fullmatch(r'[0-9]{1,9}', condition['text']), 'Oracle requires a configured numeric observation')
    bug, expected = oracle['bugCondition'], oracle['expectedCondition']
    require(bug['target'] == expected['target'] and bug != expected, 'Oracle outcomes must be distinct')
    build = document['build']
    require(isinstance(build, dict) and set(build) == {'task', 'apk', 'regressionTask', 'regressionResults'},
            'Invalid build contract')
    require(isinstance(build['task'], str) and re.fullmatch(r':(?:[A-Za-z][A-Za-z0-9_-]*:)*assemble[A-Za-z0-9]+', build['task']),
            'Expected one assemble Gradle task')
    require(isinstance(build['regressionTask'], str) and re.fullmatch(r':(?:[A-Za-z][A-Za-z0-9_-]*:)*test[A-Za-z0-9]+UnitTest', build['regressionTask']),
            'Expected one unit-test Gradle task')
    relative_path(build['apk'], '.apk'); relative_path(build['regressionResults'])
    module_parts = build['task'].split(':')[1:-1]
    require(build['regressionTask'].split(':')[1:-1] == module_parts,
            'Build and regression tasks must target the same app module')
    expected_results = PurePosixPath(*module_parts, 'build', 'test-results', build['regressionTask'].split(':')[-1]).as_posix()
    require(build['regressionResults'] == expected_results, 'Regression evidence must be the selected task fresh build output')
    edit = document['edit']
    require(isinstance(edit, dict) and set(edit) == {'kind', 'path', 'function'}
            and edit['kind'] == 'kotlin_numeric_expression_v1', 'Unsupported edit policy')
    relative_path(edit['path'], '.kt')
    edit_parts = PurePosixPath(edit['path']).parts
    prefix = tuple(module_parts) + ('src', 'main')
    require(edit_parts[:len(prefix)] == prefix and len(edit_parts) > len(prefix) + 1
            and edit_parts[len(prefix)] in {'java', 'kotlin'}, 'Edit target must be in the selected app module product sources')
    require(not any(part.lower() in {'test', 'tests', 'androidtest', 'buildsrc', 'build', 'build-logic', 'gradle',
                                     'plugin', 'plugins', 'convention', 'conventions'} for part in edit_parts),
            'Edit target is protected infrastructure or a test')
    require(isinstance(edit['function'], str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', edit['function']),
            'Invalid Kotlin expression function')
    if capture_mode == 'debug_receiver':
        instrumentation = document.get('instrumentation')
        require(isinstance(instrumentation, dict) and set(instrumentation) == {'kind', 'sites'}
                and isinstance(instrumentation['kind'], str)
                and instrumentation['kind'] in {'kotlin_psi_v1', 'android_asm_v1'},
                'Missing supported instrumentation contract')
        sites = instrumentation['sites']
        require(isinstance(sites, list) and 0 < len(sites) <= 128, 'Invalid instrumentation site list')
        site_ids, site_targets = set(), set()
        for site in sites:
            require(isinstance(site, dict) and set(site) == {'id', 'path', 'line', 'target', 'kind'}
                    and site['kind'] == 'tap' and type(site['line']) is int and 1 <= site['line'] <= 1000000,
                    'Invalid instrumentation site')
            identifier(site['id']); identifier(site['target']); relative_path(site['path'], '.kt')
            site_parts = PurePosixPath(site['path']).parts
            require(site_parts[:len(prefix)] == prefix and len(site_parts) > len(prefix) + 1
                    and site_parts[len(prefix)] in {'java', 'kotlin'} and site['target'] in targets['tap']
                    and site['id'] not in site_ids, 'Unbound or duplicate instrumentation site')
            site_ids.add(site['id']); site_targets.add(site['target'])
        require(site_targets == set(targets['tap']), 'Every configured tap requires a source instrumentation site')
    if 'sourceInputs' in document:
        from .android_sources import validate_source_inputs
        inputs = validate_source_inputs(document['sourceInputs'])
        require({'settings.gradle.kts', PurePosixPath(*module_parts, 'build.gradle.kts').as_posix(), edit['path']}
                <= set(inputs), 'Missing required Android build inputs')
    profile = AndroidAppProfile(json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False))
    require(len(profile._json.encode()) <= (256 if 'sourceInputs' in document else 16) * 1024
            and len(json.dumps(profile.native(), separators=(',', ':')).encode()) <= 12 * 1024,
            'App profile exceeds the native configuration size limit')
    return profile


def load_app_profile(path):
    from .storage import read_json
    return validate_app_profile(read_json(path))


def sample_app_profile():
    return validate_app_profile({
        'schemaVersion': 1, 'id': 'counter', 'package': 'io.reproloop.sample', 'activity': '.MainActivity',
        'fixture': {'id': 'default', 'version': 1, 'inputs': {}},
        'startState': {'screen': 'main', 'nodes': {'count': '0', 'name': ''}},
        'targets': {'tap': ['add', 'next', 'back', 'bottom'], 'text': ['name'], 'numeric': ['count'],
                    'scroll': {'list': ['bottom']}, 'back': 'back', 'report': 'report'},
        'oracle': {'bugCondition': {'target': 'count', 'text': '2'}, 'expectedCondition': {'target': 'count', 'text': '1'}},
        'build': {'task': ':sample:assembleBuggyDebug', 'apk': 'sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk',
                  'regressionTask': ':sample:testBuggyDebugUnitTest', 'regressionResults': 'sample/build/test-results/testBuggyDebugUnitTest'},
        'edit': {'kind': 'kotlin_numeric_expression_v1', 'path': 'sample/src/main/java/io/reproloop/sample/CounterLogic.kt',
                 'function': 'increment'},
    })


@dataclass(frozen=True)
class AndroidRuntimeProfile:
    """General runtime metadata; separate from the legacy repair profile."""

    _json: str

    @property
    def data(self):
        return json.loads(self._json)

    @property
    def digest(self):
        return digest(self.data)

    @property
    def package(self):
        return self.data['package']

    @property
    def component_name(self):
        package = self.package
        activity = self.data['launchTarget']['value']
        qualified = package + activity if activity.startswith('.') else (
            activity if '.' in activity else package + '.' + activity)
        short = qualified[len(package):] if qualified.startswith(package + '.') else qualified
        return package + '/' + short

    def native(self):
        data = self.data
        locator = data['capabilities']['locator']
        return {
            'schemaVersion': 2,
            'package': data['package'],
            'activity': data['launchTarget']['value'],
            'applicationId': data['applicationId'],
            'actions': list(data['capabilities']['actions']),
            'locatorTargets': [] if locator is None else list(locator['targets']),
            'observations': list(data['capabilities']['observations']),
        }

    @property
    def native_digest(self):
        return digest(self.native())

    @property
    def application_identity(self):
        data = self.data
        artifact = data['artifact']
        return {
            'bundle': data['package'],
            'artifactDigest': artifact['sha256'],
            'applicationProfileDigest': self.digest,
            'versionCode': artifact['versionCode'],
            'identityProof': 'selected-apk',
        }

    def supports_identity_requirement(self, requirement):
        return requirement in {'selected-artifact-sha256', 'installed-sha256'}


def validate_android_runtime_profile(document):
    value = _validate_runtime_document(document, 'android')
    return AndroidRuntimeProfile(json.dumps(
        value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False))


def load_android_runtime_profile(path):
    from .storage import read_json
    return validate_android_runtime_profile(read_json(path))
