import UIKit

/// XCUITest 번들(SimpleRunnerTests)을 담기 위한 빈 호스트 앱.
///
/// 실행기는 이 앱을 띄우지 않고 `REPROOF_TARGET_BUNDLE`로 지정한 대상 앱을 구동한다.
/// 그래서 화면을 만들지 않고 시작만 성공으로 알린다.
@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    /// 시작 시 할 일이 없어 항상 true를 돌려준다.
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        true
    }
}
