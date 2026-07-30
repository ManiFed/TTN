import CoreHaptics
import Observation
import UIKit

enum HapticManagerError: LocalizedError, Sendable {
    case engineUnavailable
    case patternPlaybackFailed(String)

    var errorDescription: String? {
        switch self {
        case .engineUnavailable:
            "Haptic engine is not available on this device."
        case .patternPlaybackFailed(let message):
            "Haptic playback failed: \(message)"
        }
    }
}

/// Native haptic feedback using Core Haptics with UIKit fallbacks.
@MainActor
@Observable
final class HapticManager {
    private(set) var isPlaying = false
    private(set) var lastError: HapticManagerError?
    private(set) var supportsAdvancedHaptics: Bool

    private var engine: CHHapticEngine?
    private var activePlayer: CHHapticPatternPlayer?
    private var impactGenerators: [HapticStyle: UIImpactFeedbackGenerator] = [:]

    init() {
        supportsAdvancedHaptics = CHHapticEngine.capabilitiesForHardware().supportsHaptics
        if supportsAdvancedHaptics {
            prepareEngine()
        }
    }

    // MARK: - Public API

    func supportsHaptics() -> Bool {
        #if targetEnvironment(simulator)
        return true
        #else
        return CHHapticEngine.capabilitiesForHardware().supportsHaptics
        #endif
    }

    func vibrate(style: HapticStyle = .medium) {
        lastError = nil

        #if targetEnvironment(simulator)
        let generator = notificationGenerator(for: style)
        generator.notificationOccurred(.success)
        return
        #endif

        if supportsAdvancedHaptics {
            do {
                try playPattern(intensity: style.defaultIntensity, duration: 0.12)
                return
            } catch {
                // Fall through to UIKit generator.
            }
        }

        let generator = impactGenerator(for: style)
        generator.prepare()
        generator.impactOccurred(intensity: CGFloat(style.defaultIntensity))
    }

    func playPattern(intensity: Float, duration: TimeInterval) throws {
        let event = HapticEvent(
            relativeTime: 0,
            intensity: intensity,
            sharpness: 0.5,
            duration: duration
        )
        try playPattern(events: [event])
    }

    func playPattern(events: [HapticEvent]) throws {
        guard !events.isEmpty else { return }
        lastError = nil

        #if targetEnvironment(simulator)
        vibrate(style: .medium)
        return
        #endif

        guard supportsAdvancedHaptics else {
            playUIKitFallback(events: events)
            return
        }

        try ensureEngineRunning()

        let hapticEvents = events.map { event -> CHHapticEvent in
            CHHapticEvent(
                eventType: .hapticContinuous,
                parameters: [
                    CHHapticEventParameter(
                        parameterID: .hapticIntensity,
                        value: event.intensity
                    ),
                    CHHapticEventParameter(
                        parameterID: .hapticSharpness,
                        value: event.sharpness
                    ),
                ],
                relativeTime: event.relativeTime,
                duration: event.duration
            )
        }

        do {
            let pattern = try CHHapticPattern(events: hapticEvents, parameters: [])
            let player = try engine?.makePlayer(with: pattern)
            activePlayer = player
            isPlaying = true

            try player?.start(atTime: CHHapticTimeImmediate)

            let totalDuration = events.map { $0.relativeTime + $0.duration }.max() ?? 0
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(totalDuration + 0.05))
                if self.isPlaying {
                    self.isPlaying = false
                    self.activePlayer = nil
                }
            }
        } catch {
            let message = error.localizedDescription
            lastError = .patternPlaybackFailed(message)
            throw HapticManagerError.patternPlaybackFailed(message)
        }
    }

    func stop() {
        try? activePlayer?.stop(atTime: CHHapticTimeImmediate)
        activePlayer = nil
        isPlaying = false
        engine?.stop(completionHandler: nil)
    }

    func reset() {
        stop()
        engine = nil
        if supportsAdvancedHaptics {
            prepareEngine()
        }
    }

    // MARK: - Private

    private func prepareEngine() {
        guard supportsAdvancedHaptics else { return }

        do {
            engine = try CHHapticEngine()
            engine?.isAutoShutdownEnabled = true
            engine?.resetHandler = { [weak self] in
                Task { @MainActor in
                    self?.handleEngineReset()
                }
            }
            engine?.stoppedHandler = { [weak self] reason in
                Task { @MainActor in
                    if reason == .audioSessionInterrupt {
                        self?.handleEngineReset()
                    }
                }
            }
            try engine?.start()
        } catch {
            supportsAdvancedHaptics = false
            lastError = .engineUnavailable
        }
    }

    private func handleEngineReset() {
        isPlaying = false
        activePlayer = nil
        prepareEngine()
    }

    private func ensureEngineRunning() throws {
        guard supportsAdvancedHaptics else {
            throw HapticManagerError.engineUnavailable
        }

        if engine == nil {
            prepareEngine()
        }

        do {
            try engine?.start()
        } catch {
            let message = error.localizedDescription
            lastError = .patternPlaybackFailed(message)
            throw HapticManagerError.patternPlaybackFailed(message)
        }
    }

    private func playUIKitFallback(events: [HapticEvent]) {
        isPlaying = true
        Task { @MainActor in
            for event in events {
                let style = HapticStyle.from(intensity: event.intensity)
                let generator = impactGenerator(for: style)
                generator.prepare()
                generator.impactOccurred(intensity: CGFloat(event.intensity))
                try? await Task.sleep(for: .seconds(event.duration))
            }
            isPlaying = false
        }
    }

    private func impactGenerator(for style: HapticStyle) -> UIImpactFeedbackGenerator {
        if let cached = impactGenerators[style] {
            return cached
        }
        let generator = UIImpactFeedbackGenerator(style: style.impactStyle)
        impactGenerators[style] = generator
        return generator
    }

    private func notificationGenerator(for _: HapticStyle) -> UINotificationFeedbackGenerator {
        UINotificationFeedbackGenerator()
    }
}