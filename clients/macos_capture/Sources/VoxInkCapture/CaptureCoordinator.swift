import Foundation

/// One running system-audio source, whichever facility provides it.
protocol SystemAudioSourceCapture: AnyObject {
    var droppedFrameCount: Int { get }
    func start() async throws
    func pause() async
    func resume()
    func stop() async
}

extension SystemAudioCapture: SystemAudioSourceCapture {}

@available(macOS 14.2, *)
extension ProcessTapCapture: SystemAudioSourceCapture {}

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
    private let statusHandler: WebSocketAudioFrameSink.StatusHandler?
    private var microphone: MicrophoneCapture?
    private var systemSources = [SystemAudioSourceCapture]()
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
        self.statusHandler = statusHandler
        self.sink = try sink ?? WebSocketAudioFrameSink(
            endpoint: configuration.serviceURL,
            authToken: configuration.authToken,
            applications: configuration.selectedApplications,
            retainAudio: configuration.retainAudio,
            language: configuration.language,
            spoolDirectory: configuration.spoolDirectory,
            transcriptHandler: transcriptHandler,
            statusHandler: statusHandler
        )
    }

    public var currentState: State { state }

    /// Frames discarded across all sources because transcription fell behind.
    public var droppedFrameCount: Int {
        (microphone?.droppedFrameCount ?? 0) + systemSources.reduce(0) { $0 + $1.droppedFrameCount }
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
                    sink: sink,
                    deviceUID: configuration.microphoneDeviceUID
                )
                try await microphone.start()
                self.microphone = microphone
            }
            if let selection = configuration.systemAudio {
                systemSources = try await startSystemSources(selection)
            }
            state = .recording
        } catch {
            await microphone?.stop()
            microphone = nil
            for source in systemSources { await source.stop() }
            systemSources = []
            await sink.disconnect(sessionID: configuration.sessionID, reason: "startup_failed")
            throw error
        }
    }

    /// Open the system sources the configuration asks for.
    ///
    /// With the tap backend, selected applications become one stream each so
    /// the transcript can say which application spoke. ScreenCaptureKit mixes
    /// every selected application into one stream, as before.
    private func startSystemSources(_ selection: SystemAudioSelection) async throws -> [SystemAudioSourceCapture] {
        let wantsTap = configuration.backend != .screenCaptureKit
        if wantsTap, #available(macOS 14.2, *) {
            var started = [SystemAudioSourceCapture]()
            do {
                for channel in tapChannels(selection) {
                    let tap = ProcessTapCapture(
                        channel: channel,
                        sessionID: configuration.sessionID,
                        sampleRate: configuration.sampleRate,
                        sink: sink
                    )
                    try await tap.start()
                    started.append(tap)
                }
                return started
            } catch {
                for source in started { await source.stop() }
                guard configuration.backend == .auto else { throw error }
                statusHandler?(CaptureStatusEvent(
                    sessionID: configuration.sessionID,
                    source: .system,
                    level: .warning,
                    code: "tap_fallback",
                    message: "Process tap unavailable (\(error.localizedDescription)); using ScreenCaptureKit."
                ))
            }
        } else if configuration.backend == .tap {
            throw CaptureError.tapUnavailable("process taps need macOS 14.2 or newer")
        }
        let capture = SystemAudioCapture(
            selection: selection,
            sessionID: configuration.sessionID,
            sampleRate: configuration.sampleRate,
            sink: sink
        )
        try await capture.start()
        return [capture]
    }

    @available(macOS 14.2, *)
    private func tapChannels(_ selection: SystemAudioSelection) -> [ProcessTapCapture.Channel] {
        switch selection {
        case .all:
            return [ProcessTapCapture.Channel(bundleIdentifier: nil, name: nil, streamID: "system-1")]
        case .applications(let identifiers):
            return identifiers.sorted().map { bundle in
                ProcessTapCapture.Channel(
                    bundleIdentifier: bundle,
                    name: configuration.applicationNames[bundle],
                    streamID: "system-\(bundle)"
                )
            }
        }
    }

    /// Stop delivering audio, then tell the service. Draining first means the
    /// utterance in progress is finalized instead of being cut off, and no
    /// audio arrives after the service has paused the session.
    public func pause() async {
        guard state == .recording else { return }
        state = .pausing
        await microphone?.pause()
        for source in systemSources { await source.pause() }
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
        for source in systemSources { source.resume() }
        state = .recording
    }

    public func stop() async {
        guard state != .idle else { return }
        pendingResume = false
        await microphone?.stop()
        microphone = nil
        for source in systemSources { await source.stop() }
        systemSources = []
        state = .idle
        await sink.disconnect(sessionID: configuration.sessionID, reason: "user_stopped")
    }
}
