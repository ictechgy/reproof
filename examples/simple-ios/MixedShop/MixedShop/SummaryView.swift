import SwiftUI

/// 순수 SwiftUI 요약 탭. 접근성 ID 없음.
struct SummaryView: View {
    /// 새로고침 횟수.
    @State private var refreshCount = 0

    /// 제목, 새로고침 횟수, 새로고침 버튼을 보여준다.
    var body: some View {
        VStack(spacing: 16) {
            Text("주문 요약")
            Text("새로고침 \(refreshCount)회")
            Button("새로고침") { refreshCount += 1 }
        }
    }
}
