"""Android sample artifacts and evidence at the Live repair boundary."""
from pathlib import Path
import sys

from .model import check
from ..android_build import validate_protected_build
from ..core import ContractError, classify_runs, digest
from ..orchestrator import APK_RELATIVE, BUILD_TASK, PRODUCT_FILE
from ..repair import snapshot_source
from ..storage import create_bundle, load_bundle, read_json, sha_file


class AndroidRepairProject:
    def __init__(self, source, build, app_profile=None):
        self.source = Path(source)
        self.build = Path(build)
        self.app_profile = app_profile

    def validate(self):
        try:
            receipt = validate_protected_build(self.build, source=self.source, app_profile=self.app_profile)
        except (ContractError, OSError):
            check(False, 'repair_configuration', 'Protected Android source or build artifact changed', 400)
        if self.app_profile is None:
            check(receipt.get('buildTask') == BUILD_TASK and receipt.get('variant') == 'buggy',
                  'repair_configuration', 'Android repair requires the matching protected buggy sample build', 400)
        return receipt, self.build, self.build / 'original.apk'

    def capture_bundle(self, provider, capture, receipt, root):
        from ..cli import sample_oracle
        oracle = self.app_profile.oracle() if self.app_profile else sample_oracle()
        provider.device.installation_proof(self.build / 'original.apk')
        observation = provider.observe()
        if self.app_profile:
            check(observation.get('profileDigest') == self.app_profile.digest
                  and observation.get('nativeDigest') == self.app_profile.native_digest,
                  'capture_invalid', 'Native observation used a different app profile')
        bug, expected = oracle['bugCondition'], oracle['expectedCondition']
        counts = [n for n in observation.get('nodes', []) if n.get('id') == bug['target']]
        check(observation.get('ready') is True and len(counts) == 1
              and counts[0].get('visible') is True and counts[0].get('text') == bug['text'],
              'analysis_inconclusive', 'The visible sample count does not clearly show the counter bug')
        analysis = {'method': 'native-app-accessibility' if self.app_profile else 'native-sample-accessibility',
                    'observedCount': bug['text'], 'expectedCount': expected['text']}
        if self.app_profile:analysis['appProfileDigest'] = self.app_profile.digest
        diagnostics = None
        if self.app_profile and self.app_profile.data.get('captureMode') == 'debug_receiver':
            diagnostics = provider.collect_sdk_diagnostics(capture)
            analysis['instrumentedActions'] = len(diagnostics['actions'])
        bundle = create_bundle(capture, oracle, self.build / 'original.apk',
                               root / 'bundle', receipt, 'live-sdk', app_profile=self.app_profile, diagnostics=diagnostics)
        return bundle, analysis

    def command(self, provider, bundle, root, receipt):
        command = [sys.executable, '-m', 'reproloop', 'repair', str(bundle['path']),
                '--serial', provider.device.serial, '--source', str(self.source),
                '--output', str(root / 'repair'), '--agent', 'claude', '--max-attempts', '2',
                '--driver-apk', str(self.build / 'driver.apk'), '--driver-sha256', receipt['driverSha256']]
        if self.app_profile:
            command.extend(['--app-profile', str(self.build / 'app-profile.json')])
        return command

    def candidate(self, result, root, receipt, device_id):
        check(result.get('status') == 'verified' and result.get('agent') == 'claude',
              'repair_failed', 'Android repair did not verify a real agent proposal')
        baseline = read_json(root / 'repair/baseline/result.json')
        attempts = result.get('attempts', [])
        check(bool(attempts) and attempts[-1].get('attemptId') in {1, 2}
              and attempts[-1].get('status') == 'verified', 'repair_failed', 'Missing verified candidate attempt')
        attempt = root / ('repair/attempt-' + str(attempts[-1]['attemptId']))
        verification = read_json(attempt / 'verification/result.json')
        bundle = load_bundle(root / 'bundle', app_profile=self.app_profile)
        check(baseline.get('status') == 'reproduced' and baseline.get('repeats') == 3
              and classify_runs(baseline['runs'], 'original', 3) == 'reproduced'
              and verification.get('status') == 'verified' and verification.get('repeats') == 3
              and classify_runs(verification['runs'], 'patched', 3) == 'verified',
              'repair_failed', 'Android repair lacks complete original and patched device evidence')
        check(baseline.get('apkSha256') == receipt['apkSha256']
              and baseline.get('bundleDigest') == bundle['manifestDigest']
              and verification.get('bundleDigest') == bundle['manifestDigest'],
              'repair_failed', 'Android results do not belong to the recorded bundle')
        for report in (baseline, verification):
            check(all(run.get('installation', {}).get('apkSha256') == report.get('apkSha256')
                      and run['installation'].get('installedVerified') is True
                      and run['installation'].get('fixtureVerified') is True for run in report['runs']),
                  'repair_failed', 'Android run lacks proof of the installed app and reset fixture')
        for run in baseline['runs'] + verification['runs']:
            if self.app_profile:
                native = run.get('nativeProfileProof', {})
                check(run.get('appProfileDigest') == self.app_profile.digest
                      and run.get('nativeDigest') == self.app_profile.native_digest
                      and native.get('profileDigest') == self.app_profile.digest
                      and native.get('nativeDigest') == self.app_profile.native_digest
                      and native.get('operation') == 'observe',
                      'repair_failed', 'Replay used a different app profile')
            runner = run.get('runner', {})
            check(runner.get('apkSha256') == receipt['driverSha256'] and runner.get('installedVerified') is True
                  and all(runner.get(phase, {}).get('apkSha256') == receipt['driverSha256']
                          and runner[phase].get('installedVerified') is True
                          and runner[phase].get('deviceId') == device_id for phase in ('before', 'after')),
                  'repair_failed', 'Android replay did not preserve its protected driver')
            check(run.get('bundleDigest') == bundle['manifestDigest']
                  and run.get('scenarioDigest') == bundle['scenario']['scenarioDigest']
                  and run.get('installation', {}).get('deviceId') == device_id,
                  'repair_failed', 'Android run does not match the recorded scenario and selected device')
        tests = attempts[-1].get('regressionTests', {})
        check(type(tests.get('tests')) is int and tests['tests'] > 0
              and all(tests.get(k) == 0 for k in ('failures', 'errors', 'skipped')),
              'repair_failed', 'Android candidate lacks passing protected regression tests')
        apk_relative = self.app_profile.data['build']['apk'] if self.app_profile else APK_RELATIVE
        product_file = self.app_profile.data['edit']['path'] if self.app_profile else PRODUCT_FILE
        candidate = attempt / 'source' / apk_relative
        proof = read_json(attempt / 'build-receipt.json')
        candidate_source = snapshot_source(attempt / 'source',
            source_inputs=self.app_profile.data.get('sourceInputs') if self.app_profile else None, isolated=True)
        protected = {k: v for k, v in receipt['sourceFiles'].items() if k != product_file}
        check(candidate.is_file() and not candidate.is_symlink()
              and proof.get('buildCompleted') is True and sha_file(candidate) == proof.get('apkSha256')
              and verification.get('apkSha256') == proof['apkSha256']
              and proof.get('sourceDigest') == digest(candidate_source)
              and {k: v for k, v in candidate_source.items() if k != product_file} == protected
              and proof.get('protectedDigest') == digest(protected),
              'repair_failed', 'Android candidate differs from its verified artifact')
        if self.app_profile:
            check(proof.get('appProfileDigest') == self.app_profile.digest
                  and result.get('appProfileDigest') == self.app_profile.digest,
                  'repair_failed', 'Candidate was built under a different app profile')
            from ..build_instrumentation import is_build_instrumented, validate_bytecode_artifacts
            if is_build_instrumented(self.app_profile):
                try:
                    bytecode = validate_bytecode_artifacts(attempt / 'source', self.app_profile)
                    check(proof.get('bytecodeInstrumentation') == bytecode,
                          'repair_failed', 'Candidate bytecode differs from the verified instrumentation')
                except (ContractError, OSError):
                    check(False, 'repair_failed', 'Candidate bytecode instrumentation proof is invalid')
        agent_receipt = attempts[-1].get('agentReceipt', {})
        check(agent_receipt.get('provider') == 'claude' and agent_receipt.get('status') == 'completed',
              'repair_failed', 'Android candidate has no completed Claude request receipt')
        self.validate()
        identity = {'bundle': self.app_profile.data['package'] if self.app_profile else 'io.reproloop.sample',
                    'artifactDigest': proof['apkSha256']}
        if self.app_profile:identity['appProfileDigest'] = self.app_profile.digest
        return candidate, identity
