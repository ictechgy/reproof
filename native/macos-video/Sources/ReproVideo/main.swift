import AVFoundation
import CoreGraphics
import CoreMedia
import CoreVideo
import CryptoKit
import Darwin
import Foundation
import ImageIO
import UniformTypeIdentifiers

private let protocolMagic = Data("RLVID001".utf8)
private let maxConfigBytes = 4 * 1024
private let maxMetadataBytes = 4 * 1024
private let absoluteMaxCompressedBytes = 64 * 1024 * 1024
private let absoluteMaxOutputBytes = 64 * 1024 * 1024
private let absoluteMaxPixels = 4_194_304

enum VideoFailure: Error {
    case invalid(String)
}

func require(_ condition: @autoclosure () -> Bool, _ reason: String) throws {
    if !condition() { throw VideoFailure.invalid(reason) }
}

struct Configuration: Decodable {
    let schemaVersion: Int
    let codec: String
    let container: String
    let width: Int
    let height: Int
    let maxFrames: Int
    let maxCompressedBytes: Int
    let maxDecodedPixels: Int
    let maxOutputBytes: Int
    let faultMode: String
    let targetBitrate: Int?
    let maxKeyFrameInterval: Int?
}

struct FrameMetadata: Decodable {
    let schemaVersion: Int
    let mimeType: String
    let width: Int
    let height: Int
    let acquisitionSequence: Int64
    let digest: String
    let ptsNs: Int64
    let timingSource: String
    let uncertaintyNs: Int64
    let providerIncarnation: String
    let nativeIncarnation: String
    let earliestRecordingOffsetMs: Int?
    let latestRecordingOffsetMs: Int?
    let displayRecordingOffsetMs: Int?
    let ptsRelation: String
}

func readExact(_ count: Int) throws -> Data {
    try require(count >= 0 && count <= absoluteMaxCompressedBytes, "read_length_invalid")
    var result = Data()
    result.reserveCapacity(min(count, 64 * 1024))
    while result.count < count {
        guard let chunk = try FileHandle.standardInput.read(
            upToCount: min(64 * 1024, count - result.count)
        ), !chunk.isEmpty else {
            throw VideoFailure.invalid("input_truncated")
        }
        result.append(chunk)
    }
    return result
}

func uint32(_ data: Data) throws -> Int {
    try require(data.count == 4, "integer_truncated")
    return data.reduce(0) { ($0 << 8) | Int($1) }
}

func canonicalJSONObject(_ data: Data) throws -> [String: Any] {
    let object: Any
    do {
        object = try JSONSerialization.jsonObject(with: data)
    } catch {
        throw VideoFailure.invalid("json_invalid")
    }
    guard let dictionary = object as? [String: Any] else {
        throw VideoFailure.invalid("json_shape_invalid")
    }
    let canonical = try JSONSerialization.data(
        withJSONObject: dictionary, options: [.sortedKeys, .withoutEscapingSlashes]
    )
    try require(canonical == data, "json_not_canonical")
    return dictionary
}

func jsonObject(_ data: Data, keys: Set<String>) throws -> [String: Any] {
    let dictionary = try canonicalJSONObject(data)
    try require(Set(dictionary.keys) == keys, "json_fields_invalid")
    return dictionary
}

