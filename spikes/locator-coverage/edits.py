"""Spike S1(버림용, 테스트 없음): MixedShop 사본에 'AI 수정' 흉내 변경을 적용한 변형 소스를 만든다.

examples/simple-ios/MixedShop은 절대 고치지 않는다. 사본은 ~/.reproof/spikes/locator-coverage/sources/<변형>/MixedShop에
만들고, 매번 지우고 새로 복사한다. 변경은 정확한 문자열 치환이며, 기준 문자열이 정확히 한 번 나오지 않으면 바로 멈춘다
(조용히 변경이 빠진 빌드를 측정하지 않으려는 것).

변형:
- baseline: 변경 없음(비교 기준)
- edit-above: 대상 요소보다 위에 20줄짜리 코드 추가(화면에는 안 보이는 도우미 코드)
- edit-other: 같은 화면의 다른 컨트롤 로직 변경
- edit-action: 대상 버튼의 action 내용 변경
- edit-all: 위 세 가지를 모두 적용(참고)
- ui-insert: 대상 요소 위에 보이는 버튼을 하나 추가(참고, 결정 규칙 밖)

대상 요소: CartViewController는 "담기" 버튼, OptionsView는 상품 행의 "추가" 버튼.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIXEDSHOP = REPO / "examples" / "simple-ios" / "MixedShop"
SPIKE_HOME = Path.home() / ".reproof" / "spikes" / "locator-coverage"
CART = "MixedShop/CartViewController.swift"
OPTIONS = "MixedShop/OptionsView.swift"

# 종류 1: 대상 요소("담기") 정의보다 위에 넣는 20줄. 아직 호출하지 않아 화면은 그대로다.
CART_ABOVE_LINES = """\
    /// 담기 가능 여부를 검사한다. 최대 수량을 넘으면 담지 않는다(spike: AI 수정 흉내, 20줄).
    private func canAddMoreItems() -> Bool {
        let maximumItemCount = 99
        guard cart.count < maximumItemCount else {
            return false
        }
        return true
    }

    /// 수량 문구를 만든다. 큰 수는 천 단위 구분 기호를 넣는다.
    private func countDescription(for count: Int) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        guard let formatted = formatter.string(from: NSNumber(value: count)) else {
            return "담은 수 \\(count)"
        }
        return "담은 수 \\(formatted)"
    }
    // spike: 위 두 함수는 아직 호출하지 않는다(화면은 그대로).

"""

# 종류 1: OptionsView body(대상 "추가" 버튼이 들어 있음)보다 위에 넣는 20줄.
OPTIONS_ABOVE_LINES = """\
    /// 상품별 단가(원). spike: AI 수정 흉내로 넣은 20줄이며 화면에는 아직 쓰지 않는다.
    private let unitPrices: [String: Int] = [
        "사과": 1200,
        "배": 2500,
        "귤": 800,
    ]

    private var giftWrapFee: Int {
        isGiftWrapped ? 3000 : 0
    }

    /// 담은 상품의 합계 금액을 계산한다(선물 포장비 포함).
    private func totalPrice(for names: [String]) -> Int {
        var total = giftWrapFee
        for name in names {
            total += unitPrices[name] ?? 0
        }
        return total
    }

"""

# (파일, 기준 문자열, 바꿀 문자열) 목록. 종류별로 두 파일에 하나씩 적용한다.
EDIT_KINDS = {
    "above": [
        (CART, '    /// "담기" 버튼(ID 없음).\n', CART_ABOVE_LINES + '    /// "담기" 버튼(ID 없음).\n'),
        (OPTIONS, "    /// 토글, 메모, 상품 3행, 합계를 보여준다.\n",
         OPTIONS_ABOVE_LINES + "    /// 토글, 메모, 상품 3행, 합계를 보여준다.\n"),
    ],
    "other": [
        (CART, "    @objc private func clearTapped() {\n        cart.clear()\n        refreshCountLabel()\n    }\n",
         "    @objc private func clearTapped() {\n        guard cart.count > 0 else {\n            return\n        }\n"
         "        cart.clear()\n        refreshCountLabel()\n        view.setNeedsLayout()\n    }\n"),
        (OPTIONS, '            Toggle("선물 포장", isOn: $isGiftWrapped)\n',
         '            Toggle("선물 포장", isOn: $isGiftWrapped)\n'
         "                .onChange(of: isGiftWrapped) { _, isOn in\n"
         "                    if !isOn {\n                        memo = \"\"\n                    }\n                }\n"),
    ],
    "action": [
        (CART, "    @objc private func addTapped() {\n        cart.add()\n        refreshCountLabel()\n    }\n",
         "    @objc private func addTapped() {\n        let maximumItemCount = 99\n"
         "        guard cart.count < maximumItemCount else {\n            return\n        }\n"
         "        cart.add()\n        refreshCountLabel()\n    }\n"),
        (OPTIONS, '                    Button("추가") { count += 1 }\n',
         '                    Button("추가") {\n                        guard count < 99 else {\n'
         "                            return\n                        }\n                        count += 1\n"
         "                    }\n"),
    ],
    "ui-insert": [
        (CART, "[countLabel, makeAddButton(),", "[countLabel, makeButton(title: \"쿠폰\", action: #selector(addTapped)), "
         "makeAddButton(),"),
        (OPTIONS, "            ForEach(productNames, id: \\.self) { name in\n",
         '            Button("쿠폰 적용") { count += 0 }\n            ForEach(productNames, id: \\.self) { name in\n'),
    ],
}

VARIANTS = {
    "baseline": [],
    "edit-above": ["above"],
    "edit-other": ["other"],
    "edit-action": ["action"],
    "edit-all": ["above", "other", "action"],
    "ui-insert": ["ui-insert"],
}


def apply_edit(source_root: Path, relative: str, anchor: str, replacement: str) -> None:
    """source_root/relative에서 anchor를 replacement로 한 번 바꾼다.

    anchor가 정확히 한 번 나오지 않으면 RuntimeError. 왜: 기준이 어긋나 변경이 빠진 빌드를 '유지됨'으로
    잘못 세지 않게 한다.
    """
    path = source_root / relative
    text = path.read_text(encoding="utf-8")
    occurrences = text.count(anchor)
    if occurrences != 1:
        raise RuntimeError(f"{relative}: 기준 문자열이 {occurrences}번 나옵니다(1번이어야 함): {anchor!r}")
    path.write_text(text.replace(anchor, replacement), encoding="utf-8")


def make_variant(name: str, spike_home: Path) -> Path:
    """변형 하나의 소스 사본을 새로 만들고 MixedShop 폴더 경로를 돌려준다."""
    target = spike_home / "sources" / name / "MixedShop"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(MIXEDSHOP, target, ignore=shutil.ignore_patterns("*.xcodeproj", "DerivedData", ".build"))
    for kind in VARIANTS[name]:
        for relative, anchor, replacement in EDIT_KINDS[kind]:
            apply_edit(target, relative, anchor, replacement)
    return target


def main(argv: list[str]) -> int:
    """명령줄: edits.py [변형 이름...]. 이름이 없으면 모든 변형을 만든다."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("variants", nargs="*", help=f"변형 이름: {', '.join(VARIANTS)}")
    parser.add_argument("--spike-home", type=Path, default=SPIKE_HOME)
    arguments = parser.parse_args(argv)
    unknown = sorted(set(arguments.variants) - set(VARIANTS))
    if unknown:
        parser.error(f"모르는 변형: {', '.join(unknown)}")
    for name in arguments.variants or VARIANTS:
        print(f"{name}: {make_variant(name, arguments.spike_home.expanduser())}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
