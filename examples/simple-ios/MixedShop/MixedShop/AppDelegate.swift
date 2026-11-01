import UIKit

/// 앱 진입점. 화면 구성은 SceneDelegate가 맡는다.
@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    /// 시작 시 특별히 할 일이 없어 항상 true를 돌려준다.
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        true
    }
}
