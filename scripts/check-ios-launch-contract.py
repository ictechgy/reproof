#!/usr/bin/env python3
"""Execute the actual Live helper's pure launch parser on synthetic host-only vectors.

No XCTest, Simulator, app, device, credentials, or network is involved. Requires
Apple's preinstalled xcrun/Swift (Foundation and CryptoKit), as used by compile CI.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def vectors():
    base = {
        "schemaVersion": 1, "kind": "reproof-approved-ios-launch",
        "referenceId": "qa_launch", "profileDigest": "a" * 64,
        "artifactDigest": "b" * 64,
        "environment": [
            {"key": "QA_FIXTURE_ID", "type": "string", "value": "case_1"},
            {"key": "QA_FIXTURE_VERSION", "type": "integer", "value": 2147483647},
            {"key": "QA_MODE", "type": "string", "value": "qa"},
            {"key": "QA_SERVER", "type": "string", "value": "https://qa.example.test/api/v1/"},
        ],
    }
    cases = []

    def add(document=None, *, accepted=False, simulator=True, raw=None, overrides=None):
        document = copy.deepcopy(base if document is None else document)
        text = canonical(document) if raw is None else raw
        environment = {
            "REPRO_LIVE_GENERAL_PROFILE_DIGEST": "a" * 64,
            "REPRO_LIVE_APPLICATION_ID": "qa_app",
            "REPRO_LIVE_GENERAL_ACTIONS": "tap,launch,terminate",
            "REPRO_LIVE_APPROVED_LAUNCH": text,
            "REPRO_LIVE_APPROVED_LAUNCH_DIGEST": hashlib.sha256(text.encode()).hexdigest(),
            "REPRO_LIVE_APPROVED_LAUNCH_REFERENCE_ID": "qa_launch",
            "REPRO_LIVE_APPROVED_LAUNCH_ARTIFACT_DIGEST": "b" * 64,
        }
        for key, value in (overrides or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        expected = {entry["key"]: str(entry["value"]) for entry in document["environment"]} if accepted else None
        cases.append({"environment": environment, "simulator": simulator,
                      "accepted": accepted, "expected": expected})

    add(accepted=True)
    for key, value in [("QA_MODE", "mock"), ("QA_SERVER", "https://a-b.test"),
                       ("QA_SERVER", "https://a.test/"), ("QA_FIXTURE_VERSION", 1)]:
        doc = copy.deepcopy(base)
        next(entry for entry in doc["environment"] if entry["key"] == key)["value"] = value
        add(doc, accepted=True)
    doc = copy.deepcopy(base)
    doc["environment"] = [{"key": "QA_MODE", "type": "string", "value": "qa"}]
    add(doc, accepted=True)
    for field, value in [("schemaVersion", True), ("schemaVersion", 1.0),
                         ("schemaVersion", 2), ("kind", "other"),
                         ("profileDigest", "c" * 64), ("artifactDigest", "c" * 64),
                         ("referenceId", "another_reference")]:
        doc = copy.deepcopy(base); doc[field] = value; add(doc)
    for key in ["REPRO_LIVE_APPROVED_LAUNCH", "REPRO_LIVE_APPROVED_LAUNCH_DIGEST",
                "REPRO_LIVE_APPROVED_LAUNCH_REFERENCE_ID", "REPRO_LIVE_APPROVED_LAUNCH_ARTIFACT_DIGEST",
                "REPRO_LIVE_GENERAL_PROFILE_DIGEST", "REPRO_LIVE_GENERAL_ACTIONS"]:
        add(overrides={key: None})
    add(overrides={"REPRO_LIVE_APPROVED_LAUNCH_DIGEST": "0" * 64})
    add(simulator=False)
    add(raw=canonical(base) + "\n")
    add(raw=canonical(base).replace('"schemaVersion":1', '"schemaVersion":1,"schemaVersion":1'))
    add(raw=canonical(base).replace("https://", "https:\\/\\/"))
    for value in [True, 1.0, 0, -1, 2147483648, 18446744073709551617, "1"]:
        doc = copy.deepcopy(base); doc["environment"][1]["value"] = value; add(doc)
    for value in ["http://qa.test", "https://User@qa.test", "https://qa.test:443",
                  "https://QA.test", "https://127.0.0.1", "https://qa.test?a=1",
                  "https://qa.test#x", "https://qa.test/%2e", "https://qa.test/a//b",
                  "https://qa.test/../a", "https://qa.test/./a", "https://qa.test/\\x",
                  "https://qa.test\n", "https://qa.test/" + "a" * 500]:
        doc = copy.deepcopy(base); doc["environment"][3]["value"] = value; add(doc)
    for key, value in [("QA_MODE", "production"), ("QA_FIXTURE_ID", "a\n"),
                       ("QA_FIXTURE_ID", "../path")]:
        doc = copy.deepcopy(base)
        next(entry for entry in doc["environment"] if entry["key"] == key)["value"] = value
        add(doc)
    doc = copy.deepcopy(base); doc["environment"][0]["key"] = "DYLD_INSERT_LIBRARIES"; add(doc)
    doc = copy.deepcopy(base); doc["environment"][1]["type"] = "string"; add(doc)
    doc = copy.deepcopy(base); doc["environment"].reverse(); add(doc)
    doc = copy.deepcopy(base); doc["environment"][1] = doc["environment"][0]; add(doc)
    doc = copy.deepcopy(base); del doc["environment"][1]; add(doc)
    doc = copy.deepcopy(base); doc["extra"] = True; add(doc)
    doc = copy.deepcopy(base); doc["environment"][0]["extra"] = True; add(doc)
    cases.append({"environment": {}, "simulator": False, "accepted": True, "expected": None})
    return cases


def swift_source():
    source = (ROOT / "live-ios/Tests/LiveControlTests.swift").read_text()
    actions = next(line for line in source.splitlines() if line.startswith("private let generalLiveActions ="))
    general = source.split("private struct GeneralRuntimeConfiguration {", 1)[1].split("private struct AnyCodingKey", 1)[0]
    scanner = source.split("private struct StrictJSONKeyScanner {", 1)[1].split("// BEGIN APPROVED IOS LAUNCH PURE CONTRACT", 1)[0]
    parser = source.split("// BEGIN APPROVED IOS LAUNCH PURE CONTRACT", 1)[1].split("// END APPROVED IOS LAUNCH PURE CONTRACT", 1)[0]
    launch = source[source.index("    private func launchTarget("):source.index("    private func execute(_ command:")]
    launch_probe = r'''
private final class SyntheticTarget {
    var launchEnvironment: [String: String] = [:]
    var launches = 0
    func launch() { launches += 1 }
}
private final class LaunchProbe {
    let targetApplication = SyntheticTarget()
    let generalRuntime: GeneralRuntimeConfiguration? = GeneralRuntimeConfiguration(
        profileDigest: String(repeating: "a", count: 64), applicationID: "qa_app", actions: ["launch"])
    let approvedLaunch: ApprovedLaunchConfiguration?
    var activeAutoRunID: String?
    var activeAutoProfileDigest: String?
    var activeSanitationPolicyDigest: String?
    init(_ approved: ApprovedLaunchConfiguration?) { approvedLaunch = approved }
    func XCTFail(_ message: String) { fatalError("launch_probe_rejected") }
    func runLaunch() { launchTarget() }
''' + launch + r'''
}
do {
    let approved = try approvedLaunchConfiguration(
        cases[0]["environment"] as! [String: String], simulator: true)!
    for selected in [approved, nil] as [ApprovedLaunchConfiguration?] {
        let probe = LaunchProbe(selected)
        let expected = selected?.environment ?? [:]
        probe.runLaunch()
        guard probe.targetApplication.launchEnvironment == expected else {
            fatalError("initial_launch_environment")
        }
        // Simulate stale target state between launches. Ambient QA variables are
        // separately set by the harness process; neither may leak into launch.
        probe.targetApplication.launchEnvironment["QA_SERVER"] = "https://stale.example.test"
        probe.targetApplication.launchEnvironment["UNAPPROVED_TEST_SETTING"] = "stale"
        probe.runLaunch()
        guard probe.targetApplication.launchEnvironment == expected,
              probe.targetApplication.launches == 2 else {
            fatalError("repeated_launch_environment")
        }
    }
}
print("iOS approved launch application: 4 synthetic launches passed")
'''
    return """import Foundation
