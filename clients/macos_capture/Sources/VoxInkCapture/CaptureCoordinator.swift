import Foundation

/// An actor so the signal handler that drives pause and resume cannot mutate
/// this state concurrently with a stop arriving on another task.
public actor CaptureCoordinator {
    public enum State: String, Sendable {
        case idle
        case recording
        /// Draining the sources before the pause is complete.
        case pausing
        case paused
    }

    private let configuration: CaptureConfiguration
    private let sink: AudioFrameSink
    private var microphone: MicrophoneCapture?
    private var systemAudio: SystemAudioCapture?
    private var state: State = .idle
    /// A resume that arrived while `pause()` was still draining.
    private var pendingResume = false

    public init(
        configuration: CaptureConfiguration,
        sink: AudioFrameSink? = nil,
        transcriptHandler: WebSocketAudioFrameSink.TranscriptHandler? = nil,
        statusHandler: WebSocketAudioFrameSink.StatusHandler? = nil
    ) throws {
        guard configuration.microphoneEnabled || configuration.systemAudio != nil else {
            throw CaptureError.noCaptureSource
        }
        try LocalEndpointPolicy.validate(configuration.serviceURL)
        self.configuration = configuration
        self.sink = try sink ?? WebSocketAudioFrameSink(
            endpoint: configuration.serviceURL,
            authToken: configuration.authToken,
            applications: configuration.selectedApplications,
            retainAudio: configuration.retainAudio,
            transcriptHandler: transcriptHandler,
            statusHandler: statusHandler
        )
    }

    public var currentState: State { state }

    /// Frames discarded across all sources because transcription fell behind.
    public var droppedFrameCount: Int {
        (microphone?.droppedFrameCount ?? 0) + (systemAudio?.droppedFrameCount ?? 0)
    }

    public func start() async throws {
        guard state == .idle else { return }
        var sources = [AudioSource]()
        if configuration.microphoneEnabled { sources.append(.microphone) }
        if configuration.systemAudio != nil { sources.append(.system) }
        try await sink.connect(sessionID: configuration.sessionID, sources: sources)
        do {
            if configuration.microphoneEnabled {
                let microphone = MicrophoneCapture(
                    sessionID: configuration.sessionID,
                    sampleRate: configuration.sampleRate,
                    sink: sink
                )
                try await microphone.start()
                self.microphone = microphone
            }
            if let selection = configuration.systemAudio {
                let systemAudio = SystemAudioCapture(
                    selection: selection,
                    sessionID: configuration.sessionID,
                    sampleRate: configuration.sampleRate,
                    sink: sink
                )
                try await systemAudio.start()
                self.systemAudio = systemAudio
            }
            state = .recording
        } catch {
            await microphone?.stop()
            microphone = nil
            await systemAudio?.stop()
            systemAudio = nil
            await sink.disconnect(sessionID: configuration.sessionID, reason: "startup_failed")
            throw error
        }
    }

    /// Stop delivering audio, then tell the service. Draining first means the
    /// utterance in progress is finalized instead of being cut off, and no
    /// audio arrives after the service has paused the session.
    public func pause() async {
        guard state == .recording else { return }
        state = .pausing
        await microphone?.pause()
        await systemAudio?.pause()
        await sink.pause(sessionID: configuration.sessionID)
        // A stop can land while the drain is in flight; it owns the state then.
        guard state == .pausing else { return }
        state = .paused
        if pendingResume {
            pendingResume = false
            await resume()
        }
    }

    public func resume() async {
        if state == .pausing {
            // The drain has not finished, so resuming now would restart sources
            // that pause() is still stopping. The service has already told the
            // user this was accepted, so record it and let pause() honour it
            // rather than dropping it and recording nothing.
            pendingResume = true
            return
        }
        guard state == .paused else { return }
        await sink.resume(sessionID: configuration.sessionID)
        microphone?.resume()
        systemAudio?.resume()
        state = .recording
    }

    public func stop() async {
        guard state != .idle else { return }
        pendingResume = false
        await microphone?.stop()
        microphone = nil
        await systemAudio?.stop()
        systemAudio = nil
        state = .idle
        await sink.disconnect(sessionID: configuration.sessionID, reason: "user_stopped")
    }
}
