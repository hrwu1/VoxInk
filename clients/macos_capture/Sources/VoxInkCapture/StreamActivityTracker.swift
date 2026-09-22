import Foundation

/// Notices when a stream stops carrying any signal, and when it comes back.
///
/// "Silent" here is not quiet. It is a peak at or below `silenceThreshold`
/// (8 of 32768, about -72 dBFS), the level a device delivers when nothing
/// reaches it at all; a quiet room sits far above it. The point is a
/// timestamped line in the capture log for the moment a source went dead,
/// which the summary written at stop cannot give — a recording that went
/// silent for the last hour and one that was silent for ten minutes in the
/// middle look the same in a byte count.
struct StreamActivityTracker: Equatable, Sendable {
    enum Transition: Equatable, Sendable {
        /// No signal for `silentAfterSeconds`.
        case silent
        /// Signal is back after this long without any.
        case active(silentSeconds: Double)
    }

    var silenceThreshold: Int
    var silentAfterSeconds: Double
    private var silentSince: Double?
    private var reportedSilent = false

    init(silenceThreshold: Int = 8, silentAfterSeconds: Double = 10) {
        self.silenceThreshold = silenceThreshold
        self.silentAfterSeconds = silentAfterSeconds
    }

    /// Feed one frame's peak amplitude; returns a transition to report, if any.
    mutating func observe(peak: Int, atSeconds now: Double) -> Transition? {
        if peak > silenceThreshold {
            let since = silentSince
            silentSince = nil
            guard reportedSilent, let since else { return nil }
            reportedSilent = false
            return .active(silentSeconds: now - since)
        }
        if silentSince == nil { silentSince = now }
        guard !reportedSilent, let since = silentSince, now - since >= silentAfterSeconds else { return nil }
        reportedSilent = true
        return .silent
    }

    /// The status line for a transition on `streamID`.
    func describe(_ transition: Transition, streamID: String) -> (code: String, message: String) {
        switch transition {
        case .silent:
            return ("stream_silent", "Stream \(streamID) has delivered no signal for \(Int(silentAfterSeconds)) s.")
        case .active(let seconds):
            return ("stream_active", "Stream \(streamID) is delivering signal again after \(Int(seconds.rounded())) s without any.")
        }
    }
}
