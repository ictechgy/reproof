"""화면 계층 해석과 여러 근거 locator 계약. Xcode 없이 실행된다."""
import copy
import json
import unittest
from pathlib import Path

from reproof.simple.errors import SimpleError
from reproof.simple.hierarchy import element_at, iter_elements, parse_hierarchy
from reproof.simple.locator import LocatorEvidence, capture_evidence, resolve

FIXTURE = Path(__file__).parent / "fixtures" / "simple" / "hierarchy-mixed.json"
# iOS 27 탭 바 구조를 줄인 픽스처: 선택 표시·하단 영역의 label 없는 other가 탭 버튼의 형제 쪽 가지에서 버튼보다 깊다.
TAB_BAR_FIXTURE = Path(__file__).parent / "fixtures" / "simple" / "hierarchy-tabbar-ios27.json"


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
        evidence = LocatorEvidence("", "button", "없는 버튼", "", 0, 99, 1, 100)
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

    def _rows(self, document):
        """SwiftUI 섹션의 행 목록(문서 안의 실제 리스트)을 돌려준다."""
        return document["children"][1]["children"]

    def _second_add_evidence(self):
        adds = [e for e in iter_elements(self.root) if e.label == "추가"]
        return capture_evidence(self.root, adds[1])

    def test_removed_sibling_does_not_resolve_to_another_button(self):
        evidence = self._second_add_evidence()
        document = load_document()
        del self._rows(document)[2]
        r = resolve(parse_hierarchy(document), evidence)
        self.assertEqual((r.element, r.strength, r.matched_by), (None, "none", "none"))

    def test_removed_first_row_does_not_shift_pick(self):
        evidence = self._second_add_evidence()
        document = load_document()
        del self._rows(document)[0]
        r = resolve(parse_hierarchy(document), evidence)
        self.assertIsNone(r.element)

    def test_removed_element_does_not_pick_neighbor_by_position(self):
        evidence = capture_evidence(self.root, find(self.root, label="합계 2"))
        document = load_document()
        document["children"][2]["children"] = []
        r = resolve(parse_hierarchy(document), evidence)
        self.assertEqual((r.element, r.matched_by), (None, "none"))

    def test_duplicate_identifier_falls_through_to_label(self):
        target = find(self.root, identifier="cart.clear")
        evidence = capture_evidence(self.root, target)
        document = load_document()
        document["children"][0]["children"][2]["identifier"] = "cart.clear"
        r = resolve(parse_hierarchy(document), evidence)
        self.assertEqual((r.strength, r.matched_by), ("weak", "type-label-anchor"))
        self.assertEqual(r.element.label, "비우기")

    def test_element_in_cell_returns_button_not_cell(self):
        document = load_document()
        document["children"][1]["children"] = [{
            "type": "cell", "identifier": "", "label": "", "value": "", "placeholder": "",
            "frame": [0, 300, 390, 60], "enabled": True, "selected": False, "children": [{
                "type": "button", "identifier": "", "label": "추가", "value": "", "placeholder": "",
                "frame": [300, 310, 70, 40], "enabled": True, "selected": False, "children": []}]}]
        root = parse_hierarchy(document)
        self.assertEqual(element_at(root, 320, 320).element_type, "button")
        self.assertEqual(element_at(root, 10, 320).element_type, "cell")

    def test_malformed_evidence_document_is_rejected(self):
        good = capture_evidence(self.root, find(self.root, label="담기")).to_document()
        for bad in ({k: v for k, v in good.items() if k != "label"}, {**good, "labelOrdinal": "0"}):
            with self.assertRaises(SimpleError) as caught:
                LocatorEvidence.from_document(bad)
            self.assertEqual(caught.exception.code, "locator_evidence_invalid")


class TabBarHitTests(unittest.TestCase):
    """iOS 27 탭 바: 버튼보다 깊은 장식용 other가 있어도 탭 버튼을 골라야 한다(Spike S2에서 발견)."""

    def setUp(self):
        self.document = json.loads(TAB_BAR_FIXTURE.read_text(encoding="utf-8"))

    def hit(self, x, y, document=None):
        """좌표의 element_at 결과를 (종류, label)로 돌려준다."""
        element = element_at(parse_hierarchy(document or self.document), x, y)
        return element.element_type, element.label

    def test_selected_tab_overlay_does_not_hide_tab_button(self):
        self.assertEqual(self.hit(239.67, 800), ("button", "요약"))

    def test_bottom_region_overlay_does_not_hide_tab_button(self):
        self.assertEqual(self.hit(153.67, 800), ("button", "장바구니"))

    def test_decorative_hit_without_interactive_keeps_deepest(self):
        self.assertEqual(self.hit(50, 800), ("other", ""))

    def test_zero_size_interactive_is_never_chosen(self):
        overlay = self.document["children"][0]["children"][0]["children"][0]["children"][0]["children"][0]
        overlay["children"][2]["children"][0]["children"] = [{
            "type": "button", "identifier": "", "label": "가짜", "value": "", "placeholder": "",
            "frame": [239.67, 800, 0, 0], "enabled": True, "selected": False, "children": []}]
        self.assertEqual(self.hit(239.67, 800, self.document), ("button", "요약"))


if __name__ == "__main__":
    unittest.main()
