"""화면 계층 해석과 여러 근거 locator 계약. Xcode 없이 실행된다."""
import copy
import json
import unittest
from pathlib import Path

from reproof.simple.errors import SimpleError
from reproof.simple.hierarchy import element_at, iter_elements, parse_hierarchy
from reproof.simple.locator import LocatorEvidence, capture_evidence, resolve

FIXTURE = Path(__file__).parent / "fixtures" / "simple" / "hierarchy-mixed.json"


def load_document() -> dict:
    """픽스처 JSON 문서를 새로 읽는다(테스트마다 독립 사본)."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def find(root, *, identifier=None, label=None, element_type=None):
    """조건에 맞는 첫 요소(pre-order)를 돌려준다."""
    for e in iter_elements(root):
        if ((identifier is None or e.identifier == identifier) and (label is None or e.label == label)
                and (element_type is None or e.element_type == element_type)):
            return e
    raise AssertionError("요소 없음")


class LocatorTests(unittest.TestCase):
    def setUp(self):
        self.root = parse_hierarchy(load_document())

    def test_unique_identifier_is_strong(self):
        r = resolve(self.root, capture_evidence(self.root, find(self.root, identifier="cart.clear")))
        self.assertEqual((r.strength, r.matched_by), ("strong", "identifier"))

    def test_auto_identifier_prefix_is_reported(self):
        target = find(self.root, identifier="rp.CartViewController.addButton")
        r = resolve(self.root, capture_evidence(self.root, target))
        self.assertEqual((r.strength, r.matched_by), ("strong", "auto-identifier"))

    def test_label_without_identifier_is_weak_type_label_anchor(self):
        target = find(self.root, label="담기")
        r = resolve(self.root, capture_evidence(self.root, target))
        self.assertEqual((r.strength, r.matched_by, r.candidate_count), ("weak", "type-label-anchor", 1))
        self.assertEqual(r.element.path, target.path)

    def test_duplicate_labels_use_label_ordinal(self):
        adds = [e for e in iter_elements(self.root) if e.label == "추가"]
        self.assertEqual(len(adds), 3)
        r = resolve(self.root, capture_evidence(self.root, adds[1]))
        self.assertEqual(r.element.path, adds[1].path)
        self.assertEqual(r.matched_by, "label-ordinal")
        self.assertEqual(r.candidate_count, 3)

    def test_changed_label_falls_back_to_position_ordinal(self):
        total = find(self.root, label="합계 2")
        evidence = capture_evidence(self.root, total)
        document = copy.deepcopy(load_document())
        summary = next(c for c in document["children"] if c["identifier"] == "cart.summary")
        summary["children"][0]["label"] = "합계 1"
        after = parse_hierarchy(document)
        r = resolve(after, evidence)
        self.assertEqual(r.matched_by, "position-ordinal")
        self.assertEqual(r.element.label, "합계 1")

    def test_missing_element_is_none(self):
        evidence = LocatorEvidence("", "button", "없는 버튼", "", 0, 99)
        r = resolve(self.root, evidence)
        self.assertEqual((r.element, r.strength, r.matched_by, r.candidate_count), (None, "none", "none", 0))

    def test_element_at_returns_button_not_inner_text(self):
        button = find(self.root, label="담기")
        document = load_document()
        document["children"][0]["children"][0]["children"] = [{
            "type": "staticText", "identifier": "", "label": "담기", "value": "", "placeholder": "",
            "frame": [20, 80, 50, 20], "enabled": True, "selected": False, "children": []}]
        root = parse_hierarchy(document)
        hit = element_at(root, 30, 90)
        self.assertEqual((hit.element_type, hit.path), ("button", button.path))
        self.assertIsNone(element_at(root, 5000, 5000))

    def test_evidence_document_round_trip(self):
        evidence = capture_evidence(self.root, find(self.root, label="담기"))
        self.assertEqual(LocatorEvidence.from_document(evidence.to_document()), evidence)

    def test_oversized_hierarchy_is_rejected(self):
        wide = load_document()
        wide["children"] = [dict(wide["children"][0], children=[]) for _ in range(8001)]
        with self.assertRaises(SimpleError) as caught:
            parse_hierarchy(wide)
        self.assertEqual(caught.exception.code, "hierarchy_too_large")
        deep = load_document()
        node = deep
        for _ in range(65):
            child = dict(deep, children=[])
            node["children"] = [child]
            node = child
        with self.assertRaises(SimpleError) as caught:
            parse_hierarchy(deep)
        self.assertEqual(caught.exception.code, "hierarchy_too_large")

    def test_invalid_node_type_is_rejected(self):
        document = load_document()
        document["frame"] = [0, 0, 1]
        with self.assertRaises(SimpleError) as caught:
            parse_hierarchy(document)
        self.assertEqual(caught.exception.code, "hierarchy_invalid")


if __name__ == "__main__":
    unittest.main()
