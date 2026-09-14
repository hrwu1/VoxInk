import Foundation

public protocol AudioFrameSink: AnyObject {
    func connect(sessionID: String, sources: [AudioSource]) async throws
    func send(_ frame: AudioFrame) async throws
    func send(_ event: CaptureStatusEvent) async throws
    func pause(sessionID: String) async
    func resume(sessionID: String) async
    func disconnect(sessionID: String, reason: String) async
}

public enum LocalEndpointPolicy {
    public static func validate(_ url: URL) throws {
        guard let scheme = url.scheme?.lowercased(), ["ws", "wss"].contains(scheme),
              let host = url.host?.lowercased(),
              ["127.0.0.1", "localhost", "::1"].contains(host)
        else { throw CaptureError.invalidServiceURL(url) }
    }
}

public struct TranscriptWireEvent: Codable, Sendable {
    public let segmentID: String
    public let source: AudioSource
    public let text: String
    public let startMilliseconds: Int64
    public let endMilliseconds: Int64
    public let isFinal: Bool
    /// Bundle identifier of the application a system stream isolates, if any.
    public let channel: String?
    public let channelName: String?
    /// On-device translation of `text` into the other language, when enabled.
    public let translation: String?
    public let translationLanguage: String?
    /// A remembered voice the service recognized live, if any. Live
    /// diarization puts its anonymous label here instead.
    public let speaker: String?
    /// Set by live diarization: the label names a voice within this session
    /// only, and may still change.
    public let speakerProvisional: Bool?
    /// Set by live diarization when more than one voice shares the turn.
    public let speakerOverlap: Bool?

    public init(
        segmentID: String,
        source: AudioSource,
        text: String,
        startMilliseconds: Int64,
        endMilliseconds: Int64,
        isFinal: Bool,
        channel: String? = nil,
        channelName: String? = nil,
        translation: String? = nil,
        translationLanguage: String? = nil,
        speaker: String? = nil,
        speakerProvisional: Bool? = nil,
        speakerOverlap: Bool? = nil
    ) {
        self.segmentID = segmentID
        self.source = source
        self.text = text
        self.startMilliseconds = startMilliseconds
        self.endMilliseconds = endMilliseconds
        self.isFinal = isFinal
        self.channel = channel
        self.channelName = channelName
        self.translation = translation
        self.translationLanguage = translationLanguage
        self.speaker = speaker
        self.speakerProvisional = speakerProvisional
        self.speakerOverlap = speakerOverlap
    }

    enum CodingKeys: String, CodingKey {
        case text, source, channel, translation, speaker
        case segmentID = "segment_id"
        case startMilliseconds = "start_ms"
        case endMilliseconds = "end_ms"
        case isFinal = "is_final"
        case channelName = "channel_name"
        case translationLanguage = "translation_language"
        case speakerProvisional = "speaker_provisional"
        case speakerOverlap = "speaker_overlap"
    }
}

/// How a turn's speaker is written above it.
///
/// Live diarization names voices within one session and can revise them, so
/// its labels say so; a remembered enrolled voice is shown as its plain name.
public enum SpeakerLabel {
    public static func compose(speaker: String?, provisional: Bool, overlap: Bool) -> String? {
        let name = speaker?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard provisional else { return name.isEmpty ? nil : name }
        // Live diarization reports speech it could not attribute as an
        // unnamed turn; showing it keeps the caption from looking enrolled.
        return "\(name.isEmpty ? "Unassigned" : name)\(overlap ? " · overlap" : "") · provisional"
    }
}

/// A translation that arrived after its segment was already finalized.
public struct TranslationWireEvent: Codable, Sendable {
    public let segmentID: String
    public let text: String
    public let language: String?

    enum CodingKeys: String, CodingKey {
        case text, language
        case segmentID = "segment_id"
    }
}

