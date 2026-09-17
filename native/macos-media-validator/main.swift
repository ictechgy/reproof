// Trusted local decoder for inert issue archives. No content-supplied commands.
import Foundation
import AVFoundation
import CoreMedia
import CoreVideo
import ImageIO
import CoreGraphics

enum Failure: Error { case invalid }
func require(_ condition: Bool) throws {
    if !condition { throw Failure.invalid }
}
func dimensions(_ width: Int, _ height: Int) throws {
    try require(width > 0 && height > 0 && width <= 4096 && height <= 4096
                && width * height <= 4_194_304)
}

do {
    let args = Array(CommandLine.arguments.dropFirst())
    try require(args.count == 2 && ["image/png", "image/jpeg", "video/mp4"].contains(args[1]))
    let url = URL(fileURLWithPath: args[0])
    let properties = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
    try require(properties.isRegularFile == true && properties.isSymbolicLink != true
                && (properties.fileSize ?? 0) > 0
                && (properties.fileSize ?? Int.max) <= 32 * 1024 * 1024)
    var report: [String: Any]
    if args[1] == "video/mp4" {
        var options: [String: Any] = [
            AVURLAssetReferenceRestrictionsKey: AVAssetReferenceRestrictions.forbidAll.rawValue
        ]
        // G2 stages digest-bound bytes under opaque names. Tell AVFoundation
        // the requested parser without relying on a filename extension; codec,
        // dimensions and every decoded frame are still checked below.
        if #available(macOS 14.0, *) {
            options[AVURLAssetOverrideMIMETypeKey] = "video/mp4"
        } else {
            throw Failure.invalid
        }
        let asset = AVURLAsset(url: url, options: options)
        let tracks = try await asset.loadTracks(withMediaType: .video)
        let audio = try await asset.loadTracks(withMediaType: .audio)
        try require(tracks.count == 1 && audio.isEmpty)
        let descriptions = try await tracks[0].load(.formatDescriptions)
        try require(descriptions.count == 1
                    && CMFormatDescriptionGetMediaSubType(descriptions[0]) == kCMVideoCodecType_H264)
        let geometry = CMVideoFormatDescriptionGetDimensions(descriptions[0])
        let width = Int(geometry.width), height = Int(geometry.height)
        try dimensions(width, height)
        let reader = try AVAssetReader(asset: asset)
        let output = AVAssetReaderTrackOutput(track: tracks[0], outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ])
        output.alwaysCopiesSampleData = false
        try require(reader.canAdd(output))
        reader.add(output)
        try require(reader.startReading())
        var times: [Double] = []
        while let sample = output.copyNextSampleBuffer() {
            try require(times.count < 256)
            guard let buffer = CMSampleBufferGetImageBuffer(sample) else { throw Failure.invalid }
            try require(CVPixelBufferGetWidth(buffer) == width && CVPixelBufferGetHeight(buffer) == height)
            let milliseconds = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sample)) * 1000
            try require(milliseconds.isFinite && milliseconds >= 0 && milliseconds <= 60_000
                        && (times.last == nil || milliseconds > times.last!))
            // Access the decoded planes: metadata alone is not a decode check.
            try require(CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess)
            let present = CVPixelBufferGetBaseAddress(buffer) != nil
            CVPixelBufferUnlockBaseAddress(buffer, .readOnly)
            try require(present)
            times.append(milliseconds)
        }
        try require(reader.status == .completed && !times.isEmpty)
        report = ["width": width, "height": height, "codec": "h264",
                  "frameCount": times.count, "presentationTimesMs": times]
    } else {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let type = CGImageSourceGetType(source) as String?,
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int else { throw Failure.invalid }
        try require(CGImageSourceGetCount(source) == 1)
        try require(type == (args[1] == "image/png" ? "public.png" : "public.jpeg"))
        try dimensions(width, height)
        guard let image = CGImageSourceCreateImageAtIndex(source, 0, [
            kCGImageSourceShouldCacheImmediately: true
        ] as CFDictionary),
              let context = CGContext(data: nil, width: width, height: height,
                                      bitsPerComponent: 8, bytesPerRow: width * 4,
                                      space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw Failure.invalid
        }
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        try require(CGImageSourceGetStatus(source) == .statusComplete
                    && CGImageSourceGetStatusAtIndex(source, 0) == .statusComplete)
        report = ["width": width, "height": height, "mimeType": args[1]]
    }
    let result = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
    try require(result.count <= 16 * 1024)
    FileHandle.standardOutput.write(result)
} catch {
    // Never print imported content, a local path, or framework error details.
    FileHandle.standardError.write(Data("media_validation_failed\n".utf8))
    exit(2)
}
