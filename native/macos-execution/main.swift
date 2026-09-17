// Trusted host bridge. Candidate bytes use a separate inherited descriptor;
// they cannot write lifecycle JSON or select host commands/devices/shares.
import Foundation
import Virtualization
import Darwin
import CoreFoundation

let outputLock = NSLock()
func emit(_ event: String) {
    outputLock.lock()
    defer { outputLock.unlock() }
    let raw = try! JSONSerialization.data(withJSONObject: ["event": event], options: [.sortedKeys])
    FileHandle.standardOutput.write(raw + Data([10]))
}

func line(_ maximum: Int) -> Data? {
    var result = Data()
    var byte: UInt8 = 0
    while result.count <= maximum {
        let count = Darwin.read(STDIN_FILENO, &byte, 1)
        if count == -1 && errno == EINTR { continue }
        if count != 1 { return nil }
        if byte == 10 { return result }
        result.append(byte)
    }
    return nil
}

enum Rejected: Error { case configuration }
func integer(_ config: [String: Any], _ key: String, _ low: Int, _ high: Int) throws -> Int {
    guard let number = config[key] as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue == Double(number.intValue),
          low <= number.intValue, number.intValue <= high else { throw Rejected.configuration }
    return number.intValue
}
func path(_ config: [String: Any], _ key: String) throws -> URL {
    guard let value = config[key] as? String, value.hasPrefix("/"), value.utf8.count <= 4096,
          !value.utf8.contains(0), !value.split(separator: "/").contains("..") else {
        throw Rejected.configuration
    }
    var info = stat()
    let maximum: Int64 = ["hardware": 1048576, "machine": 1048576, "auxiliary": 134217728][key] ?? 549755813888
    guard lstat(value, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
          info.st_size > 0, info.st_size <= maximum else {
        throw Rejected.configuration
    }
    return URL(fileURLWithPath: value)
}

struct StopRecord {
    let file: Int32
    let directory: Int32
    let operation: String
    let requestDigest: String
    init(_ config: [String: Any]) throws {
        file = Int32(try integer(config, "terminationFD", 3, 1000000))
        directory = Int32(try integer(config, "runDirectoryFD", 3, 1000000))
        guard let operation = config["operationId"] as? String,
              operation.range(of: "^[a-z][a-z0-9_-]{0,63}\\z", options: .regularExpression) != nil,
              let requestDigest = config["requestDigest"] as? String,
              requestDigest.range(of: "^[a-f0-9]{64}\\z", options: .regularExpression) != nil else {
            throw Rejected.configuration
        }
        self.operation = operation
        self.requestDigest = requestDigest
        var fileInfo = stat(), directoryInfo = stat()
        guard fstat(file, &fileInfo) == 0, (fileInfo.st_mode & S_IFMT) == S_IFREG,
              fileInfo.st_nlink == 1, fileInfo.st_size == 0, fileInfo.st_uid == getuid(),
              fstat(directory, &directoryInfo) == 0, (directoryInfo.st_mode & S_IFMT) == S_IFDIR,
              directoryInfo.st_uid == getuid() else { throw Rejected.configuration }
    }
    func persist(_ state: String) -> Bool {
        let object: [String: Any] = ["schemaVersion": 1, "operationId": operation,
                                     "requestDigest": requestDigest, "state": state]
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
              ftruncate(file, 0) == 0, lseek(file, 0, SEEK_SET) == 0 else { return false }
        var offset = 0
        while offset < data.count {
            let count = data.withUnsafeBytes { bytes in
                Darwin.write(file, bytes.baseAddress!.advanced(by: offset), data.count - offset)
            }
            if count < 0 && errno == EINTR { continue }
            if count <= 0 { return false }
            offset += count
        }
        return fsync(file) == 0 && fsync(directory) == 0
    }
}

final class Bridge: NSObject, VZVirtioSocketListenerDelegate, VZVirtualMachineDelegate {
    var vm: VZVirtualMachine!
    var connection: VZVirtioSocketConnection?
    var listener: VZVirtioSocketListener!
    var stopping = false
    var stopPending = false
    let channel: Int32
    let stopRecord: StopRecord

    init(_ config: [String: Any], stopRecord: StopRecord) throws {
        let keys: Set<String> = ["schemaVersion", "cpuCount", "memoryBytes", "disk", "auxiliary",
                                 "hardware", "machine", "toolchain", "channelFD", "port", "timeoutMs",
                                 "terminationFD", "runDirectoryFD", "operationId", "requestDigest"]
        guard Set(config.keys) == keys, try integer(config, "schemaVersion", 1, 1) == 1,
              VZVirtualMachine.isSupported else { throw Rejected.configuration }
        channel = Int32(try integer(config, "channelFD", 3, 1_000_000))
        self.stopRecord = stopRecord
        var channelInfo = stat()
        guard fstat(channel, &channelInfo) == 0, (channelInfo.st_mode & S_IFMT) == S_IFSOCK else {
            throw Rejected.configuration
        }
        let cpu = try integer(config, "cpuCount", VZVirtualMachineConfiguration.minimumAllowedCPUCount,
                              VZVirtualMachineConfiguration.maximumAllowedCPUCount)
        let memory = try integer(config, "memoryBytes", Int(VZVirtualMachineConfiguration.minimumAllowedMemorySize),
                                 Int(VZVirtualMachineConfiguration.maximumAllowedMemorySize))
        let port = UInt32(try integer(config, "port", 4050, 4050))
        let timeout = try integer(config, "timeoutMs", 1, 86400000)
        let hardwareURL = try path(config, "hardware")
        let machineURL = try path(config, "machine")
        guard let hardware = VZMacHardwareModel(dataRepresentation: try Data(contentsOf: hardwareURL)),
              hardware.isSupported,
              let machine = VZMacMachineIdentifier(dataRepresentation: try Data(contentsOf: machineURL)) else {
            throw Rejected.configuration
        }
        let platform = VZMacPlatformConfiguration()
        platform.hardwareModel = hardware
        platform.machineIdentifier = machine
        platform.auxiliaryStorage = VZMacAuxiliaryStorage(url: try path(config, "auxiliary"))
        let settings = VZVirtualMachineConfiguration()
        settings.platform = platform
        settings.bootLoader = VZMacOSBootLoader()
        settings.cpuCount = cpu
        settings.memorySize = UInt64(memory)
        settings.networkDevices = []
        settings.directorySharingDevices = []
        settings.socketDevices = [VZVirtioSocketDeviceConfiguration()]
        settings.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
        let disk = try VZDiskImageStorageDeviceAttachment(url: path(config, "disk"), readOnly: false)
        let tools = try VZDiskImageStorageDeviceAttachment(url: path(config, "toolchain"), readOnly: true)
        settings.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: disk),
                                   VZVirtioBlockDeviceConfiguration(attachment: tools)]
        try settings.validate()
        super.init()
        vm = VZVirtualMachine(configuration: settings)
        vm.delegate = self
        listener = VZVirtioSocketListener()
        listener.delegate = self
        guard let socket = vm.socketDevices.first as? VZVirtioSocketDevice else { throw Rejected.configuration }
        socket.setSocketListener(listener, forPort: port)
        emit("configured-no-network-no-shares")
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(timeout)) { self.stop() }
    }

    func start() {
        vm.start { result in
            switch result {
            case .success:
                emit("started")
                if self.stopping { self.stop() }
            case .failure:
                emit("start-failed")
                if self.vm.state == .stopped { self.confirmStop() }
                else { self.stop() }
            }
        }
    }

    func confirmStop() {
        guard vm.state == .stopped else { emit("stop-unconfirmed"); exit(2) }
        guard stopRecord.persist("stopped") else { emit("stop-unconfirmed"); exit(2) }
        emit("stopped")
        exit(0)
    }

    func stop() {
        stopping = true
        if vm.state == .stopped { confirmStop(); return }
        if vm.state == .starting { return }
        if stopPending { return }
        guard vm.canStop else { emit("stop-unconfirmed"); exit(2) }
        stopPending = true
        vm.stop { error in
            if error == nil && self.vm.state == .stopped { self.confirmStop() }
            else { emit("stop-unconfirmed"); exit(2) }
        }
    }

    func guestDidStop(_ virtualMachine: VZVirtualMachine) { confirmStop() }
    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        confirmStop()
    }

    func pump(_ input: Int32, _ output: Int32) {
        var buffer = [UInt8](repeating: 0, count: 16384)
        while true {
            let count = Darwin.read(input, &buffer, buffer.count)
            if count < 0 && errno == EINTR { continue }
            if count <= 0 { break }
            var written = 0
            while written < count {
                let result = buffer.withUnsafeBytes { bytes in
                    Darwin.write(output, bytes.baseAddress!.advanced(by: written), count - written)
                }
                if result < 0 && errno == EINTR { continue }
                if result <= 0 { DispatchQueue.main.async { self.stop() }; return }
                written += result
            }
        }
        DispatchQueue.main.async { self.stop() }
    }

    func listener(_ listener: VZVirtioSocketListener, shouldAcceptNewConnection connection: VZVirtioSocketConnection,
                  from socketDevice: VZVirtioSocketDevice) -> Bool {
        guard self.connection == nil, !stopping else { return false }
        self.connection = connection
        emit("guest-connected")
        Thread.detachNewThread { self.pump(self.channel, connection.fileDescriptor) }
        Thread.detachNewThread { self.pump(connection.fileDescriptor, self.channel) }
        return true
    }
}