/// One localhost WebSocket is opened per source stream. Each audio frame is sent
/// as an `audio.frame` JSON header immediately followed by raw PCM binary, which
/// matches the Python service's schema-version-1 streaming contract.
public actor WebSocketAudioFrameSink: AudioFrameSink {
    public typealias TranscriptHandler = @Sendable (TranscriptWireEvent) -> Void
    public typealias TranslationHandler = @Sendable (TranslationWireEvent) -> Void
    public typealias StatusHandler = @Sendable (CaptureStatusEvent) -> Void

    private let endpoint: URL
    private let authToken: String?
    private let applications: [String]
    private let retainAudio: Bool
    private let language: String?
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()
    private let transcriptHandler: TranscriptHandler?
    private let translationHandler: TranslationHandler?
    private let statusHandler: StatusHandler?
    private var session: URLSession?
    private var sessionID: String?
    private var sessionSources = [AudioSource]()
    private var sessionStartedNanoseconds: UInt64 = 0
    private var sockets = [String: URLSessionWebSocketTask]()
    private var reconnects = StreamReconnectPolicy()
    private var sendTails = [String: Task<Void, Error>]()
    private var stoppedWaiters = [String: CheckedContinuation<Void, Never>]()
    /// Where undeliverable audio is spooled; nil derives a per-session
    /// directory under the user's Application Support.
    private let spoolDirectory: URL?
    private let backlogCapacityBytes: Int
    private var clock = StreamClock()
    /// Frames waiting for a socket, per stream, oldest first.
    private var backlogs = [String: AudioBacklog]()
    private var spools = [String: AudioSpool]()
    /// Streams whose spool could not be opened or written; reported once.
    private var spoolFailures = Set<String>()

    /// About eight minutes of 16 kHz mono audio held in memory before the
    /// oldest of it is written to the spool file.
    public static let defaultBacklogCapacityBytes = 16 * 1024 * 1024

    /// How long a stop waits for the service to acknowledge `stream.stop`. The
    /// service finalizes its last VAD chunk first, so closing the socket early
    /// loses the final caption.
    private static let stopAcknowledgementNanoseconds: UInt64 = 2_000_000_000

    public init(
        endpoint: URL,
        authToken: String? = nil,
        applications: [String] = [],
        retainAudio: Bool = false,
        language: String? = nil,
        spoolDirectory: URL? = nil,
        backlogCapacityBytes: Int = WebSocketAudioFrameSink.defaultBacklogCapacityBytes,
        transcriptHandler: TranscriptHandler? = nil,
        translationHandler: TranslationHandler? = nil,
        statusHandler: StatusHandler? = nil
    ) throws {
        try LocalEndpointPolicy.validate(endpoint)
        self.endpoint = endpoint
        self.authToken = authToken
        self.applications = applications
        self.retainAudio = retainAudio
        self.language = language
        self.spoolDirectory = spoolDirectory
        self.backlogCapacityBytes = backlogCapacityBytes
        self.transcriptHandler = transcriptHandler
        self.translationHandler = translationHandler
        self.statusHandler = statusHandler
        encoder.outputFormatting = [.sortedKeys]
    }

    public func connect(sessionID: String, sources: [AudioSource]) async throws {
        self.sessionID = sessionID
        sessionSources = sources
        sessionStartedNanoseconds = DispatchTime.now().uptimeNanoseconds
        let configuration = URLSessionConfiguration.ephemeral
        configuration.waitsForConnectivity = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: configuration)
    }

    public func send(_ frame: AudioFrame) async throws {
        let previous = sendTails[frame.streamID]
        let task = Task { [weak self] in
            if let previous { _ = try? await previous.value }
            guard !Task.isCancelled, let self else { throw CancellationError() }
            try await self.sendPair(frame)
        }
        sendTails[frame.streamID] = task
        try await task.value
    }

    /// A frame is delivered now, kept for a socket that is coming back, or
    /// written to the spool. It is never dropped.
    ///
    /// Serializing each header/binary pair is required because the service
    /// allows exactly one outstanding audio header on a connection, and the
    /// backlog goes first so the session timeline stays in capture order.
    private func sendPair(_ frame: AudioFrame) async {
        guard let socket = await socketIfAvailable(for: frame) else {
            keep(frame)
            return
        }
        var pending = backlogs[frame.streamID]?.drain() ?? []
        pending.append(frame)
        for (index, queued) in pending.enumerated() {
            do {
                try await deliver(queued, on: socket)
            } catch {
                dropSocket(socket, streamID: frame.streamID)
                for remaining in pending[index...] { keep(remaining) }
                return
            }
        }
    }

    private func deliver(_ frame: AudioFrame, on socket: URLSessionWebSocketTask) async throws {
        let durationMilliseconds = Double(frame.pcmS16LE.count) / 2.0 / Double(frame.sampleRate) * 1_000.0
        let elapsedNanoseconds = frame.timestampNanoseconds >= sessionStartedNanoseconds
            ? frame.timestampNanoseconds - sessionStartedNanoseconds
            : 0
        let captureMilliseconds = Double(elapsedNanoseconds) / 1_000_000.0
        let startMilliseconds = clock.startMilliseconds(
            streamID: frame.streamID,
            capturedAtMilliseconds: captureMilliseconds,
            durationMilliseconds: durationMilliseconds
        )
        let header = AudioFrameHeader(
            sequence: frame.sequence,
            streamID: frame.streamID,
            startMilliseconds: startMilliseconds,
            durationMilliseconds: durationMilliseconds,
            capturedAtNanoseconds: frame.timestampNanoseconds,
            byteLength: frame.pcmS16LE.count
        )
        try await sendJSON(header, on: socket)
        try await socket.send(.data(frame.pcmS16LE))
        clock.advance(streamID: frame.streamID, toMilliseconds: startMilliseconds + durationMilliseconds)
    }

    private func dropSocket(_ socket: URLSessionWebSocketTask, streamID: String) {
        guard sockets[streamID] === socket else { return }
        sockets.removeValue(forKey: streamID)
        socket.cancel(with: .goingAway, reason: nil)
    }

    /// Hold a frame that could not be delivered: queued for the reconnect,
    /// or spooled once the queue is full or the session is gone.
    private func keep(_ frame: AudioFrame) {
        if reconnects.isDead(frame.streamID) {
            spool([frame], streamID: frame.streamID)
            return
        }
        var backlog = backlogs[frame.streamID] ?? AudioBacklog(capacityBytes: backlogCapacityBytes)
        let evicted = backlog.append(frame)
        backlogs[frame.streamID] = backlog
        spool(evicted, streamID: frame.streamID)
    }

    private func spoolBacklog(_ streamID: String) {
        guard var backlog = backlogs[streamID] else { return }
        let frames = backlog.drain()
        backlogs[streamID] = backlog
        spool(frames, streamID: streamID)
    }

    private func spool(_ frames: [AudioFrame], streamID: String) {
        guard let first = frames.first, !spoolFailures.contains(streamID) else { return }
        let spool: AudioSpool
        if let open = spools[streamID] {
            spool = open
        } else {
            do {
                spool = try AudioSpool(
                    directory: resolvedSpoolDirectory(),
                    streamID: streamID,
                    sampleRate: first.sampleRate,
                    channels: first.channels
                )
            } catch {
                spoolFailures.insert(streamID)
                statusHandler?(CaptureStatusEvent(
                    sessionID: sessionID ?? first.sessionID,
                    source: first.source,
                    level: .error,
                    code: "audio_spool_failed",
                    message: "Stream \(streamID) cannot reach the service and its spool file could not be created (\(error.localizedDescription)); audio is being lost."
                ))
                return
            }
            spools[streamID] = spool
            statusHandler?(CaptureStatusEvent(
                sessionID: sessionID ?? first.sessionID,
                source: first.source,
                level: .warning,
                code: "audio_spooled",
                message: "Stream \(streamID) cannot reach the service; its audio is being saved to \(spool.url.path) and is not in the live transcript."
            ))
        }
        do {
            for frame in frames { try spool.append(frame.pcmS16LE) }
        } catch {
            spoolFailures.insert(streamID)
            statusHandler?(CaptureStatusEvent(
                sessionID: sessionID ?? first.sessionID,
                source: first.source,
                level: .error,
                code: "audio_spool_failed",
                message: "Stream \(streamID) could not write to \(spool.url.path) (\(error.localizedDescription)); audio is being lost."
            ))
        }
    }

    private func resolvedSpoolDirectory() -> URL {
        if let spoolDirectory { return spoolDirectory }
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser
        return support
            .appendingPathComponent("VoxInk", isDirectory: true)
            .appendingPathComponent("spool", isDirectory: true)
            .appendingPathComponent(sessionID ?? "session", isDirectory: true)
    }

    /// Deliver what is still queued at Stop, opening a socket if the service
    /// is back, and spool whatever cannot go.
    private func settleBacklogs() async {
        for streamID in backlogs.keys.sorted() {
            guard var backlog = backlogs[streamID], !backlog.isEmpty else { continue }
            let frames = backlog.drain()
            backlogs[streamID] = backlog
            var socket = sockets[streamID]
            if socket == nil, let first = frames.first {
                socket = await socketIfAvailable(for: first)
            }
            guard let socket else {
                spool(frames, streamID: streamID)
                continue
            }
            for (index, frame) in frames.enumerated() {
                do {
                    try await deliver(frame, on: socket)
                } catch {
                    dropSocket(socket, streamID: streamID)
                    spool(Array(frames[index...]), streamID: streamID)
                    break
                }
            }
        }
        backlogs.removeAll()
    }

    private func closeSpools() {
        for (streamID, spool) in spools.sorted(by: { $0.key < $1.key }) {
            let seconds = spool.durationSeconds
            do {
                try spool.close()
            } catch {
                statusHandler?(CaptureStatusEvent(
                    sessionID: sessionID ?? "unknown",
                    source: nil,
                    level: .error,
                    code: "audio_spool_failed",
                    message: "Spool \(spool.url.path) could not be finalized (\(error.localizedDescription)); its header may be stale."
                ))
                continue
            }
            statusHandler?(CaptureStatusEvent(
                sessionID: sessionID ?? "unknown",
                source: nil,
                level: .warning,
                code: "audio_spooled",
                message: "Stream \(streamID): \(String(format: "%.1f", seconds)) s of audio never reached the service and were saved to \(spool.url.path). Transcribe it with: voxink transcribe \(spool.url.path)"
            ))
        }
        spools.removeAll()
    }

    private func uptimeSeconds() -> Double {
        Double(DispatchTime.now().uptimeNanoseconds) / 1_000_000_000.0
    }

    /// Capture diagnostics are local events, not service protocol messages.
    public func send(_ event: CaptureStatusEvent) async throws {
        statusHandler?(event)
    }

    /// Pause every open stream. Callers must stop feeding frames first so the
    /// control message lands after the audio it follows.
    public func pause(sessionID: String) async {
        await drainPendingSends()
        let atMilliseconds = elapsedMilliseconds()
        for (streamID, socket) in sockets {
            try? await sendJSON(
                StreamControlEnvelope(type: "stream.pause", streamID: streamID, atMilliseconds: atMilliseconds),
                on: socket
            )
        }
    }

    public func resume(sessionID: String) async {
        let atMilliseconds = elapsedMilliseconds()
        for (streamID, socket) in sockets {
            try? await sendJSON(
                StreamControlEnvelope(type: "stream.resume", streamID: streamID, atMilliseconds: atMilliseconds),
                on: socket
            )
        }
    }

    public func disconnect(sessionID: String, reason: String) async {
        // Let queued audio finish rather than cancelling it: the tail of a
        // session is exactly the part a user is still waiting to read.
        await drainPendingSends()
        await settleBacklogs()
        closeSpools()
        let atMilliseconds = elapsedMilliseconds()
        for (streamID, socket) in sockets {
            let stop = StreamStopEnvelope(streamID: streamID, atMilliseconds: atMilliseconds)
            try? await sendJSON(stop, on: socket)
            await awaitStopAcknowledgement(streamID: streamID)
        }
        for socket in sockets.values {
            socket.cancel(with: .normalClosure, reason: reason.data(using: .utf8))
        }
        sockets.removeAll()
        session?.invalidateAndCancel()
        session = nil
        self.sessionID = nil
        sessionSources = []
    }

    private func drainPendingSends() async {
        let tails = sendTails
        sendTails.removeAll()
        for task in tails.values { _ = try? await task.value }
    }

    private func elapsedMilliseconds() -> Double {
        let now = DispatchTime.now().uptimeNanoseconds
        return Double(now >= sessionStartedNanoseconds ? now - sessionStartedNanoseconds : 0) / 1_000_000.0
    }

    /// Wait for `stream.stopped`, or give up so a wedged service cannot block
    /// the user's Stop.
    private func awaitStopAcknowledgement(streamID: String) async {
        guard sockets[streamID] != nil else { return }
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            stoppedWaiters[streamID] = continuation
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: Self.stopAcknowledgementNanoseconds)
                await self?.completeStopAcknowledgement(streamID: streamID)
            }
        }
    }

    /// Actor isolation makes remove-then-resume safe against a double resume.
    private func completeStopAcknowledgement(streamID: String) {
        stoppedWaiters.removeValue(forKey: streamID)?.resume()
    }

    /// The stream's socket, opening one when the policy allows an attempt
    /// now. Nil means the frame must be kept: the stream is backing off from
    /// a failed attempt, the attempt just failed, or the session is gone.
    private func socketIfAvailable(for frame: AudioFrame) async -> URLSessionWebSocketTask? {
        if let socket = sockets[frame.streamID] { return socket }
        guard let session, let sessionID else { return nil }
        switch reconnects.socketNeeded(for: frame.streamID, now: uptimeSeconds()) {
        case .connect:
            break
        case .reconnect(let attempt):
            statusHandler?(CaptureStatusEvent(
                sessionID: sessionID,
                source: frame.source,
                level: .warning,
                code: "service_reconnecting",
                message: "Reconnecting stream \(frame.streamID) to the service (attempt \(attempt)); audio is kept until it is back."
            ))
        case .wait, .dead:
            return nil
        }
        var request = URLRequest(url: endpoint)
        if let authToken { request.setValue("Bearer \(authToken)", forHTTPHeaderField: "Authorization") }
        let task = session.webSocketTask(with: request)
        task.resume()
        let start = StreamStartEnvelope(
            sessionID: sessionID,
            streamID: frame.streamID,
            source: frame.source,
            sessionSources: sessionSources,
            sampleRate: frame.sampleRate,
            channels: frame.channels,
            frameDurationMilliseconds: 20,
            // The microphone stream carries no per-frame application list, but
            // the session's selection belongs in the transcript metadata no
            // matter which stream opens first.
            applicationBundleIdentifiers: frame.applicationBundleIdentifiers
                ?? (applications.isEmpty ? nil : applications),
            retainAudio: retainAudio,
            channel: frame.channel,
            channelName: frame.channelName,
            language: language
        )
        do {
            try await sendJSON(start, on: task)
        } catch {
            task.cancel(with: .goingAway, reason: nil)
            let delay = reconnects.attemptFailed(frame.streamID, now: uptimeSeconds())
            statusHandler?(CaptureStatusEvent(
                sessionID: sessionID,
                source: frame.source,
                level: .warning,
                code: "service_unreachable",
                message: "Stream \(frame.streamID) could not reach the service (\(error.localizedDescription)); trying again in \(String(format: "%.2g", delay)) s, audio is kept meanwhile."
            ))
            return nil
        }
        sockets[frame.streamID] = task
        Task { [weak self, task] in await self?.receiveLoop(task: task, streamID: frame.streamID) }
        return task
    }

    private func receiveLoop(task: URLSessionWebSocketTask, streamID: String) async {
        while sockets[streamID] === task {
            do {
                let message = try await task.receive()
                guard case .string(let json) = message, let data = json.data(using: .utf8) else { continue }
                if let response = try? decoder.decode(TranscriptResponse.self, from: data),
                   ["transcript.partial", "transcript.final"].contains(response.type),
                   let event = response.event {
                    transcriptHandler?(event)
                } else if let response = try? decoder.decode(TranslationResponse.self, from: data),
                          response.type == "transcript.translation",
                          let event = response.event {
                    translationHandler?(event)
                } else if let envelope = try? decoder.decode(TypeOnlyResponse.self, from: data),
                          envelope.type == "stream.stopped" {
                    completeStopAcknowledgement(streamID: streamID)
                } else if let envelope = try? decoder.decode(TypeOnlyResponse.self, from: data),
                          envelope.type == "stream.ready" {
                    if reconnects.streamReady(streamID) {
                        statusHandler?(CaptureStatusEvent(
                            sessionID: sessionID ?? "unknown",
                            source: nil,
                            level: .info,
                            code: "service_reconnected",
                            message: "Stream \(streamID) reconnected to the service."
                        ))
                    }
                } else if let response = try? decoder.decode(ErrorResponse.self, from: data), response.type == "error" {
                    if response.code == "session_unavailable" {
                        // The session is finalized; a reconnect would only be
                        // refused again. What is queued goes to the spool now.
                        reconnects.sessionUnavailable(streamID)
                        spoolBacklog(streamID)
                    }
                    // Live speakers giving up is not a recording problem.
                    let level: CaptureStatusEvent.Level = response.code == "live_speakers_stopped" ? .warning : .error
                    statusHandler?(CaptureStatusEvent(
                        sessionID: sessionID ?? "unknown",
                        source: nil,
                        level: level,
                        code: response.code ?? "service_error",
                        message: response.message ?? "The transcription service reported an error."
                    ))
                }
            } catch {
                // A closed socket will never acknowledge a stop.
                completeStopAcknowledgement(streamID: streamID)
                if sockets[streamID] === task {
                    sockets.removeValue(forKey: streamID)
                    // A refused stream already reported why; its close is expected.
                    if !reconnects.isDead(streamID) {
                        statusHandler?(CaptureStatusEvent(
                            sessionID: sessionID ?? "unknown",
                            source: nil,
                            level: .warning,
                            code: "service_connection_closed",
                            message: error.localizedDescription
                        ))
                    }
                }
                return
            }
        }
    }

    private func sendJSON<T: Encodable>(_ value: T, on socket: URLSessionWebSocketTask) async throws {
        let data = try encoder.encode(value)
        try await socket.send(.string(String(decoding: data, as: UTF8.self)))
    }
}

