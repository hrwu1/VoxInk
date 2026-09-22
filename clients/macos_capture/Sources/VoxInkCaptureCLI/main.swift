import Foundation
import VoxInkCapture

private struct Options {
    var microphone = false
    var allSystemAudio = false
    var applicationBundleIdentifiers = Set<String>()
    var listApplications = false
    var listDevices = false
    var listSources = false
    var retainAudio = false
    var endpoint: URL?
    var sessionID = SessionIdentifier.make()
    var backend = SystemAudioBackend.auto
    var microphoneDeviceUID: String?
    var holdMicrophoneGain = false
    var language: String?
    var spoolDirectory: URL?

    static func parse(_ arguments: [String]) throws -> Options {
        var options = Options()
        var index = 0
        while index < arguments.count {
            switch arguments[index] {
            case "--mic": options.microphone = true
            case "--system-all": options.allSystemAudio = true
            case "--list-apps": options.listApplications = true
            case "--list-devices": options.listDevices = true
            case "--list-sources": options.listSources = true
            case "--retain-audio": options.retainAudio = true
            case "--hold-mic-gain": options.holdMicrophoneGain = true
            case "--app", "--endpoint", "--session-id", "--backend", "--mic-device", "--language", "--spool-dir":
                guard index + 1 < arguments.count else { throw CLIError("Missing value after \(arguments[index]).") }
                let value = arguments[index + 1]
                switch arguments[index] {
                case "--app": options.applicationBundleIdentifiers.insert(value)
                case "--spool-dir": options.spoolDirectory = URL(fileURLWithPath: value)
                case "--endpoint":
                    guard let url = URL(string: value) else { throw CLIError("Invalid endpoint: \(value)") }
                    options.endpoint = url
                case "--session-id": options.sessionID = value
                case "--backend":
                    guard let backend = SystemAudioBackend(rawValue: value) else {
                        throw CLIError("Unknown backend: \(value). Use auto, tap, or screencapturekit.")
                    }
                    options.backend = backend
                case "--mic-device": options.microphoneDeviceUID = value
                case "--language": options.language = value
                default: break
                }
                index += 1
            case "--help", "-h": throw CLIError.usage
            default: throw CLIError("Unknown option: \(arguments[index])")
            }
            index += 1
        }
        guard !(options.allSystemAudio && !options.applicationBundleIdentifiers.isEmpty) else {
            throw CLIError("Choose --system-all or one or more --app values, not both.")
        }
        return options
    }
}

private struct CLIError: LocalizedError {
    static let usage = CLIError("""
    Usage:
      voxink-capture --list-apps
      voxink-capture --list-devices
      voxink-capture --list-sources
      voxink-capture [--mic] [--mic-device UID] [--hold-mic-gain]
                      [--system-all | --app BUNDLE_ID ...]
                      [--backend auto|tap|screencapturekit] [--language en|zh]
                      [--endpoint ws://127.0.0.1:8300/v1/sessions/SESSION_ID/stream]
                      [--session-id ID]
                      [--retain-audio]
                      [--spool-dir DIR]   where audio the service never received is saved
      --hold-mic-gain   put the microphone's input volume back if another app lowers it

    Examples:
      voxink-capture --mic
      voxink-capture --system-all
      voxink-capture --mic --app us.zoom.xos --app com.apple.Safari
    """)

    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

@main
private struct VoxInkCaptureCommand {
    private static var signalSources = [DispatchSourceSignal]()

    static func main() async {
        do {
            let options = try Options.parse(Array(CommandLine.arguments.dropFirst()))
            if options.listApplications {
                let applications = try await CaptureApplicationCatalog.runningApplications()
                for application in applications {
                    print("\(application.bundleIdentifier)\t\(application.name)\tpid=\(application.processID)")
                }
                return
            }
            if options.listSources {
                // The helper asks every capture backend this; the portable
                // Python one answers "microphone" alone.
                print("microphone")
                print("system")
                return
            }
            if options.listDevices {
                for device in try CaptureApplicationCatalog.inputDevices() {
                    print("\(device.uid)\t\(device.name)\(device.isDefault ? "\tdefault" : "")")
                }
                return
            }

            let systemAudio: SystemAudioSelection?
            var applicationNames = [String: String]()
            if options.allSystemAudio {
                systemAudio = .all
            } else if !options.applicationBundleIdentifiers.isEmpty {
                systemAudio = .applications(bundleIdentifiers: options.applicationBundleIdentifiers)
                // Names let a per-application stream be labeled "Zoom" rather
                // than "us.zoom.xos". Best effort: an app that is not running
                // yet is labeled by its bundle identifier until it is.
                if let running = try? await CaptureApplicationCatalog.runningApplications() {
                    for application in running where options.applicationBundleIdentifiers.contains(application.bundleIdentifier) {
                        applicationNames[application.bundleIdentifier] = application.name
                    }
                }
            } else {
                systemAudio = nil
            }
            // The service enables Pause the moment /capture/start returns, while
            // this process may still be blocked on a permission prompt. At their
            // default disposition SIGUSR1 and SIGUSR2 terminate the helper and
            // the recording is lost, so take them off it before start().
            signal(SIGUSR1, SIG_IGN)
            signal(SIGUSR2, SIG_IGN)
            let configuration = CaptureConfiguration(
                sessionID: options.sessionID,
                microphoneEnabled: options.microphone,
                systemAudio: systemAudio,
                serviceURL: options.endpoint,
                retainAudio: options.retainAudio,
                backend: options.backend,
                microphoneDeviceUID: options.microphoneDeviceUID,
                holdMicrophoneGain: options.holdMicrophoneGain,
                language: options.language,
                applicationNames: applicationNames,
                spoolDirectory: options.spoolDirectory
            )
            // Headless: the helper launches this process and the interfaces
            // draw captions from the helper's status feed.
            let coordinator = try CaptureCoordinator(
                configuration: configuration,
                transcriptHandler: { event in
                    if event.isFinal {
                        let label = event.channelName ?? event.source.rawValue.uppercased()
                        print("[\(label)] \(event.text)")
                        if let translation = event.translation { print("    \(translation)") }
                    }
                },
                statusHandler: { event in
                    FileHandle.standardError.write(Data(("\(event.code): \(event.message)\n").utf8))
                }
            )
            try await coordinator.start()
            print("Capture started for session \(options.sessionID). Press Ctrl-C to stop.")
            await waitForInterrupt(coordinator: coordinator)
            await coordinator.stop()
            print("Capture stopped.")
        } catch {
            let message = error.localizedDescription
            if !message.isEmpty { FileHandle.standardError.write(Data((message + "\n").utf8)) }
            exit(EXIT_FAILURE)
        }
    }

    private static func waitForInterrupt(coordinator: CaptureCoordinator) async {
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        // SIGUSR1 and SIGUSR2 were ignored before start(), above.
        await withCheckedContinuation { continuation in
            var resumed = false
            signalSources = [SIGINT, SIGTERM, SIGUSR1, SIGUSR2].map { signalNumber -> DispatchSourceSignal in
                let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: .main)
                source.setEventHandler {
                    if signalNumber == SIGUSR1 {
                        Task { await coordinator.pause() }
                        return
                    }
                    if signalNumber == SIGUSR2 {
                        Task { await coordinator.resume() }
                        return
                    }
                    guard !resumed else { return }
                    resumed = true
                    signalSources.forEach { $0.cancel() }
                    signalSources.removeAll()
                    continuation.resume()
                }
                source.resume()
                return source
            }
        }
    }
}
