#!/usr/bin/env python3
"""Record and replay all supported iOS semantic input kinds on a chosen Simulator."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from reproloop.core import require,digest
from reproloop.ios_cli import sample_capture,sample_oracle
from reproloop.ios_core import compile_ios_capture
from reproloop.ios_runner import IosSimulator
from reproloop.ios_storage import create_ios_bundle,tree_manifest
from reproloop.storage import read_json,write_json

parser=argparse.ArgumentParser()
parser.add_argument('--simulator',required=True);parser.add_argument('--build',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
a=parser.parse_args();require(not a.output.exists(),'Smoke output exists');a.output.mkdir(parents=True,mode=0o700)
receipt=read_json(a.build/'receipt.json');products=(a.build/'DerivedData/Build/Products').resolve();app=products/receipt['appRelative']
require(receipt.get('productsDigest')==digest(tree_manifest(products)),'Smoke build receipt mismatch')
capture=sample_capture();capture['events']=[
    {'id':'e1','seq':1,'action':'replace','target':'counter.name','parameters':{'value':'QA'}},
    {'id':'e2','seq':2,'action':'tap','target':'counter.next','parameters':{}},
    {'id':'e3','seq':3,'action':'navigate_back','target':'counter.back','parameters':{}},
    {'id':'e4','seq':4,'action':'scroll_to','target':'counter.bottom','parameters':{'container':'counter.list','direction':'forward'}},
    {'id':'e5','seq':5,'action':'tap','target':'counter.add','parameters':{}}]
capture['endSequence']=5;scenario=compile_ios_capture(capture,sample_oracle());sim=IosSimulator(a.simulator)
with sim.lease():
    recording=sim.run_scenario(products,app,scenario,a.output/'record','record')
    require(recording['runValid'],'Semantic recording test failed')
    actual=sim.collect_capture(recording['startedAtMs']);compiled=compile_ios_capture(actual,sample_oracle())
    expected=[(e['action'],e['target'],e['parameters']) for e in capture['events']]
    observed=[(e['action'],e['target'],e['parameters']) for e in actual['events']]
    require(expected==observed,'Recorded events differ from actual scripted input')
    bundle=create_ios_bundle(actual,sample_oracle(),products,receipt,a.output/'bundle')
    replay=sim.run_scenario(bundle['products'],bundle['app'],compiled,a.output/'replay')
    require(replay['runValid'] and replay['bugCondition'] and not replay['expectedCondition'],'Semantic replay failed')
result={'status':'semantic-smoke-passed','executionEnvironment':'simulator','actions':[e['action'] for e in actual['events']],
        'recordRun':recording['runId'],'replayRun':replay['runId']}
write_json(a.output/'result.json',result)
print(result)
