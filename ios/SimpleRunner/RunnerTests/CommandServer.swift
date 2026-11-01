import CryptoKit
import Foundation
import Network

/// 받은 명령 하나. testSession(메인 스레드)이 꺼내 실행하고 `reply`로 응답한다.
///
/// 왜 메인 스레드로 넘기나: XCUITest API는 메인 스레드에서만 호출해야 한다. 네트워크 큐는 요청을 읽어 넘기기만 한다.
final class PendingCommand {
    /// 요청 본문 JSON 객체. `action` 문자열과 명령별 값을 담는다.
    let body: [String: Any]
    /// 응답을 보낼 연결.
    private let connection: NWConnection
    /// 응답을 직렬화해 보내는 서버.
    private let server: CommandServer

    /// 본문과 연결, 서버를 묶는다.
    init(body: [String: Any], connection: NWConnection, server: CommandServer) {
        self.body = body
        self.connection = connection
        self.server = server
    }

    /// 응답 JSON을 보내고 전송이 끝날 때까지(최대 5초) 기다린다.
    ///
    /// 왜 기다리나: stop 직후 테스트가 끝나 프로세스가 내려가도 응답이 먼저 도착하게 하려는 것이다.
    func reply(_ document: [String: Any]) {
        let sent = DispatchSemaphore(value: 0)
        server.send(connection, status: 200, document: document) { sent.signal() }
        _ = sent.wait(timeout: .now() + 5)
    }
}

/// 받은 HTTP 요청 하나(메서드, 경로, 헤더, 본문).
struct HTTPRequest {
    /// 파싱 결과. incomplete면 더 받아야 하고, invalid면 (상태 코드, 오류 코드)로 답한다.
    enum ParseResult {
        case incomplete
        case invalid(Int, String)
        case complete(HTTPRequest)
    }

    /// 요청 메서드(예: POST).
    let method: String
    /// 요청 경로(예: /command).
    let path: String
    /// 소문자 이름 → 값 헤더. 같은 이름이 두 번 오면 요청 자체를 거절한다(검사를 엇갈리게 하는 요청 방지).
    let headers: [String: String]
    /// Content-Length만큼의 본문.
    let body: Data

    /// 지금까지 받은 바이트를 해석한다. 헤더 16 KiB, 본문 1 MiB를 넘으면 invalid다. chunked 전송은 받지 않는다.
    static func parse(_ data: Data) -> ParseResult {
        guard let headerEnd = data.range(of: Data("\r\n\r\n".utf8)) else {
            return data.count > CommandServer.maxHeaderBytes ? .invalid(431, "headers_too_large") : .incomplete
        }
        guard headerEnd.lowerBound <= CommandServer.maxHeaderBytes,
              let head = String(data: data[data.startIndex..<headerEnd.lowerBound], encoding: .utf8) else {
            return .invalid(400, "invalid_headers")
        }
        let lines = head.components(separatedBy: "\r\n")
        let requestLine = lines[0].split(separator: " ", omittingEmptySubsequences: false).map(String.init)
        guard requestLine.count == 3, requestLine[2].hasPrefix("HTTP/1.") else {
            return .invalid(400, "invalid_request_line")
        }
        guard let headers = headerFields(lines.dropFirst()) else { return .invalid(400, "invalid_header_fields") }
        return parseBody(data, from: headerEnd.upperBound, headers: headers, method: requestLine[0], path: requestLine[1])
    }

