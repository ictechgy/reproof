import ShopKit
import SwiftUI
import UIKit

/// 순수 UIKit 장바구니 화면. 접근성 ID는 "비우기" 버튼에만 둔다(ID가 거의 없는 회사 앱을 흉내 낸다).
final class CartViewController: UIViewController {
    /// 장바구니 로직(ShopKit).
    private let cart = Cart()
    /// 담은 수를 보여주는 라벨.
    private let countLabel = UILabel()

    /// 화면을 구성한다.
    override func viewDidLoad() {
        super.viewDidLoad()
        title = "장바구니"
        view.backgroundColor = .systemBackground
        let stack = UIStackView(arrangedSubviews: [countLabel, makeAddButton(), makeClearButton(), makeOptionsButton()])
        stack.axis = .vertical
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor)
        ])
        refreshCountLabel()
    }

    /// "담기" 버튼(ID 없음).
    private func makeAddButton() -> UIButton {
        makeButton(title: "담기", action: #selector(addTapped))
    }

    /// "비우기" 버튼. 유일하게 accessibilityIdentifier를 가진다.
    private func makeClearButton() -> UIButton {
        let button = makeButton(title: "비우기", action: #selector(clearTapped))
        button.accessibilityIdentifier = "cart.clear"
        return button
    }

    /// "옵션" 버튼(ID 없음). SwiftUI 화면을 push한다.
    private func makeOptionsButton() -> UIButton {
        makeButton(title: "옵션", action: #selector(optionsTapped))
    }

    /// 제목과 동작이 있는 기본 버튼을 만든다.
    private func makeButton(title: String, action: Selector) -> UIButton {
        let button = UIButton(type: .system)
        button.setTitle(title, for: .normal)
        button.addTarget(self, action: action, for: .touchUpInside)
        return button
    }

    /// 라벨을 현재 수량으로 갱신한다.
    private func refreshCountLabel() {
        countLabel.text = "담은 수 \(cart.count)"
        countLabel.textAlignment = .center
    }

    /// 한 개 담고 라벨을 갱신한다.
    @objc private func addTapped() {
        cart.add()
        refreshCountLabel()
    }

    /// 비우고 라벨을 갱신한다.
    @objc private func clearTapped() {
        cart.clear()
        refreshCountLabel()
    }

    /// 옵션 화면(SwiftUI)을 push한다.
    @objc private func optionsTapped() {
        let options = UIHostingController(rootView: OptionsView())
        options.title = "옵션"
        navigationController?.pushViewController(options, animated: true)
    }
}
