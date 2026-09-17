#if DEBUG
import Foundation

enum RLAutoConfig {
    static let profileJSON = #"{"schemaVersion":1,"kind":"uikit-runtime-v1","applicationId":"io.reproloop.sample.ios","project":"ReproLoop.xcodeproj","target":"ReproSample","cases":["counter","duplicate-submit","reset"],"textTargets":["counter.name"],"numericTargets":["counter.count"],"tapTargets":["counter.add","counter.next","counter.reset"],"backTarget":"counter.back","screenTargets":{"counter.screen.main":"main","counter.screen.details":"details"},"startState":{"screen":"main","nodes":{"counter.name":"","counter.count":"0"}}}"#
    static let profileDigest = "9945d4fbf9ded0d23476bc1c0247b595404c7863b83f6d5629946a0cae8e2d83"
}
#endif
