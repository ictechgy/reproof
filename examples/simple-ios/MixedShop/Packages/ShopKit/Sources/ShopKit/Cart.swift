/// 장바구니 로직. 담은 수만 센다. 화면은 이 타입을 통해서만 수량을 바꾼다.
public final class Cart {
    /// 현재 담은 수.
    public private(set) var count: Int = 0

    /// 빈 장바구니를 만든다.
    public init() {}

    /// 한 개를 담는다.
    public func add() {
        count += 1
    }

    /// 모두 비운다.
    public func clear() {
        count = 0
    }
}
