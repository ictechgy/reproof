"""Inert fixtures only. run_tests installs independent process/socket audit denial."""
import ast
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('flow_under_test', HERE/'run.py')
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)


def frame(n=1):
    return {'id': n, 'geometryVersion': 1, 'imageBase64': base64.b64encode(b'synthetic-jpeg').decode()}


def observed(*texts):
    return json.dumps({'observations': [{'text': t, 'confidence': 1.0} for t in texts]}).encode()


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        self.proof = d.new_report()

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, **kw):
        return d.observe_state(kw.pop('source', lambda: frame()), Path('/inert/observer'), self.output,
                               self.proof, kw.pop('name', '01-empty'), ['EXPECTED'], **kw)

    def test_success_binds_original_image_and_result(self):
        with mock.patch.object(d, 'observe_frame', return_value=observed('EXPECTED')) as call:
            self.assertEqual(self.observe(), 1)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.proof["observerCalls"], 1)
        self.assertLessEqual(call.call_args.args[2], 15)
        state = self.proof['states'][0]
        self.assertEqual((self.output/state['imageFile']).read_bytes(), b'synthetic-jpeg')
        self.assertEqual(state['imageSha256'], hashlib.sha256(b'synthetic-jpeg').hexdigest())
        self.assertEqual(json.loads((self.output/state['ocrFile']).read_text())['imageSha256'], state['imageSha256'])

    def test_bad_image_counts_no_ocr_invocation(self):
        bad=frame(); bad["imageBase64"]="!invalid!"
        with mock.patch.object(d,"observe_frame") as call:
            with self.assertRaises(Exception): self.observe(source=lambda:bad)
        call.assert_not_called(); self.assertEqual(self.proof["observerCalls"],0)

    def test_stopped_provider_after_ocr_cannot_pass(self):
        health=mock.Mock(side_effect=[None,None,d.DiagnosticError("capture_inactive")])
        with mock.patch.object(d,"observe_frame",return_value=observed("EXPECTED")) as call:
            with self.assertRaisesRegex(d.DiagnosticError,"capture_inactive"): self.observe(health=health)
        self.assertEqual(call.call_count,1); self.assertEqual(self.proof["states"],[])

    def test_health_lost_during_oracle_cannot_pass(self):
        lost=[False]
        def health():
            if lost[0]: raise d.DiagnosticError("capture_inactive")
        original=d.state_matches
        def oracle(*args):
            result=original(*args); lost[0]=True; return result
        with mock.patch.object(d,"observe_frame",return_value=observed("EXPECTED")), mock.patch.object(d,"state_matches",side_effect=oracle):
            with self.assertRaisesRegex(d.DiagnosticError,"capture_inactive"): self.observe(health=health)
        self.assertEqual(self.proof["states"],[])

    def test_mismatch_no_second_call(self):
        with mock.patch.object(d, 'observe_frame', return_value=observed('WRONG')) as call:
            with self.assertRaisesRegex(d.DiagnosticError, 'pixel_state_mismatch'):
                self.observe()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.proof['observationDiagnostics'][0]['outcome'], 'mismatch')
        self.assertEqual(len(list(self.output.glob('*.jpg'))), 1)

    def test_forbidden_low_confidence_still_rejected(self):
        value = {'observations': [{'text':'EXPECTED', 'confidence':1}, {'text':'BAD', 'confidence':0.01}]}
        with mock.patch.object(d, 'observe_frame', return_value=json.dumps(value).encode()):
            with self.assertRaises(d.DiagnosticError): self.observe(forbidden=['BAD'])

    def test_timeout_no_second_call_and_stages_retained(self):
        error = subprocess.TimeoutExpired(['/inert/observer'], 15,
            stderr=b'reproof_observer stage=vision_started elapsed_ms=4\nPRIVATE_VALUE')
        with mock.patch.object(d.native.subprocess, 'run', side_effect=error) as call:
            with self.assertRaisesRegex(d.GateFailure, 'pixel_observer_timeout'): self.observe()
        self.assertEqual(call.call_count, 1)
        attempt = self.proof['observationDiagnostics'][0]['attempts'][0]
        self.assertEqual(attempt['processOutcome'], 'timeout')
        self.assertEqual(attempt['observerStages'], [{'stage':'vision_started', 'elapsedMs':4}])
        self.assertNotIn('PRIVATE_VALUE', json.dumps(self.proof))

    def test_absolute_deadline_includes_slow_frame(self):
        now=[0.0]
        def source(): now[0]=26; return frame()
        with mock.patch.object(d.time, 'monotonic', side_effect=lambda:now[0]), mock.patch.object(d, 'observe_frame') as call:
            with self.assertRaisesRegex(d.GateFailure, 'pixel_state_deadline'): self.observe(source=source)
        call.assert_not_called()

    def test_remaining_budget_not_reset_for_process(self):
        now=[0.0]
        def source(): now[0]=20; return frame()
        with mock.patch.object(d.time, 'monotonic', side_effect=lambda:now[0]), mock.patch.object(d, 'observe_frame', return_value=observed('EXPECTED')) as call:
            self.observe(source=source)
        self.assertEqual(call.call_args.args[2], 5)

    def test_late_parse_cannot_pass(self):
        now=[0.0]
        def oracle(*args): now[0]=26; return True
        with mock.patch.object(d.time,'monotonic',side_effect=lambda:now[0]), mock.patch.object(d,'observe_frame',return_value=observed('EXPECTED')), mock.patch.object(d,'state_matches',side_effect=oracle):
            with self.assertRaisesRegex(d.GateFailure,'pixel_state_deadline'): self.observe()
        self.assertEqual(self.proof['states'], [])

    def test_no_ninth_ocr(self):
        self.proof['observationDiagnostics'] = [{}]*8
        with mock.patch.object(d,'observe_frame') as call:
            with self.assertRaisesRegex(d.DiagnosticError,'observer_call_limit'): self.observe()
        call.assert_not_called()

    def test_invalid_output_stops_once(self):
        with mock.patch.object(d,'observe_frame',return_value=b'not-json') as call:
            with self.assertRaisesRegex(d.GateFailure,'pixel_observer_invalid'): self.observe()
        self.assertEqual(call.call_count,1)

    def test_frame_poll_is_not_ocr_retry(self):
        with mock.patch.object(d,'observe_frame',return_value=observed('EXPECTED')) as call, mock.patch.object(d.time,'sleep'):
            self.assertEqual(self.observe(source=mock.Mock(side_effect=[frame(1),frame(2)]),last_frame=1),2)
        self.assertEqual(call.call_count,1)


