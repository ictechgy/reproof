"""Owned Chromium UI smoke using actual authenticated loopback ticket routes."""
import json
from pathlib import Path
import shutil


def browser_check(origin, credential, first, second, output, *, inspect, revise, expire, revoke):
    executable = shutil.which('chromium') or shutil.which('chromium-browser')
    if not executable: raise RuntimeError('An already installed Chromium is required; no download is attempted')
    try: from playwright.sync_api import sync_playwright
    except ImportError: raise RuntimeError('Already installed Python Playwright is required; no install is attempted') from None
    checks = []; output = Path(output)
    def check(name, condition=True):
        if not condition: raise AssertionError(name)
        checks.append({'check': name, 'passed': True})
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
        # Constrain every browser request, including accidental tracker URLs.
        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin + '/') else route.abort())
        page = context.new_page(); errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_default_timeout(10000)
        def wait(expression): page.wait_for_function(expression)
        def select(issue, *, ready=True):
            page.locator('#qa-library button').filter(has_text=issue[:12]).click()
            wait('document.querySelector("#qa-issue-title").textContent.includes(' + json.dumps(issue[:12]) + ') && !document.querySelector("#qa-ticket-panel").hidden')
            if ready: wait('!document.querySelector("#qa-ticket-refresh").disabled')
        def login():
            page.locator('#qa-credential').fill(credential)
            page.locator('#qa-login-form button').click()
            wait('!document.querySelector("#qa-workspace").hidden')
        def export():
            with page.expect_download(): page.locator('#qa-export').click()
            wait('document.querySelector("#qa-ticket-candidate").textContent.includes("Exported package")')
            return page.locator('#qa-ticket-candidate').inner_text().split(' · ')[0].removeprefix('Exported package ')
        def delay(suffix, method='GET', fail=False, lose_response=False):
            # Deliver an actual server response late; deliberately ignore the
            # abort during delivery to prove generation guards independently.
            page.evaluate('''({suffix,method,fail,lose_response}) => {
              const original=globalThis.fetch; globalThis.__ticketReady=false;
              globalThis.__ticketRestore=()=>{globalThis.fetch=original;};
              globalThis.fetch=async(path,options={})=>{
                if(String(path).endsWith(suffix) && (options.method || 'GET')===method){
                  if(fail){globalThis.fetch=original;throw new TypeError('owned synthetic network failure');}
                  const response=await original(path,options); globalThis.fetch=original;
                  if(lose_response) throw new TypeError('owned response loss after commit');
                  await new Promise(resolve=>{globalThis.__ticketRelease=resolve;globalThis.__ticketReady=true;});
                  return response;
                }
                return original(path,options);
              };
            }''', {'suffix': suffix, 'method': method, 'fail': fail, 'lose_response': lose_response})
        def release():
            page.evaluate('globalThis.__ticketRestore(); globalThis.__ticketRelease();')
            page.wait_for_timeout(150)
        try:
            page.goto(origin); login(); select(first)
            check('no-implicit-export-or-approval', inspect()['packages'] == 2 and inspect()['unchanged'])
            check('link-disabled-without-explicit-export', page.locator('#qa-ticket-link').is_disabled())
            selected = export(); page.locator('#qa-ticket-id').fill('QA-42')
            delay('/ticket-references', 'POST')
            # Two programmatic click events in one turn exercise serialization.
            page.evaluate('document.querySelector("#qa-ticket-link").click(); document.querySelector("#qa-ticket-link").click();')
            wait('globalThis.__ticketReady'); check('pending-link-serialized', len(inspect()['references']) == 1)
            release(); wait('document.querySelector("#qa-ticket-list").textContent.includes("QA-42")')
            page.locator('#qa-ticket-link').click(); wait('!document.querySelector("#qa-ticket-link").disabled')
            check('exact-selected-package-and-idempotent-repeat', len(inspect()['references']) == 1 and inspect()['references'][0]['packageId'] == selected and inspect()['packages'] == 3)
            check('link-leaves-approvals-and-execution-unchanged', inspect()['unchanged'] and inspect()['noExecution'])
            check('explicit-reference-only-status', 'Matches current revision' in page.locator('#qa-ticket-list').inner_text() and page.locator('#qa-replay').is_disabled())
            page.locator('#qa-ticket-panel').scroll_into_view_if_needed(); page.screenshot(path=str(output/'ticket-desktop.png'))
            page.set_viewport_size({'width': 390, 'height': 844}); page.locator('#qa-ticket-panel').scroll_into_view_if_needed()
            check('mobile-layout-no-horizontal-overflow', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.screenshot(path=str(output/'ticket-mobile.png'))
            page.set_viewport_size({'width': 1440, 'height': 1000})
            delay('/ticket-references'); page.locator('#qa-ticket-refresh').click(); wait('globalThis.__ticketReady')
            select(second, ready=False); release(); wait('!document.querySelector("#qa-ticket-refresh").disabled')
            check('late-read-after-issue-navigation-is-discarded', page.locator('#qa-ticket-list').inner_text() == '' and 'Exported package' not in page.locator('#qa-ticket-candidate').inner_text())
            select(first); export(); page.locator('#qa-ticket-id').fill('QA-RECOVER')
            delay('/ticket-references', 'POST', lose_response=True); page.locator('#qa-ticket-link').click()
            wait('document.querySelector("#qa-notice").textContent.includes("response loss")')
            check('uncertain-link-clears-candidate', 'Exported package' not in page.locator('#qa-ticket-candidate').inner_text())
            page.locator('#qa-ticket-refresh').click(); wait('document.querySelector("#qa-ticket-list").textContent.includes("QA-RECOVER")')
            check('refresh-recovers-committed-link-without-candidate', 'Exported package' not in page.locator('#qa-ticket-candidate').inner_text())
            export(); page.locator('#qa-ticket-id').fill('QA-LATE')
            delay('/ticket-references', 'POST'); page.locator('#qa-ticket-link').click(); wait('globalThis.__ticketReady')
            page.locator('#qa-logout').click(); wait('document.querySelector("#qa-workspace").hidden'); release()
            check('late-link-after-logout-cannot-resurrect-private-state', page.locator('#qa-ticket-list').inner_text() == '' and page.locator('#qa-ticket-id').input_value() == '')
            login(); select(first)
            delay('/api/auth/logout', 'POST', fail=True); page.locator('#qa-logout').click()
            wait('document.querySelector("#qa-notice").textContent.includes("could not be confirmed")')
            page.locator('#qa-logout').click(); wait('document.querySelector("#qa-logout").hidden && !document.querySelector("#qa-login-form button").disabled')
            check('failed-logout-retry-uses-existing-csrf', page.evaluate('async () => (await fetch("/api/release/projects")).status') == 401)
            login(); select(first); selected = export(); page.locator('#qa-ticket-id').fill('QA-EXPIRY'); page.locator('#qa-ticket-link').click()
            wait('document.querySelector("#qa-ticket-list").textContent.includes("QA-EXPIRY")')
            expire(selected); page.locator('#qa-ticket-refresh').click()
            wait('document.querySelector("#qa-ticket-status").textContent.includes("unavailable") || document.querySelector("#qa-ticket-status").textContent.includes("retention changed")')
            check('expired-package-metadata-is-omitted-and-candidate-cleared', 'QA-EXPIRY' not in page.locator('#qa-ticket-list').inner_text() and 'Exported package' not in page.locator('#qa-ticket-candidate').inner_text())
            export(); revise()
            wait('document.querySelector("#qa-revision").textContent.startsWith("Revision 2") && !document.querySelector("#qa-ticket-refresh").disabled')
            check('saved-revision-clears-candidate-and-displays-stale-reference', 'Exported package' not in page.locator('#qa-ticket-candidate').inner_text() and 'Stale' in page.locator('#qa-ticket-list').inner_text())
            export(); revoke(); wait('document.querySelector("#qa-ticket-panel").hidden')
            check('capability-revocation-clears-private-metadata', page.locator('#qa-ticket-list').inner_text() == '' and 'Exported package' not in page.locator('#qa-ticket-candidate').inner_text())
            check('no-browser-javascript-errors', not errors)
            report = {'kind': 'owned-loopback-chromium', 'actualDevice': False, 'externalTracker': False, 'checks': checks}
            (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
            return report
        finally:
            context.close(); browser.close()
