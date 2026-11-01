import XCTest

/// XCUITest 스냅숏을 Python `reproof.simple.hierarchy.parse_hierarchy`가 읽는 JSON 노드로 바꾼다.
///
/// 노드 스키마: type, identifier, label, value, placeholder, frame([x, y, w, h]), enabled, selected, children.
/// frame은 스냅숏이 주는 화면 좌표계 포인트다. 탭(`RunnerSessionTests.coordinate`)은 앱 frame 원점 기준 offset을
/// 쓰는데, iPhone에서는 앱이 전체 화면이라 원점이 (0, 0)이어서 같은 좌표계가 된다(iPad 분할 화면 등은 지원하지 않는다).
/// 요소를 고르는 판단은 하지 않는다(모두 Python이 맡는다).
/// 왜 `snapshot()`인가: 요소마다 질의하면 앱과 수천 번 통신하지만, 스냅숏은 한 번에 전체 트리를 가져온다.
enum HierarchyExport {
    /// 내보낼 수 있는 최대 깊이(루트가 0). Python `hierarchy.MAX_DEPTH`와 같아야 한다.
    static let maxDepth = 64
    /// 내보낼 수 있는 최대 노드 수. Python `hierarchy.MAX_NODE_COUNT`와 같아야 한다.
    static let maxNodeCount = 8000

    /// 스냅숏 전체를 노드 사전으로 바꾼다. 깊이·노드 수가 상한을 넘으면 `hierarchy_too_large`로 실패한다.
    ///
    /// 왜 상한을 두나: 재귀로 바꾸므로 아주 깊은 접근성 트리는 실행기 스택을 넘겨 프로세스가 오류 응답 없이 죽고,
    /// 노드가 아주 많으면 응답이 커진다. Python도 같은 상한으로 거절하므로 미리 멈춰 이유를 알린다.
    static func document(from snapshot: XCUIElementSnapshot) throws -> [String: Any] {
        var count = 0
        return try node(from: snapshot, depth: 0, count: &count)
    }

    /// 스냅숏 하나와 자식들을 재귀로 노드 사전으로 바꾼다. depth: 루트에서의 깊이, count: 지금까지 센 노드 수.
    static func node(from snapshot: XCUIElementSnapshot, depth: Int, count: inout Int) throws -> [String: Any] {
        count += 1
        guard depth <= maxDepth, count <= maxNodeCount else {
            throw CommandFailure(code: "hierarchy_too_large")
        }
        let children = try snapshot.children.map { try node(from: $0, depth: depth + 1, count: &count) }
        return [
            "type": typeName(of: snapshot.elementType),
            "identifier": snapshot.identifier,
            "label": snapshot.label,
            "value": text(of: snapshot.value),
            "placeholder": snapshot.placeholderValue ?? "",
            "frame": numbers(of: snapshot.frame),
            "enabled": snapshot.isEnabled,
            "selected": snapshot.isSelected,
            "children": children,
        ]
    }

    /// value(문자열·숫자 등 임의 값)를 문자열로 바꾼다. nil은 "".
    static func text(of value: Any?) -> String {
        switch value {
        case nil: return ""
        case let string as String: return string
        case let other?: return String(describing: other)
        }
    }

    /// frame을 [x, y, width, height]로 바꾼다. 값 하나라도 무한·NaN이면 크기 0인 [0, 0, 0, 0]으로 둔다.
    ///
    /// 왜 통째로 0인가: 무한·NaN은 JSON이 될 수 없는데, 그 값만 0으로 바꾸면 (0, 0)에 실제 크기가 있는 요소처럼 보여
    /// 왼쪽 위의 다른 컨트롤 대신 눌릴 수 있다. 크기 0인 frame은 Python이 좌표 검색에서 절대 고르지 않는다.
    static func numbers(of frame: CGRect) -> [Double] {
        let values = [frame.origin.x, frame.origin.y, frame.size.width, frame.size.height].map { Double($0) }
        return values.allSatisfy { $0.isFinite } ? values : [0, 0, 0, 0]
    }

    /// elementType을 Python 쪽 type 문자열(lowerCamelCase)로 바꾼다. 표에 없는 값(any 포함)은 "other".
    static func typeName(of type: XCUIElement.ElementType) -> String {
        typeNames[type] ?? "other"
    }

    /// elementType → type 문자열 표. 키를 Swift case로 써서 컴파일러가 이름을 확인하게 한다.
    static let typeNames: [XCUIElement.ElementType: String] = [
        .other: "other", .application: "application", .group: "group", .window: "window", .sheet: "sheet",
        .drawer: "drawer", .alert: "alert", .dialog: "dialog", .button: "button", .radioButton: "radioButton",
        .radioGroup: "radioGroup", .checkBox: "checkBox", .disclosureTriangle: "disclosureTriangle",
        .popUpButton: "popUpButton", .comboBox: "comboBox", .menuButton: "menuButton",
        .toolbarButton: "toolbarButton", .popover: "popover", .keyboard: "keyboard", .key: "key",
        .navigationBar: "navigationBar", .tabBar: "tabBar", .tabGroup: "tabGroup", .toolbar: "toolbar",
        .statusBar: "statusBar", .table: "table", .tableRow: "tableRow", .tableColumn: "tableColumn",
        .outline: "outline", .outlineRow: "outlineRow", .browser: "browser", .collectionView: "collectionView",
        .slider: "slider", .pageIndicator: "pageIndicator", .progressIndicator: "progressIndicator",
        .activityIndicator: "activityIndicator", .segmentedControl: "segmentedControl", .picker: "picker",
        .pickerWheel: "pickerWheel", .switch: "switch", .toggle: "toggle", .link: "link", .image: "image",
        .icon: "icon", .searchField: "searchField", .scrollView: "scrollView", .scrollBar: "scrollBar",
        .staticText: "staticText", .textField: "textField", .secureTextField: "secureTextField",
        .datePicker: "datePicker", .textView: "textView", .menu: "menu", .menuItem: "menuItem",
        .menuBar: "menuBar", .menuBarItem: "menuBarItem", .map: "map", .webView: "webView",
        .incrementArrow: "incrementArrow", .decrementArrow: "decrementArrow", .timeline: "timeline",
        .ratingIndicator: "ratingIndicator", .valueIndicator: "valueIndicator", .splitGroup: "splitGroup",
        .splitter: "splitter", .relevanceIndicator: "relevanceIndicator", .colorWell: "colorWell",
        .helpTag: "helpTag", .matte: "matte", .dockItem: "dockItem", .ruler: "ruler",
        .rulerMarker: "rulerMarker", .grid: "grid", .levelIndicator: "levelIndicator", .cell: "cell",
        .layoutArea: "layoutArea", .layoutItem: "layoutItem", .handle: "handle", .stepper: "stepper",
        .tab: "tab", .touchBar: "touchBar", .statusItem: "statusItem",
    ]
}
