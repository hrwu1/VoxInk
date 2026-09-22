import Foundation

public enum AudioSource: String, Codable, Sendable {
    case microphone
    case system
}

public enum SystemAudioSelection: Equatable, Sendable {
    case all
    case applications(bundleIdentifiers: Set<String>)
}

/// Which macOS facility delivers system audio.
public enum SystemAudioBackend: String, Sendable, CaseIterable {
    /// Prefer a Core Audio process tap and fall back to ScreenCaptureKit.
    case auto
    /// Core Audio process taps (macOS 14.2+). Narrower permission, works
    /// while output is muted, and can isolate one application per stream.
    case tap
    /// ScreenCaptureKit audio, mixed into one stream.
    case screenCaptureKit = "screencapturekit"
}

public struct CaptureConfiguration: Sendable {
    public var sessionID: String
    public var microphoneEnabled: Bool
    public var systemAudio: SystemAudioSelection?
    public var sampleRate: Int
    public var serviceURL: URL
    public var authToken: String?
    public var retainAudio: Bool
    public var backend: SystemAudioBackend
    /// Core Audio device UID of the microphone to open; nil means the default.
    public var microphoneDeviceUID: String?
    /// Put the microphone's input volume back when another process lowers it
    /// during the session. Off unless asked for: a call app's automatic gain
    /// and this would otherwise take turns.
    public var holdMicrophoneGain: Bool
    /// Manual language override handed to the service (for example "en" or
    /// "zh"); nil lets the model detect it.
    public var language: String?
    /// Display names for selected applications, keyed by bundle identifier,
    /// so a per-application stream can be labeled the way the user knows it.
    public var applicationNames: [String: String]
    /// Where audio the service never received is written as WAV files; nil
    /// keeps it under the user's Application Support directory.
    public var spoolDirectory: URL?

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
        retainAudio: Bool = false,
        backend: SystemAudioBackend = .auto,
        microphoneDeviceUID: String? = nil,
        holdMicrophoneGain: Bool = false,
        language: String? = nil,
        applicationNames: [String: String] = [:],
        spoolDirectory: URL? = nil
    ) {
        self.sessionID = sessionID
        self.microphoneEnabled = microphoneEnabled
        self.systemAudio = systemAudio
        self.sampleRate = sampleRate
        self.retainAudio = retainAudio
        self.backend = backend
        self.microphoneDeviceUID = microphoneDeviceUID
        self.holdMicrophoneGain = holdMicrophoneGain
        self.language = language
        self.applicationNames = applicationNames
        self.spoolDirectory = spoolDirectory
        let safeSessionID = sessionID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? sessionID
        self.serviceURL = serviceURL ?? URL(string: "ws://127.0.0.1:8300/v1/sessions/\(safeSessionID)/stream")!
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
    /// For a per-application system stream, the bundle identifier it isolates.
    public let channel: String?
    public let channelName: String?

    public init(
        sessionID: String,
        streamID: String,
        source: AudioSource,
        sequence: UInt64,
        timestampNanoseconds: UInt64,
        sampleRate: Int,
        channels: Int = 1,
        pcmS16LE: Data,
        applicationBundleIdentifiers: [String]? = nil,
        channel: String? = nil,
        channelName: String? = nil
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
        self.channel = channel
        self.channelName = channelName
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
    case microphoneDeviceUnavailable(String)
    case screenRecordingPermissionDenied
    case noDisplayAvailable
    case noSelectedApplications(Set<String>)
    case audioFormatUnavailable(String)
    case conversionFailed
    case tapUnavailable(String)

    public var errorDescription: String? {
        switch self {
        case .noCaptureSource:
            return "Enable microphone capture, system capture, or both."
        case .invalidServiceURL(let url):
            return "The capture service must be a ws:// or wss:// loopback URL; received \(url.absoluteString)."
        case .microphonePermissionDenied:
            return "Microphone access was denied. Enable it in System Settings > Privacy & Security > Microphone."
        case .microphoneDeviceUnavailable(let uid):
            return "The selected microphone is not connected: \(uid)."
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
        case .tapUnavailable(let detail):
            return "System audio could not be tapped: \(detail)"
        }
    }
}

/// What a stream does when it has no socket.
///
/// The first time is an ordinary connect. After that the socket died under a
/// live stream, and reopening it is a reconnect the user must hear about: the
/// service holds the session open for a grace period, so a prompt reconnect
/// keeps the recording whole. A reconnect that fails is retried with
/// exponential backoff for as long as the recording runs, never abandoned:
/// the frames captured meanwhile are queued, and a service that is gone for
/// good costs a spool file, not the recording. Only a service that says the
/// session is unavailable ends the stream, because it has finalized the
/// session and would keep nothing sent afterwards.
public struct StreamReconnectPolicy: Sendable {
    public enum Decision: Equatable, Sendable {
        case connect
        /// Try now; `attempt` is one more than the consecutive failures so far.
        case reconnect(attempt: Int)
        /// Still backing off from the last failure; keep the frame for later.
        case wait(seconds: Double)
        /// The session is finalized; nothing sent would be kept.
        case dead
    }

    public let initialDelaySeconds: Double
    public let maximumDelaySeconds: Double
    private var started = Set<String>()
    private var reconnecting = Set<String>()
    private var failures = [String: Int]()
    private var notBefore = [String: Double]()
    private var dead = Set<String>()

    public init(initialDelaySeconds: Double = 0.25, maximumDelaySeconds: Double = 5) {
        self.initialDelaySeconds = initialDelaySeconds
        self.maximumDelaySeconds = maximumDelaySeconds
    }

    /// Called when a frame arrives for a stream with no socket. `now` is a
    /// monotonic clock in seconds, the same one `attemptFailed` was given.
    public mutating func socketNeeded(for streamID: String, now: Double) -> Decision {
        if dead.contains(streamID) { return .dead }
        if !started.contains(streamID) {
            started.insert(streamID)
            return .connect
        }
        if let earliest = notBefore[streamID], now < earliest {
            return .wait(seconds: earliest - now)
        }
        reconnecting.insert(streamID)
        return .reconnect(attempt: (failures[streamID] ?? 0) + 1)
    }

    /// The socket could not be opened or `stream.start` not sent. Returns
    /// how long to wait before the next attempt.
    public mutating func attemptFailed(_ streamID: String, now: Double) -> Double {
        let count = (failures[streamID] ?? 0) + 1
        failures[streamID] = count
        let delay = min(initialDelaySeconds * pow(2.0, Double(count - 1)), maximumDelaySeconds)
        notBefore[streamID] = now + delay
        return delay
    }

    /// The service accepted the stream. Returns whether this closed a reconnect.
    public mutating func streamReady(_ streamID: String) -> Bool {
        failures[streamID] = nil
        notBefore[streamID] = nil
        return reconnecting.remove(streamID) != nil
    }

    /// The service refused the stream because its session is gone.
    public mutating func sessionUnavailable(_ streamID: String) {
        dead.insert(streamID)
    }

    public func isDead(_ streamID: String) -> Bool { dead.contains(streamID) }
}