struct StreamStartEnvelope: Encodable {
    let type = "stream.start"
    let schemaVersion = 1
    let sessionID: String
    let streamID: String
    let source: AudioSource
    let sessionSources: [AudioSource]
    let audio: AudioDescription
    let client = ClientDescription(name: "voxink-macos-capture", version: "0.1.0")
    let streamMode = "frames"
    let vad = VADDescription()
    let applicationBundleIdentifiers: [String]?
    let retention: RetentionDescription
    let channel: String?
    let channelName: String?
    let language: String?

    init(
        sessionID: String,
        streamID: String,
        source: AudioSource,
        sessionSources: [AudioSource],
        sampleRate: Int,
        channels: Int,
        frameDurationMilliseconds: Double,
        applicationBundleIdentifiers: [String]?,
        retainAudio: Bool,
        channel: String? = nil,
        channelName: String? = nil,
        language: String? = nil
    ) {
        retention = RetentionDescription(audio: retainAudio, separate: true, combined: false)
        self.sessionID = sessionID
        self.streamID = streamID
        self.source = source
        self.sessionSources = sessionSources
        audio = AudioDescription(
            encoding: "pcm_s16le",
            sampleRate: sampleRate,
            channels: channels,
            frameDurationMilliseconds: frameDurationMilliseconds
        )
        self.applicationBundleIdentifiers = applicationBundleIdentifiers
        self.channel = channel
        self.channelName = channelName
        self.language = language
    }

