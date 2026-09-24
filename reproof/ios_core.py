"""Strict iOS v2 policy; Android v1 remains unchanged."""
from __future__ import annotations
import copy
import re
from .core import ContractError,compile_capture,digest,require
from .ios_cases import case_from_fixture,case_spec

APPLICATION_ID='io.reproof.sample.ios'
EVIDENCE_KIND='ios-simulator-install-receipt-runtime-id'
PHYSICAL_EVIDENCE_KIND='ios-physical-install-receipt-runtime-id'
IOS_IDS={f'counter.{name}':name for name in ('name','count','add','next','back','list','bottom','reset')}


def ios_evidence_kind(environment):
    require(environment in {'simulator','physical-iphone'},'Unsupported iOS execution environment')
    return PHYSICAL_EVIDENCE_KIND if environment=='physical-iphone' else EVIDENCE_KIND


def _local_id(value):
    require(isinstance(value,str) and value in IOS_IDS,'Unsupported iOS semantic ID')
    return IOS_IDS[value]


def compile_ios_capture(capture,oracle):
    require(isinstance(capture,dict) and type(capture.get('schemaVersion')) is int and capture['schemaVersion']==2,
            'iOS capture requires schema v2')
    require(capture.get('platform')=='ios' and capture.get('applicationId')==APPLICATION_ID,'Unsupported iOS application')
    require(isinstance(capture.get('fixture'),dict) and type(capture['fixture'].get('version')) is int,'Invalid iOS fixture schema')
    spec=case_from_fixture(capture.get('fixture'))
    normalized=copy.deepcopy(capture);normalized.pop('platform');normalized.pop('applicationId')
    normalized['schemaVersion']=1;normalized['fixture']={'id':'default','version':1,'inputs':{}}
    start=normalized.get('startState')
    require(isinstance(start,dict) and isinstance(start.get('nodes'),dict),'Missing iOS start state')
    start['nodes']={_local_id(k):v for k,v in start['nodes'].items()}
    events=normalized.get('events');require(isinstance(events,list),'Missing iOS events')
    for event in events:
        require(isinstance(event,dict),'Invalid iOS event')
        require(event.get('action') in {'tap','replace','scroll_to','navigate_back'},'Unsupported iOS action')
        event['target']=_local_id(event.get('target'))
        if event['action']=='navigate_back':
            require(event['target']=='back','Invalid navigation target');event['action']='tap'
        params=event.get('parameters')
        require(isinstance(params,dict),'Missing iOS parameters')
        if 'container' in params:params['container']=_local_id(params['container'])
    require(isinstance(oracle,dict) and oracle.get('bugCondition')=={'target':'counter.count','text':spec.bug_value}
            and oracle.get('expectedCondition')=={'target':'counter.count','text':spec.expected_value},'Unsupported iOS sample oracle')
    normalized_oracle=copy.deepcopy(oracle)
    require(isinstance(normalized_oracle,dict),'Invalid iOS oracle')
    for name in ('bugCondition','expectedCondition'):
        require(isinstance(normalized_oracle.get(name),dict),'Missing iOS oracle condition')
        normalized_oracle[name]['target']=_local_id(normalized_oracle[name].get('target'))
    compiled=compile_capture(normalized,normalized_oracle,tap_targets=spec.tap_targets)
    compiled.update(schemaVersion=2,platform='ios',applicationId=APPLICATION_ID,
                    fixture=copy.deepcopy(capture['fixture']),startState=copy.deepcopy(capture['startState']),
                    oracle=copy.deepcopy(oracle),captureDigest=digest(capture))
    for step,event in zip(compiled['steps'],capture['events']):
        step['action']=event['action'];step['target']=event['target'];step['parameters']=copy.deepcopy(event['parameters'])
    compiled.pop('scenarioDigest');compiled['scenarioDigest']=digest(compiled)
    return compiled


def validate_swift_expression(before,after,case_name="counter"):
    pattern=re.compile(case_spec(case_name).expression_pattern)
    original=list(pattern.finditer(before));patched=list(pattern.finditer(after))
    require(len(original)==1 and len(patched)==1,'Unsupported Swift increment expression')
    a,b=original[0],patched[0]
    require(before[:a.start('value')]==after[:b.start('value')] and before[a.end('value'):]==after[b.end('value'):],
            'Patch changes protected Swift structure')
    require(a.group('value')!=b.group('value'),'Swift patch makes no product change')


def validate_test_summary(summary):
    require(isinstance(summary,dict),'Missing XCTest summary')
    for key in ('totalTestCount','passedTests','failedTests','skippedTests'):
        require(type(summary.get(key)) is int,'Missing XCTest execution count')
    require(summary['totalTestCount']==1 and summary['passedTests']==1 and summary['failedTests']==0
            and summary['skippedTests']==0 and not summary.get('testFailures'),
            'XCTest did not execute exactly one passing test without skips')
    return True


def validate_finalization(capture,metadata,marker,min_started_at=None):
    require(isinstance(capture,dict) and isinstance(metadata,dict) and isinstance(marker,dict),'Missing iOS finalization evidence')
    require(metadata.get('finalized') is True and marker.get('finalized') is True,'iOS session was not durably finalized')
    require(metadata.get('sessionId')==capture.get('sessionId') and metadata.get('endSequence')==capture.get('endSequence')
            and marker.get('endSequence')==capture.get('endSequence'),'iOS finalization boundary mismatch')
    require(metadata.get('fixture')==capture.get('fixture') and metadata.get('startState')==capture.get('startState'),
            'Finalized starting state differs from capture')
    require(type(capture.get('startedAtMs')) is int and metadata.get('startedAtMs')==capture['startedAtMs'],
            'Missing capture start timestamp')
    if min_started_at is not None:require(capture['startedAtMs']>=min_started_at,'Collected iOS capture is stale')
    return True
