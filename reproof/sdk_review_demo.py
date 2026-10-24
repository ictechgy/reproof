"""Synthetic real-format SDK exports plus a separate PR21 synthetic capsule.

No native SDK is run and no annotations are converted into injection evidence.
"""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from .ios_cases import case_spec
from .issue_package import canonical
from .sdk_review import (CollectorBinding, ReviewInvalid, export_digest,
                         linked_review, normalize_export)
from .ticket_intake import IDENTITY
from .ticket_intake_demo import demo_material


def sample_export(platform):
    """Repository-owned synthetic files matching native finalized export formats."""
    c = case_spec('counter').capture()
    c.update(sessionId='00000000-0000-4000-8000-000000000003', startedAtMs=1000)
    for i, event in enumerate(c['events']):
        event['elapsedMs'] = i * 100
    if platform == 'android':
        c.pop('platform'); c.pop('applicationId'); c['schemaVersion'] = 1
        c['fixture'] = {'id': 'default', 'version': 1, 'inputs': {}}
        c['startState'] = {'screen': 'main', 'nodes': {'name': '', 'count': '0'}}
        for e in c['events']:
            e['target'] = e['target'].removeprefix('counter.')
        m = {k: copy.deepcopy(c[k]) for k in ('schemaVersion', 'sessionId', 'startState')}
        m.update(fixture={'id': 'default', 'version': 1}, finalized=True,
                 incomplete=False, lostEvents=False, unsupported=False)
        marker = None
    elif platform == 'ios':
        m = {k: copy.deepcopy(c[k]) for k in ('schemaVersion', 'platform', 'applicationId',
             'sessionId', 'fixture', 'startState', 'startedAtMs', 'endSequence')}
        m['finalized'] = True
        marker = canonical({'finalized': True, 'endSequence': c['endSequence']})
    else:
        raise ValueError('unsupported_platform')
    return dict(platform=platform, capture=canonical(c), metadata=canonical(m), marker=marker)


def sample_join(platform, root):
    files = sample_export(platform)
    envelope = normalize_export(**files)
    project = json.loads((Path(root) / 'examples/ticket-repair/project.json').read_text())
    # Explicitly synthetic capsule variant, not a claim about an installed build.
    project['applications'][0].update(platform=platform, bundle=envelope._application)
    oracle_digest = hashlib.sha256((Path(root) / 'examples/ticket-repair/oracle.json').read_bytes()).hexdigest()
    ticket, entry, _ = demo_material(project, oracle_digest)
    binding = CollectorBinding(export_digest(**files), '00000000-0000-4000-8000-000000000003', platform,
        envelope._application, hashlib.sha256(entry.capsule).hexdigest(),
        tuple(ticket[k] for k in IDENTITY))
    return files, envelope, binding, canonical(ticket), entry


def run_demo(root):
    outputs = {}
    for platform in ('android', 'ios'):
        files, envelope, binding, ticket, entry = sample_join(platform, root)
        args = dict(envelope=envelope, ticket=ticket, tenant_id='local_demo', entry=entry)
        incomplete = json.loads(files['capture']); incomplete['lostEvents'] = True
        try:
            normalize_export(**dict(files, capture=canonical(incomplete)))
        except ReviewInvalid:
            rejected = True
        else:
            rejected = False
        outputs[platform] = {'normalized': envelope.summary(),
            'linked': linked_review(**args, binding=binding),
            'missingBinding': linked_review(**args, binding=None),
            'mismatchedBinding': linked_review(**args, binding=replace(binding, export_digest='0' * 64)),
            'incompleteRejectedBeforeDraft': rejected}
    return {'provenance': 'repository-synthetic-sdk-format-and-separate-pr21-capsule-variant',
            'actualAI': False, 'nativeExecution': False, 'companyAcceptance': False,
            'offlineCausalStageInvoked': False, 'platforms': outputs}
