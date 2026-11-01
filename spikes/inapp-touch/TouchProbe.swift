#if DEBUG
import Darwin
import QuartzCore
import UIKit

// Spike S2(버림용, 테스트 없음): 직접 터치 때 앱 안에서 SwiftUI 접근성 요소를 찾을 수 있는지 잰다.
// spikes/inapp-touch/apply.py가 MixedShop 작업 사본의 Debug 빌드에만 넣는다(examples/ 원본은 바꾸지 않는다).
// 기록 위치: 앱 데이터 컨테이너의 Documents/touch-probe.jsonl(한 줄에 JSON 객체 하나).
// 실행 환경 변수(모두 선택):
//   TOUCH_PROBE_ACTIVATE_AX=1   시작할 때 접근성 활성화 방법 1가지(_AXSSetAutomationEnabled)를 쓴다.
//   TOUCH_PROBE_SELFCHECK=<JSON> 터치 없이 화면을 직접 옮기고 주어진 좌표를 같은 방법으로 해석한다.

/// 터치 프로브의 진입점. ObjC `+load`(TouchProbeBootstrap.m)가 런타임 이름으로 찾아 `install`을 부른다.
///
/// 왜 ObjC 이름을 고정하나: Swift에는 `+load`가 없어, 앱 소스를 고치지 않고 시작 시점에 끼우려면 ObjC 쪽이
/// 이름으로 찾아야 한다(`reproof/ios_instrumentation_templates/RLAutoBootstrap.m`과 같은 방식).
@MainActor
@objc(MixedShopTouchProbe)
final class TouchProbe: NSObject {
    /// 두 번 설치하면 메서드 교체가 되돌려지므로 한 번만 설치하려는 표시.
    private static var isInstalled = false

    /// 접근성 활성화(요청 때만), sendEvent 교체, self-check 예약을 한 번만 한다. main 전에 main thread에서 불린다.
    @objc static func install() {
        guard !isInstalled else { return }
        isInstalled = true
        let activation = AccessibilityActivation.applyIfRequested()
        ProbeLog.append(["event": "probe-start", "activation": activation,
                         "sendEventSwizzled": swizzleSendEvent(), "axState": AccessibilityActivation.currentState()])
        SelfCheck.scheduleIfRequested()
    }

