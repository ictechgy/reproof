import UIKit

// Independently authored, offline CI fixture. No Reproof SDK calls, network,
// accounts, persistence, reset hook, or product-counter implementation.
@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        return true
    }

    func application(_ application: UIApplication, configurationForConnecting session: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let configuration = UISceneConfiguration(name: "Inventory", sessionRole: session.role)
        configuration.delegateClass = SceneDelegate.self
        return configuration
    }
}

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: windowScene)
        window.rootViewController = InventoryController()
        window.makeKeyAndVisible()
        self.window = window
    }
}

final class InventoryController: UIViewController {
    private enum Screen { case list, editor, detail }
    private var screen = Screen.list
    private var items: [String] = []
    private var selectedItem = ""
    private var placements: [(UIView, CGFloat, CGFloat)] = []
    private var nameField: UITextField?
    private var errorLabel: UILabel?

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .white
        render()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        for (element, centerY, height) in placements {
            element.frame = CGRect(x: 24, y: view.bounds.height * centerY - height / 2,
                                   width: view.bounds.width - 48, height: height)
        }
    }

    private func place(_ element: UIView, at centerY: CGFloat, height: CGFloat = 48) {
        view.addSubview(element)
        placements.append((element, centerY, height))
    }

    private func label(_ text: String, id: String, at centerY: CGFloat, size: CGFloat = 25) -> UILabel {
        let value = UILabel()
        value.text = text
        value.accessibilityIdentifier = id
        value.textColor = .black
        value.textAlignment = .center
        value.font = .systemFont(ofSize: size, weight: .semibold)
        value.adjustsFontSizeToFitWidth = true
        place(value, at: centerY)
        return value
    }

    private func button(_ title: String, id: String, at centerY: CGFloat, action: Selector) {
        let value = UIButton(type: .system)
        value.setTitle(title, for: .normal)
        value.titleLabel?.font = .systemFont(ofSize: 22, weight: .semibold)
        value.accessibilityIdentifier = id
        value.backgroundColor = UIColor(red: 0.88, green: 0.94, blue: 1, alpha: 1)
        value.layer.cornerRadius = 9
        value.addTarget(self, action: action, for: .touchUpInside)
        place(value, at: centerY, height: 52)
    }

    private func render() {
        view.endEditing(true)
        view.subviews.forEach { $0.removeFromSuperview() }
        placements.removeAll()
        nameField = nil
        errorLabel = nil
        let total = items.isEmpty ? "ZERO" : items.count == 1 ? "ONE" : String(items.count)
        _ = label("ITEM TOTAL " + total, id: "inventory.total", at: 0.22, size: 21)
        switch screen {
        case .list:
            view.accessibilityIdentifier = "inventory.list"
            _ = label("INVENTORY LIST", id: "inventory.title", at: 0.14)
            button("ADD ITEM", id: "inventory.add", at: 0.34, action: #selector(addItem))
            if items.isEmpty {
                _ = label("INVENTORY IS EMPTY", id: "inventory.empty", at: 0.49)
            } else {
                for (index, item) in items.prefix(3).enumerated() {
                    button(item, id: "inventory.item." + String(index),
                           at: 0.49 + CGFloat(index) * 0.09, action: #selector(openItem(_:)))
                }
            }
        case .editor:
            view.accessibilityIdentifier = "inventory.editor"
            _ = label("CREATE ITEM", id: "inventory.title", at: 0.14)
            let field = UITextField()
            field.accessibilityIdentifier = "inventory.name"
            field.placeholder = "ITEM NAME"
            field.borderStyle = .roundedRect
            field.font = .systemFont(ofSize: 22)
            field.textColor = .black
            field.backgroundColor = .white
            field.autocorrectionType = .no
            field.spellCheckingType = .no
            field.autocapitalizationType = .allCharacters
            field.returnKeyType = .done
            place(field, at: 0.32)
            nameField = field
            button("SAVE ITEM", id: "inventory.save", at: 0.44, action: #selector(saveItem))
            button("CANCEL", id: "inventory.cancel", at: 0.54, action: #selector(cancelItem))
            errorLabel = label("", id: "inventory.error", at: 0.64, size: 22)
        case .detail:
            view.accessibilityIdentifier = "inventory.detail"
            _ = label("ITEM DETAILS", id: "inventory.title", at: 0.14)
            _ = label(selectedItem, id: "inventory.detail.name", at: 0.32)
            button("BACK TO LIST", id: "inventory.back", at: 0.50, action: #selector(backToList))
        }
        view.setNeedsLayout()
    }

    @objc private func addItem() { screen = .editor; render() }
    @objc private func cancelItem() { screen = .list; render() }
    @objc private func backToList() { screen = .list; render() }

    @objc private func saveItem() {
        let name = (nameField?.text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else {
            errorLabel?.text = "NAME REQUIRED"
            return
        }
        items.append(name)
        screen = .list
        render()
    }

    @objc private func openItem(_ sender: UIButton) {
        guard let identifier = sender.accessibilityIdentifier,
              let index = Int(identifier.split(separator: ".").last ?? ""),
              items.indices.contains(index) else { return }
        selectedItem = items[index]
        screen = .detail
        render()
    }
}
