import Foundation

enum ReproCase: String {
    case counter
    case duplicateSubmit = "duplicate-submit"
    case reset

    static var current: ReproCase {
        guard let rawValue = ProcessInfo.processInfo.environment["REPRO_CASE"] else { return .counter }
        guard let value = ReproCase(rawValue: rawValue) else {
            preconditionFailure("Unsupported sample case")
        }
        return value
    }

    var fixtureID: String {
        switch self {
        case .counter:
            return "ios-counter"
        case .duplicateSubmit:
            return "ios-duplicate-submit"
        case .reset:
            return "ios-reset"
        }
    }
}
