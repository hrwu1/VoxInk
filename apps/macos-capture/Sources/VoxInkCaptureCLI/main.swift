import Foundation
import VoxInkCapture

private struct Options {
    var microphone = false
    var allSystemAudio = false
    var applicationBundleIdentifiers = Set<String>()
    var listApplications = false
    var endpoint: URL?
    var sessionID = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")

    static func parse(_ arguments: [String]) throws -> Options {
        var options = Options()
        var index = 0
        while index < arguments.count {
            switch arguments[index] {
            case "--mic": options.microphone = true
            case "--system-all": options.allSystemAudio = true
            case "--list-apps": options.listApplications = true
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
                      [--session-id ID]

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
            let configuration = CaptureConfiguration(
                sessionID: options.sessionID,
                microphoneEnabled: options.microphone,
                systemAudio: systemAudio,
                serviceURL: options.endpoint
            )
            let coordinator = try CaptureCoordinator(
                configuration: configuration,
                transcriptHandler: { event in
                    print("[\(event.source.rawValue.uppercased())] \(event.text)")
                },
                statusHandler: { event in
                    FileHandle.standardError.write(Data(("\(event.code): \(event.message)\n").utf8))
                }
            )
            try await coordinator.start()
            print("Capture started for session \(options.sessionID). Press Ctrl-C to stop.")
            await waitForInterrupt()
            await coordinator.stop()
            print("Capture stopped.")
        } catch {
            let message = error.localizedDescription
            if !message.isEmpty { FileHandle.standardError.write(Data((message + "\n").utf8)) }
            exit(EXIT_FAILURE)
        }
    }

    private static func waitForInterrupt() async {
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        await withCheckedContinuation { continuation in
            var resumed = false
            signalSources = [SIGINT, SIGTERM].map { signalNumber -> DispatchSourceSignal in
                let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: .main)
                source.setEventHandler {
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
