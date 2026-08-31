import Foundation

public enum SessionIdentifier {
    /// Generate the canonical user-facing session directory name in local time.
    public static func make(at date: Date = Date(), timeZone: TimeZone = .current) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        var components = calendar.dateComponents(
            [.year, .month, .day, .hour, .minute, .second, .nanosecond],
            from: date
        )
        var microseconds = ((components.nanosecond ?? 0) + 500) / 1_000
        if microseconds == 1_000_000 {
            components = calendar.dateComponents(
                [.year, .month, .day, .hour, .minute, .second],
                from: date.addingTimeInterval(0.000_001)
            )
            microseconds = 0
        }
        return String(
            format: "%04d-%02d-%02d_%02d-%02d-%02d-%06d",
            locale: Locale(identifier: "en_US_POSIX"),
            components.year ?? 0,
            components.month ?? 0,
            components.day ?? 0,
            components.hour ?? 0,
            components.minute ?? 0,
            components.second ?? 0,
            microseconds
        )
    }
}
