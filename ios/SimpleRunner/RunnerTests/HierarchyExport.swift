import XCTest

/// XCUITest 스냅숏을 Python `reproof.simple.hierarchy.parse_hierarchy`가 읽는 JSON 노드로 바꾼다.
///
/// 노드 스키마: type, identifier, label, value, placeholder, frame([x, y, w, h], 앱 좌표계 포인트),
/// enabled, selected, children. 요소를 고르는 판단은 하지 않는다(모두 Python이 맡는다).
/// 왜 `snapshot()`인가: 요소마다 질의하면 앱과 수천 번 통신하지만, 스냅숏은 한 번에 전체 트리를 가져온다.
enum HierarchyExport {
    /// 스냅숏 하나와 자식들을 재귀로 노드 사전으로 바꾼다.
    static func node(from snapshot: XCUIElementSnapshot) -> [String: Any] {
        [
            "type": typeName(of: snapshot.elementType),
            "identifier": snapshot.identifier,
            "label": snapshot.label,
            "value": text(of: snapshot.value),
            "placeholder": snapshot.placeholderValue ?? "",
            "frame": numbers(of: snapshot.frame),
            "enabled": snapshot.isEnabled,
            "selected": snapshot.isSelected,
            "children": snapshot.children.map { node(from: $0) },
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

    /// frame을 [x, y, width, height]로 바꾼다. 무한·NaN은 JSON이 될 수 없어 0으로 둔다(화면 밖 요소 등).
    static func numbers(of frame: CGRect) -> [Double] {
        [frame.origin.x, frame.origin.y, frame.size.width, frame.size.height].map { $0.isFinite ? Double($0) : 0 }
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
