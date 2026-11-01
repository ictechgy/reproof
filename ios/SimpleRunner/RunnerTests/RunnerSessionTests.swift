import XCTest

/// 실행 환경 변수에서 읽은 세션 설정.
///
/// Python은 `TEST_RUNNER_REPROOF_*`로 넘기고, xcodebuild가 접두사를 떼어 테스트 프로세스에 전달한다.
struct SessionConfiguration {
    /// 명령 서버 포트(1...65535). `REPROOF_RUNNER_PORT`.
    let port: UInt16
    /// 구동할 대상 앱의 bundle id. `REPROOF_TARGET_BUNDLE`.
    let targetBundle: String

    /// 두 환경 변수를 읽는다. 없거나 형식이 틀리면 nil.
    init?(environment: [String: String]) {
        guard let portText = environment["REPROOF_RUNNER_PORT"], let port = UInt16(portText), port > 0,
              let bundle = environment["REPROOF_TARGET_BUNDLE"], !bundle.isEmpty else {
            return nil
        }
        self.port = port
        self.targetBundle = bundle
    }
}

/// 명령 처리 실패. code는 Python에 `error`로 그대로 전달하는 짧은 문자열이다.
struct CommandFailure: Error {
    /// 실패 원인 코드(예: invalid_x, unknown_action: foo).
    let code: String
}

/// 범용 실행기 세션. `testSession` 하나가 stop을 받거나 6시간이 지날 때까지 명령을 처리한다.
///
/// 요소 찾기는 하지 않는다. 계층 내보내기와 좌표 입력만 하고, 판단은 모두 Python이 맡는다.
final class RunnerSessionTests: XCTestCase {
    /// 세션 최대 길이(6시간). 잊힌 세션이 Simulator를 계속 붙잡지 않게 한다.
    static let maxSessionSeconds: TimeInterval = 6 * 60 * 60
    /// type 명령 한 번의 최대 글자 수.
    static let maxTextLength = 256
    /// 좌표 값의 절댓값 상한(pt). 잘못된 거대한 값이 XCTest로 넘어가지 않게 한다.
    static let maxCoordinate = 100_000.0
    /// long_press·swipe 시간의 상한(초).
    static let maxDuration = 60.0

    /// 구동 대상 앱.
    private var application: XCUIApplication!
    /// 대상 앱 bundle id(status 응답용).
    private var targetBundle = ""
    /// stop을 받았는지.
    private var isStopRequested = false
    /// 현재 명령을 실행하는 동안 XCTest가 기록한 문제. 응답을 실패로 바꾸는 데 쓴다.
    private var commandIssues: [String] = []

    /// XCTest 문제를 원래대로 기록하면서 현재 명령의 실패 원인으로도 모은다.
    ///
    /// 왜: 탭·입력 실패는 Swift 오류가 아니라 XCTest 문제로만 드러나므로, 이렇게 모아야 Python이 알 수 있다.
    override func record(_ issue: XCTIssue) {
        commandIssues.append(issue.compactDescription)
        super.record(issue)
    }

    /// 세션 본체. 서버를 열고 명령을 메인 스레드에서 하나씩 처리한다.
    @MainActor
    func testSession() throws {
        continueAfterFailure = true  // 명령 하나의 실패가 세션 전체를 끝내지 않게 한다.
        let environment = ProcessInfo.processInfo.environment
        let configuration = try XCTUnwrap(SessionConfiguration(environment: environment),
                                          "REPROOF_RUNNER_PORT·REPROOF_TARGET_BUNDLE 환경 변수가 없거나 잘못됐습니다.")
        targetBundle = configuration.targetBundle
        application = XCUIApplication(bundleIdentifier: configuration.targetBundle)
        let server = try CommandServer(port: configuration.port)
        defer { server.stop() }
        guard server.waitUntilReady(timeout: 10) else {
            XCTFail("127.0.0.1:\(configuration.port) 리스너를 열지 못했습니다. 포트를 다른 프로그램이 쓰는지 확인하세요.")
            return
        }
        serve(server, until: Date().addingTimeInterval(Self.maxSessionSeconds))
        if !isStopRequested {
            XCTFail("stop 없이 세션이 끝났습니다(6시간 초과 또는 리스너 종료). 세션을 다시 시작하세요.")
        }
    }

    /// stop을 받거나, deadline이 지나거나, 리스너가 닫힐 때까지 명령을 꺼내 실행하고 응답한다.
    @MainActor
    private func serve(_ server: CommandServer, until deadline: Date) {
        while !isStopRequested && server.isListening && Date() < deadline {
            guard let command = server.next(timeout: 0.1) else { continue }
            command.reply(execute(command.body))
        }
    }

