import Foundation

/// The capture helper's latest status line, reduced to what a person
/// recording should see beside the Live badge.
///
/// The helper reports every state change as `code: message`. Only the ones
/// that mean the recording is degraded are shown; a line that says the
/// trouble is over (`service_reconnected`) clears the display, and counters
/// such as dropped-frame totals stay in the logs.
enum HelperNotice {
    static func line(from notice: String?) -> String? {
        guard let notice, let separator = notice.range(of: ": ") else { return nil }
        let code = String(notice[..<separator.lowerBound])
        let message = String(notice[separator.upperBound...])
        switch code {
        case "audio_spooled":
            return "Audio saved locally — " + message
        case "audio_spool_failed":
            return "Audio is being lost — " + message
        case "service_reconnecting", "service_unreachable", "service_connection_closed":
            return "Service connection — " + message
        case "live_speakers_stopped":
            return "Live speakers — " + message
        default:
            return nil
        }
    }
}
