import Foundation

/// A single event in a Core Haptics pattern.
struct HapticEvent: Sendable, Identifiable {
    let id = UUID()
    let relativeTime: TimeInterval
    let intensity: Float
    let sharpness: Float
    let duration: TimeInterval

    init(
        relativeTime: TimeInterval = 0,
        intensity: Float,
        sharpness: Float = 0.5,
        duration: TimeInterval = 0.1
    ) {
        self.relativeTime = relativeTime
        self.intensity = min(max(intensity, 0), 1)
        self.sharpness = min(max(sharpness, 0), 1)
        self.duration = max(duration, 0.01)
    }

    /// Convenience initializer for a simple pulse at a given offset.
    static func pulse(
        at time: TimeInterval,
        intensity: Float,
        duration: TimeInterval = 0.15
    ) -> HapticEvent {
        HapticEvent(
            relativeTime: time,
            intensity: intensity,
            sharpness: 0.6,
            duration: duration
        )
    }
}