import CoreFoundation
import CryptoKit
""" + actions + "\nprivate struct GeneralRuntimeConfiguration {" + general + "private struct StrictJSONKeyScanner {" + scanner + parser + r'''
let bytes = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
let cases = try JSONSerialization.jsonObject(with: bytes) as! [[String: Any]]
precondition(!cases.isEmpty)
for (index, vector) in cases.enumerated() {
    let expectedAccepted = vector["accepted"] as! Bool
    let environment = vector["environment"] as! [String: String]
    var actual: ApprovedLaunchConfiguration?
    var accepted = false
    do {
        actual = try approvedLaunchConfiguration(environment, simulator: vector["simulator"] as! Bool)
        accepted = true
    } catch {}
    guard accepted == expectedAccepted else { fatalError("launch_contract_vector_\(index)_acceptance") }
    if accepted {
        let expected = vector["expected"] as? [String: String]
        guard actual?.environment == expected else { fatalError("launch_contract_vector_\(index)_values") }
        if let actual {
            guard actual.digest == environment["REPRO_LIVE_APPROVED_LAUNCH_DIGEST"],
                  actual.handshake.version == 1, actual.handshake.digest == actual.digest else {
                fatalError("launch_contract_vector_\(index)_handshake")
            }
        }
    }
}
print("iOS approved launch contract: \(cases.count) synthetic vectors passed")
''' + launch_probe


def main():
    with tempfile.TemporaryDirectory(prefix="reproof-ios-launch-contract-") as temporary:
        path = Path(temporary)
        (path / "contract.swift").write_text(swift_source())
        (path / "vectors.json").write_text(canonical(vectors()))
        # Keep the host toolchain environment but exclude accidental application
        # configuration. Deliberately hostile non-loader ambient values exercise
        # the actual launchTarget assignment, without loading/invoking any app.
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("REPRO_", "QA_"))}
        environment.update(QA_SERVER="https://ambient.example.test", QA_MODE="production",
                           UNAPPROVED_TEST_SETTING="ambient")
        subprocess.run(["/usr/bin/xcrun", "swift", str(path / "contract.swift"),
                        str(path / "vectors.json")], check=True, timeout=120, env=environment)


if __name__ == "__main__":
    main()
