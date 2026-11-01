import XCTest
@testable import ShopKit

/// Cart 단위 테스트.
final class CartTests: XCTestCase {
    /// 담으면 수가 늘고 비우면 0이 된다.
    func testAddAndClear() {
        let cart = Cart()
        cart.add()
        cart.add()
        XCTAssertEqual(cart.count, 2)
        cart.clear()
        XCTAssertEqual(cart.count, 0)
    }
}