    /// 헤더 줄들을 소문자 이름 → 값 사전으로 바꾼다. 콜론이 없는 줄이나 같은 이름이 두 번 나오면 nil.
    private static func headerFields(_ lines: ArraySlice<String>) -> [String: String]? {
        var fields: [String: String] = [:]
        for line in lines {
            guard let colon = line.firstIndex(of: ":") else { return nil }
            let name = line[..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            guard fields[name] == nil else { return nil }
            fields[name] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        return fields
    }

    /// Content-Length를 확인하고 본문이 다 왔으면 complete를 돌려준다.
    private static func parseBody(_ data: Data, from start: Data.Index, headers: [String: String],
                                  method: String, path: String) -> ParseResult {
        guard headers["transfer-encoding"] == nil else { return .invalid(400, "chunked_not_supported") }
        guard let length = Int(headers["content-length"] ?? "0"), length >= 0 else {
            return .invalid(400, "invalid_content_length")
        }
        guard length <= CommandServer.maxBodyBytes else { return .invalid(413, "body_too_large") }
        guard data.distance(from: start, to: data.endIndex) >= length else { return .incomplete }
        let body = data.subdata(in: start..<data.index(start, offsetBy: length))
        return .complete(HTTPRequest(method: method, path: path, headers: headers, body: body))
    }
}

/// 127.0.0.1에만 바인드하는 최소 HTTP/1.1 명령 서버.
///
/// `POST /command`(JSON 객체 본문, 최대 1 MiB)만 받는다. loopback이 아닌 상대는 바로 끊는다.
/// 요청 검사 순서: Origin 헤더가 있으면 403(브라우저가 보낸 요청 차단), Host가 `127.0.0.1:<port>`나
/// `localhost:<port>`가 아니면 403(DNS rebinding 차단), 세션 토큰이 다르면 401, 다른 경로는 404,
/// Content-Type이 application/json이 아니면 415. 토큰은 SHA-256 digest만 보관하고 상수 시간으로 비교하며
/// 어디에도 기록하지 않는다. 요청은 네트워크 큐에서 읽고, 명령은 `next(timeout:)`으로 메인 스레드에 넘긴다.
final class CommandServer: @unchecked Sendable {
    /// 서버를 만들 수 없을 때의 오류.
    enum ServerError: Error {
        /// 포트가 0이거나 범위를 벗어났다.
        case invalidPort(UInt16)
        /// 세션 토큰이 비어 있다.
        case missingToken
    }

    /// 요청을 거절하는 이유. status는 HTTP 상태 코드, code는 응답 `error` 값이다.
    private struct Rejection {
        let status: Int
        let code: String
    }

    /// 리스너 상태. starting → ready 또는 failed.
    private enum ListenerState {
        case starting, ready, failed
    }

    /// 요청 본문 상한(1 MiB).
    static let maxBodyBytes = 1 << 20
    /// 요청 헤더 상한(16 KiB).
    static let maxHeaderBytes = 16 * 1024
    /// 요청을 다 받기까지 기다리는 시간(초). 멈춘 연결이 자원을 계속 잡지 않게 한다.
    static let receiveTimeout: TimeInterval = 30
    /// 동시에 열어 둘 연결 수 상한.
    static let maxConnections = 16

    /// 127.0.0.1:<port> 리스너.
    private let listener: NWListener
    /// 바인드한 포트. Host 헤더 검사에 쓴다.
    private let port: UInt16
    /// 세션 토큰의 SHA-256 digest. 원문 대신 digest를 비교해 길이가 새지 않게 한다.
    private let expectedTokenDigest: Data
    /// 연결 처리 전용 직렬 큐.
    private let queue = DispatchQueue(label: "io.reproof.simple-runner.server")
    /// inbox·상태 보호와 대기용 조건 변수.
    private let condition = NSCondition()
    /// 메인 스레드가 꺼내 갈 명령들(받은 순서).
    private var inbox: [PendingCommand] = []
    /// 현재 리스너 상태.
    private var listenerState = ListenerState.starting
    /// 열린 연결 수.
    private var connectionCount = 0

    /// 127.0.0.1:port에 리스너를 열기 시작한다. token: 모든 요청이 Bearer로 보내야 하는 세션 토큰.
    /// 준비 여부는 `waitUntilReady(timeout:)`로 확인한다.
    init(port: UInt16, token: String) throws {
        guard port > 0, let endpointPort = NWEndpoint.Port(rawValue: port) else {
            throw ServerError.invalidPort(port)
        }
        guard !token.isEmpty else { throw ServerError.missingToken }
        self.port = port
        expectedTokenDigest = Self.digest(token)
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: endpointPort)
        // 주소 재사용(SO_REUSEPORT)은 켜지 않는다. 켜면 다른 프로세스가 같은 포트에 함께 바인드할 수 있다.
        parameters.allowLocalEndpointReuse = false
        listener = try NWListener(using: parameters)
        listener.stateUpdateHandler = { [weak self] state in self?.updateListener(state) }
        listener.newConnectionHandler = { [weak self] connection in self?.accept(connection) }
        listener.start(queue: queue)
    }

    /// 리스너가 열려 명령을 받을 수 있는지 돌려준다.
    var isListening: Bool {
        condition.lock()
        defer { condition.unlock() }
        return listenerState == .ready
    }

    /// 리스너가 준비될 때까지 최대 timeout초 기다린다. 준비되면 true, 실패하거나 시간이 지나면 false.
    func waitUntilReady(timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        condition.lock()
        defer { condition.unlock() }
        while listenerState == .starting && condition.wait(until: deadline) {}
        return listenerState == .ready
    }

    /// 다음 명령을 최대 timeout초 기다려 꺼낸다. 없으면 nil.
    func next(timeout: TimeInterval) -> PendingCommand? {
        let deadline = Date().addingTimeInterval(timeout)
        condition.lock()
        defer { condition.unlock() }
        while inbox.isEmpty && condition.wait(until: deadline) {}
        return inbox.isEmpty ? nil : inbox.removeFirst()
    }

    /// 리스너를 닫는다. 이미 받은 연결은 각자 응답 후 닫힌다.
    func stop() {
        listener.cancel()
    }

    /// JSON 응답을 보내고 연결을 닫는다. completion은 전송이 끝난 뒤(실패 포함) 네트워크 큐에서 불린다.
    func send(_ connection: NWConnection, status: Int, document: [String: Any], completion: (() -> Void)? = nil) {
        let body = Self.encode(document)
        let head = "HTTP/1.1 \(status) \(Self.reason(status))\r\nContent-Type: application/json\r\n"
            + "Content-Length: \(body.count)\r\nConnection: close\r\n\r\n"
        connection.send(content: Data(head.utf8) + body, completion: .contentProcessed { _ in
            connection.cancel()
            completion?()
        })
    }

    /// 응답 사전을 JSON으로 바꾼다. NaN 등 JSON이 될 수 없는 값이 있으면 원인을 담은 오류 응답으로 대신한다.
    ///
    /// 왜 먼저 isValidJSONObject를 보나: JSONSerialization은 잘못된 값에 Swift 오류가 아닌 ObjC 예외를 던져
    /// 실행기 프로세스가 죽기 때문이다.
    private static func encode(_ document: [String: Any]) -> Data {
        guard JSONSerialization.isValidJSONObject(document) else {
            return failureBody("response_not_json")
        }
        do {
            return try JSONSerialization.data(withJSONObject: document)
        } catch {
            return failureBody("response_encoding_failed: \(error.localizedDescription)")
        }
    }

    /// {"ok": false, "error": code} 본문을 만든다. 문자열과 Bool만 담으므로 직렬화가 실패하지 않는다.
    private static func failureBody(_ code: String) -> Data {
        let document: [String: Any] = ["ok": false, "error": code]
        return (try? JSONSerialization.data(withJSONObject: document)) ?? Data(#"{"ok":false}"#.utf8)
    }

    /// 상태 코드의 HTTP 사유 문구.
    private static func reason(_ status: Int) -> String {
        switch status {
        case 200: return "OK"
        case 400: return "Bad Request"
        case 401: return "Unauthorized"
        case 403: return "Forbidden"
        case 404: return "Not Found"
        case 413: return "Payload Too Large"
        case 415: return "Unsupported Media Type"
        case 431: return "Request Header Fields Too Large"
        default: return "Internal Server Error"
        }
    }

    /// 상대 주소가 loopback(127.0.0.0/8, ::1)인지 확인한다.
    static func isLoopback(_ endpoint: NWEndpoint) -> Bool {
        guard case let .hostPort(host, _) = endpoint else { return false }
        switch host {
        case let .ipv4(address): return address.isLoopback
        case let .ipv6(address): return address.isLoopback
        default: return false
        }
    }

    /// 리스너 상태 변화를 기록하고 기다리는 쪽을 깨운다.
    private func updateListener(_ state: NWListener.State) {
        condition.lock()
        defer { condition.unlock() }
        switch state {
        case .ready: listenerState = .ready
        case .failed, .cancelled: listenerState = .failed
        default: return
        }
        condition.broadcast()
    }

    /// 새 연결을 받는다. loopback이 아니거나 연결이 너무 많으면 바로 끊는다.
    private func accept(_ connection: NWConnection) {
        guard Self.isLoopback(connection.endpoint), reserveConnection() else {
            connection.cancel()
            return
        }
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            switch state {
            case .failed: connection?.cancel()
            case .cancelled: self?.releaseConnection()
            default: break
            }
        }
        connection.start(queue: queue)
        receive(on: connection, buffer: Data())
    }

    /// 연결 수 자리를 하나 잡는다. 상한에 닿았으면 false.
    private func reserveConnection() -> Bool {
        condition.lock()
        defer { condition.unlock() }
        guard connectionCount < Self.maxConnections else { return false }
        connectionCount += 1
        return true
    }

    /// 닫힌 연결의 자리를 돌려준다.
    private func releaseConnection() {
        condition.lock()
        connectionCount -= 1
        condition.unlock()
    }

    /// 요청이 다 올 때까지 이어 받는다. receiveTimeout 안에 다음 조각이 오지 않으면 연결을 끊는다.
    private func receive(on connection: NWConnection, buffer: Data) {
        let timeout = DispatchWorkItem { [weak connection] in connection?.cancel() }
        queue.asyncAfter(deadline: .now() + Self.receiveTimeout, execute: timeout)
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] data, _, isComplete, error in
            timeout.cancel()
            guard let self, error == nil else {
                connection.cancel()
                return
            }
            self.handle(buffer + (data ?? Data()), on: connection, isComplete: isComplete)
        }
    }

    /// 받은 바이트를 해석해 더 받거나, 오류로 답하거나, 라우팅한다.
    private func handle(_ buffer: Data, on connection: NWConnection, isComplete: Bool) {
        switch HTTPRequest.parse(buffer) {
        case .incomplete where isComplete:
            connection.cancel()
        case .incomplete:
            receive(on: connection, buffer: buffer)
        case let .invalid(status, code):
            send(connection, status: status, document: ["ok": false, "error": code])
        case let .complete(request):
            route(request, on: connection)
        }
    }

    /// 검사를 통과한 `POST /command`만 inbox에 넣는다. 본문이 JSON 객체가 아니면 400으로 답한다.
    private func route(_ request: HTTPRequest, on connection: NWConnection) {
        if let rejection = rejection(for: request) {
            send(connection, status: rejection.status, document: ["ok": false, "error": rejection.code])
            return
        }
        let object: Any
        do {
            object = try JSONSerialization.jsonObject(with: request.body)
        } catch {
            send(connection, status: 400, document: ["ok": false, "error": "invalid_json: \(error.localizedDescription)"])
            return
        }
        guard let body = object as? [String: Any] else {
            send(connection, status: 400, document: ["ok": false, "error": "body_not_object"])
            return
        }
        enqueue(PendingCommand(body: body, connection: connection, server: self))
    }

    /// 요청 헤더·경로를 검사한다. 통과하면 nil, 아니면 거절 이유. 순서는 타입 설명과 같다.
    private func rejection(for request: HTTPRequest) -> Rejection? {
        if request.headers["origin"] != nil { return Rejection(status: 403, code: "origin_rejected") }
        guard isAllowedHost(request.headers["host"]) else { return Rejection(status: 403, code: "host_rejected") }
        guard isAuthorized(request.headers["authorization"]) else { return Rejection(status: 401, code: "unauthorized") }
        guard request.method == "POST", request.path == "/command" else {
            return Rejection(status: 404, code: "not_found")
        }
        guard Self.isJSON(request.headers["content-type"]) else {
            return Rejection(status: 415, code: "unsupported_media_type")
        }
        return nil
    }

    /// Host가 정확히 `127.0.0.1:<port>`나 `localhost:<port>`인지 본다(대소문자 무시). 없으면 거절한다.
    private func isAllowedHost(_ host: String?) -> Bool {
        guard let host = host?.lowercased() else { return false }
        return host == "127.0.0.1:\(port)" || host == "localhost:\(port)"
    }

    /// `Authorization: Bearer <token>`의 토큰 digest가 세션 토큰 digest와 같은지 상수 시간으로 비교한다.
    private func isAuthorized(_ value: String?) -> Bool {
        guard let value, value.hasPrefix("Bearer ") else { return false }
        return Self.constantTimeEqual(Self.digest(String(value.dropFirst(7))), expectedTokenDigest)
    }

    /// Content-Type의 미디어 타입(`;` 앞)이 application/json인지 본다. charset 같은 매개변수는 허용한다.
    private static func isJSON(_ value: String?) -> Bool {
        guard let mediaType = value?.split(separator: ";", maxSplits: 1).first else { return false }
        return mediaType.trimmingCharacters(in: .whitespaces).lowercased() == "application/json"
    }

    /// 문자열의 SHA-256 digest.
    private static func digest(_ text: String) -> Data {
        Data(SHA256.hash(data: Data(text.utf8)))
    }

    /// 두 값을 내용과 무관하게 같은 시간에 비교한다(타이밍으로 토큰을 추측하지 못하게).
    private static func constantTimeEqual(_ left: Data, _ right: Data) -> Bool {
        guard left.count == right.count else { return false }
        var difference: UInt8 = 0
        for (a, b) in zip(left, right) { difference |= a ^ b }
        return difference == 0
    }

    /// 명령을 inbox에 넣고 메인 스레드를 깨운다.
    private func enqueue(_ command: PendingCommand) {
        condition.lock()
        inbox.append(command)
        condition.broadcast()
        condition.unlock()
    }
}
