import AVFoundation
import XCTest
@testable import VoxInkCapture

final class ContractTests: XCTestCase {
    func testTranscriptWireEventMapsToCaption() throws {
        let json = Data(#"{"segment_id":"seg-1","source":"system","text":"hello","start_ms":10,"end_ms":30,"is_final":true}"#.utf8)
        let event = try JSONDecoder().decode(TranscriptWireEvent.self, from: json)
        XCTAssertEqual(event.captionItem.id, "seg-1")
        XCTAssertEqual(event.captionItem.source, .system)
        XCTAssertEqual(event.captionItem.text, "hello")
        XCTAssertTrue(event.captionItem.isFinal)
    }

    func testDefaultEndpointIncludesSessionID() {
        let configuration = CaptureConfiguration(
            sessionID: "session-1",
            microphoneEnabled: true,
            systemAudio: nil
        )
        XCTAssertEqual(
            configuration.serviceURL.absoluteString,
            "ws://127.0.0.1:8765/v1/sessions/session-1/stream"
        )
    }

    func testEndpointPolicyOnlyAllowsLoopbackWebSockets() throws {
        XCTAssertNoThrow(try LocalEndpointPolicy.validate(URL(string: "ws://127.0.0.1:8765/stream")!))
        XCTAssertNoThrow(try LocalEndpointPolicy.validate(URL(string: "wss://localhost/stream")!))
        XCTAssertNoThrow(try LocalEndpointPolicy.validate(URL(string: "ws://[::1]:8765/stream")!))
        XCTAssertThrowsError(try LocalEndpointPolicy.validate(URL(string: "ws://0.0.0.0:8765/stream")!))
        XCTAssertThrowsError(try LocalEndpointPolicy.validate(URL(string: "ws://192.168.1.10:8765/stream")!))
        XCTAssertThrowsError(try LocalEndpointPolicy.validate(URL(string: "https://127.0.0.1/stream")!))
    }

    func testPCMNormalizerDownmixesAndResamples() throws {
        let inputFormat = try XCTUnwrap(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 2, interleaved: false))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: 480))
        buffer.frameLength = 480
        for channel in 0..<2 {
            for frame in 0..<480 {
                buffer.floatChannelData?[channel][frame] = channel == 0 ? 0.25 : -0.25
            }
        }
        let data = try PCMNormalizer(outputSampleRate: 16_000).normalize(buffer)
        XCTAssertGreaterThan(data.count, 0)
        XCTAssertLessThanOrEqual(data.count, 322)
        XCTAssertEqual(data.count % 2, 0)
    }
}