func decodeConfiguration(_ data: Data) throws -> Configuration {
    let baseKeys: Set<String> = [
        "schemaVersion", "codec", "container", "width", "height",
        "maxFrames", "maxCompressedBytes", "maxDecodedPixels",
        "maxOutputBytes", "faultMode",
    ]
    let object = try canonicalJSONObject(data)
    guard let schema = object["schemaVersion"] as? Int else {
        throw VideoFailure.invalid("configuration_invalid")
    }
    let v2Keys = baseKeys.union(["targetBitrate", "maxKeyFrameInterval"])
    let expectedKeys: Set<String>
    switch schema {
    case 1: expectedKeys = baseKeys
    case 2: expectedKeys = v2Keys
    default: throw VideoFailure.invalid("protocol_version_unsupported")
    }
    _ = try jsonObject(data, keys: expectedKeys)
    let value: Configuration
    do { value = try JSONDecoder().decode(Configuration.self, from: data) }
    catch { throw VideoFailure.invalid("configuration_invalid") }
    try require(value.schemaVersion == schema, "configuration_invalid")
    try require(value.codec == "h264" && value.container == "mp4",
                "codec_invalid")
    try require((2...4096).contains(value.width)
                && (2...4096).contains(value.height)
                && value.width % 2 == 0 && value.height % 2 == 0,
                "geometry_invalid")
    try require(value.width <= absoluteMaxPixels / value.height,
                "pixel_limit_exceeded")
    try require((1...256).contains(value.maxFrames), "frame_limit_invalid")
    try require((1...absoluteMaxCompressedBytes).contains(value.maxCompressedBytes),
                "compressed_limit_invalid")
    try require((1...absoluteMaxPixels).contains(value.maxDecodedPixels)
                && value.width * value.height <= value.maxDecodedPixels,
                "decoded_pixel_limit_invalid")
    try require((4096...absoluteMaxOutputBytes).contains(value.maxOutputBytes),
                "output_limit_invalid")
    try require(["none", "stall-finalization", "stall-after-first-accept",
                 "fail-write"].contains(value.faultMode), "fault_mode_invalid")
    if schema == 1 {
        try require(value.targetBitrate == nil && value.maxKeyFrameInterval == nil,
                    "configuration_fields_invalid")
    } else {
        try require(value.targetBitrate.map { (128_000...4_000_000).contains($0) } == true,
                    "target_bitrate_invalid")
        try require(value.maxKeyFrameInterval.map { (1...60).contains($0) } == true,
                    "keyframe_interval_invalid")
    }
    return value
}

func decodeFrameMetadata(_ data: Data, configuration: Configuration) throws -> FrameMetadata {
    let common: Set<String> = [
        "schemaVersion", "mimeType", "width", "height",
        "acquisitionSequence", "digest", "ptsNs", "timingSource",
        "uncertaintyNs", "providerIncarnation", "nativeIncarnation",
        "ptsRelation",
    ]
    guard let dictionary = try? JSONSerialization.jsonObject(with: data)
        as? [String: Any] else {
        throw VideoFailure.invalid("frame_metadata_invalid")
    }
    let timingSource = dictionary["timingSource"] as? String
    let expected = timingSource == "native-unmapped"
        ? common.union(["displayRecordingOffsetMs"])
        : common.union(["earliestRecordingOffsetMs", "latestRecordingOffsetMs"])
    _ = try jsonObject(data, keys: expected)
    let value: FrameMetadata
    do { value = try JSONDecoder().decode(FrameMetadata.self, from: data) }
    catch { throw VideoFailure.invalid("frame_metadata_invalid") }
    try require(value.schemaVersion == 1, "protocol_version_unsupported")
    try require(value.mimeType == "image/png" || value.mimeType == "image/jpeg",
                "mime_type_invalid")
    try require(value.width == configuration.width
                && value.height == configuration.height,
                "frame_geometry_changed")
    try require(value.acquisitionSequence > 0, "acquisition_sequence_invalid")
    try require(value.digest.range(of: "^[0-9a-f]{64}$",
                                   options: .regularExpression) != nil,
                "frame_digest_invalid")
    try require(value.ptsNs >= 0 && value.ptsNs <= 600_000_000_000, "pts_invalid")
    try require(value.uncertaintyNs >= 0
                && value.uncertaintyNs <= 60_000_000_000,
                "uncertainty_invalid")
    try require(value.providerIncarnation.range(
                    of: "^[a-z][a-z0-9_-]{0,63}$",
                    options: .regularExpression) != nil
                && value.nativeIncarnation.range(
                    of: "^[a-z][a-z0-9_-]{0,63}$",
                    options: .regularExpression) != nil,
                "frame_incarnation_invalid")
    if value.timingSource == "native-unmapped" {
        try require(value.ptsRelation == "display-publication-order-only"
                    && value.displayRecordingOffsetMs != nil
                    && value.earliestRecordingOffsetMs == nil
                    && value.latestRecordingOffsetMs == nil,
                    "native_timing_claim_invalid")
    } else {
        try require((value.timingSource == "host-acquired"
                     || value.timingSource == "provider-mapped")
                    && value.ptsRelation == "recording-elapsed"
                    && value.displayRecordingOffsetMs == nil
                    && value.earliestRecordingOffsetMs != nil
                    && value.latestRecordingOffsetMs != nil
                    && value.earliestRecordingOffsetMs! <= value.latestRecordingOffsetMs!,
                    "capture_interval_invalid")
    }
    return value
}

