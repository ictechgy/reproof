import Foundation

/// Policy-declared identity, not installed-build attestation.
public struct ApplicationMetadata {
    public let id: String
    public init(id: String) { self.id = id }
}

public struct FixtureMetadata: Encodable {
    public let id: String
    public let version: Int
    public let inputs: [String: String] = [:]
    public init(id: String, version: Int) {
        self.id = id; self.version = version
    }
}

public struct StartState: Encodable {
    public let screen: String
    public let nodes: [String: String]
    public init(screen: String, nodes: [String: String]) {
        self.screen = screen; self.nodes = nodes
    }
}

public enum ScrollDirection: String, Codable { case forward, backward }

public struct ScrollRule: Hashable {
    public let target: String
    public let container: String
    public let direction: ScrollDirection
    public init(target: String, container: String, direction: ScrollDirection = .forward) {
        self.target = target; self.container = container; self.direction = direction
    }
}

/// Only explicitly declared, bounded synthetic/public values may be recorded.
/// Do not populate these allowlists from user input. Empty defaults deny all values.
public struct CapturePolicy {
    public let screens: Set<String>
    public let startNodeValues: [String: Set<String>]
    public let tapTargets: Set<String>
    public let replacementValues: [String: Set<String>]
    public let scrollRules: Set<ScrollRule>
    public let backTargets: Set<String>

    public init(screens: Set<String> = [], startNodeValues: [String: Set<String>] = [:],
                tapTargets: Set<String> = [],
                replacementValues: [String: Set<String>] = [:], scrollRules: Set<ScrollRule> = [],
                backTargets: Set<String> = []) {
        self.screens = screens; self.startNodeValues = startNodeValues
        self.tapTargets = tapTargets
        self.replacementValues = replacementValues; self.scrollRules = scrollRules
        self.backTargets = backTargets
    }
}

public struct CaptureLimits {
    public let maxEvents: Int
    public let maxDurationMs: Int
    public let maxBytes: Int
    public init(maxEvents: Int = 256, maxDurationMs: Int = 600_000, maxBytes: Int = 20 * 1024 * 1024) {
        self.maxEvents = maxEvents; self.maxDurationMs = maxDurationMs; self.maxBytes = maxBytes
    }
}

public struct RecorderConfiguration {
    public let rootDirectory: URL
    public let application: ApplicationMetadata
    public let fixture: FixtureMetadata
    public let startState: StartState
    public let policy: CapturePolicy
    public let limits: CaptureLimits
    public init(rootDirectory: URL, application: ApplicationMetadata, fixture: FixtureMetadata,
                startState: StartState, policy: CapturePolicy, limits: CaptureLimits = CaptureLimits()) {
        self.rootDirectory = rootDirectory; self.application = application; self.fixture = fixture
        self.startState = startState; self.policy = policy; self.limits = limits
    }
}

// Validate lengths before lookups, hashing, copying into commands, UUIDs, clocks or I/O.
// ASCII avoids normalization aliases: membership is an exact byte policy, not a pattern.
internal enum Admission {
    static func ascii(_ value: String, max: Int, identifier: Bool = false) -> Bool {
        var count = 0
        for byte in value.utf8 {
            count += 1
            if count > max { return false }
            if identifier {
                if !((65...90).contains(byte) || (97...122).contains(byte) ||
                     (48...57).contains(byte) || byte == 45 || byte == 46 || byte == 95) { return false }
            } else if !(32...126).contains(byte) { return false }
        }
        return !identifier || count > 0
    }
    static func identifier(_ value: String) -> Bool { ascii(value, max: 128, identifier: true) }
    static func value(_ value: String) -> Bool { ascii(value, max: 256) }
    static func identifiers(_ values: Set<String>) -> Bool {
        values.count <= 32 && values.allSatisfy(identifier)
    }
    static func values(_ values: [String: Set<String>]) -> Bool {
        values.count <= 32 && values.allSatisfy { key, allowed in
            identifier(key) && !allowed.isEmpty && allowed.count <= 32 && allowed.allSatisfy(value)
        }
    }
    static func fields(_ fields: [String: String], allowed: [String: Set<String>]) -> Bool {
        fields.count <= 32 && fields.allSatisfy { key, value in
            identifier(key) && self.value(value) && allowed[key]?.contains(value) == true
        }
    }
    static func configuration(_ c: RecorderConfiguration) -> Bool {
        let p = c.policy
        guard c.rootDirectory.isFileURL, c.rootDirectory.host == nil || c.rootDirectory.host == "" || c.rootDirectory.host == "localhost",
              c.rootDirectory.query == nil, c.rootDirectory.fragment == nil,
              c.rootDirectory.user == nil, c.rootDirectory.password == nil,
              ascii(c.rootDirectory.path, max: 4096), c.rootDirectory.path.hasPrefix("/"),
              identifier(c.application.id), identifier(c.fixture.id), (1...1_000_000).contains(c.fixture.version),
              (1...256).contains(c.limits.maxEvents), (1...600_000).contains(c.limits.maxDurationMs),
              (524_288...20 * 1024 * 1024).contains(c.limits.maxBytes),
              identifiers(p.screens), identifiers(p.tapTargets), identifiers(p.backTargets),
              values(p.startNodeValues), values(p.replacementValues),
              p.scrollRules.count <= 32,
              p.scrollRules.allSatisfy({ identifier($0.target) && identifier($0.container) }),
              identifier(c.startState.screen), p.screens.contains(c.startState.screen),
              c.startState.nodes.count == p.startNodeValues.count,
              fields(c.startState.nodes, allowed: p.startNodeValues),
              c.fixture.inputs.isEmpty else { return false }
        return true
    }
}