    /// 명령 하나를 실행해 응답 사전을 만든다. 오류와 XCTest 문제는 {"ok": false, "error": ...}로 바꾼다.
    @MainActor
    private func execute(_ body: [String: Any]) -> [String: Any] {
        commandIssues.removeAll()
        do {
            let result = try perform(body)
            guard commandIssues.isEmpty else {
                return failure("xctest_issue: " + commandIssues.joined(separator: " | "))
            }
            return result
        } catch let commandFailure as CommandFailure {
            return failure(commandFailure.code)
        } catch {
            return failure("\(type(of: error)): \(error.localizedDescription)")
        }
    }

    /// action에 맞는 동작을 실행한다. 응답에 값이 없는 명령은 {"ok": true}를 돌려준다.
    @MainActor
    private func perform(_ body: [String: Any]) throws -> [String: Any] {
        guard let action = body["action"] as? String else { throw CommandFailure(code: "invalid_action") }
        switch action {
        case "status":
            return ["ok": true, "targetBundle": targetBundle, "targetState": Self.stateName(application.state)]
        case "hierarchy":
            return ["ok": true, "root": HierarchyExport.node(from: try application.snapshot())]
        case "screenshot":
            return ["ok": true, "png": XCUIScreen.main.screenshot().pngRepresentation.base64EncodedString()]
        case "launch": try launch(body)
        case "terminate": application.terminate()
        case "activate": application.activate()
        case "tap": try coordinate(body, x: "x", y: "y").tap()
        case "long_press": try coordinate(body, x: "x", y: "y").press(forDuration: try duration(body))
        case "swipe": try swipe(body)
        case "type": try typeText(body)
        case "stop": isStopRequested = true
        default: throw CommandFailure(code: "unknown_action: \(action)")
        }
        return ["ok": true]
    }

    /// arguments([String])와 environment([String: String])로 대상 앱을 (다시) 실행한다.
    @MainActor
    private func launch(_ body: [String: Any]) throws {
        guard let arguments = body["arguments"] as? [String] else { throw CommandFailure(code: "invalid_arguments") }
        guard let environment = body["environment"] as? [String: String] else {
            throw CommandFailure(code: "invalid_environment")
        }
        application.launchArguments = arguments
        application.launchEnvironment = environment
        application.launch()
    }

    /// 앱 좌표계 포인트 (x, y)의 좌표 객체를 만든다(정규화 좌표가 아니다). 앱 원점에서 offset만큼 이동한다.
    @MainActor
    private func coordinate(_ body: [String: Any], x: String, y: String) throws -> XCUICoordinate {
        let offset = CGVector(dx: try number(body, x), dy: try number(body, y))
        return application.coordinate(withNormalizedOffset: .zero).withOffset(offset)
    }

    /// from → to로 duration초 동안 끄는 swipe. 속도는 거리/시간으로 정하며 실제 속도는 XCTest가 보장하지 않는다.
    @MainActor
    private func swipe(_ body: [String: Any]) throws {
        let start = try coordinate(body, x: "fromX", y: "fromY")
        let end = try coordinate(body, x: "toX", y: "toY")
        let distance = hypot(try number(body, "toX") - number(body, "fromX"),
                             try number(body, "toY") - number(body, "fromY"))
        let velocity = XCUIGestureVelocity(rawValue: CGFloat(max(distance / (try duration(body)), 1)))
        start.press(forDuration: 0.05, thenDragTo: end, withVelocity: velocity, thenHoldForDuration: 0)
    }

    /// 포커스된 입력 칸에 text(256자 이하)를 입력한다.
    @MainActor
    private func typeText(_ body: [String: Any]) throws {
        guard let text = body["text"] as? String, text.count <= Self.maxTextLength else {
            throw CommandFailure(code: "invalid_text")
        }
        application.typeText(text)
    }

    /// 유한한 숫자 값을 읽는다. Bool은 JSON에서 숫자로 섞여 들어오므로 거절한다.
    private func number(_ body: [String: Any], _ key: String) throws -> Double {
        guard let value = body[key] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
              value.doubleValue.isFinite, abs(value.doubleValue) <= Self.maxCoordinate else {
            throw CommandFailure(code: "invalid_\(key)")
        }
        return value.doubleValue
    }

    /// duration(초)을 읽는다. 0보다 크고 60 이하여야 한다.
    private func duration(_ body: [String: Any]) throws -> Double {
        let value = try number(body, "duration")
        guard value > 0, value <= Self.maxDuration else { throw CommandFailure(code: "invalid_duration") }
        return value
    }

    /// 실패 응답 사전을 만든다.
    private func failure(_ code: String) -> [String: Any] {
        ["ok": false, "error": code]
    }

    /// 앱 상태를 Python에 넘길 문자열로 바꾼다.
    private static func stateName(_ state: XCUIApplication.State) -> String {
        switch state {
        case .unknown: return "unknown"
        case .notRunning: return "notRunning"
        case .runningBackgroundSuspended: return "runningBackgroundSuspended"
        case .runningBackground: return "runningBackground"
        case .runningForeground: return "runningForeground"
        @unknown default: return "unknown"
        }
    }
}
