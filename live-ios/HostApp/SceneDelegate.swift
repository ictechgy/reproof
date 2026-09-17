import UIKit

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(
        _ scene: UIScene,
        willConnectTo session: UISceneSession,
        options connectionOptions: UIScene.ConnectionOptions
    ) {
        guard let windowScene = scene as? UIWindowScene else { return }

        let window = UIWindow(windowScene: windowScene)
        let label = UILabel()
        label.text = "Repro Live host"
        label.font = .preferredFont(forTextStyle: .title2)
        label.textAlignment = .center
        label.translatesAutoresizingMaskIntoConstraints = false

        let viewController = UIViewController()
        viewController.view.backgroundColor = .systemBackground
        viewController.view.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: viewController.view.leadingAnchor, constant: 20),
            label.trailingAnchor.constraint(equalTo: viewController.view.trailingAnchor, constant: -20),
            label.centerYAnchor.constraint(equalTo: viewController.view.centerYAnchor)
        ])

        window.rootViewController = viewController
        window.makeKeyAndVisible()
        self.window = window
    }
}
