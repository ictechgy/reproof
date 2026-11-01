import SwiftUI
import UIKit

/// 윈도우와 루트 탭 구성을 만든다. 탭 1은 UIKit, 탭 2는 순수 SwiftUI 루트다.
final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    /// 앱 윈도우.
    var window: UIWindow?

    /// 씬이 연결될 때 탭 바 컨트롤러를 루트로 설정한다.
    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: windowScene)
        window.rootViewController = makeTabBarController()
        window.makeKeyAndVisible()
        self.window = window
    }

    /// "장바구니"(UIKit)와 "요약"(SwiftUI) 탭을 가진 탭 바 컨트롤러를 만든다.
    private func makeTabBarController() -> UITabBarController {
        let cartNavigation = UINavigationController(rootViewController: CartViewController())
        cartNavigation.tabBarItem = UITabBarItem(title: "장바구니", image: UIImage(systemName: "cart"), tag: 0)
        let summary = UIHostingController(rootView: SummaryView())
        summary.tabBarItem = UITabBarItem(title: "요약", image: UIImage(systemName: "list.bullet"), tag: 1)
        let tabBarController = UITabBarController()
        tabBarController.viewControllers = [cartNavigation, summary]
        return tabBarController
    }
}
