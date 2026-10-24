"""Causal, repository-owned local software proof, not protected verification.

Only a tiny checkout function's comparator can vary. Python executes the actual
function only after its AST matches that finite, call-free template. This is
deliberately not a sandbox or an arbitrary-project repair runner. The frozen source
and independent oracle reuse the production RepairSource/BlobSet contracts.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from . import contracts
from .execution.artifacts import ArtifactError, BlobSet, read_regular
from .project_repair import RepairError, RepairSource


PRODUCT = "checkout.py"
ORACLE = "oracle.json"
RECIPE = "recipe.json"
SOURCE_PATHS = (PRODUCT, ORACLE, RECIPE)
MAX_SOURCE_BYTES = 512
MAX_AST_NODES = 64
MAX_STEPS_PER_CASE = 64
MAX_INPUT_CENTS = 1_000_000
MAX_RESULT_CENTS = 1_000_500
MAX_CASES = 16
_TEMPLATE = (
    "def checkout_total(subtotal_cents):\n"
    "    if subtotal_cents > 5000:\n"
    "        return subtotal_cents\n"
    "    return subtotal_cents + 500\n"
)
_ALLOWED_NODES = frozenset({
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.If, ast.Return,
    ast.Compare, ast.Name, ast.Load, ast.Constant, ast.BinOp, ast.Add,
    ast.Gt, ast.GtE, ast.Lt,
})
_RECIPE = {
    "schemaVersion": 1, "kind": "bounded-python-checkout",
    "entryPoint": "checkout_total", "productFile": PRODUCT,
    "oracleFile": ORACLE, "candidateGenerator": "scripted-comparator-edit",
    "maxSourceBytes": MAX_SOURCE_BYTES, "maxAstNodes": MAX_AST_NODES,
    "maxStepsPerCase": MAX_STEPS_PER_CASE, "maxInputCents": MAX_INPUT_CENTS,
    "maxResultCents": MAX_RESULT_CENTS, "maxCases": MAX_CASES,
}


class DemoError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(condition, code):
    if not condition:
        raise DemoError(code)


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    _require(type(raw) is bytes and len(raw) <= 16_384, "fixture_limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError):
        raise DemoError("fixture_json") from None


def _bounded_cents(value, maximum, code):
    _require(type(value) is int and 0 <= value <= maximum, code)
    return value


@dataclass(frozen=True)
class _Oracle:
    digest: str
    # Independent expected values are loaded once, before candidate generation.
    cases: tuple[tuple[str, int, int], ...]


def _freeze_oracle(raw):
    value = _json(raw)
    _require(type(value) is dict and set(value) == {
        "schemaVersion", "specification", "cases"}, "oracle_schema")
    _require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1
             and type(value["specification"]) is str
             and 1 <= len(value["specification"]) <= 512, "oracle_schema")
    rows = value["cases"]
    _require(type(rows) is list and 1 <= len(rows) <= MAX_CASES, "oracle_cases")
    cases = []
    names = set()
    for row in rows:
        _require(type(row) is dict and set(row) == {
            "id", "subtotalCents", "expectedTotalCents"}, "oracle_schema")
        name = row["id"]
        _require(type(name) is str and 1 <= len(name) <= 64
                 and name.isascii() and name.replace("_", "").isalnum()
                 and name not in names, "oracle_case_id")
        names.add(name)
        cases.append((name,
                      _bounded_cents(row["subtotalCents"], MAX_INPUT_CENTS, "input_limit"),
                      _bounded_cents(row["expectedTotalCents"], MAX_RESULT_CENTS, "result_limit")))
    return _Oracle(_sha256(raw), tuple(cases))


def _validated_ast(raw):
    """Accept one exact finite function shape, varying only > / >= / <."""
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_SOURCE_BYTES, "source_limit")
    try:
        tree = ast.parse(raw.decode("utf-8"), mode="exec")
    except (UnicodeError, SyntaxError, ValueError, RecursionError):
        raise DemoError("source_shape") from None
    nodes = list(ast.walk(tree))
    _require(len(nodes) <= MAX_AST_NODES, "ast_limit")
    _require(all(type(node) in _ALLOWED_NODES for node in nodes), "source_shape")
    for node in nodes:
        if type(node) is ast.Constant:
            _bounded_cents(node.value, MAX_INPUT_CENTS, "constant_limit")
    comparisons = [node for node in nodes if type(node) is ast.Compare]
    _require(len(comparisons) == 1 and len(comparisons[0].ops) == 1, "source_shape")
    comparator = comparisons[0].ops[0]
    comparisons[0].ops[0] = ast.Gt()
    expected = ast.parse(_TEMPLATE)
    _require(ast.dump(tree, include_attributes=False)
             == ast.dump(expected, include_attributes=False), "source_shape")
    comparisons[0].ops[0] = comparator
    return tree


class _CheckoutFunction:
    """Execute only the exact allowlisted function, with a static step bound.

    There are no calls, loops, imports, attributes, recursion, decorators,
    defaults, annotations or allocation expressions in the accepted template.
    Each AST node can be visited at most once per call. This is a small trusted
    repository demo, not a security boundary for arbitrary untrusted Python.
    """

    def __init__(self, raw):
        tree = _validated_ast(raw)
        self.step_bound = len(list(ast.walk(tree)))
        _require(self.step_bound <= MAX_STEPS_PER_CASE, "step_limit")
        namespace = {"__builtins__": {}}
        # Crucially, only the checked AST is compiled, not a fresh file read.
        # Empty builtins are defense in depth, not the reason this is bounded.
        exec(compile(tree, "<repository-checkout-demo>", "exec"), namespace)
        self._function = namespace["checkout_total"]

    def run(self, subtotal):
        _bounded_cents(subtotal, MAX_INPUT_CENTS, "input_limit")
        return _bounded_cents(self._function(subtotal),
                              MAX_RESULT_CENTS, "result_limit")


def _observe(raw, oracle):
    function = _CheckoutFunction(raw)
    cases = []
    for name, subtotal, expected in oracle.cases:
        actual = function.run(subtotal)
        cases.append({"id": name, "subtotalCents": subtotal,
                      "expectedTotalCents": expected, "observedTotalCents": actual,
                      "passed": actual == expected, "stepUpperBound": function.step_bound})
    passed = sum(row["passed"] for row in cases)
    return {"cases": cases, "passed": passed, "failed": len(cases) - passed,
            "allPassed": passed == len(cases)}


def _load_source(root):
    project = contracts.validate_project_revision(_json(read_regular(root, "project.json", maximum=16_384)))
    # The project schema describes mobile identities; this descriptor is only a
    # schema-compatible fixture, not evidence of a built/installed mobile app.
    _require(project["editablePaths"] == [PRODUCT]
             and project["fixtures"] == []
             and project["recipes"] == [{"id": "checkout_regression", "kind": "regression",
                 "productFile": RECIPE, "operation": "regression"}]
             and len(project["builds"]) == 1
             and project["builds"][0]["id"] == "original", "project_policy")
    source = RepairSource(project, root, source_paths=SOURCE_PATHS,
                          protected_paths=(ORACLE, RECIPE),
                          original_artifact_root=root, original_artifact_paths=(PRODUCT,))
    frozen = source.freeze("original")
    entries = dict(frozen.entries)
    # Limits are policy, not candidate-configurable recipe knobs.
    recipe = _json(entries[RECIPE])
    _require(type(recipe) is dict and recipe == _RECIPE
             and all(type(recipe[key]) is type(value) for key, value in _RECIPE.items()),
             "recipe_policy")
    oracle = _freeze_oracle(entries[ORACLE])
    _validated_ast(entries[PRODUCT])
    return source, frozen, oracle


def _claims():
    return {
        "verificationTier": "local-software-proof", "candidateGenerator": "scripted-not-ai",
        "execution": "allowlisted-python-function", "sandbox": False,
        "stepBoundKind": "static-acyclic-ast-node-bound",
        "actualAI": False, "externalAI": False, "mobile": False, "VM": False,
        "sdkCapture": False, "scenarioReplay": False, "syntheticEvidence": True,
        "companyAcceptance": False, "protectedVerification": False,
        "networkUsed": False, "credentialsUsed": False,
    }


def _run_demo(root, candidate):
    _require(type(candidate) is str and candidate in {"fixed", "wrong", "noop"}, "candidate_selection")
    source, frozen, oracle = _load_source(root)
    from .ticket_intake_demo import demo_draft
    draft = demo_draft(source.project, oracle.digest)
    _require(type(draft) is dict and draft.get("status") == "review-required"
             and draft.get("approved") is False and draft.get("aiTransfer") is False
             and draft.get("executionAuthority") == "none"
             and type(draft.get("references")) is dict
             and draft["references"].get("sourceDigest") == frozen.digest
             and draft["references"].get("projectDigest") == source.project_digest
             and type(draft.get("demoBinding")) is dict
             and draft["demoBinding"].get("syntheticEvidence") is True
             and draft["demoBinding"].get("oracleDigest") == oracle.digest
             and draft["demoBinding"].get("specificationDigest")
             == draft["references"].get("specificationDigest")
             and type(draft["references"].get("specificationDigest")) is str,
             "ticket_draft_binding")
    entries = dict(frozen.entries)
    baseline = _observe(entries[PRODUCT], oracle)
    _require(not baseline["allPassed"], "baseline_defect_missing")
    report = {
        "schemaVersion": 1, "kind": "offline-ticket-repair-demo", **_claims(),
        "candidateSelection": candidate, "status": "rejected",
        "ticketIntake": draft, "draftOracleDigest": draft["demoBinding"]["oracleDigest"],
        "approval": "not-granted-demo-only",
        "sourceDigest": frozen.digest, "candidateDigest": None,
        "sourceFileDigest": _sha256(entries[PRODUCT]), "candidateFileDigest": None,
        "oracleDigest": oracle.digest, "candidateOracleDigest": None,
        "recipeDigest": _sha256(entries[RECIPE]), "projectDigest": source.project_digest,
        "originalArtifactKind": "python-source-fixture-not-mobile-build",
        "baselineDefectReproduced": True, "baseline": baseline, "candidate": None,
        "protectedInputsUnchanged": True, "protectedEditRejections": [],
    }
    # Exercise real policy rejection, including undeclared test/harness paths.
    for path in (ORACLE, RECIPE, "project.json", "tests/test_offline_repair_demo.py"):
        try:
            source.apply(frozen, [{"path": path, "old": "expected", "new": "bypassed"}])
        except RepairError as error:
            _require(error.code == "protected_path", "protected_policy_failed")
            report["protectedEditRejections"].append({"path": path, "code": error.code})
        else:
            raise DemoError("protected_policy_failed")
    replacement = {"fixed": ">=", "wrong": "<", "noop": ">"}[candidate]
    edit = {"path": PRODUCT, "old": "subtotal_cents > 5000",
            "new": f"subtotal_cents {replacement} 5000"}
    try:
        changed = source.apply(frozen, [edit])
    except RepairError as error:
        # Expected no-op rejection happens before any candidate evaluation.
        if candidate != "noop" or error.code not in {"patch_invalid", "patch_noop"}:
            raise
        report["error"] = {"code": error.code, "reason": "no-op candidate rejected"}
        source.require_original(frozen, "original")
        return report
    _require(type(changed) is BlobSet and changed.digest != frozen.digest, "candidate_unchanged")
    changed_entries = dict(changed.entries)
    _require(all(changed_entries[path] == entries[path] for path in (ORACLE, RECIPE)),
             "protected_inputs_changed")
    observed = _observe(changed_entries[PRODUCT], oracle)
    source.require_original(frozen, "original")
    report.update({
        "status": "local_repair_passed" if observed["allPassed"] else "candidate_failed",
        "candidateDigest": changed.digest,
        "candidateFileDigest": _sha256(changed_entries[PRODUCT]),
        "candidateOracleDigest": _sha256(changed_entries[ORACLE]), "candidate": observed,
        "edit": edit,
    })
    return report


def run_demo(candidate="fixed"):
    """Run only the checked-in fixture; no user path, code, command, or provider."""
    root = Path(__file__).absolute().parents[1] / "examples" / "ticket-repair"
    return _run_demo(root, candidate)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Repository-checkout-only local software proof. Scripted candidate, not AI; "
                    "no mobile/VM/company acceptance or protected verification.")
    parser.add_argument("--candidate", choices=("fixed", "wrong", "noop"), default="fixed")
    args = parser.parse_args(argv)
    try:
        report = run_demo(args.candidate)
    except (DemoError, RepairError, ArtifactError, contracts.ContractError, OSError) as error:
        report = {"schemaVersion": 1, "kind": "offline-ticket-repair-demo", **_claims(),
                  "status": "rejected", "error": {"code": getattr(error, "code", "fixture_unavailable")}}
    print(json.dumps(report, indent=2, sort_keys=True))
    return {"local_repair_passed": 0, "candidate_failed": 1}.get(report["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
