"""Human/CI commands for durable local recordings and queued automation jobs."""
from __future__ import annotations
import argparse
import getpass
import json
from pathlib import Path
import sys
import time
from urllib.error import URLError
import uuid
from .client import Client
from .model import LiveError,check,public_id
from ..core import ContractError
from ..storage import read_json,_unique_object

TERMINAL={'succeeded','failed','cancelled','interrupted'}


def wait_job(client,job_id,seconds=3600):
    public_id(job_id);deadline=time.monotonic()+seconds
    while True:
        job=client.call('/api/jobs/'+job_id)['job']
        if job['state'] in TERMINAL:return job
        check(time.monotonic()<deadline,'wait_timeout','Client wait expired; the submitted job continues on the server')
        time.sleep(.2)


def _variables(record,stdin=False):
    if stdin:
        raw=sys.stdin.read(1024*1024+1)
        check(len(raw.encode())<=1024*1024,'invalid_variables','Variable input exceeds 1 MiB',400)
        value=json.loads(raw,object_pairs_hook=_unique_object)
        check(isinstance(value,dict),'invalid_variables','Expected a JSON object of variables',400)
        return value
    names=record['variables']
    check(not names or sys.stdin.isatty(),'variables_required','Use --variables-stdin for noninteractive text variables',400)
    return {name:getpass.getpass(f'{name}: ') for name in names}


def _parser():
    parser=argparse.ArgumentParser(prog='reproof',description='Local Live recording and automation commands')
    commands=parser.add_subparsers(dest='command',required=True)
    from .issue_commands import add_issue_parser
    add_issue_parser(commands)
    recordings=commands.add_parser('live-recordings');rs=recordings.add_subparsers(dest='operation',required=True)
    jobs=commands.add_parser('live-jobs');js=jobs.add_subparsers(dest='operation',required=True)
    leaves=[]
    leaves.append(rs.add_parser('list'))
    show=rs.add_parser('show');show.add_argument('id');leaves.append(show)
    imp=rs.add_parser('import');imp.add_argument('file',type=Path);leaves.append(imp)
    derive=rs.add_parser('derive');derive.add_argument('id');derive.add_argument('--speed',type=float,default=1);derive.add_argument('--events',nargs='*');leaves.append(derive)
    export=rs.add_parser('export');export.add_argument('id');export.add_argument('--output',type=Path,required=True)
    export.add_argument('--format',choices=['json','python'],default='json');leaves.append(export)
    leaves.append(js.add_parser('list'))
    for name in ('show','cancel','wait'):
        child=js.add_parser(name);child.add_argument('id');leaves.append(child)
        if name=='wait':child.add_argument('--wait-timeout',type=int,default=3600)
    submit=js.add_parser('submit');submit.add_argument('id');submit.add_argument('--request-id',default=None)
    submit.add_argument('--repeats',type=int,default=1);submit.add_argument('--timeout',type=int,default=900)
    submit.add_argument('--variables-stdin',action='store_true');submit.add_argument('--wait',action='store_true');leaves.append(submit)
    for child in leaves:child.add_argument('--server',default='http://127.0.0.1:8765')
    return parser


def main(argv=None):
    args=_parser().parse_args(argv)
    try:
        if args.command=='live-issues':
            from .issue_commands import run_issue_command
            result=run_issue_command(args)
            print(json.dumps(result,ensure_ascii=False))
            return 2 if result.get('issue',{}).get('state') in {'failed','quarantined','unknown','cancelled'} else 0
        client=Client(args.server)
        if hasattr(args,'id'):public_id(args.id)
        if args.command=='live-recordings':
            if args.operation=='list':result=client.call('/api/recordings')
            elif args.operation=='show':result=client.call('/api/recordings/'+args.id)
            elif args.operation=='import':
                record=read_json(args.file)
                result=client.call('/api/recordings/import',{'recording':record.get('recording',record) if isinstance(record,dict) else record})
            elif args.operation=='derive':
                body={'speed':args.speed}
                if args.events is not None:body['eventIds']=args.events
                result=client.call('/api/recordings/'+args.id+'/derive',body)
            else:
                check(not args.output.exists(),'output_exists','Export output already exists')
                if args.format=='json':data=json.dumps(client.call('/api/recordings/'+args.id)['recording'],ensure_ascii=False,indent=2).encode()+b'\n'
                else:data=client.download('/api/recordings/'+args.id+'/script')
                args.output.parent.mkdir(parents=True,exist_ok=True)
                with args.output.open('xb') as stream:stream.write(data)
                result={'exported':str(args.output),'format':args.format}
        else:
            if args.operation=='list':result=client.call('/api/jobs')
            elif args.operation=='show':result=client.call('/api/jobs/'+args.id)
            elif args.operation=='cancel':result=client.call('/api/jobs/'+args.id+'/cancel',{})
            elif args.operation=='wait':
                check(1<=args.wait_timeout<=7200,'invalid_timeout','Wait timeout must be 1–7200 seconds',400)
                result={'job':wait_job(client,args.id,args.wait_timeout)}
            else:
                record=client.call('/api/recordings/'+args.id)['recording'];variables=_variables(record,args.variables_stdin)
                try:
                    result=client.call('/api/jobs',{'recordingId':args.id,'requestId':args.request_id or uuid.uuid4().hex,
                        'variables':variables,'repeats':args.repeats,'timeoutSeconds':args.timeout})
                finally:variables.clear()
                if args.wait:result={'job':wait_job(client,result['job']['id'],args.timeout+65)}
        print(json.dumps(result,ensure_ascii=False))
        state=result.get('job',{}).get('state')
        return 2 if state in {'failed','interrupted'} or (args.operation in {'submit','wait'} and state=='cancelled') else 0
    except LiveError as error:
        print(json.dumps({'error':{'code':error.code,'message':str(error)}}));return 2
    except (ContractError,ValueError,TypeError,OSError,URLError):
        print(json.dumps({'error':{'code':'local_operation_failed','message':'Check the local server, input file, and request arguments'}}));return 2
    except KeyboardInterrupt:
        print(json.dumps({'error':{'code':'client_interrupted','message':'Client stopped; submitted jobs remain on the server'}}));return 130
