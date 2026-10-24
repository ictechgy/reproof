"""Local-only causal repair checks: no services, devices, providers or network."""
from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from reproof import contracts
from reproof.execution.artifacts import ArtifactError, BlobSet
from reproof.project_repair import RepairError
from reproof import offline_repair_demo as demo


FIXTURE = Path(__file__).absolute().parents[1] / "examples" / "ticket-repair"


class OfflineRepairDemoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "fixture"
        shutil.copytree(FIXTURE, self.root)

    def test_fixed_candidate_changes_actual_function_output_against_frozen_oracle(self):
        report = demo._run_demo(self.root, "fixed")
        self.assertEqual(report["status"], "local_repair_passed")
        self.assertEqual(report["baseline"]["failed"], 1)
        self.assertEqual(report["candidate"]["passed"], 7)
        baseline = {row["id"]: row for row in report["baseline"]["cases"]}
        changed = {row["id"]: row for row in report["candidate"]["cases"]}
        self.assertEqual(baseline["at_threshold"]["observedTotalCents"], 5500)
        self.assertEqual(changed["at_threshold"]["observedTotalCents"], 5000)
        self.assertNotEqual(report["sourceDigest"], report["candidateDigest"])
        self.assertNotEqual(report["sourceFileDigest"], report["candidateFileDigest"])
        self.assertEqual(report["oracleDigest"], report["candidateOracleDigest"])
        self.assertEqual(report["oracleDigest"], hashlib.sha256((self.root / demo.ORACLE).read_bytes()).hexdigest())

    def test_wrong_patch_is_executed_and_fails_actual_oracle_cases(self):
        report = demo._run_demo(self.root, "wrong")
        self.assertEqual(report["status"], "candidate_failed")
        self.assertEqual(report["candidate"]["failed"], 7)
        first = report["candidate"]["cases"][0]
        self.assertEqual(first["expectedTotalCents"], 500)
        self.assertEqual(first["observedTotalCents"], 0)
        self.assertNotEqual(report["sourceDigest"], report["candidateDigest"])

    def test_noop_rejected_before_candidate_execution(self):
        with patch.object(demo, "_observe", wraps=demo._observe) as observe:
            report = demo._run_demo(self.root, "noop")
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["error"]["code"], "patch_invalid")
        self.assertIsNone(report["candidate"])
        self.assertIsNone(report["candidateDigest"])

    def test_run_does_not_modify_any_fixture_file(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        for candidate in ("fixed", "wrong", "noop"):
            demo._run_demo(self.root, candidate)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})

    def test_no_protected_mobile_ai_or_sandbox_claims(self):
        report = demo._run_demo(self.root, "fixed")
        for key in ("actualAI", "externalAI", "mobile", "VM", "companyAcceptance",
                    "protectedVerification", "sandbox", "networkUsed", "credentialsUsed",
                    "sdkCapture", "scenarioReplay"):
            self.assertIs(report[key], False)
        self.assertIs(report["syntheticEvidence"], True)
        self.assertEqual(report["verificationTier"], "local-software-proof")
        self.assertEqual(report["candidateGenerator"], "scripted-not-ai")
        self.assertEqual(report["execution"], "allowlisted-python-function")

    def test_ticket_draft_is_bound_to_source_and_oracle_but_never_approved(self):
        report = demo._run_demo(self.root, "fixed")
        draft = report["ticketIntake"]
        self.assertEqual(draft["status"], "review-required")
        self.assertEqual(draft["references"]["sourceDigest"], report["sourceDigest"])
        self.assertEqual(draft["references"]["projectDigest"], report["projectDigest"])
        self.assertEqual(draft["references"]["specificationDigest"],
                         draft["demoBinding"]["specificationDigest"])
        self.assertIs(draft["demoBinding"]["syntheticEvidence"], True)
        self.assertEqual(report["draftOracleDigest"], report["oracleDigest"])
        self.assertEqual(report["approval"], "not-granted-demo-only")
        self.assertEqual(draft["executionAuthority"], "none")
        self.assertIs(draft["approved"], False)
        self.assertIs(draft["aiTransfer"], False)

    def test_wrong_source_or_approval_in_ticket_draft_rejects_before_execution(self):
        from reproof.ticket_intake_demo import demo_draft
        source, _, oracle = demo._load_source(self.root)
        original = demo_draft(source.project, oracle.digest)
        variants = []
        value = copy.deepcopy(original); value["references"]["sourceDigest"] = "f" * 64; variants.append(value)
        value = copy.deepcopy(original); value["references"]["projectDigest"] = "f" * 64; variants.append(value)
        value = copy.deepcopy(original); value["demoBinding"]["oracleDigest"] = "f" * 64; variants.append(value)
        value = copy.deepcopy(original); value["demoBinding"]["specificationDigest"] = "f" * 64; variants.append(value)
        value = copy.deepcopy(original); value["demoBinding"]["syntheticEvidence"] = False; variants.append(value)
        value = copy.deepcopy(original); value["approved"] = True; variants.append(value)
        value = copy.deepcopy(original); value["executionAuthority"] = "protected"; variants.append(value)
        value = copy.deepcopy(original); value["status"] = "abstained"; variants.append(value)
        for value in variants:
            with self.subTest(value=value), patch("reproof.ticket_intake_demo.demo_draft", return_value=value):
                with patch.object(demo, "_observe") as observe, self.assertRaises(demo.DemoError) as caught:
                    demo._run_demo(self.root, "fixed")
                self.assertEqual(caught.exception.code, "ticket_draft_binding")
                observe.assert_not_called()

    def test_original_source_digest_is_bound_by_checked_in_project(self):
        project = contracts.validate_project_revision(json.loads((self.root / "project.json").read_text()))
        self.assertEqual(project["builds"][0]["sourceDigest"],
                         BlobSet.from_directory(self.root, demo.SOURCE_PATHS).digest)
        project["builds"][0]["sourceDigest"] = "f" * 64
        (self.root / "project.json").write_text(json.dumps(project))
        with self.assertRaises(RepairError) as caught:
            demo._run_demo(self.root, "fixed")
        self.assertEqual(caught.exception.code, "source_provenance")

    def test_oracle_recipe_and_product_tampering_rejected_before_execution(self):
        for path in demo.SOURCE_PATHS:
            with self.subTest(path=path):
                original = (self.root / path).read_bytes()
                (self.root / path).write_bytes(original + b" ")
                with patch.object(demo, "_observe") as observe, self.assertRaises(RepairError):
                    demo._run_demo(self.root, "fixed")
                observe.assert_not_called()
                (self.root / path).write_bytes(original)

    def test_oracle_recipe_project_and_test_edits_rejected_by_repair_source(self):
        source, frozen, _ = demo._load_source(self.root)
        for path in (demo.ORACLE, demo.RECIPE, "project.json", "tests/test_offline_repair_demo.py",
                     "../checkout.py", "/tmp/checkout.py"):
            with self.subTest(path=path), self.assertRaises(RepairError) as caught:
                source.apply(frozen, [{"path": path, "old": "5000", "new": "5500"}])
            self.assertEqual(caught.exception.code, "protected_path")

    def test_mutating_frozen_oracle_cannot_change_candidate_expectations(self):
        source, frozen, oracle = demo._load_source(self.root)
        expected = tuple(oracle.cases)
        (self.root / demo.ORACLE).write_text('{"cases": []}')
        self.assertEqual(oracle.cases, expected)
        with self.assertRaises(RepairError) as caught:
            source.apply(frozen, [{"path": demo.PRODUCT, "old": "> 5000", "new": ">= 5000"}])
        self.assertEqual(caught.exception.code, "original_changed")

    def test_symlink_product_oracle_recipe_and_project_are_rejected(self):
        for name in (*demo.SOURCE_PATHS, "project.json"):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                other = self.root.parent / "target"
                other.write_bytes(original)
                path.unlink()
                path.symlink_to(other)
                with self.assertRaises((RepairError, ArtifactError)):
                    demo._run_demo(self.root, "fixed")
                path.unlink()
                path.write_bytes(original)

    def test_protected_path_widening_is_not_accepted(self):
        project = json.loads((self.root / "project.json").read_text())
        project["editablePaths"].append(demo.ORACLE)
        (self.root / "project.json").write_text(json.dumps(project))
        with self.assertRaises(demo.DemoError) as caught:
            demo._run_demo(self.root, "fixed")
        self.assertEqual(caught.exception.code, "project_policy")

    def test_expected_cases_are_independent_of_candidate_function(self):
        oracle = demo._freeze_oracle((self.root / demo.ORACLE).read_bytes())
        changed_cases = tuple((name, subtotal, expected + 1) for name, subtotal, expected in oracle.cases)
        altered_oracle = demo._Oracle(oracle.digest, changed_cases)
        fixed = (self.root / demo.PRODUCT).read_bytes().replace(b"> 5000", b">= 5000")
        self.assertEqual(demo._observe(fixed, altered_oracle)["failed"], 7)

    def test_runtime_is_a_real_python_function_not_a_success_lookup(self):
        original = (self.root / demo.PRODUCT).read_bytes()
        fixed = original.replace(b"> 5000", b">= 5000")
        function = demo._CheckoutFunction(fixed)
        self.assertEqual(function._function.__code__.co_name, "checkout_total")
        # Inputs not listed in oracle.json still produce arithmetic results.
        self.assertEqual(function.run(2378), 2878)
        self.assertEqual(function.run(7823), 7823)
        self.assertEqual(demo._CheckoutFunction(original).run(5000), 5500)
        self.assertEqual(function.run(5000), 5000)

    def test_only_exact_comparator_variant_is_executable(self):
        original = (self.root / demo.PRODUCT).read_bytes()
        for comparator in (b">", b">=", b"<"):
            with self.subTest(comparator=comparator):
                demo._CheckoutFunction(original.replace(b"> 5000", comparator + b" 5000"))
        for altered in (original.replace(b"> 5000", b"<= 5000"),
                        original.replace(b"5000", b"5001"),
                        original.replace(b"+ 500", b"+ 501"),
                        original.replace(b"checkout_total", b"different_name"),
                        original.replace(b"subtotal_cents):", b"subtotal_cents=0):")):
            with self.subTest(altered=altered), patch("builtins.exec") as execute:
                with self.assertRaises(demo.DemoError):
                    demo._CheckoutFunction(altered)
                execute.assert_not_called()

    def test_unsafe_python_constructs_rejected_before_exec(self):
        unsafe = [
            b"import os\n", b"from os import system\n", b"print('side effect')\n",
            b"def checkout_total(subtotal_cents):\n    return subtotal_cents.real\n",
            b"def checkout_total(subtotal_cents):\n    return checkout_total(subtotal_cents)\n",
            b"def checkout_total(subtotal_cents):\n    return 2 ** 999999\n",
            b"def checkout_total(subtotal_cents):\n    while True: pass\n",
            b"def checkout_total(subtotal_cents):\n    return [x for x in range(10)]\n",
            b"@print\ndef checkout_total(subtotal_cents):\n    return 1\n",
            b"def checkout_total(subtotal_cents: print('bad')):\n    return 1\n",
            b"def checkout_total(subtotal_cents):\n    return 'x' * 999999\n",
            b"def checkout_total(subtotal_cents):\n    return True\n",
            b"def checkout_total(subtotal_cents):\n    return 9999999999999999999999999999999\n",
            b"def checkout_total(subtotal_cents):\n    global changed\n    changed = 1\n",
            b"def checkout_total(subtotal_cents):\n    return lambda: 1\n",
        ]
        for raw in unsafe:
            with self.subTest(raw=raw), patch("builtins.exec") as execute:
                with self.assertRaises(demo.DemoError):
                    demo._CheckoutFunction(raw)
                execute.assert_not_called()

    def test_source_size_is_checked_before_parsing(self):
        with patch.object(demo.ast, "parse") as parse, self.assertRaises(demo.DemoError) as caught:
            demo._validated_ast(b" " * (demo.MAX_SOURCE_BYTES + 1))
        self.assertEqual(caught.exception.code, "source_limit")
        parse.assert_not_called()

    def test_input_types_bounds_and_static_step_limit(self):
        function = demo._CheckoutFunction((self.root / demo.PRODUCT).read_bytes())
        for subtotal in (-1, demo.MAX_INPUT_CENTS + 1, True, 1.0, "5000", [], None):
            with self.subTest(subtotal=subtotal), self.assertRaises(demo.DemoError):
                function.run(subtotal)
        self.assertLessEqual(function.step_bound, demo.MAX_STEPS_PER_CASE)
        with patch.object(demo, "MAX_STEPS_PER_CASE", 1), patch("builtins.exec") as execute:
            with self.assertRaises(demo.DemoError) as caught:
                demo._CheckoutFunction((self.root / demo.PRODUCT).read_bytes())
            self.assertEqual(caught.exception.code, "step_limit")
            execute.assert_not_called()

    def test_oracle_size_case_count_duplicate_names_and_integer_bounds(self):
        original = json.loads((self.root / demo.ORACLE).read_text())
        variants = []
        value = copy.deepcopy(original); value["cases"] = []; variants.append(value)
        value = copy.deepcopy(original); value["cases"] *= 3; variants.append(value)
        value = copy.deepcopy(original); value["cases"][1]["id"] = value["cases"][0]["id"]; variants.append(value)
        for key, bad in (("subtotalCents", True), ("subtotalCents", -1),
                         ("subtotalCents", demo.MAX_INPUT_CENTS + 1),
                         ("expectedTotalCents", demo.MAX_RESULT_CENTS + 1)):
            value = copy.deepcopy(original); value["cases"][0][key] = bad; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(demo.DemoError):
                demo._freeze_oracle(json.dumps(value).encode())
        with self.assertRaises(demo.DemoError):
            demo._freeze_oracle(b" " * 16_385)
        with self.assertRaises(demo.DemoError):
            demo._json(b'{"cases": [], "cases": []}')

    def test_arbitrary_code_path_or_command_is_not_a_candidate(self):
        for candidate in ("../../checkout.py", "open('anything')", "sh", None, []):
            with self.subTest(candidate=candidate), self.assertRaises(demo.DemoError):
                demo._run_demo(self.root, candidate)

    def test_cli_exit_codes_and_json_report(self):
        for candidate, code, status in (("fixed", 0, "local_repair_passed"),
                                         ("wrong", 1, "candidate_failed"),
                                         ("noop", 2, "rejected")):
            with self.subTest(candidate=candidate), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(demo.main(["--candidate", candidate]), code)
            self.assertEqual(json.loads(output.getvalue())["status"], status)

    def test_cli_failure_is_a_rejection_not_a_false_pass(self):
        with patch.object(demo, "run_demo", side_effect=demo.DemoError("source_shape")):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(demo.main([]), 2)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["error"]["code"], "source_shape")
        self.assertIs(report["protectedVerification"], False)


if __name__ == "__main__":
    unittest.main()
