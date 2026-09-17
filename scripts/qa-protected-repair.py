#!/usr/bin/env python3
"""Owned proposal/protected software QA; actual VM, device and AI are separate gates."""
import argparse
import json
from pathlib import Path
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reproloop.agents import ProjectPatchAgent
from reproloop.execution.artifacts import BlobSet
from reproloop.execution.wire import canonical


def run_synthetic(output, *, browser=False, protected=False):
    from reproloop.live.server import LiveServer
    from tests.test_live_project_repair import LiveProjectRepairTests
    from tests.test_project_repair import EDIT
    fixture = LiveProjectRepairTests('runTest')
    server = None; thread = None
    try:
        fixture.setUp()
        patch_file = fixture.env.root / 'local-proposal.json'
        patch_file.write_bytes(canonical({'edits': [EDIT]}))
        executor = fixture.protected_runtime() if protected else None
        fixture.service(ProjectPatchAgent(patch_file), executor=executor,
                        disk_limit=(288 if protected and browser else 256) * 1024 * 1024)
        before = fixture.env.source.freeze('original')
        job = fixture.wait(fixture.start()['id'])
        if job['status'] != 'proposal-ready': raise RuntimeError('proposal-path-failed')
        proposal = fixture.repairs.proposal(fixture.fixture.owner, job['id'])
        verification = fixture.wait(fixture.start(mode='verify', request='verification_request')['id'])
        if not protected and (verification['status'] != 'blocked' or verification['reason'] != 'protected_verification_unavailable'):
            raise RuntimeError('unqualified-verification-was-not-blocked')
        if protected and (verification['status'] != 'verified' or verification['result']['verified'] is not True):
            raise RuntimeError('protected-software-composition-failed')
        fixture.env.source.require_original(before, 'original')
        report = {'schemaVersion': 1, 'status': 'passed-proposal-software', 'verified': False,
            'actualVM': False, 'actualMobile': False, 'actualAI': False, 'companyAcceptance': False,
            'providerKind': 'local-test-adapter', 'projectDigest': fixture.env.source.project_digest,
            'originalSourceDigest': before.digest, 'candidateSourceDigest': proposal['candidateSourceDigest'],
            'repairPlanDigest': proposal['repairPlanDigest'], 'baselineDigest': job['plan']['baselineDigest'],
            'attemptBudget': job['plan']['attemptBudget'], 'originalUnchanged': True,
            'checks': [{'check': 'original-reproduced-three-times', 'passed': True},
                {'check': 'general-product-patch-from-local-adapter', 'passed': True},
                {'check': 'immutable-source-and-protected-inputs', 'passed': True}]}
        if protected:
            after = verification['result']['afterEvidence']
            report.update(status='passed-protected-software', protectedComposition={
                'kind': 'explicit-vm-and-disposable-device-doubles', 'jobVerified': True,
                'candidateRuns': len(after['attempts']), 'cleanupConfirmed': after['cleanupConfirmed']})
            report['checks'].append({'check': 'bound-build-signing-independent-checks-three-replays-and-cleanup', 'passed': True})
            with (output / 'protected-evidence.json').open('xb') as stream:
                stream.write(canonical({'schemaVersion': 1, 'scope': 'explicit-protocol-doubles',
                    'actualVM': False, 'actualMobile': False, 'actualAI': False, 'companyAcceptance': False,
                    'attemptBudget': verification['plan']['attemptBudget'], 'result': verification['result']}) + b'\n')
        else:
            report['verificationDenial'] = {'status': verification['status'], 'reason': verification['reason']}
            report['checks'].append({'check': 'unqualified-verification-denied-before-proposal', 'passed': True})
        with (output / 'proposal.json').open('xb') as stream: stream.write(canonical(proposal) + b'\n')
        with (output / 'change.diff').open('xb') as stream: stream.write(proposal['patch'].encode('utf-8'))
        if browser:
            from tests.fixtures.g9_browser_check import browser_check
            store = fixture.fixture.access_store
            token = store.issue_principal_credential('admin', 'owner', lifetime_seconds=600)['token']
            principal = store.authenticate_principal(token)
            server = LiveServer(fixture.env.lab, access=fixture.fixture.access, issue_workflow=fixture.workflow)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            report['browser'] = browser_check(server.origin, token, fixture.issue_id, output,
                revoke=lambda: store.revoke_credential('admin', principal.credential_id), verification=protected)
        return report
    finally:
        if server is not None:
            server.shutdown(); thread.join(2); server.server_close()
        fixture.doCleanups()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', help='synthetic-local or synthetic-protected for explicit software doubles; actual repair needs an operator-qualified environment')
    parser.add_argument('--browser', action='store_true', help='Use the installed isolated browser with synthetic data')
    parser.add_argument('--output-new', type=Path, required=True, nargs='?',
                        const=ROOT / 'artifacts' / ('protected-repair-' + uuid.uuid4().hex))
    args = parser.parse_args()
    report = {'schemaVersion': 1, 'status': 'blocked-unqualified', 'verified': False,
        'actualVM': False, 'actualMobile': False, 'actualAI': False, 'companyAcceptance': False,
        'reason': 'environment-not-supplied' if args.environment is None else 'operator-qualified-environment-not-supplied', 'checks': []}
    output = None
    try:
        selected = args.output_new.absolute()
        # Inert bounded output creation pins parent components and never
        # overwrites an existing directory. No environment receipt grants authority.
        BlobSet((('scope.json', canonical({'schemaVersion': 1, 'kind': 'repair-software-qa', 'verified': False,
            'actualVM': False, 'actualMobile': False, 'actualAI': False, 'companyAcceptance': False})),)).write_new(selected)
        output = selected
        if args.environment in {'synthetic-local', 'synthetic-protected'}:
            report = run_synthetic(output, browser=args.browser, protected=args.environment == 'synthetic-protected')
    except Exception:
        report.update(status='failed', reason='protected-repair-qa-or-output-failed', verified=False)
    if output is not None:
        try:
            with (output / 'result.json').open('xb') as stream: stream.write(canonical(report) + b'\n')
        except OSError:
            report.update(status='failed', reason='result-publication-failed', verified=False)
    print(json.dumps(report, sort_keys=True, separators=(',', ':')))
    return 0 if report['status'] in {'passed-proposal-software', 'passed-protected-software'} else 2


if __name__ == '__main__': sys.exit(main())
