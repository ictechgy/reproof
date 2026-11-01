"""요소를 여러 근거(identifier, 종류+label+앵커, 순번)로 기록하고 재생 때 찾는다. 일치 강도를 함께 알려준다."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from reproof.simple.errors import SimpleError
from reproof.simple.hierarchy import AUTO_ID_PREFIX, Element, ancestors_of, iter_elements


def _invalid_evidence(detail: str) -> SimpleError:
    """저장된 locator 근거가 손상됐을 때의 오류를 만든다."""
    return SimpleError(
        "locator_evidence_invalid",
        f"저장된 요소 근거가 손상됐습니다: {detail}",
        "해당 단계를 다시 녹화하세요.",
    )


@dataclass(frozen=True)
class LocatorEvidence:
    """녹화 시점에 모은 요소 근거. 회사 앱엔 identifier가 없는 경우가 많아 여러 근거를 함께 보관한다.

    anchor_identifier: 가장 가까운 identifier 있는 조상의 값(없으면 ""). label_ordinal: 같은
    (종류, label, 앵커) 요소 중 pre-order 순번. position_ordinal: 같은 (종류, 앵커) 요소 중 순번.
    """

    identifier: str
    element_type: str
    label: str
    anchor_identifier: str
    label_ordinal: int
    position_ordinal: int
    label_count: int
    position_count: int

    def to_document(self) -> dict:
        """JSON으로 저장할 dict로 바꾼다."""
        return {
            "identifier": self.identifier, "elementType": self.element_type, "label": self.label,
            "anchorIdentifier": self.anchor_identifier, "labelOrdinal": self.label_ordinal,
            "positionOrdinal": self.position_ordinal,
            "labelCount": self.label_count, "positionCount": self.position_count,
        }

    @classmethod
    def from_document(cls, document: Mapping) -> "LocatorEvidence":
        """to_document()가 만든 dict에서 복원한다.

        키가 없거나 타입이 틀리면 SimpleError("locator_evidence_invalid"). 왜: 손상된 저장 파일로
        엉뚱한 요소를 찾지 않고, 사용자에게 다시 녹화하라고 안내하기 위해서다.
        """
        text_keys = ("identifier", "elementType", "label", "anchorIdentifier")
        count_keys = ("labelOrdinal", "positionOrdinal", "labelCount", "positionCount")
        try:
            texts = [document[k] for k in text_keys]
            counts = [document[k] for k in count_keys]
        except (KeyError, TypeError) as error:
            raise _invalid_evidence(f"필드 누락: {error}") from error
        if not all(isinstance(t, str) for t in texts) or not all(
                isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts):
            raise _invalid_evidence("필드 타입이 올바르지 않습니다.")
        return cls(*texts, *counts)


@dataclass(frozen=True)
class Resolution:
    """재생 때 요소를 찾은 결과. strength는 strong/weak/none, matched_by는 쓴 근거 이름이다."""

    element: Element | None
    strength: str
    matched_by: str
    candidate_count: int


def _anchor_of(root: Element, element: Element) -> str:
    """가장 가까운 조상 중 identifier가 비어 있지 않은 것의 값. 없으면 ""."""
    for ancestor in ancestors_of(root, element):
        if ancestor.identifier:
            return ancestor.identifier
    return ""


def _same_type_anchor(root: Element, element_type: str, anchor: str) -> list[Element]:
    """(종류, 앵커)가 같은 요소를 pre-order로 모은다."""
    return [e for e in iter_elements(root)
            if e.element_type == element_type and _anchor_of(root, e) == anchor]


def capture_evidence(root: Element, element: Element) -> LocatorEvidence:
    """element에서 locator 근거를 뽑는다. 순번은 root 트리 기준이다."""
    anchor = _anchor_of(root, element)
    same_type = _same_type_anchor(root, element.element_type, anchor)
    same_label = [e for e in same_type if e.label == element.label]
    return LocatorEvidence(
        element.identifier, element.element_type, element.label, anchor,
        _index_by_path(same_label, element), _index_by_path(same_type, element),
        len(same_label), len(same_type),
    )


def _index_by_path(elements: list[Element], target: Element) -> int:
    """path 동일성으로 target의 위치를 찾는다(값이 같은 요소와 혼동하지 않기 위해)."""
    return next(i for i, e in enumerate(elements) if e.path == target.path)


def _resolve_by_identifier(root: Element, evidence: LocatorEvidence) -> Resolution | None:
    """1단계: identifier가 트리에서 정확히 하나일 때만 strong. 0개/여러 개면 None(다음 단계로)."""
    if not evidence.identifier:
        return None
    found = [e for e in iter_elements(root) if e.identifier == evidence.identifier]
    if len(found) != 1:
        return None
    kind = "auto-identifier" if evidence.identifier.startswith(AUTO_ID_PREFIX) else "identifier"
    return Resolution(found[0], "strong", kind, 1)


def _resolve_by_label(root: Element, evidence: LocatorEvidence) -> Resolution | None:
    """2,3단계: (종류, label, 앵커) 후보가 하나면 type-label-anchor, 여럿이면 label_ordinal번째.

    후보 수가 녹화 때(label_count)와 다르면 순번이 밀렸을 수 있어 건너뛴다.
    """
    candidates = [e for e in _same_type_anchor(root, evidence.element_type, evidence.anchor_identifier)
                  if e.label == evidence.label]
    if len(candidates) != evidence.label_count:
        return None
    if len(candidates) == 1:
        return Resolution(candidates[0], "weak", "type-label-anchor", 1)
    if 0 <= evidence.label_ordinal < len(candidates):
        return Resolution(candidates[evidence.label_ordinal], "weak", "label-ordinal", len(candidates))
    return None


def _resolve_by_position(root: Element, evidence: LocatorEvidence) -> Resolution | None:
    """4단계: label이 바뀐 경우를 위해 (종류, 앵커) 요소 중 position_ordinal번째를 고른다.

    목록 크기가 녹화 때(position_count)와 다르면 요소가 추가/삭제된 것이라 이웃을 잘못 고를 수 있어 건너뛴다.
    """
    candidates = _same_type_anchor(root, evidence.element_type, evidence.anchor_identifier)
    if len(candidates) != evidence.position_count:
        return None
    if 0 <= evidence.position_ordinal < len(candidates):
        return Resolution(candidates[evidence.position_ordinal], "weak", "position-ordinal", len(candidates))
    return None


def resolve(root: Element, evidence: LocatorEvidence) -> Resolution:
    """근거를 강한 순서로 시도해 요소를 찾는다. 모두 실패하면 strength/matched_by가 "none"이다.

    왜: identifier가 없거나 중복이어도 실패하지 않고, 대신 얼마나 약한 근거였는지 호출자가 알게 한다.
    """
    for step in (_resolve_by_identifier, _resolve_by_label, _resolve_by_position):
        result = step(root, evidence)
        if result is not None:
            return result
    return Resolution(None, "none", "none", 0)
