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

    @staticmethod
    def _node(element_type, identifier, label, frame, children=()):
        """계층 JSON 노드 하나를 만든다."""
        return {"type": element_type, "identifier": identifier, "label": label, "value": "", "placeholder": "",
                "frame": list(frame), "enabled": True, "selected": False, "children": list(children)}

    def _screen(self, *buttons):
        """identifier 'screen'인 화면 아래에 (identifier, label) 버튼들을 세로로 놓은 계층을 만든다."""
        nodes = [self._node("button", identifier, label, (0, 100 + 50 * index, 390, 44))
                 for index, (identifier, label) in enumerate(buttons)]
        return parse_hierarchy(self._node("application", "", "App", (0, 0, 390, 844), [
            self._node("other", "screen", "", (0, 0, 390, 844), nodes)]))

    def test_replacement_with_different_identifier_is_never_chosen(self):
        """'checkout/Pay' 자리에 'delete-account/Delete account'가 오면 위치가 같아도 찾지 않는다(none).

        왜: 기록한 identifier와 다른 identifier를 가진 요소는 다른 요소라는 강한 근거다. 위치·순번만 맞는다고 고르면
        결제 대신 계정 삭제를 누를 수 있다. 못 찾는 쪽이 틀린 요소를 누르는 것보다 낫다.
        """
        before = self._screen(("checkout", "Pay"))
        evidence = capture_evidence(before, find(before, identifier="checkout"))
        after = self._screen(("delete-account", "Delete account"))
        r = resolve(after, evidence)
        self.assertEqual((r.element, r.strength, r.matched_by), (None, "none", "none"))

    def test_duplicate_identifier_list_keeps_ordinal_among_same_identifier(self):
        """identifier가 중복된 목록(row.add ×3)은 같은 identifier 후보 안에서 순번으로 찾고, 다른 identifier 요소가 끼어도
        순번이 밀리지 않는다."""
        rows = [("row.add", "추가")] * 3
        before = self._screen(*rows)
        second = [e for e in iter_elements(before) if e.identifier == "row.add"][1]
        evidence = capture_evidence(before, second)
        after = self._screen(("promo.add", "추가"), *rows)
        r = resolve(after, evidence)
        self.assertEqual((r.matched_by, r.candidate_count), ("label-ordinal", 3))
        self.assertEqual(r.element.identifier, "row.add")
        self.assertEqual(r.element.frame.y, 200)

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


def plain_node(kind, frame, children=(), label=""):
    """계층 노드 사전 하나를 만든다(테스트용 합성 계층)."""
    return {"type": kind, "identifier": "", "label": label, "value": "", "placeholder": "", "frame": list(frame),
            "enabled": True, "selected": False, "children": list(children)}


def overlay_chain(frame, depth):
    """label 없는 other가 depth단 겹친 덮개(맨 아래가 가장 깊다)."""
    node = plain_node("other", frame)
    for _ in range(depth - 1):
        node = plain_node("other", frame, [node])
    return node


SCREEN = (0, 0, 393, 852)
BUTTON = (100, 400, 120, 44)


class DecorativeOverlayBoundTests(unittest.TestCase):
    """장식용 덮개 보정은 작은 묶음 안에서만: 화면 전체 덮개(scrim) 뒤의 버튼은 고르지 않는다(틀린 요소보다 없음이 낫다)."""

    def hit(self, window_children):
        """창 아래에 window_children을 둔 합성 계층에서 버튼 가운데 좌표의 element_at을 (종류, label)로 돌려준다."""
        document = plain_node("application", SCREEN, [plain_node("window", SCREEN, window_children)], label="앱")
        element = element_at(parse_hierarchy(document), 160, 422)
        return element.element_type, element.label

    def test_full_screen_scrim_under_window_does_not_pick_button_behind(self):
        content = plain_node("other", SCREEN, [plain_node("button", BUTTON, label="결제")])
        self.assertEqual(self.hit([content, overlay_chain(SCREEN, 4)]), ("other", ""))

    def test_full_screen_scrim_in_full_screen_container_does_not_pick_button_behind(self):
        content = plain_node("other", SCREEN, [plain_node("button", BUTTON, label="결제")])
        container = plain_node("other", SCREEN, [content, overlay_chain(SCREEN, 4)])
        self.assertEqual(self.hit([container]), ("other", ""))

    def test_overlay_in_small_shared_container_picks_button(self):
        group = (90, 390, 200, 64)
        container = plain_node("other", group, [plain_node("button", BUTTON, label="결제"), overlay_chain(group, 3)])
        self.assertEqual(self.hit([plain_node("other", SCREEN, [container])]), ("button", "결제"))

    def test_container_at_ninety_percent_of_window_is_full_screen(self):
        tall = (0, 0, 393, 852 * 0.9)
        container = plain_node("other", tall, [plain_node("button", BUTTON, label="결제"), overlay_chain(tall, 3)])
        self.assertEqual(self.hit([container]), ("other", ""))


if __name__ == "__main__":
    unittest.main()
