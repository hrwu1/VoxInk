import CoreAudio
import Foundation

/// One microphone the system exposes, addressed by the stable device UID.
public struct InputDevice: Equatable, Sendable {
    public let uid: String
    public let name: String
    public let isDefault: Bool
}

/// A process coreaudiod knows about: anything that has opened an audio
/// device. Processes that have never played audio are not listed, which is
/// why selected-application capture waits for them rather than failing.
struct AudioProcess: Equatable, Sendable {
    let objectID: AudioObjectID
    let pid: pid_t
    let bundleIdentifier: String?
}

/// Thin, typed wrappers over the Core Audio property API. Nothing here holds
/// state; each call reads the HAL as it is right now.
enum CoreAudioSupport {
    static let systemObject = AudioObjectID(kAudioObjectSystemObject)

    static func inputDevices() throws -> [InputDevice] {
        let ids: [AudioDeviceID] = try arrayProperty(
            systemObject, selector: kAudioHardwarePropertyDevices
        )
        let defaultInput: AudioDeviceID? = try? scalarProperty(
            systemObject, selector: kAudioHardwarePropertyDefaultInputDevice
        )
        return ids.compactMap { device -> InputDevice? in
            let streams: [AudioStreamID] = (try? arrayProperty(
                device, selector: kAudioDevicePropertyStreams, scope: kAudioObjectPropertyScopeInput
            )) ?? []
            guard !streams.isEmpty,
                  let uid: String = try? stringProperty(device, selector: kAudioDevicePropertyDeviceUID)
            else { return nil }
            let name: String = (try? stringProperty(device, selector: kAudioObjectPropertyName)) ?? uid
            return InputDevice(uid: uid, name: name, isDefault: device == defaultInput)
        }
    }

    static func deviceID(forUID uid: String) -> AudioDeviceID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslateUIDToDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var qualifier = uid as CFString
        var device = AudioDeviceID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = withUnsafeMutablePointer(to: &qualifier) { qualifierPointer in
            AudioObjectGetPropertyData(
                systemObject, &address,
                UInt32(MemoryLayout<CFString>.size), qualifierPointer,
                &size, &device
            )
        }
        guard status == noErr, device != kAudioObjectUnknown else { return nil }
        return device
    }

    static func defaultOutputDeviceUID() throws -> String {
        let device: AudioDeviceID = try scalarProperty(
            systemObject, selector: kAudioHardwarePropertyDefaultSystemOutputDevice
        )
        return try stringProperty(device, selector: kAudioDevicePropertyDeviceUID)
    }

    @available(macOS 14.2, *)
    static func audioProcesses() throws -> [AudioProcess] {
        let ids: [AudioObjectID] = try arrayProperty(
            systemObject, selector: kAudioHardwarePropertyProcessObjectList
        )
        return ids.compactMap { object in
            guard let pid: pid_t = try? scalarProperty(object, selector: kAudioProcessPropertyPID) else {
                return nil
            }
            let bundle: String? = try? stringProperty(object, selector: kAudioProcessPropertyBundleID)
            return AudioProcess(objectID: object, pid: pid, bundleIdentifier: bundle?.isEmpty == false ? bundle : nil)
        }
    }

    @available(macOS 14.2, *)
    static func processObject(forPID pid: pid_t) -> AudioObjectID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var qualifier = pid
        var object = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let status = withUnsafeMutablePointer(to: &qualifier) { qualifierPointer in
            AudioObjectGetPropertyData(
                systemObject, &address,
                UInt32(MemoryLayout<pid_t>.size), qualifierPointer,
                &size, &object
            )
        }
        guard status == noErr, object != kAudioObjectUnknown else { return nil }
        return object
    }

    // MARK: - Property primitives

    static func scalarProperty<T>(
        _ object: AudioObjectID,
        selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
    ) throws -> T {
        var address = AudioObjectPropertyAddress(
            mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain
        )
        var size = UInt32(MemoryLayout<T>.size)
        let pointer = UnsafeMutablePointer<T>.allocate(capacity: 1)
        defer { pointer.deallocate() }
        let status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, pointer)
        guard status == noErr else { throw CoreAudioError(status: status, selector: selector) }
        return pointer.pointee
    }

    static func arrayProperty<T>(
        _ object: AudioObjectID,
        selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
    ) throws -> [T] {
        var address = AudioObjectPropertyAddress(
            mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        var status = AudioObjectGetPropertyDataSize(object, &address, 0, nil, &size)
        guard status == noErr else { throw CoreAudioError(status: status, selector: selector) }
        let count = Int(size) / MemoryLayout<T>.stride
        guard count > 0 else { return [] }
        var values = [T](unsafeUninitializedCapacity: count) { buffer, initialized in
            status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, buffer.baseAddress!)
            initialized = status == noErr ? Int(size) / MemoryLayout<T>.stride : 0
        }
        guard status == noErr else { throw CoreAudioError(status: status, selector: selector) }
        values.removeAll(where: { _ in false })
        return values
    }

    static func stringProperty(
        _ object: AudioObjectID,
        selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
    ) throws -> String {
        var address = AudioObjectPropertyAddress(
            mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain
        )
        var size = UInt32(MemoryLayout<CFString?>.size)
        var value: Unmanaged<CFString>?
        let status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value)
        guard status == noErr else { throw CoreAudioError(status: status, selector: selector) }
        guard let string = value?.takeRetainedValue() else {
            throw CoreAudioError(status: status, selector: selector)
        }
        return string as String
    }
}

struct CoreAudioError: LocalizedError {
    let status: OSStatus
    let selector: AudioObjectPropertySelector

    var errorDescription: String? {
        "Core Audio returned status \(status) for property \(fourCharacterCode(selector))."
    }
}

func fourCharacterCode(_ value: UInt32) -> String {
    let bytes = [
        UInt8((value >> 24) & 0xFF), UInt8((value >> 16) & 0xFF),
        UInt8((value >> 8) & 0xFF), UInt8(value & 0xFF),
    ]
    return String(bytes: bytes, encoding: .macOSRoman) ?? String(value)
}