class FlowTests(unittest.TestCase):
    def make_capture(self):
        self.commands=[]
        self.n=0
        def get_frame(*args): self.n+=1; return frame(self.n)
        def inject(sid, owner, command): self.commands.append(command); return {'status':'injected'}
        lab=SimpleNamespace(frame=get_frame, input=inject)
        return SimpleNamespace(lab=lab, session={'id':'inert','epoch':1}, assert_healthy=mock.Mock())

    def test_exact_eight_state_nine_input_flow(self):
        capture=self.make_capture(); proof=d.new_report(); names=[]
        def observe(source, observer, output, proof, name, required, forbidden, **kw):
            names.append(name); proof['states'].append({'name':name}); return source()['id']
        with mock.patch.object(d,'observe_state',side_effect=observe):
            d.perform_flow(capture,Path('/inert'),Path('/inert'),proof)
        self.assertEqual(names,['01-empty','02-editor','03-invalid','04-created','05-detail','06-back','07-cancel-editor','08-cancelled'])
        self.assertEqual(len(self.commands),9)
        self.assertEqual([c['action'] for c in self.commands],['tap','tap','tap','text','tap','tap','tap','tap','tap'])
        self.assertTrue(proof['flowVerified'])

    def test_first_failure_stops_later_inputs(self):
        capture=self.make_capture(); proof=d.new_report()
        with mock.patch.object(d,'observe_state',side_effect=d.DiagnosticError('failed')) as call:
            with self.assertRaises(d.DiagnosticError): d.perform_flow(capture,Path('/inert'),Path('/inert'),proof)
        self.assertEqual(call.call_count,1); self.assertEqual(self.commands,[])

    def test_failure_at_each_state_stops_exactly(self):
        expected_inputs=[0,1,2,5,6,7,8,9]
        for fail_at, count in enumerate(expected_inputs):
            capture=self.make_capture(); proof=d.new_report(); calls=[]
            def observe(source, observer, output, proof, name, required, forbidden, **kw):
                calls.append(name)
                if len(calls)==fail_at+1: raise d.DiagnosticError("failed")
                proof["states"].append({"name":name}); return source()['id']
            with mock.patch.object(d,'observe_state',side_effect=observe):
                with self.assertRaises(d.DiagnosticError): d.perform_flow(capture,Path('/inert'),Path('/inert'),proof)
            self.assertEqual(len(self.commands),count)
            self.assertEqual(len(calls),fail_at+1)

    def test_original_action_oracle_block_ast_identical(self):
        old=(d.ROOT/'scripts/ios-native-fixture-qa.py').read_text()
        new=(HERE/'run.py').read_text()
        # Compare expression statements in both functions, independent of indentation.
        def expressions(source, name):
            node=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.FunctionDef) and n.name==name)
            return [ast.dump(n) for n in ast.walk(node) if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
                    and isinstance(n.value.func,ast.Name) and n.value.func.id in {'observe','tap','send'}]
        self.assertEqual(expressions(old,'run_flow'),expressions(new,'perform_flow'))

    def test_oracles_and_observer_are_original_objects(self):
        self.assertIs(d.state_matches,d.native.state_matches)
        self.assertIs(d.observe_frame,d.native.observe_frame)
        d.validate_inputs()