func sha256(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

func decodePixelBuffer(_ data: Data, metadata: FrameMetadata,
                       configuration: Configuration) throws -> CVPixelBuffer {
    try require(sha256(data) == metadata.digest, "frame_digest_mismatch")
    guard let source = CGImageSourceCreateWithData(data as CFData, nil) else {
        throw VideoFailure.invalid("image_decode_failed")
    }
    try require(CGImageSourceGetCount(source) == 1, "image_count_invalid")
    guard let sourceType = CGImageSourceGetType(source) as String? else {
        throw VideoFailure.invalid("image_type_missing")
    }
    let expectedType = metadata.mimeType == "image/png"
        ? UTType.png.identifier : UTType.jpeg.identifier
    try require(sourceType == expectedType, "mime_decode_mismatch")
    guard let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil)
        as? [CFString: Any],
          let widthValue = properties[kCGImagePropertyPixelWidth] as? NSNumber,
          let heightValue = properties[kCGImagePropertyPixelHeight] as? NSNumber else {
        throw VideoFailure.invalid("image_properties_missing")
    }
    let width = widthValue.intValue, height = heightValue.intValue
    try require(width == metadata.width && height == metadata.height,
                "decoded_geometry_mismatch")
    try require(width > 0 && height > 0
                && width <= configuration.maxDecodedPixels / height,
                "decoded_pixel_limit_exceeded")
    if let orientation = properties[kCGImagePropertyOrientation] as? NSNumber {
        try require(orientation.intValue == 1, "image_orientation_unsupported")
    }
    guard let image = CGImageSourceCreateImageAtIndex(source, 0, [
        kCGImageSourceShouldCacheImmediately: true,
        kCGImageSourceShouldAllowFloat: false,
    ] as CFDictionary) else {
        throw VideoFailure.invalid("image_decode_failed")
    }
    var optionalBuffer: CVPixelBuffer?
    let attributes: [CFString: Any] = [
        kCVPixelBufferCGImageCompatibilityKey: true,
        kCVPixelBufferCGBitmapContextCompatibilityKey: true,
        kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary,
    ]
    let status = CVPixelBufferCreate(
        kCFAllocatorDefault, width, height, kCVPixelFormatType_32BGRA,
        attributes as CFDictionary, &optionalBuffer
    )
    try require(status == kCVReturnSuccess && optionalBuffer != nil,
                "pixel_buffer_allocation_failed")
    let buffer = optionalBuffer!
    CVPixelBufferLockBaseAddress(buffer, [])
    defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
    guard let base = CVPixelBufferGetBaseAddress(buffer) else {
        throw VideoFailure.invalid("pixel_buffer_unavailable")
    }
    memset(base, 0, CVPixelBufferGetDataSize(buffer))
    let bitmap = CGBitmapInfo.byteOrder32Little.rawValue
        | CGImageAlphaInfo.premultipliedFirst.rawValue
    guard let context = CGContext(
        data: base,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: bitmap
    ) else { throw VideoFailure.invalid("image_context_failed") }
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    return buffer
}

func writeJSON(_ value: [String: Any]) throws {
    let data = try JSONSerialization.data(withJSONObject: value,
                                          options: [.sortedKeys])
    try require(data.count <= maxMetadataBytes, "helper_output_too_large")
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0a]))
}

