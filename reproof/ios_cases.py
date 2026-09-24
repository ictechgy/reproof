"""Trusted, finite sample-case contracts; recordings cannot register new policies."""
from dataclasses import dataclass
from .core import require


@dataclass(frozen=True)
class IosCase:
    name: str
    fixture_id: str
    product_file: str
    logic_test: str
    bug_value: str
    expected_value: str
    actual: str
    expected: str
    expression_pattern: str
    edit_policy: str

    @property
    def fixture(self):return {'id':self.fixture_id,'version':1,'inputs':{}}
    @property
    def policy_adapter(self):return self.fixture_id+'-v1'
    @property
    def tap_targets(self):return frozenset({'add','next','back'} | ({'reset'} if self.name=='reset' else set()))
    def oracle(self):
        return {'actual':self.actual,'expected':self.expected,
                'bugCondition':{'target':'counter.count','text':self.bug_value},
                'expectedCondition':{'target':'counter.count','text':self.expected_value}}
    def capture(self):
        events=[{'id':'e1','seq':1,'action':'replace','target':'counter.name','parameters':{'value':'QA'}},
                {'id':'e2','seq':2,'action':'tap','target':'counter.add','parameters':{}}]
        if self.name in {'duplicate-submit','reset'}:
            events.append({'id':'e3','seq':3,'action':'tap',
                           'target':'counter.reset' if self.name=='reset' else 'counter.add','parameters':{}})
        return {'schemaVersion':2,'platform':'ios','applicationId':'io.reproof.sample.ios','sessionId':'scripted-ios-session',
                'fixture':self.fixture,'startState':{'screen':'main','nodes':{'counter.name':'','counter.count':'0'}},
                'events':events,'truncated':False,'lostEvents':False,'endSequence':len(events)}


CASES={
    'counter':IosCase('counter','ios-counter','Sample/CounterLogic.swift',
        'ReproLogicTests/CounterLogicTests/testIncrementIsOne','2','1',
        'One completed Add increments count twice','One completed Add increments count once',
        r'\bstatic\s+func\s+increment\s*\(\s*\)\s*->\s*Int\s*\{\s*return\s+(?P<value>[0-9]{1,2})\s*\}',
        'Change only the integer return expression. Preserve all surrounding source.'),
    'duplicate-submit':IosCase('duplicate-submit','ios-duplicate-submit','Sample/SubmissionLogic.swift',
        'ReproLogicTests/SubmissionLogicTests/testDuplicateIsRejected','2','1',
        'Two submissions of the same item are both accepted','Accept the first submission and reject subsequent duplicates',
        r'\bstatic\s+func\s+shouldAccept\s*\(submitted:\s*Bool\)\s*->\s*Bool\s*\{\s*return\s+(?P<value>true|false|!submitted)\s*\}',
        'Change only the Boolean return expression using true, false or !submitted. Preserve all other source.'),
    'reset':IosCase('reset','ios-reset','Sample/ResetLogic.swift',
        'ReproLogicTests/ResetLogicTests/testResetClearsValue','1','0',
        'Reset leaves the previous count in place','Reset clears the count to zero for any previous count',
        r'\bstatic\s+func\s+resetValue\s*\(previous:\s*Int\)\s*->\s*Int\s*\{\s*return\s+(?P<value>previous|[0-9]{1,2})\s*\}',
        'Change only the return expression using previous or an integer literal. Preserve all other source.')
}


def case_spec(name='counter'):
    require(isinstance(name,str) and name in CASES,'Unknown iOS sample case')
    return CASES[name]


def case_from_fixture(fixture):
    require(isinstance(fixture,dict) and set(fixture)=={'id','version','inputs'}
            and type(fixture.get('version')) is int and fixture['version']==1 and fixture['inputs']=={},'Invalid iOS fixture schema')
    matches=[c for c in CASES.values() if c.fixture_id==fixture.get('id')]
    require(len(matches)==1,'Unsupported iOS fixture')
    return matches[0]
