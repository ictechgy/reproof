import XCTest
@testable import ReproSample

final class CounterLogicTests: XCTestCase {
    func testIncrementIsOne() {
        XCTAssertEqual(CounterLogic.increment(), 1)
    }
}

final class SubmissionLogicTests: XCTestCase {
    func testDuplicateIsRejected() {
        XCTAssertTrue(SubmissionLogic.shouldAccept(submitted: false))
        XCTAssertFalse(SubmissionLogic.shouldAccept(submitted: true))
    }
}

final class ResetLogicTests: XCTestCase {
    func testResetClearsValue() {
        XCTAssertEqual(ResetLogic.resetValue(previous: 1), 0)
        XCTAssertEqual(ResetLogic.resetValue(previous: 7), 0)
    }
}
