import CoreAudio
import Foundation

/// What a change in the captured microphone's input volume means, and
/// whether to put it back.
///
/// A call app with automatic microphone volume adjusts the device's input
/// volume at the system level, and every other process reading that device
/// gets the result. Seen live: Zoom took a shared AirPods microphone from
/// 71% to 24% the moment it was unmuted, and the recording fell below the
/// speech gate for the rest of the session with nothing in the logs. The
/// policy is pure so the rules can be tested without a device.
public struct InputGainPolicy: Equatable, Sendable {
    public enum Decision: Equatable, Sendable {
        case unchanged
        /// A new level worth a log line; `warning` when it fell under `dropRatio`
        /// of the level at start, which is where captions start to go missing.
        case changed(warning: Bool)
        /// Back within `tolerance` of the level at start.
        case recovered
        /// Put the device back to `reference` and say so.
        case restore
        /// Restoring has been tried too often for it to be anything but a fight
        /// with another gain control; report once and stop holding.
        case holdExhausted
    }

    /// The level at start, 0...1, which a hold puts back.
    public private(set) var reference: Double
    public private(set) var hold: Bool
    public var dropRatio = 0.6
    public var tolerance = 0.02
    public var restoreCooldownSeconds = 5.0
    public var maximumRestoresPerMinute = 5
    private var lastSeen: Double
    private var restoreTimes: [Double] = []
    private var expectingRestore = false

    public init(reference: Double, hold: Bool) {
        self.reference = reference
        self.hold = hold
        lastSeen = reference
    }

    /// Decide on a newly observed level. `now` is any monotonic clock in seconds.
    public mutating func observe(gain: Double, now: Double) -> Decision {
        guard abs(gain - lastSeen) >= tolerance else { return .unchanged }
        lastSeen = gain
        if abs(gain - reference) < tolerance {
            // The device reporting our own restore is not news.
            if expectingRestore {
                expectingRestore = false
                return .unchanged
            }
            return .recovered
        }
        expectingRestore = false
        let warning = gain < reference * dropRatio
        guard hold, gain < reference else { return .changed(warning: warning) }
        restoreTimes.removeAll { now - $0 > 60 }
        if restoreTimes.count >= maximumRestoresPerMinute {
            hold = false
            return .holdExhausted
        }
        if let last = restoreTimes.last, now - last < restoreCooldownSeconds {
            return .changed(warning: warning)
        }
        restoreTimes.append(now)
        expectingRestore = true
        return .restore
    }

    /// The restore the last decision asked for could not be applied.
    public mutating func restoreFailed() {
        expectingRestore = false
        hold = false
    }
}

/// Watches one input device's volume and runs the policy on every change.
final class InputGainMonitor {
    enum Event: Equatable {
        case changed(from: Double, to: Double, warning: Bool)
        case recovered(to: Double)
        case restored(from: Double, to: Double)
        case restoreFailed(from: Double, to: Double, status: OSStatus)
        case holdExhausted(at: Double, reference: Double)
    }
    typealias Handler = @Sendable (Event) -> Void

    let device: AudioDeviceID
    private let element: AudioObjectPropertyElement
    private let handler: Handler
    private let queue = DispatchQueue(label: "dev.voxink.capture.input-gain")
    private let lock = NSLock()
    private var policy: InputGainPolicy
    private var lastReported: Double
    private var listener: AudioObjectPropertyListenerBlock?

    /// Nil when the device exposes no input volume, which is then nothing
    /// another app could turn down either.
    init?(device: AudioDeviceID, hold: Bool, handler: @escaping Handler) {
        guard let (element, gain) = CoreAudioSupport.inputVolume(of: device) else { return nil }
        self.device = device
        self.element = element
        self.handler = handler
        policy = InputGainPolicy(reference: gain, hold: hold)
        lastReported = gain
    }

    var referenceGain: Double { lock.lock(); defer { lock.unlock() }; return policy.reference }

    func start() {
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in self?.check() }
        listener = block
        CoreAudioSupport.addListener(
            device, selector: kAudioDevicePropertyVolumeScalar, scope: kAudioObjectPropertyScopeInput,
            element: element, queue: queue, block: block
        )
    }

    func stop() {
        guard let block = listener else { return }
        listener = nil
        CoreAudioSupport.removeListener(
            device, selector: kAudioDevicePropertyVolumeScalar, scope: kAudioObjectPropertyScopeInput,
            element: element, queue: queue, block: block
        )
    }

    private func check() {
        guard let gain = CoreAudioSupport.inputVolume(of: device, element: element) else { return }
        let now = Double(DispatchTime.now().uptimeNanoseconds) / 1_000_000_000
        lock.lock()
        let decision = policy.observe(gain: gain, now: now)
        let previous = lastReported
        let reference = policy.reference
        if decision != .unchanged { lastReported = gain }
        lock.unlock()
        switch decision {
        case .unchanged:
            return
        case .changed(let warning):
            handler(.changed(from: previous, to: gain, warning: warning))
        case .recovered:
            handler(.recovered(to: gain))
        case .restore:
            let status = CoreAudioSupport.setInputVolume(reference, of: device, element: element)
            if status == noErr {
                handler(.restored(from: gain, to: reference))
            } else {
                lock.lock()
                policy.restoreFailed()
                lock.unlock()
                handler(.restoreFailed(from: gain, to: reference, status: status))
            }
        case .holdExhausted:
            handler(.holdExhausted(at: gain, reference: reference))
        }
    }
}
