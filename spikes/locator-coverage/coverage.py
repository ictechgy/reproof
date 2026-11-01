"""Spike S1(버림용, 테스트 없음): 같은 화면의 두 계층(A, B)에서 `reproof.simple.locator`의 해석 결과를 세어 표로 낸다.

방법: A의 요소마다 capture_evidence(A, 요소) → resolve(B, 근거)를 돌리고, 결과를 "B의 정답 요소"와 비교한다.
정답 요소(counterpart)는 locator와 별개로 다음 순서로 정한다(먼저 맞는 규칙을 쓴다).
  1. identifier: A에서 유일한 identifier가 B에서도 같은 종류로 유일하면 그 요소.
  2. signature: 서명 = (종류, identifier, label, 행 글자). 행 글자는 요소 자신 또는 가장 가까운 조상 cell 안의
     staticText label들이다(cell 밖이면 비어 있음). 같은 서명 요소 수가 A와 B에서 같으면 A에서의 순번과 같은
     B 요소(대부분 하나뿐이라 순번이 필요 없다. 둘 이상이면 "signature-ordinal"로 따로 표시).
  3. frame: 같은 서명이면서 frame까지 같은 B 요소가 하나면 그 요소.
  4. path: B의 같은 path 요소가 (종류, identifier, label)까지 같으면 그 요소(행 글자만 바뀐 경우).
  어느 것도 아니면 "B에 같은 요소 없음"이다(실행마다 바뀌는 네트워크 콘텐츠, label이 바뀐 요소 등).
  왜 path를 맨 뒤에 두나: 목록에 행이 끼어들면 같은 path에 다른 행의 같은 label 버튼이 와서 정답을 잘못 고른다.
분류: 정답이 있고 해석 결과의 path가 같으면 matched_by로 센다(identifier/auto-identifier는 strong). 정답이 있는데 못
찾으면 "실패", 다른 path를 고르면 "잘못 해석"이다. 정답이 없을 때 못 찾으면 "B에 없음"(올바른 동작)이고, 같은
path·종류(label만 바뀐 자리)를 고르면 "B에 없음(같은 자리)"로 따로 표시하며, 그 밖의 요소를 고르면(path와 label이
모두 다름) "잘못 해석"이다.

입력: ~/.reproof/spikes/locator-coverage/dumps/<앱>/<변형>/<run>/<화면>.json (collect.py가 만든다)
출력: 표준 출력에 Markdown 표, ~/.reproof/spikes/locator-coverage/coverage-details.json에 요소별 기록.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from reproof.simple.hierarchy import (INTERACTIVE_TYPES, Element, ancestors_of, iter_elements,  # noqa: E402
                                      parse_hierarchy)
from reproof.simple.locator import capture_evidence, resolve  # noqa: E402

SPIKE_HOME = Path.home() / ".reproof" / "spikes" / "locator-coverage"
STRONG_KINDS = {"identifier", "auto-identifier"}
COLUMNS = ["strong", "type-label-anchor", "label-ordinal", "position-ordinal", "fail", "wrong", "absent"]
HEADERS = ["strong", "type-label-anchor", "label-ordinal", "position-ordinal", "실패", "잘못 해석", "B에 없음"]
# 실행마다 네트워크에서 다른 내용을 받아 그리는 화면. 이 분류는 측정 결과(잘못 해석 12건)를 본 뒤에 정한 사후 분류다.
# 고정 규칙은 문자 그대로 이 화면들을 포함한 합계로 판정하고, 이 화면을 뺀 합계는 참고로만 보인다(결과 문서 S1 판정).
NETWORK_SCREENS = {("wikipedia", "onboarding-interests"), ("wikipedia", "home")}
# SwiftUI로 그린 화면(소스 확인: MixedShop OptionsView·SummaryView, Wikipedia WMFAppOnboardingView·
# WMFProfileHostingController·WMFSettingsView·WMFSearchSettingsView·WMFStorageAndSyncingSettingsView). 소계에 쓴다.
SWIFTUI_SCREENS = {("mixedshop", "options"), ("mixedshop", "summary"), ("wikipedia", "onboarding-intro"),
                   ("wikipedia", "onboarding-privacy"), ("wikipedia", "onboarding-languages"),
                   ("wikipedia", "onboarding-personalization"), ("wikipedia", "onboarding-interests"),
                   ("wikipedia", "profile"), ("wikipedia", "settings"), ("wikipedia", "settings-search"),
                   ("wikipedia", "settings-storage")}
KINDS = {"interactive": lambda e: e.element_type in INTERACTIVE_TYPES,
         "staticText": lambda e: e.element_type == "staticText"}


def load_root(path: Path) -> Element:
    """collect.py가 저장한 실행기 문서에서 루트 요소를 읽는다."""
    return parse_hierarchy(json.loads(path.read_text(encoding="utf-8"))["root"])


def content_key(element: Element) -> tuple[str, str, str]:
    """정답 찾기에 쓰는 (종류, identifier, label)."""
    return (element.element_type, element.identifier, element.label)


def row_text(root: Element, element: Element) -> tuple[str, ...]:
    """element 자신 또는 가장 가까운 조상 cell 안의 staticText label들. cell 밖이면 ().

    왜: 목록 행마다 같은 label("추가")을 가진 버튼은 행의 글자("사과")로만 구분된다. 행이 끼어들어 path가 밀려도
    같은 행을 찾게 하려는 것이다.
    """
    cell = next((e for e in (element, *ancestors_of(root, element)) if e.element_type == "cell"), None)
    if cell is None:
        return ()
    return tuple(e.label for e in iter_elements(cell) if e.element_type == "staticText")


class Screen:
    """한 계층의 요소 목록, path 색인, 요소별 서명. 정답 찾기에서 여러 번 훑지 않으려고 미리 만든다."""

    def __init__(self, root: Element):
        """root: 계층 루트. 서명 = (종류, identifier, label, 행 글자)."""
        self.root = root
        self.elements = list(iter_elements(root))
        self.by_path = {element.path: element for element in self.elements}
        self.signatures = {element.path: (*content_key(element), row_text(root, element))
                           for element in self.elements}

    def same_signature(self, signature: tuple) -> list[Element]:
        """서명이 같은 요소 목록(pre-order)."""
        return [e for e in self.elements if self.signatures[e.path] == signature]


def find_counterpart(screen_a: Screen, element: Element, screen_b: Screen) -> tuple[Element | None, str]:
    """B에서 A의 element와 같은 요소를 모듈 설명의 규칙 1–4로 찾는다. (요소 또는 None, 쓴 규칙)."""
    if element.identifier:
        ids_a = [e for e in screen_a.elements if e.identifier == element.identifier]
        ids_b = [e for e in screen_b.elements
                 if e.identifier == element.identifier and e.element_type == element.element_type]
        if len(ids_a) == 1 and len(ids_b) == 1:
            return ids_b[0], "identifier"
    signature = screen_a.signatures[element.path]
    group_a, group_b = screen_a.same_signature(signature), screen_b.same_signature(signature)
    if group_b and len(group_a) == len(group_b):
        index = next(i for i, e in enumerate(group_a) if e.path == element.path)
        return group_b[index], "signature" if len(group_b) == 1 else "signature-ordinal"
    same_frame = [e for e in group_b if e.frame == element.frame]
    if len(same_frame) == 1:
        return same_frame[0], "frame"
    same_path = screen_b.by_path.get(element.path)
    if same_path is not None and content_key(same_path) == content_key(element):
        return same_path, "path"
    return None, "none"


def classify(screen_a: Screen, element: Element, screen_b: Screen) -> dict:
    """A의 요소 하나를 B에서 해석해 분류 기록(category, matched_by, 정답 규칙 등)을 돌려준다."""
    resolution = resolve(screen_b.root, capture_evidence(screen_a.root, element))
    counterpart, rule = find_counterpart(screen_a, element, screen_b)
    found = resolution.element
    record = {"type": element.element_type, "identifier": element.identifier, "label": element.label,
              "path": list(element.path), "matched_by": resolution.matched_by, "counterpart_rule": rule,
              "resolved_path": list(found.path) if found else None,
              "resolved_label": found.label if found else None}
    record["category"] = category_of(element, found, counterpart, resolution.matched_by, screen_b, record)
    return record


def category_of(element: Element, found: Element | None, counterpart: Element | None, matched_by: str,
                screen_b: Screen, record: dict) -> str:
    """분류 이름을 정한다. 정답이 없을 때 같은 자리를 골랐는지는 record["absent_slot"]에 남긴다."""
    if counterpart is not None:
        if found is None:
            return "fail"
        if found.path != counterpart.path:
            return "wrong"
        return "strong" if matched_by in STRONG_KINDS else matched_by
    if found is None:
        return "absent"
    slot = screen_b.by_path.get(element.path)
    if slot is not None and found.path == slot.path and slot.element_type == element.element_type:
        record["absent_slot"] = True
        return "absent"
    return "wrong"


def compare(path_a: Path, path_b: Path, kind: str) -> list[dict]:
    """두 계층 파일에서 kind(interactive/staticText) 요소를 모두 분류한다."""
    screen_a, screen_b = Screen(load_root(path_a)), Screen(load_root(path_b))
    return [classify(screen_a, element, screen_b) for element in screen_a.elements if KINDS[kind](element)]


def percent(count: int, total: int) -> str:
    """"n (p%)" 형식. total이 0이면 "0"."""
    return f"{count} ({100 * count / total:.1f}%)" if total else "0"


def summary_row(name: str, records: list[dict]) -> list[str]:
    """표 한 줄: 이름, 요소 수, 분류별 수(비율), 빈 label로 찾은 수, 성공 비율(strong+tla+lo)."""
    counts = Counter(record["category"] for record in records)
    total = len(records)
    empty = sum(1 for r in records if r["category"] in ("type-label-anchor", "label-ordinal") and not r["label"])
    slots = sum(1 for r in records if r.get("absent_slot"))
    cells = [percent(counts[column], total) for column in COLUMNS]
    if slots:
        cells[-1] += f" (같은 자리 {slots})"
    good = counts["strong"] + counts["type-label-anchor"] + counts["label-ordinal"]
    return [name, str(total), *cells, str(empty), percent(good, total)]


def markdown_table(rows: list[list[str]], first: str) -> str:
    """행 목록을 Markdown 표로 만든다."""
    header = [first, "요소", *HEADERS, "빈 label로 찾음", "strong+tla+lo"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return "\n".join([*lines, *("| " + " | ".join(row) + " |" for row in rows)])


def screen_pairs(dumps: Path, app: str, variant_a: str, run_a: str, variant_b: str, run_b: str):
    """두 run 폴더에 모두 있는 화면 이름과 (A 파일, B 파일)을 collect 순서(meta.json)대로 돌려준다."""
    folder_a, folder_b = dumps / app / variant_a / run_a, dumps / app / variant_b / run_b
    meta = json.loads((folder_a / "meta.json").read_text(encoding="utf-8"))
    names = [step["target"] for step in meta["steps"] if step["step"] == "dump"]
    return [(name, folder_a / f"{name}.json", folder_b / f"{name}.json") for name in names
            if (folder_b / f"{name}.json").exists()]


def stability_tables(dumps: Path, details: dict) -> list[str]:
    """앱별 baseline run-a → run-b 안정성 표(interactive, staticText)와 합계를 만든다."""
    output = []
    for kind in KINDS:
        rows, local, without_home, everything, swiftui = [], [], [], [], []
        for app in ("mixedshop", "wikipedia"):
            if not (dumps / app / "baseline" / "run-b").is_dir():
                continue
            for name, path_a, path_b in screen_pairs(dumps, app, "baseline", "run-a", "baseline", "run-b"):
                records = compare(path_a, path_b, kind)
                details[f"stability/{kind}/{app}/{name}"] = records
                label = f"{app}/{name}" + (" (네트워크)" if (app, name) in NETWORK_SCREENS else "")
                rows.append(summary_row(label, records))
                everything += records
                local += [] if (app, name) in NETWORK_SCREENS else records
                without_home += [] if (app, name) == ("wikipedia", "home") else records
                is_local_swiftui = (app, name) in SWIFTUI_SCREENS and (app, name) not in NETWORK_SCREENS
                swiftui += records if is_local_swiftui else []
        rows.append(summary_row("**합계(규칙 그대로: 모든 화면)**", everything))
        rows.append(summary_row("합계(홈만 제외, 온보딩 관심사 포함)", without_home))
        rows.append(summary_row("합계(네트워크 화면 2개 사후 제외, 참고)", local))
        rows.append(summary_row("소계(그중 SwiftUI 화면)", swiftui))
        output.append(f"### 실행 간 안정성 — {kind}\n\n" + markdown_table(rows, "화면"))
    return output


def traits_table(details: dict) -> str:
    """baseline run-a interactive 요소의 근거 재료 표: identifier 있음 / label만 있음 / 둘 다 없음."""
    rows = []
    for key, records in details.items():
        if key.startswith("stability/interactive/"):
            with_id = sum(1 for r in records if r["identifier"])
            label_only = sum(1 for r in records if not r["identifier"] and r["label"])
            neither = len(records) - with_id - label_only
            rows.append([key.split("/", 2)[2], str(len(records)), percent(with_id, len(records)),
                         percent(label_only, len(records)), percent(neither, len(records))])
    header = ["| 화면 | interactive | identifier 있음 | label만 있음 | identifier·label 모두 없음 |", "|---|---|---|---|---|"]
    return "### 요소의 근거 재료 — interactive (run-a)\n\n" + "\n".join(
        [*header, *("| " + " | ".join(row) + " |" for row in rows)])


def retention_table(dumps: Path, details: dict) -> str:
    """MixedShop baseline/run-a 근거를 변형별 run-a 계층에서 해석한 유지율 표를 만든다."""
    rows = []
    root = dumps / "mixedshop"
    finished = [path.name for path in root.iterdir() if (path / "run-a" / "meta.json").exists()]
    for variant in sorted(name for name in finished if name != "baseline"):
        records = []
        for name, path_a, path_b in screen_pairs(dumps, "mixedshop", "baseline", "run-a", variant, "run-a"):
            screen_records = compare(path_a, path_b, "interactive")
            details[f"retention/{variant}/{name}"] = screen_records
            records += screen_records
        # 남은 요소 = B에 정답 요소가 있는 것(잘못 해석 중 정답이 없는 경우도 분모에서 뺀다).
        existing = [r for r in records if r["counterpart_rule"] != "none"]
        kept = sum(1 for r in existing if r["category"] not in ("fail", "wrong"))
        rows.append([*summary_row(variant, records), percent(kept, len(existing))])
    header = markdown_table([], "변형").splitlines()
    header[0] += " 유지율(맞게 찾음/남은 요소) |"
    header[1] += "---|"
    return "### AI 수정 뒤 유지율 — interactive\n\n" + "\n".join(
        [*header, *("| " + " | ".join(row) + " |" for row in rows)])


def main(argv: list[str]) -> int:
    """명령줄: coverage.py [--spike-home DIR]. 표를 출력하고 요소별 기록을 JSON으로 저장한다."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spike-home", type=Path, default=SPIKE_HOME)
    spike_home = parser.parse_args(argv).spike_home.expanduser()
    dumps, details = spike_home / "dumps", {}
    sections = stability_tables(dumps, details)
    sections.append(traits_table(details))
    if (dumps / "mixedshop").is_dir() and len(list((dumps / "mixedshop").iterdir())) > 1:
        sections.append(retention_table(dumps, details))
    print("\n\n".join(sections))
    (spike_home / "coverage-details.json").write_text(json.dumps(details, ensure_ascii=False, indent=1),
                                                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