signal(SIGPIPE, SIG_IGN)
if CommandLine.arguments == [CommandLine.arguments[0], "--preflight"] {
    emit(VZVirtualMachine.isSupported ? "runtime-supported" : "runtime-unsupported")
    exit(VZVirtualMachine.isSupported ? 0 : 2)
}
guard CommandLine.arguments.count == 1,
      let initial = line(65536),
      let config = (try? JSONSerialization.jsonObject(with: initial)) as? [String: Any] else {
    emit("configuration-rejected")
    exit(2)
}
let stopRecord: StopRecord
do { stopRecord = try StopRecord(config) }
catch { emit("configuration-rejected"); exit(2) }
let bridge: Bridge
do { bridge = try Bridge(config, stopRecord: stopRecord) }
catch {
    emit(stopRecord.persist("not-started") ? "configuration-rejected" : "stop-unconfirmed")
    exit(2)
}
Thread.detachNewThread {
    while let control = line(128) {
        if control == Data("stop".utf8) { DispatchQueue.main.async { bridge.stop() }; return }
        DispatchQueue.main.async { bridge.stop() }
        return
    }
    // Parent death closes stdin. Shutdown still runs inside the native owner.
    DispatchQueue.main.async { bridge.stop() }
}
bridge.start()
dispatchMain()
