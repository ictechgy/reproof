"""실행기가 내보낸 화면 계층(JSON)을 읽어 불변 요소 트리로 바꾼다. 순수 Python이며 Xcode가 필요 없다."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Mapping

from reproof.simple.errors import SimpleError

# 사용자가 직접 누르거나 입력하는 요소 종류. 안쪽 Text를 눌러도 바깥 버튼을 대상으로 삼기 위해 쓴다.
INTERACTIVE_TYPES = frozenset({
    "button", "textField", "secureTextField", "searchField", "switch", "toggle", "cell",
    "link", "slider", "segmentedControl", "stepper", "picker", "menuItem", "tab",
})

# 도구가 자동으로 붙인 identifier의 접두사. 사람이 붙인 ID와 근거 강도를 구분하려는 것이다.
AUTO_ID_PREFIX = "rp."

# 비정상적으로 큰 계층이 메모리와 시간을 잡아먹지 않도록 두는 상한.
MAX_NODE_COUNT = 8000
MAX_DEPTH = 64

# 장식용 덮개 아래의 interactive를 고를 수 있는 공통 조상 넓이의 상한(창 넓이 대비 비율, 미만이어야 함).
# 이 이상이면 화면 전체 덮개(모달 배경 등)로 보고 뒤의 버튼을 고르지 않는다.
MAX_OVERLAY_AREA_RATIO = 0.9
# 공통 조상이 이 종류면 덮개와 버튼이 한 묶음이 아니라 화면 단위로만 겹친 것으로 본다.
SCREEN_LEVEL_TYPES = frozenset({"application", "window"})


@dataclass(frozen=True)
class Frame:
    """화면 좌표계의 사각형. x, y는 왼쪽 위, width, height는 크기(pt)."""

    x: float
    y: float
    width: float
    height: float

    def contains(self, px: float, py: float) -> bool:
        """점이 사각형 안인지 돌려준다. 왼쪽/위 경계는 포함, 오른쪽/아래 경계는 제외한다.

        왜: 맞닿은 두 요소가 같은 점을 동시에 차지하지 않게 하려는 것이다. 크기가 0이면 항상 False.
        """
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height

    def center(self) -> tuple[float, float]:
        """사각형 중심 좌표 (x, y)를 돌려준다."""
        return (self.x + self.width / 2, self.y + self.height / 2)


@dataclass(frozen=True)
class Element:
    """화면 요소 하나. path(루트에서 내려오는 자식 인덱스)가 동일성 기준이다.

    왜: 같은 값을 가진 요소가 여러 개일 수 있어 값 비교 대신 path로 구분한다.
    """

    element_type: str
    identifier: str
    label: str
    value: str
    placeholder: str
    frame: Frame
    is_enabled: bool
    is_selected: bool
    path: tuple[int, ...]
    children: tuple["Element", ...]


def _invalid(detail: str) -> SimpleError:
    """계층 형식 오류를 만든다. detail은 어느 부분이 잘못됐는지 알려준다."""
    return SimpleError(
        "hierarchy_invalid",
        f"화면 계층 데이터 형식이 올바르지 않습니다: {detail}",
        "실행기와 도구 버전이 같은지 확인하고 다시 실행하세요.",
    )


def _read_text(node: Mapping, key: str) -> str:
    """노드에서 문자열 필드를 읽는다. 문자열이 아니거나 없으면 hierarchy_invalid."""
    value = node.get(key)
    if not isinstance(value, str):
        raise _invalid(f"'{key}'는 문자열이어야 합니다.")
    return value


def _read_flag(node: Mapping, key: str) -> bool:
    """노드에서 bool 필드를 읽는다. bool이 아니거나 없으면 hierarchy_invalid."""
    value = node.get(key)
    if not isinstance(value, bool):
        raise _invalid(f"'{key}'는 true/false여야 합니다.")
    return value


def _read_frame(node: Mapping) -> Frame:
    """노드의 frame([x, y, w, h])을 읽는다. 숫자 4개가 아니면 hierarchy_invalid. bool은 숫자로 보지 않는다."""
    raw = node.get("frame")
    ok = isinstance(raw, list) and len(raw) == 4 and all(
        isinstance(n, (int, float)) and not isinstance(n, bool) for n in raw)
    if not ok:
        raise _invalid("'frame'은 숫자 4개 목록이어야 합니다.")
    return Frame(*(float(n) for n in raw))


def _too_large(detail: str) -> SimpleError:
    """계층 크기 초과 오류를 만든다."""
    return SimpleError(
        "hierarchy_too_large",
        f"화면 계층이 너무 큽니다: {detail}",
        "요소가 아주 많은 화면에서는 더 단순한 화면에서 다시 시도하세요.",
    )


def _build(node: object, path: tuple[int, ...], counter: list[int]) -> Element:
    """노드 하나와 그 자식을 재귀로 Element로 바꾼다. counter[0]은 지금까지 센 노드 수."""
    if not isinstance(node, Mapping):
        raise _invalid("노드는 객체여야 합니다.")
    if len(path) > MAX_DEPTH:
        raise _too_large(f"깊이가 {MAX_DEPTH}를 넘습니다.")
    counter[0] += 1
    if counter[0] > MAX_NODE_COUNT:
        raise _too_large(f"요소가 {MAX_NODE_COUNT}개를 넘습니다.")
    raw_children = node.get("children")
    if not isinstance(raw_children, list):
        raise _invalid("'children'은 목록이어야 합니다.")
    children = tuple(_build(child, path + (index,), counter) for index, child in enumerate(raw_children))
    return Element(
        _read_text(node, "type"), _read_text(node, "identifier"), _read_text(node, "label"),
        _read_text(node, "value"), _read_text(node, "placeholder"), _read_frame(node),
        _read_flag(node, "enabled"), _read_flag(node, "selected"), path, children,
    )


def parse_hierarchy(document: Mapping) -> Element:
    """실행기 JSON 문서를 루트 Element로 바꾼다.

    형식 위반은 SimpleError("hierarchy_invalid"), 노드 8000개 또는 깊이 64 초과는
    SimpleError("hierarchy_too_large")이다. 왜: 잘못된 데이터로 조용히 엉뚱한 요소를 고르지 않게 한다.
    """
    return _build(document, (), [0])


def iter_elements(root: Element) -> Iterator[Element]:
    """루트부터 pre-order(부모, 그다음 자식 순서)로 모든 요소를 돌려준다."""
    yield root
    for child in root.children:
        yield from iter_elements(child)


def ancestors_of(root: Element, element: Element) -> tuple[Element, ...]:
    """element의 조상을 가까운 순서(부모 -> 루트)로 돌려준다. path로 루트에서 따라 내려가 찾는다."""
    chain = [root]
    for index in element.path[:-1]:
        chain.append(chain[-1].children[index])
    return tuple(reversed(chain)) if element.path else ()


def _is_better_hit(candidate: Element, best: Element | None) -> bool:
    """element_at 후보 비교: 더 깊거나, 같은 깊이에서 interactive 우선, 그다음 pre-order 뒤쪽(위에 그려짐)."""
    if best is None or len(candidate.path) != len(best.path):
        return best is None or len(candidate.path) > len(best.path)
    return (candidate.element_type in INTERACTIVE_TYPES) >= (best.element_type in INTERACTIVE_TYPES)


def _deepest_hit(root: Element, x: float, y: float) -> Element | None:
    """좌표를 포함하는 가장 깊은 요소(_is_better_hit 순서). 크기가 0인 frame은 포함하지 않는다."""
    best: Element | None = None
    for element in iter_elements(root):
        if element.frame.contains(x, y) and _is_better_hit(element, best):
            best = element
    return best


def _is_decorative(element: Element) -> bool:
    """interactive가 아니고 label·identifier·value가 모두 빈 요소(겹쳐 그린 장식용 view 등)면 True."""
    return element.element_type not in INTERACTIVE_TYPES and not (element.label or element.identifier or element.value)


def _deepest_interactive_at(root: Element, x: float, y: float) -> Element | None:
    """좌표를 포함하는 interactive 요소 중 가장 깊은 것. 같은 깊이면 pre-order 뒤쪽(위에 그려짐). 없으면 None."""
    best: Element | None = None
    for element in iter_elements(root):
        is_deeper = best is None or len(element.path) >= len(best.path)
        if element.element_type in INTERACTIVE_TYPES and element.frame.contains(x, y) and is_deeper:
            best = element
    return best


def _nearest_interactive_ancestor(root: Element, element: Element) -> Element | None:
    """element의 조상 중 가장 가까운 interactive. 없으면 None."""
    return next((ancestor for ancestor in ancestors_of(root, element)
                 if ancestor.element_type in INTERACTIVE_TYPES), None)


def _common_ancestor(root: Element, first: Element, second: Element) -> Element:
    """두 요소의 가장 가까운 공통 조상(path의 공통 앞부분이 가리키는 요소). 한쪽이 다른 쪽 조상이면 그 요소다."""
    node = root
    for left, right in zip(first.path, second.path):
        if left != right:
            break
        node = node.children[left]
    return node


def _area(frame: Frame) -> float:
    """frame 넓이(pt²). 음수 크기는 0으로 본다."""
    return max(frame.width, 0.0) * max(frame.height, 0.0)


def _is_bounded_overlay(root: Element, hit: Element, candidate: Element) -> bool:
    """덮개 hit와 candidate가 창보다 확실히 작은 한 묶음 안에서 겹쳤는지 돌려준다.

    공통 조상이 application·window가 아니고, 그 넓이가 가장 가까운 window(없으면 root) 넓이의 90% 미만이어야 True.
    왜: 탭 바의 선택 표시처럼 작은 묶음 안의 빈 view는 장식이지만, 화면 전체를 덮는 빈 view(모달 배경 등)는
    실제로 터치를 막는다. 그 뒤의 버튼을 고르면 틀린 요소가 되고, 틀린 요소는 못 찾는 것보다 나쁘다.
    """
    shared = _common_ancestor(root, hit, candidate)
    if shared.element_type in SCREEN_LEVEL_TYPES:
        return False
    window = next((ancestor for ancestor in ancestors_of(root, shared) if ancestor.element_type == "window"), root)
    window_area = _area(window.frame)
    return window_area > 0 and _area(shared.frame) < MAX_OVERLAY_AREA_RATIO * window_area


def _interactive_under_overlay(root: Element, hit: Element, x: float, y: float) -> Element | None:
    """hit가 작은 묶음 안의 장식용 덮개면 좌표를 포함하는 가장 깊은 interactive를, 아니면 None을 돌려준다."""
    if not _is_decorative(hit):
        return None
    candidate = _deepest_interactive_at(root, x, y)
    return candidate if candidate is not None and _is_bounded_overlay(root, hit, candidate) else None


def element_at(root: Element, x: float, y: float) -> Element | None:
    """좌표 (x, y)에 해당하는 요소를 돌려준다. 없으면 None.

    가장 깊은 요소를 고른다. 그 요소가 interactive면 그대로 돌려주고(셀 안의 버튼은 셀이 아니라 버튼),
    작은 묶음 안의 장식용 덮개(label·identifier·value가 빈 비interactive)면 좌표를 포함하는 가장 깊은
    interactive를, 아니면 조상 중 가장 가까운 interactive를 돌려준다. 어느 쪽도 없으면 가장 깊은 요소 그대로다.
    왜: 버튼 안쪽 Text를 눌러도 녹화 대상은 버튼이어야 재생이 안정적이다. iOS 27 탭 바처럼 선택 표시용
    빈 view가 버튼의 형제 가지에서 버튼보다 깊게 겹치면, 조상을 따라 올라가도 버튼을 만나지 못한다.
    다만 화면 전체 덮개 뒤의 버튼은 고르지 않는다(`_is_bounded_overlay` 참고).
    """
    best = _deepest_hit(root, x, y)
    if best is None or best.element_type in INTERACTIVE_TYPES:
        return best
    return _interactive_under_overlay(root, best, x, y) or _nearest_interactive_ancestor(root, best) or best