    enum CodingKeys: String, CodingKey {
        case type, source, audio, client, vad, retention, channel, language
        case schemaVersion = "schema_version"
        case sessionID = "session_id"
        case streamID = "stream_id"
        case sessionSources = "session_sources"
        case streamMode = "stream_mode"
        case applicationBundleIdentifiers = "application_bundle_identifiers"
        case channelName = "channel_name"
    }
}

struct RetentionDescription: Encodable {
    let audio: Bool
    let separate: Bool
    let combined: Bool
}

struct AudioDescription: Encodable {
    let encoding: String
    let sampleRate: Int
    let channels: Int
    let frameDurationMilliseconds: Double
    enum CodingKeys: String, CodingKey {
        case encoding, channels
        case sampleRate = "sample_rate_hz"
        case frameDurationMilliseconds = "frame_duration_ms"
    }
}

struct ClientDescription: Encodable { let name: String; let version: String }
/// Chunking shape only. The level at which audio counts as speech is derived
/// by the service from the audio itself: this helper applies no gain, so it
/// cannot know what level its microphone will deliver, and the fixed value it
/// used to send here discarded quiet speech as silence.
struct VADDescription: Encodable {
    let silenceDurationMilliseconds = 700
    let minSpeechDurationMilliseconds = 200
    let maxChunkDurationMilliseconds = 15_000
    let preRollMilliseconds = 200
    let overlapMilliseconds = 200
    enum CodingKeys: String, CodingKey {
        case silenceDurationMilliseconds = "silence_duration_ms"
        case minSpeechDurationMilliseconds = "min_speech_duration_ms"
        case maxChunkDurationMilliseconds = "max_chunk_duration_ms"
        case preRollMilliseconds = "pre_roll_ms"
        case overlapMilliseconds = "overlap_ms"
    }
}

