"""Actual agent-browser UI and H.264 decoding checks for the owned G7 gate."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import subprocess
import time
import uuid


class BrowserFailure(RuntimeError): pass


def require(condition, code):
    if not condition: raise BrowserFailure(code)


def browser_check(origin, credential, original_id, imported_id, view, output, *, revoke):
    executable = shutil.which("agent-browser")
    require(executable is not None, "installed_agent_browser_unavailable")
    session = "codex-g7-" + uuid.uuid4().hex[:12]
    output = Path(output); checks = []; phase = "open"
    prefix = [executable, "--session", session, "--allowed-domains", "127.0.0.1,localhost", "--json"]

    def command(*args, stdin=None):
        process = subprocess.run([*prefix, *args], input=stdin, text=True, capture_output=True, timeout=25)
        # Never forward raw browser/CLI output, which could include credentials
        # supplied to the sign-in control. Only selected public readouts leave.
        require(process.returncode == 0, "agent_browser_command_failed")
        value = json.loads(process.stdout)
        require(value.get("success") is True, "agent_browser_operation_failed")
        return value.get("data")

    def evaluate(script):
        return command("eval", "--stdin", stdin=script)["result"]

    def wait_for(script, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = evaluate(script)
            if value: return value
            time.sleep(.15)
        raise BrowserFailure("browser_state_timeout")

    def click(selector):
        evaluate(f"document.querySelector({json.dumps(selector)}).scrollIntoView({{block:'center'}}); true")
        command("click", selector)
        command("snapshot", "-i")

    def select_issue(identifier):
        short = identifier[:12]
        selector = evaluate("(() => { const buttons = [...document.querySelectorAll('#qa-library button')];"
            f"const index = buttons.findIndex(b => b.textContent.includes({json.dumps(short)}));"
            "return index < 0 ? null : '#qa-library button:nth-child(' + (index+1) + ')'; })()")
        require(selector, "issue_library_item_missing")
        click(selector)
        wait_for(f"document.querySelector('#qa-issue-title').textContent.includes({json.dumps(short)}) && !document.querySelector('#qa-seek').disabled")

    def readout():
        return evaluate("(() => { const v=document.querySelector('#qa-video'); return {hidden:v.hidden, width:v.videoWidth,height:v.videoHeight,"
            "readyState:v.readyState,currentTime:v.currentTime,paused:v.paused,hasSource:!!v.getAttribute('src'),error:v.error?.code||null,"
            "position:document.querySelector('#qa-position').textContent,timing:document.querySelector('#qa-timing').textContent,"
            "title:document.querySelector('#qa-issue-title').textContent,status:document.querySelector('#qa-state').textContent,"
            "device:document.querySelector('#qa-device').selectedOptions[0]?.textContent,deviceNote:document.querySelector('#qa-device-note').textContent,"
            "replayDisabled:document.querySelector('#qa-replay').disabled,approveDisabled:document.querySelector('#qa-approve').disabled,"
            "notice:document.querySelector('#qa-notice').textContent,results:document.querySelector('#qa-results-detail').textContent.slice(0,131072)}; })()")

    def seek(position):
        evaluate(f"(() => {{ const input=document.querySelector('#qa-seek');input.value={json.dumps(str(position))};"
            "input.dispatchEvent(new Event('input',{bubbles:true}));return true; })()")

    video = view["video"]
    segments = video["segments"]
    def frame_position(segment):
        low = segment["displayInterval"]["startOffsetMs"]
        times = [low + f["presentationTimeMs"] for f in segment["frames"]]
        for start, end in zip(times, times[1:]):
            point = math.ceil((start + end) / 2)
            if start < point < end and not any(loss["recordingInterval"]["startOffsetMs"] <= point <= loss["recordingInterval"]["endOffsetMs"] for loss in video["losses"]):
                return point
        raise BrowserFailure("playable_segment_position_missing")

    def gap_position():
        for first, second in zip(segments, segments[1:]):
            start, end = first["displayInterval"]["endOffsetMs"], second["displayInterval"]["startOffsetMs"]
            if end - start > 3: return math.ceil((start + end) / 2)
        for loss in video["losses"]:
            interval = loss["recordingInterval"]
            if interval["endOffsetMs"] - interval["startOffsetMs"] > 3:
                return math.ceil((interval["startOffsetMs"] + interval["endOffsetMs"]) / 2)
        raise BrowserFailure("recorded_gap_position_missing")

    try:
        command("open", origin)
        command("set", "viewport", "1440", "1000")
        wait_for("!!document.querySelector('#qa-credential')")
        # Fresh owned credential travels via stdin, never a process argument,
        # URL, screenshot, browser log or retained browser-state file.
        evaluate("(() => {const input=document.querySelector('#qa-credential');input.value=" + json.dumps(credential)
            + ";document.querySelector('#qa-login-form').requestSubmit();return true;})()")
        wait_for("!document.querySelector('#qa-workspace').hidden && document.querySelectorAll('#qa-library button').length >= 2")
        require(evaluate("document.querySelector('#qa-credential').value.length === 0"), "credential_control_not_cleared")
        checks.append({"check": "personal-login", "passed": True})
        select_issue(imported_id)

        phase = "portrait-decode"
        point = frame_position(segments[0]); seek(point)
        wait_for("(() => {const v=document.querySelector('#qa-video');return !v.hidden && v.readyState>=2;})()")
        portrait = readout()
        require([portrait["width"], portrait["height"]] == [segments[0]["width"], segments[0]["height"]]
                and portrait["error"] is None and "Unknown native acquisition" in portrait["timing"], "portrait_decode_or_timing_changed")
        checks.append({"check": "portrait-decode", "passed": True, **portrait})
        command("screenshot", str(output / "browser-portrait.png"))

        phase = "actual-playback"
        click("#qa-play")
        current = portrait["currentTime"]
        progressed = wait_for("(() => {const v=document.querySelector('#qa-video');return v.currentTime > " + str(current + .02) + ";})()")
        click("#qa-pause")
        playback = readout()
        require(progressed and playback["error"] is None, "mp4_playback_did_not_advance")
        checks.append({"check": "actual-playback", "passed": True, **playback})

        phase = "action-seek"
        click("#qa-actions button")
        wait_for("!document.querySelector('#qa-timing').textContent.startsWith('Loading')")
        action = readout()
        event = view["recording"]["original"]["events"][0]
        mapped = next((item["recordingOffsetMs"] for item in video["eventMappings"] if item["eventId"] == event["id"]), event["offsetMs"])
        require(abs(float(action["position"].split()[0]) - mapped) <= 1, "action_seek_time_changed")
        checks.append({"check": "action-seek", "passed": True, **action})

        phase = "gap-seek"
        gap = gap_position(); seek(gap)
        gap_view = wait_for("(() => {const v=document.querySelector('#qa-video');return v.hidden && !v.getAttribute('src') && document.querySelector('#qa-timing').textContent.startsWith('No frame');})()")
        require(gap_view, "gap_kept_old_picture")
        checks.append({"check": "gap-seek", "passed": True, "requestedMs": gap, **readout()})
        command("screenshot", str(output / "browser-gap.png"))

        phase = "rotation-decode"
        seek(frame_position(segments[-1]))
        wait_for("(() => {const v=document.querySelector('#qa-video');return !v.hidden && v.readyState>=2;})()")
        rotated = readout()
        require([rotated["width"], rotated["height"]] == [segments[-1]["width"], segments[-1]["height"]]
                and [rotated["width"], rotated["height"]] != [portrait["width"], portrait["height"]], "rotation_decode_changed")
        checks.append({"check": "rotation-decode", "passed": True, **rotated})
        command("screenshot", str(output / "browser-landscape.png"))

        phase = "late-fetch-navigation"
        # Delay delivery of a real authenticated response, then navigate through
        # the normal project control. No media response is fabricated.
        evaluate("(() => {const original=globalThis.fetch;globalThis.__g7restoreFetch=()=>{globalThis.fetch=original;delete globalThis.__g7restoreFetch;};"
            "let once=true;globalThis.fetch=async(...args)=>{const response=await original(...args);"
            "if(once && String(args[0]).includes('/media/')){once=false;await new Promise(r=>setTimeout(r,600));}return response;};return true;})()")
        seek(point)
        evaluate("document.querySelector('#qa-project').dispatchEvent(new Event('change',{bubbles:true}));true")
        time.sleep(.9)
        stale = readout()
        require(stale["hidden"] and not stale["hasSource"] and stale["title"] == "Choose an issue or start recording", "late_fetch_restored_old_issue")
        evaluate("globalThis.__g7restoreFetch();true")
        checks.append({"check": "late-fetch-navigation", "passed": True})

        phase = "late-decode-gap"
        select_issue(original_id)
        seek(point); seek(gap)
        time.sleep(.4)
        late = readout()
        require(late["hidden"] and not late["hasSource"] and late["timing"].startswith("No frame"), "late_decode_restored_gap_picture")
        checks.append({"check": "late-decode-gap", "passed": True})

        phase = "ui-prepare-record-stop"
        click("#qa-start")
        wait_for("document.querySelector('#qa-state').textContent === 'recording' && !document.querySelector('#qa-send-input').disabled")
        click("#qa-send-input")
        wait_for("document.querySelector('#qa-notice').textContent.includes('Input injected')")
        click("#qa-stop")
        wait_for("['failed','complete'].includes(document.querySelector('#qa-state').textContent) && !document.querySelector('#qa-spec-form').hidden")
        facts = evaluate("JSON.parse(document.querySelector('#qa-facts').textContent)")
        require(len(facts["recording"]["original"]["events"]) == 1
                and facts["lifecycle"]["deviceCleanup"] == "complete"
                and all(item["status"] == "complete" for item in facts["lifecycle"]["cleanup"]), "ui_recording_or_cleanup_changed")
        ui_original_digest = facts["recording"]["recordingDigest"]
        require(bool(facts['recording']['original']['events'][0]['input']['geometry'].get('frameDigest')), 'original_frame_binding_missing')
        checks.append({"check": "ui-prepare-record-stop", "passed": True, "recordingDigest": ui_original_digest})

        phase = "ui-author-approve-replay"
        command("select", '#qa-edit-actions [name="frameMatch"]', "geometry")
        for index, expected in ((1, "2"), (2, "1")):
            base = f"#qa-assertions > fieldset:nth-child({index}) "
            command("fill", base + '[name="property"]', "count")
            command("select", base + '[name="valueType"]', "number")
            for name, value in (("value", expected), ("window", "0"), ("end", "0"), ("uncertainty", "1000"), ("age", "2000")):
                command("fill", base + f'[name="{name}"]', value)
        require(evaluate("document.querySelector('#qa-approve').disabled && document.querySelector('#qa-replay').disabled"), "dirty_specification_remained_approved")
        click("#qa-save")
        wait_for("document.querySelector('#qa-revision').textContent.startsWith('Revision 1') && !document.querySelector('#qa-approve').disabled")
        exact = evaluate("(async()=>{const bytes=document.querySelector('#qa-spec-bytes').textContent;const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(bytes));"
            "return {digest:[...new Uint8Array(hash)].map(b=>b.toString(16).padStart(2,'0')).join(''),label:document.querySelector('#qa-revision').textContent};})()")
        require(exact["digest"] in exact["label"], "displayed_approval_bytes_do_not_match_digest")
        click("#qa-approve")
        wait_for("document.querySelector('#qa-approval-note').textContent.includes('has local approval')")
        phase = "ui-device-availability-after-cleanup"
        wait_for("!document.querySelector('#qa-replay').disabled", timeout=10)
        click("#qa-replay")
        phase = "ui-replay-completion"
        wait_for("!document.querySelector('#qa-results-panel').hidden && ['reproduced','unknown','failed','quarantined'].includes(document.querySelector('#qa-state').textContent)", timeout=30)
        require(evaluate("document.querySelector('#qa-state').textContent === 'reproduced' && document.querySelector('#qa-results').textContent.includes('3 recorded attempts')"), "ui_approved_replay_failed")
        ui_result = evaluate("({recording:JSON.parse(document.querySelector('#qa-facts').textContent).recording,"
            "campaign:JSON.parse(document.querySelector('#qa-results-detail').textContent)})")
        require(ui_result["recording"]["recordingDigest"] == ui_original_digest, "ui_authoring_changed_original")
        checks.append({"check": "ui-author-approve-replay", "passed": True, "specificationDigest": exact["digest"],
                       "recordingDigest": ui_original_digest, "attempts": 3})
        command("screenshot", str(output / "browser-ui-replay.png"))
        select_issue(original_id)

        phase = "narrow-layout"
        command("set", "viewport", "390", "844")
        layout = evaluate("({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,"
            "labels:!!document.querySelector('label[for]')||document.querySelectorAll('label input,label select').length>10})")
        require(layout["scrollWidth"] <= layout["width"] + 1 and layout["labels"], "narrow_layout_overflow")
        command("screenshot", str(output / "browser-narrow.png"))
        evaluate("document.querySelector('.qa-author').scrollIntoView({block:'start'});true")
        command("screenshot", str(output / "browser-narrow-authoring.png"))
        checks.append({"check": "narrow-layout", "passed": True, **layout})

        phase = "revocation"
        command("set", "viewport", "1440", "1000")
        seek(point)
        wait_for("!document.querySelector('#qa-video').hidden")
        revoke()
        wait_for("document.querySelector('#qa-video').hidden && !document.querySelector('#qa-video').getAttribute('src')")
        denied = evaluate("(async()=>{const r=await fetch(" + json.dumps("/api/release/issues/" + original_id + "/media/" + segments[0]["digest"])
            + ",{headers:{Range:'bytes=0-9'}});const status=r.status;await r.body?.cancel();return status;})()")
        require(denied in (401, 403, 404, 410), "revocation_allowed_new_media")
        checks.append({"check": "revocation", "passed": True, "mediaStatus": denied, **readout()})
        command("screenshot", str(output / "browser-revoked.png"))
        return {"passed": True, "engine": "installed-agent-browser", "checks": checks}
    except Exception as error:
        try: command("screenshot", str(output / "browser-failure.png"))
        except Exception: pass
        catalog = None
        if phase != "open":
            try:
                catalog = evaluate("(async()=>{const r=await fetch('/api/release/projects');if(!r.ok)return {status:r.status};"
                    "const body=await r.json();return {projects:body.projects.map(p=>({id:p.id,devices:p.devices.map(d=>({id:d.id,state:d.state}))}))};})()")
            except Exception: pass
        return {"passed": False, "phase": phase, "reason": str(error) if isinstance(error, BrowserFailure) else type(error).__name__,
                "checks": checks, "readout": readout() if phase != "open" else None, "currentCatalog": catalog}
    finally:
        try: command("close")
        except Exception: pass
