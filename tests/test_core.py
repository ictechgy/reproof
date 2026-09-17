import copy
import unittest
from reproloop.core import ContractError, compile_capture, classify_runs


def capture():
    return {"schemaVersion": 1, "sessionId": "qa-test", "fixture": {"id": "default", "version": 1, "inputs": {}},
            "startState": {"screen": "main", "nodes": {"count": "0", "name": ""}},
            "events": [{"id": "e1", "seq": 1, "action": "replace", "target": "name", "parameters": {"value": "QA"}},
                       {"id": "e2", "seq": 2, "action": "tap", "target": "add", "parameters": {}}],
            "truncated": False, "lostEvents": False, "endSequence": 2}


def oracle():
    return {"bugCondition": {"target": "count", "text": "2"},
            "expectedCondition": {"target": "count", "text": "1"},
            "actual": "One add increments count twice", "expected": "One add increments count once"}


def run(kind):
    return {"runValid": True, "bugCondition": kind == "bug", "expectedCondition": kind == "expected",
            "evidenceValid": True, "protectedPathsValid": True, "regressionPassed": True}


class CompilerTests(unittest.TestCase):
    def test_preserves_actions_and_coverage(self):
        result = compile_capture(capture(), oracle())
        self.assertEqual([s["sourceEventIds"] for s in result["steps"]], [["e1"], ["e2"]])
        self.assertEqual([s["action"] for s in result["steps"]], ["replace", "tap"])
        self.assertEqual(result["startState"]["nodes"]["count"], "0")

    def test_does_not_modify_source(self):
        original = capture(); before = copy.deepcopy(original)
        compile_capture(original, oracle())
        self.assertEqual(original, before)

    def test_rejects_incomplete_or_reordered_recordings(self):
        cases = []
        for field in ["truncated", "lostEvents"]:
            c = capture(); c[field] = True; cases.append(c)
        c = capture(); c["events"][1]["seq"] = 3; cases.append(c)
        c = capture(); c["endSequence"] = 1; cases.append(c)
        c = capture(); c["events"][1]["id"] = "e1"; cases.append(c)
        c = capture(); c.pop("startState"); cases.append(c)
        for c in cases:
            with self.subTest(c=c), self.assertRaises(ContractError): compile_capture(c, oracle())

    def test_rejects_invented_operations_and_unsafe_text(self):
        for action, target, parameters in [("shell", "add", {"value": "whoami"}),
                ("tap", "../../file", {}), ("replace", "password", {"value": "secret"}),
                ("replace", "name", {"value": "user@example.com"}),
                ("replace", "name", {"valueRef": "unknown"}),
                ("scroll_to", "bottom", {"direction": "forward"})]:
            c = capture(); c["events"][0].update(action=action, target=target, parameters=parameters)
            with self.subTest(action=action, target=target), self.assertRaises(ContractError): compile_capture(c, oracle())

    def test_unrecognized_metadata_cannot_bypass_privacy_checks(self):
        for mutate in [lambda c:c.update(unrecognized="not allowed"),
                       lambda c:c.update(startedAtMs={"secret":"not allowed"}),
                       lambda c:c["events"][0]["parameters"].update(value=["not allowed"])]:
            c=capture();mutate(c)
            with self.assertRaises(ContractError):compile_capture(c,oracle())

    def test_distinct_oracles_required(self):
        o = oracle(); o["expectedCondition"] = o["bugCondition"].copy()
        with self.assertRaises(ContractError): compile_capture(capture(), o)


class RepeatPolicyTests(unittest.TestCase):
    def test_original_and_patched_contract(self):
        self.assertEqual(classify_runs([run("bug") for _ in range(3)], "original"), "reproduced")
        self.assertEqual(classify_runs([run("expected") for _ in range(3)], "patched"), "verified")
        self.assertEqual(classify_runs([run("expected") for _ in range(3)], "original"), "not_reproduced")
        self.assertEqual(classify_runs([run("bug") for _ in range(3)], "patched"), "verification_failed")

    def test_mixed_and_short_runs_never_verify(self):
        for runs in [[run("bug"), run("expected"), run("expected")], [run("expected")], []]:
            self.assertEqual(classify_runs(runs, "patched"), "inconclusive")

    def test_invalid_evidence_and_weakened_checks_fail_closed(self):
        for key in ["runValid", "evidenceValid", "protectedPathsValid", "regressionPassed"]:
            runs = [run("expected") for _ in range(3)]; runs[1][key] = False
            self.assertNotEqual(classify_runs(runs, "patched"), "verified")
        runs = [run("expected") for _ in range(3)]; runs[0]["bugCondition"] = True
        self.assertEqual(classify_runs(runs, "patched"), "inconclusive")

    def test_repeat_count_cannot_be_reduced(self):
        with self.assertRaises(ContractError): classify_runs([run("expected")], "patched", repeats=1)

if __name__ == "__main__": unittest.main()
