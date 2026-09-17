#!/usr/bin/env python3
"""Explicit real-device smoke test; only the dedicated sample package is reset."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from reproloop.core import compile_capture,ContractError
from reproloop.device import AdbDevice,DRIVER,PACKAGE
from reproloop.storage import create_bundle,read_json,write_json
from reproloop.cli import sample_oracle

parser=argparse.ArgumentParser()
parser.add_argument('--apk',type=Path,required=True)
parser.add_argument('--driver-apk',type=Path,required=True)
parser.add_argument('--receipt',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--serial')
a=parser.parse_args();a.output.mkdir(parents=True,exist_ok=False)
d=AdbDevice(a.serial);evidence={}
with d.lease():
    d.install(a.driver_apk,DRIVER)
    try:
        d.prepare(a.apk,{'id':'default','version':1,'inputs':{}},'record')
        d.driver('replace',target='name',value='QA')
        d.driver('scroll_to',target='bottom',container='list',direction='forward')
        d.driver('tap',target='next')
        d.driver('back',target='back')
        d.driver('tap',target='add')
        capture=d.freeze_capture();compiled=compile_capture(capture,sample_oracle())
        actions=[s['action'] for s in compiled['steps']]
        assert actions==['replace','scroll_to','tap','back','tap'],actions
        create_bundle(capture,sample_oracle(),a.apk,a.output/'extended-bundle',read_json(a.receipt),'synthetic-driver')
        evidence['semanticActions']=actions;evidence['extendedCapture']='passed'
        d.prepare(a.apk,{'id':'default','version':1,'inputs':{}},'record')
        marker='REPRO_TEST_SECRET_NEVER_PERSIST'
        d.driver('replace',target='name',value=marker)
        capture=d.freeze_capture()
        assert capture['lostEvents'] is True
        serialized=json.dumps(capture)
        assert marker not in serialized
        for name in ['events.jsonl','session.json']:
            path=f"files/repro/{capture['sessionId']}/{name}"
            # An entirely rejected stream may have no JSONL file.
            result=d.shell('run-as',PACKAGE,'sh','-c','if [ -f "$1" ]; then cat "$1"; fi','sh',path)
            assert marker not in result
        try:compile_capture(capture,sample_oracle());raise AssertionError('Unsafe session was accepted')
        except ContractError:pass
        evidence['unsafeInputRejected']='passed';evidence['unsafeValuePersisted']=False
    finally:d.stop()
write_json(a.output/'smoke.json',evidence)
print(json.dumps(evidence,indent=2))
