import SwiftUI

/// UIKit 내비게이션에 끼워 넣는 SwiftUI 옵션 화면. 접근성 ID 없음.
struct OptionsView: View {
    /// 상품 이름 3개.
    private let productNames = ["사과", "배", "귤"]
    /// 선물 포장 여부.
    @State private var isGiftWrapped = false
    /// 메모 입력값.
    @State private var memo = ""
    /// 추가한 총 개수.
    @State private var count = 0

    /// 토글, 메모, 상품 3행, 합계를 보여준다.
    var body: some View {
        Form {
            Toggle("선물 포장", isOn: $isGiftWrapped)
            TextField("메모", text: $memo)
            ForEach(productNames, id: \.self) { name in
                HStack {
                    Text(name)
                    Spacer()
                    Button("추가") { count += 1 }
                        .buttonStyle(.borderless)
                }
            }
            Text("합계 \(count)")
        }
    }
}
