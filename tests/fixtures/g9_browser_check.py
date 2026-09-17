"""Actual browser checks over an owned G9 proposal service, with no external AI."""
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid


def browser_check(origin, credential, issue_id, output, *, revoke, verification=False, diagnostics=None):
    executable = shutil.which('agent-browser')
    if executable is None: raise RuntimeError('Installed agent-browser is required')
    session = 'codex-g9-' + uuid.uuid4().hex[:12]
    prefix = [executable, '--session', session, '--allowed-domains', '127.0.0.1,localhost', '--json']
    checks = []; output = Path(output)
    def require(condition, code):
        if not condition: raise RuntimeError(code)
    def command(*args, stdin=None):
        result = subprocess.run([*prefix, *args], input=stdin, text=True, capture_output=True, timeout=25)
        # Browser output can include sign-in state. Never forward it wholesale.
        require(result.returncode == 0, 'browser-command-failed')
        value = json.loads(result.stdout)
        require(value.get('success') is True, 'browser-operation-failed')
        return value.get('data')
    def evaluate(script): return command('eval', '--stdin', stdin=script)['result']
    def wait(script):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = evaluate(script)
            if value: return value
            time.sleep(.1)
        failure = {'reasonCode': 'browser-state-timeout', 'completedChecks': [row['check'] for row in checks]}
        try:
            failure['browser'] = readout()
            failure['browser']['status'] = failure['browser']['status'][:300]
            failure['browser']['provider'] = failure['browser']['provider'][:200]
        except Exception as error:
            failure['browserReadError'] = type(error).__name__
        if diagnostics is not None:
            try: failure['server'] = diagnostics()
            except Exception as error: failure['serverReadError'] = type(error).__name__
        (output/'failure.json').write_text(json.dumps(failure, indent=2)+'\n')
        raise RuntimeError('browser-state-timeout')
    def click(selector):
        evaluate('document.querySelector(' + json.dumps(selector) + ').scrollIntoView({block:"center"});true')
        command('click', selector); command('snapshot', '-i')
    def select_issue():
        click('#qa-library button')
        wait('document.querySelector("#qa-issue-title").textContent.includes(' + json.dumps(issue_id[:12]) + ')')
    def readout():
        return evaluate('(() => ({proposalDisabled:document.querySelector("#qa-propose").disabled,'
            'verifyDisabled:document.querySelector("#qa-verify").disabled,'
            'status:document.querySelector("#qa-repair-status").textContent,'
            'provider:document.querySelector("#qa-repair-provider").textContent,'
            'reviewVisible:!document.querySelector("#qa-repair-review").hidden,'
            'reviewBytes:document.querySelector("#qa-repair-patch").textContent.length,'
            'horizontalOverflow:document.documentElement.scrollWidth>innerWidth}))()')
    try:
        command('open', origin); command('set', 'viewport', '1440', '1000')
        wait('!!document.querySelector("#qa-credential")')
        # This new disposable credential travels only in stdin, never argv,
        # console output, screenshots or a retained browser state file.
        evaluate('(() => {document.querySelector("#qa-credential").value=' + json.dumps(credential)
            + ';document.querySelector("#qa-login-form").requestSubmit();return true;})()')
        wait('!document.querySelector("#qa-workspace").hidden && document.querySelectorAll("#qa-library button").length===1')
        require(evaluate('document.querySelector("#qa-credential").value.length===0'), 'credential-control-not-cleared')
        select_issue(); wait('!document.querySelector("#qa-propose").disabled')
        require(readout()['verifyDisabled'] is not verification, 'verification-availability-mismatch')
        checks.append({'check': 'qualified-original-gates-proposal', 'passed': True})
        click('#qa-propose')
        wait('document.querySelector("#qa-repair-status").textContent.includes("Ready for review")')
        click('#qa-repair-list .qa-result:last-child button')
        wait('!document.querySelector("#qa-repair-review").hidden')
        require(evaluate('document.querySelector("#qa-repair-patch").textContent.includes("+    if ready")'),
                'product-patch-not-visible')
        desktop = readout()
        require(desktop['verifyDisabled'] is not verification and not desktop['horizontalOverflow']
                and 'test adapter' in desktop['provider'] and 'verification pending' in desktop['status'],
                'proposal-presented-as-verified')
        checks.append({'check': 'actual-proposal-and-patch-review', 'passed': True, **desktop})
        if verification:
            click('#qa-verify')
            wait('document.querySelector("#qa-repair-status").textContent.includes("Verified")')
            require(evaluate('document.querySelector("#qa-repair-list .qa-result:last-child").textContent.includes("Candidate: 3 passed runs")'),
                    'candidate-evidence-not-visible')
            click('#qa-repair-list .qa-result:last-child button')
            wait('!document.querySelector("#qa-repair-review").hidden')
            require(evaluate('document.querySelector("#qa-repair-patch").textContent.includes("+    if ready")'),
                    'verified-patch-not-reviewable')
            checks.append({'check': 'protected-protocol-verification-through-browser', 'passed': True,
                           'candidateRuns': 3, 'actualVM': False, 'actualMobile': False})
        evaluate('document.querySelector("#qa-repair-panel").scrollIntoView({block:"start"});true')
        command('screenshot', str(output / 'repair-desktop.png'))
        evaluate('document.querySelector("#qa-repair-review").scrollIntoView({block:"center"});true')
        command('screenshot', str(output / 'repair-patch-desktop.png'))
        command('set', 'viewport', '390', '844')
        evaluate('document.querySelector("#qa-repair-panel").scrollIntoView({block:"start"});true')
        mobile = readout(); require(not mobile['horizontalOverflow'], 'mobile-horizontal-overflow')
        command('screenshot', str(output / 'repair-mobile.png'))
        checks.append({'check': 'mobile-patch-layout', 'passed': True, **mobile})
        # Delay delivery of an actual authorized response, then navigate away.
        evaluate('(() => {const original=globalThis.fetch;globalThis.__g9restore=()=>{globalThis.fetch=original;delete globalThis.__g9restore;};'
            'globalThis.fetch=async(...args)=>{const response=await original(...args);'
            'if(String(args[0]).endsWith("/proposal"))await new Promise(r=>setTimeout(r,600));return response;};return true;})()')
        click('#qa-repair-list .qa-result:last-child button')
        evaluate('document.querySelector("#qa-project").dispatchEvent(new Event("change",{bubbles:true}));true')
        time.sleep(.8)
        require(evaluate('document.querySelector("#qa-repair-review").hidden && document.querySelector("#qa-repair-patch").textContent===""'),
                'late-proposal-revived-old-project-source')
        evaluate('globalThis.__g9restore();true')
        checks.append({'check': 'late-proposal-after-navigation', 'passed': True})
        select_issue()
        wait('!!document.querySelector("#qa-repair-list .qa-result:last-child button") && !document.querySelector("#qa-repair-list .qa-result:last-child button").disabled')
        click('#qa-repair-list .qa-result:last-child button'); wait('!document.querySelector("#qa-repair-review").hidden')
        revoke()
        wait('document.querySelector("#qa-repair-review").hidden && document.querySelector("#qa-repair-patch").textContent===""')
        checks.append({'check': 'revocation-clears-source-review', 'passed': True})
        return {'kind': 'owned-loopback-protected-browser' if verification else 'owned-loopback-proposal-browser',
                'actualVM': False, 'actualMobile': False,
                'actualAI': False, 'verified': False, 'checks': checks}
    finally:
        subprocess.run([*prefix, 'close'], capture_output=True, timeout=25)