    /// `UIApplication.sendEvent(_:)`와 `touchProbe_sendEvent(_:)`의 구현을 맞바꾼다. 성공하면 true.
    private static func swizzleSendEvent() -> Bool {
        let original = class_getInstanceMethod(UIApplication.self, #selector(UIApplication.sendEvent(_:)))
        let replacement = class_getInstanceMethod(UIApplication.self, #selector(UIApplication.touchProbe_sendEvent(_:)))
        guard let original, let replacement else {
            NSLog("%@", "[TouchProbe] sendEvent 교체 실패: 메서드를 찾지 못했습니다. iOS SDK에서 이름이 바뀌었는지 확인하세요.")
            return false
        }
        method_exchangeImplementations(original, replacement)
        return true
    }

    /// 터치 이벤트에서 손을 뗀(.ended) 터치마다 요소를 찾아 기록한다.
    ///
    /// 원래 처리 전에 불린다. 왜: 버튼 동작이 라벨을 바꾸기 전 상태(실행기가 탭 직전에 읽은 계층과 같은 상태)를 보려는 것이다.
    static func observe(_ event: UIEvent) {
        guard event.type == .touches, let touches = event.allTouches else { return }
        for touch in touches where touch.phase == .ended {
            recordTouch(touch)
        }
    }

    /// 터치 하나의 window 좌표를 화면 좌표로 바꿔 key window부터 요소를 찾고 JSONL 한 줄로 남긴다.
    private static func recordTouch(_ touch: UITouch) {
        guard let window = touch.window else {
            ProbeLog.append(["event": "touch-error", "reason": "touch.window가 없습니다(창 밖 터치)."])
            return
        }
        let windowPoint = touch.location(in: window)
        let space: UICoordinateSpace = window.windowScene?.screen.coordinateSpace ?? window
        var entry = ElementFinder.resolveEntry(at: window.convert(windowPoint, to: space),
                                               method: "sendEvent(.ended) → keyWindow 재귀 → 가장 깊은 접근성 요소")
        entry["event"] = "touch"
        entry["windowPoint"] = ProbeJSON.point(windowPoint)
        entry["touchWindowClass"] = NSStringFromClass(type(of: window))
        ProbeLog.append(entry)
    }
}

extension UIApplication {
    /// 교체된 sendEvent. 기록을 먼저 하고 원래 구현으로 넘긴다(교체 뒤에는 이 이름이 원래 구현을 가리킨다).
    @objc dynamic func touchProbe_sendEvent(_ event: UIEvent) {
        TouchProbe.observe(event)
        touchProbe_sendEvent(event)
    }
}

// MARK: - 요소 찾기

/// 탐색 중 방문한 객체 하나. depth는 key window에서 몇 단계 아래인지, via는 부모에서 어떤 목록으로 왔는지다.
struct ProbeNode {
    /// 방문한 객체(UIView, UIAccessibilityElement, SwiftUI 접근성 노드 등).
    let object: NSObject
    /// key window가 0.
    let depth: Int
    /// "keyWindow" | "accessibilityElements" | "accessibilityElementAtIndex" | "subviews".
    let via: String
}

/// key window부터 `accessibilityElements`(없으면 접근성 컨테이너 색인, 그것도 없으면 subview)를 재귀로 돌아
/// `accessibilityFrame`이 좌표를 포함하는 가장 깊은 접근성 요소(`isAccessibilityElement == true`)를 찾는다.
///
/// 같은 깊이면 나중에 방문한 것(위에 그려진 것)을 고른다(`reproof.simple.hierarchy.element_at`의 순서 규칙과 같다).
/// 부모 frame 밖의 자식도 보려고 가지치기를 하지 않는다(실행기 계층의 element_at도 모든 노드를 본다).
@MainActor
final class ElementFinder {
    /// 비정상적으로 깊거나 큰 트리에서 멈추려는 상한.
    static let maxDepth = 80
    static let maxNodes = 5000
    /// nil이면 좌표 없이 모든 접근성 요소만 모은다(census).
    private let point: CGPoint?
    /// 같은 객체를 두 번 돌지 않으려는 표시. 객체 주소 재사용을 막으려고 방문한 객체를 retained에 잡아 둔다.
    private var visited = Set<ObjectIdentifier>()
    private var retained: [NSObject] = []
    /// key window부터 찾은 요소까지의 경로(마지막이 찾은 요소).
    private(set) var bestPath: [ProbeNode] = []
    /// 좌표를 포함하는 모든 접근성 요소(같은 깊이 동률 확인용).
    private(set) var hits: [ProbeNode] = []
    /// 방문한 모든 접근성 요소.
    private(set) var elements: [ProbeNode] = []

    /// point: 화면 좌표. nil이면 census.
    init(point: CGPoint?) {
        self.point = point
    }

    /// 방문한 객체 수.
    var visitedCount: Int { retained.count }

    /// 방문한 객체의 클래스 이름별 개수. census에서 화면이 실제로 떠 있었는지(호스팅 view·셀이 있는지) 보이려는 것이다.
    var visitedClassCounts: [String: Int] {
        retained.reduce(into: [:]) { counts, object in counts[NSStringFromClass(type(of: object)), default: 0] += 1 }
    }

    /// 객체 하나를 보고 자식으로 내려간다. 숨김 객체·상한 초과·이미 본 객체는 건너뛴다.
    func visit(_ object: NSObject, depth: Int, via: String, path: [ProbeNode]) {
        guard depth <= Self.maxDepth, retained.count < Self.maxNodes,
              visited.insert(ObjectIdentifier(object)).inserted, !Self.isHidden(object) else { return }
        retained.append(object)
        let node = ProbeNode(object: object, depth: depth, via: via)
        let current = path + [node]
        consider(node, current)
        let (children, childVia) = Self.children(of: object)
        for child in children {
            visit(child, depth: depth + 1, via: childVia, path: current)
        }
    }

    /// 접근성 요소면 모으고, 좌표를 포함하면 지금까지 가장 깊은 것과 비교한다(같으면 나중 것이 이긴다).
    private func consider(_ node: ProbeNode, _ path: [ProbeNode]) {
        guard node.object.isAccessibilityElement else { return }
        elements.append(node)
        guard let point, node.object.accessibilityFrame.contains(point) else { return }
        hits.append(node)
        if bestPath.last.map({ node.depth >= $0.depth }) ?? true {
            bestPath = path
        }
    }

    /// 자식 목록과 그 출처. 접근성 트리처럼 `accessibilityElements`가 있으면 그것이 subview를 대신한다.
    static func children(of object: NSObject) -> ([NSObject], String) {
        if let items = object.accessibilityElements, !items.isEmpty {
            return (items.compactMap { $0 as? NSObject }, "accessibilityElements")
        }
        let count = object.accessibilityElementCount()
        if count > 0, count != NSNotFound {
            let items = (0..<min(count, maxNodes)).compactMap { object.accessibilityElement(at: $0) as? NSObject }
            return (items, "accessibilityElementAtIndex")
        }
        return ((object as? UIView)?.subviews ?? [], "subviews")
    }

    /// 접근성에서 숨긴 객체, 숨김·투명 view면 true(접근성 트리에 나오지 않는다).
    static func isHidden(_ object: NSObject) -> Bool {
        if object.accessibilityElementsHidden { return true }
        guard let view = object as? UIView else { return false }
        return view.isHidden || view.alpha < 0.01
    }

    /// 앱의 key window. 없으면 nil.
    static func keyWindow() -> UIWindow? {
        UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            .flatMap { $0.windows }.first { $0.isKeyWindow }
    }

    /// 화면 좌표 point에서 요소를 찾고 기록용 사전을 만든다. method는 기록에 남길 방법 설명이다.
    static func resolveEntry(at point: CGPoint, method: String) -> [String: Any] {
        let started = CACurrentMediaTime()
        let finder = ElementFinder(point: point)
        if let window = keyWindow() {
            finder.visit(window, depth: 0, via: "keyWindow", path: [])
        }
        var entry: [String: Any] = [
            "method": method, "screenPoint": ProbeJSON.point(point), "hasKeyWindow": keyWindow() != nil,
            "elapsedMs": (CACurrentMediaTime() - started) * 1000, "visitedCount": finder.visitedCount,
            "axElementCount": finder.elements.count, "chain": finder.bestPath.map(ProbeJSON.node),
            "hits": finder.hits.map(ProbeJSON.node), "axState": AccessibilityActivation.currentState(),
        ]
        entry["found"] = finder.bestPath.last.map(ProbeJSON.node) ?? [:]
        return entry
    }

    /// 좌표 없이 현재 화면의 모든 접근성 요소를 모은 census 기록을 만든다.
    static func censusEntry(screen: String) -> [String: Any] {
        let finder = ElementFinder(point: nil)
        if let window = keyWindow() {
            finder.visit(window, depth: 0, via: "keyWindow", path: [])
        }
        return ["event": "census", "screen": screen, "visitedCount": finder.visitedCount,
                "visitedClasses": finder.visitedClassCounts, "elements": finder.elements.map(ProbeJSON.node),
                "axState": AccessibilityActivation.currentState()]
    }
}

// MARK: - 기록 형식

/// 기록 사전을 만드는 도우미. JSONSerialization이 받을 수 있는 값(숫자·문자열·목록·사전)만 만든다.
@MainActor
enum ProbeJSON {
    /// 클래스 계통으로 XCUIElement 종류를 정할 때 쓰는 공개 UIKit 클래스(먼저 맞는 것). 하위 클래스가 먼저 온다.
    static let uikitClasses: [AnyClass] = [
        UISwitch.self, UISearchTextField.self, UITextField.self, UITextView.self, UISlider.self,
        UISegmentedControl.self, UICollectionViewCell.self, UITableViewCell.self, UINavigationBar.self,
        UITabBar.self, UIButton.self, UILabel.self, UIImageView.self, UIWindow.self, UICollectionView.self,
        UITableView.self, UIScrollView.self,
    ]

    /// 점을 [x, y]로 바꾼다.
    static func point(_ point: CGPoint) -> [Double] {
        [finite(point.x), finite(point.y)]
    }

    /// 사각형을 [x, y, width, height]로 바꾼다.
    static func rect(_ rect: CGRect) -> [Double] {
        [finite(rect.origin.x), finite(rect.origin.y), finite(rect.size.width), finite(rect.size.height)]
    }

    /// 무한·NaN은 JSON이 될 수 없어 0으로 바꾼다(실행기 HierarchyExport와 같은 규칙).
    static func finite(_ value: CGFloat) -> Double {
        value.isFinite ? Double(value) : 0
    }

    /// 방문 객체 하나를 기록 사전으로 바꾼다(label·traits 원값과 이름·identifier·frame·클래스).
    static func node(_ node: ProbeNode) -> [String: Any] {
        let object = node.object
        let traits = object.accessibilityTraits.rawValue
        return [
            "class": NSStringFromClass(type(of: object)), "uikitClass": uikitClass(of: object),
            "isElement": object.isAccessibilityElement, "label": object.accessibilityLabel ?? "",
            "value": object.accessibilityValue ?? "", "identifier": identifier(of: object),
            "traits": NSNumber(value: traits), "traitsHex": String(traits, radix: 16),
            "traitNames": TraitNames.decode(traits), "frame": rect(object.accessibilityFrame),
            "depth": node.depth, "via": node.via,
        ]
    }

    /// 객체가 속한 첫 공개 UIKit 클래스 이름. 없으면 "".
    static func uikitClass(of object: NSObject) -> String {
        guard let match = uikitClasses.first(where: { object.isKind(of: $0) }) else { return "" }
        return NSStringFromClass(match)
    }

    /// accessibilityIdentifier. 프로토콜을 따르지 않아도 같은 이름의 getter가 있으면 읽는다. 없으면 "".
    static func identifier(of object: NSObject) -> String {
        if let identified = object as? UIAccessibilityIdentification {
            return identified.accessibilityIdentifier ?? ""
        }
        guard object.responds(to: NSSelectorFromString("accessibilityIdentifier")) else { return "" }
        return object.value(forKey: "accessibilityIdentifier") as? String ?? ""
    }
}

/// accessibilityTraits 원값을 이름 목록으로 푼다. 공개 상수는 SDK 값을 쓰고, 나머지 비트는 "bitN"으로 남긴다.
///
/// 왜 비공개 비트에 이름을 붙이지 않나: 이름이 공개 문서에 없어 추정이 섞이기 때문이다(분석 쪽에서 추정이라고 표시한다).
@MainActor
enum TraitNames {
    /// SDK 공개 trait과 이름.
    static let publicTraits: [(UIAccessibilityTraits, String)] = [
        (.button, "button"), (.link, "link"), (.image, "image"), (.selected, "selected"),
        (.playsSound, "playsSound"), (.keyboardKey, "keyboardKey"), (.staticText, "staticText"),
        (.summaryElement, "summaryElement"), (.notEnabled, "notEnabled"), (.updatesFrequently, "updatesFrequently"),
        (.searchField, "searchField"), (.startsMediaSession, "startsMediaSession"), (.adjustable, "adjustable"),
        (.allowsDirectInteraction, "allowsDirectInteraction"), (.causesPageTurn, "causesPageTurn"),
        (.tabBar, "tabBar"), (.header, "header"), (.supportsZoom, "supportsZoom"), (.toggleButton, "toggleButton"),
    ]

    /// raw의 각 비트를 이름으로 바꾼다. 공개 상수에 없는 비트는 "bitN"(N은 0부터)이다.
    static func decode(_ raw: UInt64) -> [String] {
        var remaining = raw
        var names: [String] = []
        for (trait, name) in publicTraits where raw & trait.rawValue != 0 {
            names.append(name)
            remaining &= ~trait.rawValue
        }
        for bit in 0..<64 where remaining & (UInt64(1) << UInt64(bit)) != 0 {
            names.append("bit\(bit)")
        }
        return names
    }
}

// MARK: - 접근성 활성화(시도하는 방법 1가지)

/// 앱 안에서 접근성 트리를 켜는 방법 1가지: `libAccessibility.dylib`의 비공개 함수 `_AXSSetAutomationEnabled(1)`.
///
/// 왜 이 방법인가: XCUITest 없이 앱 안에서 접근성 런타임을 켜는 데 EarlGrey 등 UI 테스트 도구가 쓰는 방법이라
/// 후보 중 가장 널리 쓰였다고 판단했다(추정). 요청(TOUCH_PROBE_ACTIVATE_AX=1)이 없으면 아무것도 바꾸지 않고
/// 상태 값만 읽는다. Debug 전용이며 비공개 API다.
@MainActor
enum AccessibilityActivation {
    static let libraryPath = "/usr/lib/libAccessibility.dylib"
    static let requestKey = "TOUCH_PROBE_ACTIVATE_AX"
    static let methodName = "_AXSSetAutomationEnabled(1) (libAccessibility.dylib, 비공개 API)"
    typealias Setter = @convention(c) (Int32) -> Void
    typealias Getter = @convention(c) () -> UInt8

    /// 요청이 있으면 활성화하고, 무엇을 했는지 보고 사전을 돌려준다(라이브러리가 이미 올라와 있었는지 포함).
    static func applyIfRequested() -> [String: Any] {
        let isRequested = ProcessInfo.processInfo.environment[requestKey] == "1"
        var report: [String: Any] = ["requested": isRequested, "method": methodName,
                                     "libraryAlreadyLoaded": dlopen(libraryPath, RTLD_NOW | RTLD_NOLOAD) != nil]
        guard isRequested else { return report }
        switch symbol("_AXSSetAutomationEnabled", as: Setter.self) {
        case .success(let setter):
            setter(1)
            report["applied"] = true
        case .failure(let error):
            report["applied"] = false
            report["error"] = "\(error.message) — iOS 런타임에서 비공개 함수 이름이 바뀌었는지 확인하세요."
        }
        return report
    }

    /// 접근성 상태 값(읽기 전용 getter). 없는 함수는 NSNull로 남긴다.
    static func currentState() -> [String: Any] {
        ["automationEnabled": readFlag("_AXSAutomationEnabled"),
         "applicationAccessibilityEnabled": readFlag("_AXSApplicationAccessibilityEnabled"),
         "voiceOverRunning": UIAccessibility.isVoiceOverRunning]
    }

    /// getter 하나를 불러 0/1을 돌려준다. 함수가 없으면 NSNull(없다는 사실 자체를 기록).
    static func readFlag(_ name: String) -> Any {
        switch symbol(name, as: Getter.self) {
        case .success(let getter): return Int(getter())
        case .failure: return NSNull()
        }
    }

    /// libAccessibility에서 C 함수를 찾는다. 실패하면 dlerror 문구를 담은 오류.
    static func symbol<T>(_ name: String, as type: T.Type) -> Result<T, SymbolError> {
        guard let handle = dlopen(libraryPath, RTLD_NOW), let pointer = dlsym(handle, name) else {
            let reason = dlerror().map { String(cString: $0) } ?? "이유 없음"
            return .failure(SymbolError(message: "\(name)을(를) 찾지 못했습니다: \(reason)"))
        }
        return .success(unsafeBitCast(pointer, to: type))
    }

    /// 함수 찾기 실패 사유.
    struct SymbolError: Error {
        let message: String
    }
}

// MARK: - self-check (XCUITest 없이 같은 해석)

/// TOUCH_PROBE_SELFCHECK가 있으면 터치 없이 요약 탭과 옵션 화면으로 직접 옮겨, 주어진 좌표를 같은 방법으로 해석한다.
///
/// 왜: 실행기 tap_point로 탭하면 XCUITest가 붙어 앱의 접근성 런타임이 켜질 수 있다. 실제 직접 터치(실행기 없음)와
/// 같은 조건에서 앱 안 해석이 되는지 따로 보려는 것이다. 터치 경로(sendEvent)는 이 모드에서 거치지 않는다.
@MainActor
enum SelfCheck {
    static let planKey = "TOUCH_PROBE_SELFCHECK"

    /// 화면별 좌표 목록.
    struct Plan {
        let summary: [CGPoint]
        let options: [CGPoint]
    }

    /// 계획 형식 오류.
    struct PlanError: Error {
        let message: String
    }

    /// 환경 변수가 있으면 계획을 읽어 실행을 예약한다. 형식이 틀리면 원인을 기록하고 멈춘다.
    static func scheduleIfRequested() {
        guard let raw = ProcessInfo.processInfo.environment[planKey] else { return }
        do {
            let plan = try parse(raw)
            Task { @MainActor in await run(plan) }
        } catch {
            fail("계획을 읽지 못했습니다: \(error). {\"summary\": [[x, y], ...], \"options\": [...]} 형식인지 확인하세요.")
        }
    }

    /// {"summary": [[x, y], ...], "options": [[x, y], ...]}를 읽는다.
    static func parse(_ raw: String) throws -> Plan {
        let object = try JSONSerialization.jsonObject(with: Data(raw.utf8))
        guard let plan = object as? [String: [[Double]]], let summary = plan["summary"], let options = plan["options"],
              (summary + options).allSatisfy({ $0.count == 2 }) else {
            throw PlanError(message: "summary·options 키에 [x, y] 목록이 있어야 합니다.")
        }
        return Plan(summary: summary.map { CGPoint(x: $0[0], y: $0[1]) }, options: options.map { CGPoint(x: $0[0], y: $0[1]) })
    }

    /// 요약 탭 → 좌표 해석 → 옵션 화면 → 좌표 해석 순서로 실행하고 끝 표시를 남긴다.
    static func run(_ plan: Plan) async {
        await pause(3.0)
        guard showSummaryTab() else { return fail("요약 탭으로 옮기지 못했습니다(루트가 탭 바 컨트롤러가 아님).") }
        await pause(2.5)
        probe(screen: "summary", points: plan.summary)
        guard showOptionsScreen() else { return fail("옵션 화면으로 옮기지 못했습니다('옵션' 버튼 없음).") }
        await pause(2.5)
        probe(screen: "options", points: plan.options)
        ProbeLog.append(["event": "selfcheck-done"])
    }

    /// 좌표마다 터치 때와 같은 해석을 하고, 끝에 화면 census를 남긴다(좌표 해석이 먼저: 첫 질의가 터치 때와 같게).
    static func probe(screen: String, points: [CGPoint]) {
        for (index, point) in points.enumerated() {
            var entry = ElementFinder.resolveEntry(at: point, method: "self-check(터치 없음) → keyWindow 재귀 → 가장 깊은 접근성 요소")
            entry["event"] = "selfcheck"
            entry["screen"] = screen
            entry["index"] = index
            ProbeLog.append(entry)
        }
        ProbeLog.append(ElementFinder.censusEntry(screen: screen))
    }

    /// 루트 탭 바 컨트롤러.
    static func tabBarController() -> UITabBarController? {
        ElementFinder.keyWindow()?.rootViewController as? UITabBarController
    }

    /// 요약 탭(두 번째 탭)을 고른다. 성공하면 true.
    static func showSummaryTab() -> Bool {
        guard let controller = tabBarController() else { return false }
        controller.selectedIndex = 1
        return true
    }

    /// 장바구니 탭으로 돌아가 '옵션' 버튼의 동작을 보낸다(옵션 화면 push). 성공하면 true.
    static func showOptionsScreen() -> Bool {
        guard let controller = tabBarController() else { return false }
        controller.selectedIndex = 0
        let navigation = controller.selectedViewController as? UINavigationController
        guard let root = navigation?.topViewController?.view, let button = findButton(titled: "옵션", in: root) else {
            return false
        }
        button.sendActions(for: .touchUpInside)
        return true
    }

    /// view 아래에서 제목이 title인 UIButton을 찾는다.
    static func findButton(titled title: String, in view: UIView) -> UIButton? {
        if let button = view as? UIButton, button.currentTitle == title { return button }
        return view.subviews.lazy.compactMap { findButton(titled: title, in: $0) }.first
    }

    /// seconds초 기다린다. 취소되면 원인을 기록한다(그대로 진행).
    static func pause(_ seconds: Double) async {
        do {
            try await Task.sleep(for: .seconds(seconds))
        } catch {
            ProbeLog.append(["event": "selfcheck-warning", "reason": "대기가 취소됐습니다: \(error)"])
        }
    }

    /// 실패를 기록한다. 분석 스크립트는 이 줄을 보고 멈춘다.
    static func fail(_ reason: String) {
        ProbeLog.append(["event": "selfcheck-error", "reason": reason])
    }
}

// MARK: - JSONL 기록

/// Documents/touch-probe.jsonl에 한 줄씩 덧붙인다. 실패는 NSLog로 원인과 확인 방향을 남긴다(조용히 버리지 않는다).
@MainActor
enum ProbeLog {
    static let fileName = "touch-probe.jsonl"
    /// 줄 순번(앱 실행 안에서 0부터).
    private static var sequence = 0

    /// 기록 사전에 순번·시각을 붙여 한 줄로 쓴다.
    static func append(_ entry: [String: Any]) {
        var line = entry
        line["seq"] = sequence
        line["timestamp"] = Date().timeIntervalSince1970
        sequence += 1
        guard JSONSerialization.isValidJSONObject(line) else {
            NSLog("%@", "[TouchProbe] JSON으로 바꿀 수 없는 기록입니다(seq \(sequence - 1)). 값에 숫자·문자열·목록·사전만 넣었는지 확인하세요.")
            return
        }
        do {
            try write(line)
        } catch {
            NSLog("%@", "[TouchProbe] 기록 실패: \(error). 앱 데이터 컨테이너 Documents에 쓸 수 있는지 확인하세요.")
        }
    }

    /// 파일 끝에 JSON 한 줄을 쓴다. 파일이 없으면 만든다.
    private static func write(_ line: [String: Any]) throws {
        var data = try JSONSerialization.data(withJSONObject: line, options: [.sortedKeys])
        data.append(0x0A)
        let directory = try FileManager.default.url(for: .documentDirectory, in: .userDomainMask,
                                                    appropriateFor: nil, create: true)
        let url = directory.appendingPathComponent(fileName)
        if !FileManager.default.fileExists(atPath: url.path) {
            try Data().write(to: url)
        }
        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        try handle.write(contentsOf: data)
        try handle.close()
    }
}
#endif