class CleanupTests(unittest.TestCase):
    def capture(self, fail=False):
        c=d.OwnedCapture(SimpleNamespace(),{},Path('/inert'))
        c.session={'id':'owned'}; c.descriptor={'id':'device'}
        c.provider=SimpleNamespace(stop=SimpleNamespace(is_set=lambda:True),process=SimpleNamespace(poll=lambda:0),temp=None,lease=None)
        c.lab=SimpleNamespace(close_session=mock.Mock(side_effect=RuntimeError() if fail else None, return_value={'state':'closed'}),
            devices={'device':{'state':'quarantined' if fail else 'available'}},stop_maintenance=mock.Mock(),close_evidence_store=mock.Mock())
        c.authority=SimpleNamespace(close=mock.Mock())
        c.server=SimpleNamespace(close_operations=mock.Mock(),server_close=mock.Mock())
        return c

    def test_cleanup_once_retains_quarantine_on_failure(self):
        c=self.capture(True)
        self.assertFalse(c.close()['confirmed']); self.assertTrue(c.close()['privateStateRetained'])
        c.lab.close_session.assert_called_once_with('owned',d.OWNER,require_finished_inputs=True)
        c.authority.close.assert_not_called()

    def test_cleanup_requires_provider_exit_and_lease_release(self):
        c=self.capture(); c.provider.lease=object()
        self.assertFalse(c.close()['confirmed']); c.authority.close.assert_not_called()

    def test_cleanup_success(self):
        c=self.capture(); self.assertTrue(c.close()['confirmed']); c.authority.close.assert_called_once()

    def test_wrong_simulator_identity_cannot_shutdown(self):
        with mock.patch.object(d,'simulator_state',side_effect=d.DiagnosticError('wrong_owner')), mock.patch.object(d,'command') as cmd:
            with self.assertRaises(d.DiagnosticError): d.shutdown_simulator('uuid','name')
        cmd.assert_not_called()


class ScopeTests(unittest.TestCase):
    def test_workflow_once_only(self):
        text=(d.ROOT/'.github/workflows/original-uikit-flow.yml').read_text()
        for required in ['types: [opened]','github.run_attempt == 1','contents: read','runs-on: macos-15','persist-credentials: false']:
            self.assertIn(required,text)
        for forbidden in ['workflow_dispatch:','push:','synchronize','secrets.', 'sudo ', 'xcode-select']:
            self.assertNotIn(forbidden, '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#')))
        self.assertNotIn('diagnostics/offline-vision/',text); self.assertNotIn('diagnostics/capture-vision/',text)

    def test_no_native_main_or_comparison_followon(self):
        source=(HERE/'run.py').read_text()
        self.assertNotIn('native.main(',source); self.assertNotIn('close_all(',source)
        self.assertNotIn('TemporaryDirectory(',source)
        self.assertEqual(source.count('perform_flow(capture, observer, args.output, proof)'),1)
        self.assertEqual(source.count('build_observation_app('),1)

    def test_report_overflow_keeps_state_and_cleanup(self):
        proof=d.new_report(); proof['health']={'samples':['x'*600000]}
        proof['states']=[{'name':'01-empty','passed':True}]
        proof['cleanup']={'confirmed':False,'state':'quarantined'}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'result.json'; d.write_report(path,proof)
            result=json.loads(path.read_text())
        self.assertEqual(result['cleanup']['state'],'quarantined')
        self.assertEqual(result['states'][0]['name'],'01-empty')
        self.assertEqual(result['outcome'],'report_size_limit')
        self.assertFalse(result['passed'])

    def test_bounded_health_report_fits(self):
        proof=d.new_report()
        row={'atMs':300000,'active':True,'frameId':10000,'frameBytes':3145728,'width':4096,'height':4096,
             'frameAgeMs':999999,'historyFirstFrameId':10000,'historyLastFrameId':10030}
        proof['health']={'samples':[row]*1200}
        self.assertLess(len(json.dumps(proof,indent=2).encode()),d.MAX_REPORT_BYTES)


if __name__=='__main__': unittest.main()
