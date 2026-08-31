import AppKit
import Foundation
import VoxInkCapture

private struct Options {
    var microphone = false
    var allSystemAudio = false
    var applicationBundleIdentifiers = Set<String>()
    var listApplications = false
    var captions = true
    var clickThroughCaptions = false
    var retainAudio = false
    var endpoint: URL?
    var sessionID = SessionIdentifier.make()

    static func parse(_ arguments: [String]) throws -> Options {
        var options = Options()
        var index = 0
        while index < arguments.count {
            switch arguments[index] {
            case "--mic": options.microphone = true
            case "--system-all": options.allSystemAudio = true
            case "--list-apps": options.listApplications = true
            case "--no-captions": options.captions = false
            case "--click-through": options.clickThroughCaptions = true
            case "--retain-audio": options.retainAudio = true
            case "--app", "--endpoint", "--session-id":
                guard index + 1 < arguments.count else { throw CLIError("Missing value after \(arguments[index]).") }
                let value = arguments[index + 1]
                if arguments[index] == "--app" { options.applicationBundleIdentifiers.insert(value) }
                if arguments[index] == "--endpoint" {
                    guard let url = URL(string: value) else { throw CLIError("Invalid endpoint: \(value)") }
                    options.endpoint = url
                }
                if arguments[index] == "--session-id" { options.sessionID = value }
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
      voxink-capture [--mic] [--system-all | --app BUNDLE_ID ...]
                      [--endpoint ws://127.0.0.1:8765/v1/sessions/SESSION_ID/stream]
                      [--session-id ID] [--no-captions | --click-through]
                      [--retain-audio]

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

            let systemAudio: SystemAudioSelection?
            if options.allSystemAudio {
                systemAudio = .all
            } else if !options.applicationBundleIdentifiers.isEmpty {
                systemAudio = .applications(bundleIdentifiers: options.applicationBundleIdentifiers)
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
                retainAudio: options.retainAudio
            )
            let captionPanel: FloatingCaptionPanelController? = if options.captions {
                await MainActor.run {
                    let application = NSApplication.shared
                    application.setActivationPolicy(.accessory)
                    application.finishLaunching()
                    let panel = FloatingCaptionPanelController()
                    panel.setClickThrough(options.clickThroughCaptions)
                    panel.showWindow()
                    return panel
                }
            } else {
                nil
            }
            let coordinator = try CaptureCoordinator(
                configuration: configuration,
                transcriptHandler: { event in
                    print("[\(event.source.rawValue.uppercased())] \(event.text)")
                    if let captionPanel {
                        Task { @MainActor in captionPanel.show(event.captionItem) }
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
            if let captionPanel {
                await MainActor.run { captionPanel.hideWindow() }
            }
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
