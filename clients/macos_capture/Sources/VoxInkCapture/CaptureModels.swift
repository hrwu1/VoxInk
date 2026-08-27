import Foundation

public enum AudioSource: String, Codable, Sendable {
    case microphone
    case system
}

public enum SystemAudioSelection: Equatable, Sendable {
    case all
    case applications(bundleIdentifiers: Set<String>)
}

public struct CaptureConfiguration: Sendable {
    public var sessionID: String
    public var microphoneEnabled: Bool
    public var systemAudio: SystemAudioSelection?
    public var sampleRate: Int
    public var serviceURL: URL
    public var authToken: String?
    public var retainAudio: Bool

    /// Bundle identifiers this session captures, empty for microphone-only or
    /// all-system capture. Every stream reports the same list so whichever one
    /// opens first records it in the transcript metadata.
    public var selectedApplications: [String] {
        guard case .applications(let identifiers) = systemAudio else { return [] }
        return identifiers.sorted()
    }

    public init(
        sessionID: String,
        microphoneEnabled: Bool,
        systemAudio: SystemAudioSelection?,
        sampleRate: Int = 16_000,
        serviceURL: URL? = nil,
        authToken: String? = ProcessInfo.processInfo.environment["VOXINK_AUTH_SECRET"],
        retainAudio: Bool = false
    ) {
        self.sessionID = sessionID
        self.microphoneEnabled = microphoneEnabled
        self.systemAudio = systemAudio
        self.sampleRate = sampleRate
        self.retainAudio = retainAudio
        let safeSessionID = sessionID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? sessionID
        self.serviceURL = serviceURL ?? URL(string: "ws://127.0.0.1:8765/v1/sessions/\(safeSessionID)/stream")!
        self.authToken = authToken
    }
}

public struct AudioFrame: Equatable, Sendable {
    public let sessionID: String
    public let streamID: String
    public let source: AudioSource
    public let sequence: UInt64
    public let timestampNanoseconds: UInt64
    public let sampleRate: Int
    public let channels: Int
    public let pcmS16LE: Data
    public let applicationBundleIdentifiers: [String]?

    public init(
        sessionID: String,
        streamID: String,
        source: AudioSource,
        sequence: UInt64,
        timestampNanoseconds: UInt64,
        sampleRate: Int,
        channels: Int = 1,
        pcmS16LE: Data,
        applicationBundleIdentifiers: [String]? = nil
    ) {
        self.sessionID = sessionID
        self.streamID = streamID
        self.source = source
        self.sequence = sequence
        self.timestampNanoseconds = timestampNanoseconds
        self.sampleRate = sampleRate
        self.channels = channels
        self.pcmS16LE = pcmS16LE
        self.applicationBundleIdentifiers = applicationBundleIdentifiers
    }
}

public struct CaptureStatusEvent: Codable, Equatable, Sendable {
    public enum Level: String, Codable, Sendable { case info, warning, error }

    public let type = "capture.status"
    public let schemaVersion = 1
    public let sessionID: String
    public let source: AudioSource?
    public let level: Level
    public let code: String
    public let message: String

    public init(sessionID: String, source: AudioSource?, level: Level, code: String, message: String) {
        self.sessionID = sessionID
        self.source = source
        self.level = level
        self.code = code
        self.message = message
    }

    enum CodingKeys: String, CodingKey {
        case type, source, level, code, message
        case schemaVersion = "schema_version"
        case sessionID = "session_id"
    }
}

public enum CaptureError: LocalizedError {
    case noCaptureSource
    case invalidServiceURL(URL)
    case microphonePermissionDenied
    case screenRecordingPermissionDenied
    case noDisplayAvailable
    case noSelectedApplications(Set<String>)
    case audioFormatUnavailable(String)
    case conversionFailed

    public var errorDescription: String? {
        switch self {
        case .noCaptureSource:
            return "Enable microphone capture, system capture, or both."
        case .invalidServiceURL(let url):
            return "The capture service must be a ws:// or wss:// loopback URL; received \(url.absoluteString)."
        case .microphonePermissionDenied:
            return "Microphone access was denied. Enable it in System Settings > Privacy & Security > Microphone."
        case .screenRecordingPermissionDenied:
            return "Screen and system-audio capture access was denied. Enable it in System Settings > Privacy & Security > Screen & System Audio Recording."
        case .noDisplayAvailable:
            return "ScreenCaptureKit reported no display to anchor the audio capture filter."
        case .noSelectedApplications(let identifiers):
            return "None of the selected applications are currently running: \(identifiers.sorted().joined(separator: ", "))."
        case .audioFormatUnavailable(let detail):
            return "The capture source exposed an unusable audio format: \(detail)."
        case .conversionFailed:
            return "AVAudioConverter could not normalize an audio frame."
        }
    }
}