func fsyncFileAndDirectory(_ url: URL) throws {
    let descriptor = Darwin.open(url.path, O_RDONLY | O_NOFOLLOW)
    try require(descriptor >= 0, "output_open_failed")
    defer { Darwin.close(descriptor) }
    try require(Darwin.fsync(descriptor) == 0, "output_fsync_failed")
    let directory = url.deletingLastPathComponent()
    let directoryDescriptor = Darwin.open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    try require(directoryDescriptor >= 0, "output_directory_open_failed")
    defer { Darwin.close(directoryDescriptor) }
    try require(Darwin.fsync(directoryDescriptor) == 0,
                "output_directory_fsync_failed")
}

func configureFileLimit(_ maximum: Int) throws {
    var limit = rlimit(rlim_cur: rlim_t(maximum), rlim_max: rlim_t(maximum))
    try require(setrlimit(RLIMIT_FSIZE, &limit) == 0, "file_limit_failed")
}

func encode() throws {
    let arguments = Array(CommandLine.arguments.dropFirst())
    try require(arguments == ["--protocol-stdio"], "arguments_invalid")
    let magic = try readExact(protocolMagic.count)
    try require(magic == protocolMagic, "protocol_magic_invalid")
    let configLength = try uint32(try readExact(4))
    try require((1...maxConfigBytes).contains(configLength),
                "configuration_length_invalid")
    let configuration = try decodeConfiguration(try readExact(configLength))
    try configureFileLimit(configuration.maxOutputBytes)

    let directory = URL(fileURLWithPath: FileManager.default.currentDirectoryPath,
                        isDirectory: true)
    let partialURL = directory.appendingPathComponent("segment.partial.mp4")
    let finalURL = directory.appendingPathComponent("segment.mp4")
    try require(!FileManager.default.fileExists(atPath: partialURL.path)
                && !FileManager.default.fileExists(atPath: finalURL.path),
                "output_already_exists")
    var successful = false
    defer {
        if !successful {
            try? FileManager.default.removeItem(at: partialURL)
            try? FileManager.default.removeItem(at: finalURL)
        }
    }

    let writer = try AVAssetWriter(outputURL: partialURL, fileType: .mp4)
    let bitrate = configuration.targetBitrate ?? max(
        2_000_000,
        min(20_000_000, configuration.width * configuration.height * 160)
    )
    let keyFrameInterval = configuration.maxKeyFrameInterval ?? 1
    let settings: [String: Any] = [
        AVVideoCodecKey: AVVideoCodecType.h264,
        AVVideoWidthKey: configuration.width,
        AVVideoHeightKey: configuration.height,
        AVVideoCompressionPropertiesKey: [
            AVVideoAverageBitRateKey: bitrate,
            AVVideoMaxKeyFrameIntervalKey: keyFrameInterval,
            AVVideoAllowFrameReorderingKey: false,
        ],
    ]
    let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
    input.expectsMediaDataInRealTime = false
    let adaptor = AVAssetWriterInputPixelBufferAdaptor(
        assetWriterInput: input,
        sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA),
            kCVPixelBufferWidthKey as String: configuration.width,
            kCVPixelBufferHeightKey as String: configuration.height,
            kCVPixelBufferIOSurfacePropertiesKey as String: [:],
        ]
    )
    try require(writer.canAdd(input), "writer_input_rejected")
    writer.add(input)
    if !writer.startWriting() {
        let code = (writer.error as NSError?)?.code ?? 0
        throw VideoFailure.invalid("writer_start_failed_\(code)")
    }
    writer.startSession(atSourceTime: .zero)

    var frameCount = 0
    var compressedBytes = 0
    var previousSequence: Int64 = 0
    var previousPTS: Int64 = -1
    var sawFinish = false
    while !sawFinish {
        let header = try readExact(9)
        guard let recordType = header.first else {
            throw VideoFailure.invalid("record_header_invalid")
        }
        let metadataLength = try uint32(header.subdata(in: 1..<5))
        let bodyLength = try uint32(header.subdata(in: 5..<9))
        if recordType == Character("E").asciiValue! {
            try require(metadataLength == 0 && bodyLength == 0,
                        "finish_record_invalid")
            sawFinish = true
            continue
        }
        try require(recordType == Character("F").asciiValue!, "record_type_invalid")
        try require((1...maxMetadataBytes).contains(metadataLength),
                    "frame_metadata_length_invalid")
        try require(bodyLength > 0
                    && compressedBytes <= configuration.maxCompressedBytes - bodyLength,
                    "compressed_input_limit_exceeded")
        try require(frameCount < configuration.maxFrames, "frame_count_limit_exceeded")
        let metadata = try decodeFrameMetadata(
            try readExact(metadataLength), configuration: configuration
        )
        let body = try readExact(bodyLength)
        try require(metadata.acquisitionSequence > previousSequence,
                    "acquisition_sequence_not_increasing")
        try require(frameCount > 0 || metadata.ptsNs == 0, "first_pts_not_zero")
        try require(metadata.ptsNs > previousPTS, "pts_not_increasing")
        let pixelBuffer = try decodePixelBuffer(
            body, metadata: metadata, configuration: configuration
        )
        let readyDeadline = Date().addingTimeInterval(5)
        while !input.isReadyForMoreMediaData && writer.status == .writing
                && Date() < readyDeadline {
            Thread.sleep(forTimeInterval: 0.001)
        }
        try require(input.isReadyForMoreMediaData && writer.status == .writing,
                    "writer_backpressure_timeout")
        let timestamp = CMTime(value: metadata.ptsNs, timescale: 1_000_000_000)
        try require(adaptor.append(pixelBuffer, withPresentationTime: timestamp),
                    "frame_append_failed")
        frameCount += 1
        compressedBytes += bodyLength
        previousSequence = metadata.acquisitionSequence
        previousPTS = metadata.ptsNs
        try writeJSON([
            "schemaVersion": 1,
            "type": "accepted",
            "acquisitionSequence": metadata.acquisitionSequence,
            "ptsNs": metadata.ptsNs,
        ])
        if configuration.faultMode == "stall-after-first-accept" && frameCount == 1 {
            Thread.sleep(forTimeInterval: 60)
        }
        if configuration.faultMode == "fail-write" && frameCount == 1 {
            throw VideoFailure.invalid("injected_disk_full")
        }
    }
    try require(frameCount > 0, "empty_segment")
    if let trailing = try FileHandle.standardInput.read(upToCount: 1) {
        try require(trailing.isEmpty, "trailing_input")
    }
    input.markAsFinished()
    if configuration.faultMode == "stall-finalization" {
        Thread.sleep(forTimeInterval: 60)
    }
    let finished = DispatchSemaphore(value: 0)
    writer.finishWriting { finished.signal() }
    try require(finished.wait(timeout: .now() + 30) == .success,
                "writer_finish_timeout")
    try require(writer.status == .completed, "writer_finish_failed")
    try fsyncFileAndDirectory(partialURL)
    try FileManager.default.moveItem(at: partialURL, to: finalURL)
    try fsyncFileAndDirectory(finalURL)
    let attributes = try FileManager.default.attributesOfItem(atPath: finalURL.path)
    guard let sizeNumber = attributes[.size] as? NSNumber else {
        throw VideoFailure.invalid("output_size_missing")
    }
    let size = sizeNumber.intValue
    try require(size > 0 && size <= configuration.maxOutputBytes,
                "output_size_limit_exceeded")
    let output = try Data(contentsOf: finalURL, options: [.mappedIfSafe])
    try require(output.count == size, "output_size_changed")
    try writeJSON([
        "schemaVersion": 1,
        "type": "finished",
        "codec": "h264",
        "container": "mp4",
        "frames": frameCount,
        "bytes": size,
        "sha256": sha256(output),
    ])
    successful = true
}

do {
    try encode()
} catch VideoFailure.invalid(let reason) {
    FileHandle.standardError.write(Data(("video_helper_failed:" + reason + "\n").utf8))
    exit(2)
} catch {
    FileHandle.standardError.write(Data("video_helper_failed:internal\n".utf8))
    exit(2)
}