private struct AudioFrameHeader: Encodable {
    let type = "audio.frame"
    let sequence: UInt64
    let streamID: String
    let startMilliseconds: Double
    let durationMilliseconds: Double
    let capturedAtNanoseconds: UInt64
    let byteLength: Int
    enum CodingKeys: String, CodingKey {
        case type, sequence
        case streamID = "stream_id"
        case startMilliseconds = "start_ms"
        case durationMilliseconds = "duration_ms"
        case capturedAtNanoseconds = "captured_at_ns"
        case byteLength = "byte_length"
    }
}

private struct StreamStopEnvelope: Encodable {
    let type = "stream.stop"
    let streamID: String
    let atMilliseconds: Double
    enum CodingKeys: String, CodingKey {
        case type
        case streamID = "stream_id"
        case atMilliseconds = "at_ms"
    }
}

private struct StreamControlEnvelope: Encodable {
    let type: String
    let streamID: String
    let atMilliseconds: Double
    enum CodingKeys: String, CodingKey {
        case type
        case streamID = "stream_id"
        case atMilliseconds = "at_ms"
    }
}

private struct TypeOnlyResponse: Decodable { let type: String }
private struct TranscriptResponse: Decodable { let type: String; let event: TranscriptWireEvent? }
private struct TranslationResponse: Decodable { let type: String; let event: TranslationWireEvent? }
private struct ErrorResponse: Decodable { let type: String; let code: String?; let message: String? }
