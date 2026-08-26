import Foundation

public final class CaptureCoordinator {
    private let configuration: CaptureConfiguration
    private let sink: AudioFrameSink
    private var microphone: MicrophoneCapture?
    private var systemAudio: SystemAudioCapture?
    private var running = false

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
            transcriptHandler: transcriptHandler,
            statusHandler: statusHandler
        )
    }

    public func start() async throws {
        guard !running else { return }
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
            running = true
        } catch {
            microphone?.stop()
            microphone = nil
            await systemAudio?.stop()
            systemAudio = nil
            await sink.disconnect(sessionID: configuration.sessionID, reason: "startup_failed")
            throw error
        }
    }

    public func stop() async {
        guard running else { return }
        microphone?.stop()
        microphone = nil
        await systemAudio?.stop()
        systemAudio = nil
        running = false
        await sink.disconnect(sessionID: configuration.sessionID, reason: "user_stopped")
    }
}
