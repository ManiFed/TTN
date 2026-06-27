import AVFoundation
import Observation

@MainActor
@Observable
final class SpeechHapticsViewModel: TextToSpeechManagerDelegate {
    let speechManager = TextToSpeechManager()
    let hapticManager = HapticManager()

    var speechText = "Welcome to Boundless Skies. This native Swift app uses AVSpeechSynthesizer for text-to-speech and Core Haptics for tactile feedback."
    var selectedLanguage = "en-US"
    var speechRate: Float = 0.5
    var speechPitch: Float = 1.0
    var speechVolume: Float = 1.0

    private(set) var statusMessage = "Ready"
    private(set) var progressDescription = ""

    var availableLanguages: [String] {
        speechManager.getAvailableLanguages()
    }

    var isSpeaking: Bool { speechManager.isSpeaking }
    var isPaused: Bool { speechManager.isPaused }
    var supportsHaptics: Bool { hapticManager.supportsHaptics() }

    init() {
        speechManager.delegate = self
        if !availableLanguages.contains(selectedLanguage),
           let first = availableLanguages.first {
            selectedLanguage = first
        }
    }

    func speakWithHaptic() {
        do {
            try speechManager.speak(
                text: speechText,
                language: selectedLanguage,
                rate: speechRate,
                pitch: speechPitch,
                volume: speechVolume
            )
            hapticManager.vibrate(style: .medium)
            statusMessage = "Speaking"
        } catch {
            statusMessage = error.localizedDescription
        }
    }

    func stopSpeech() {
        speechManager.stop()
        hapticManager.stop()
        statusMessage = "Stopped"
        progressDescription = ""
    }

    func pauseSpeech() {
        speechManager.pause()
        statusMessage = "Paused"
    }

    func resumeSpeech() {
        speechManager.resume()
        statusMessage = "Speaking"
    }

    func testHaptic(style: HapticStyle) {
        hapticManager.vibrate(style: style)
        statusMessage = "\(style.displayName) haptic"
    }

    func playComplexHapticPattern() {
        let events: [HapticEvent] = [
            .pulse(at: 0.0, intensity: 0.3, duration: 0.1),
            .pulse(at: 0.15, intensity: 0.6, duration: 0.12),
            .pulse(at: 0.35, intensity: 0.9, duration: 0.15),
            .pulse(at: 0.55, intensity: 0.5, duration: 0.1),
        ]
        do {
            try hapticManager.playPattern(events: events)
            statusMessage = "Playing haptic pattern"
        } catch {
            statusMessage = error.localizedDescription
        }
    }

    func voicesForSelectedLanguage() -> [AVSpeechSynthesisVoice] {
        speechManager.getAvailableVoices(for: selectedLanguage)
    }

    // MARK: - TextToSpeechManagerDelegate

    func speechDidStart(utterance: AVSpeechUtterance) {
        statusMessage = "Speaking"
        progressDescription = "Started: \(utterance.speechString.prefix(40))…"
    }

    func speechDidFinish(utterance: AVSpeechUtterance) {
        statusMessage = "Finished"
        progressDescription = "Completed \(utterance.speechString.count) characters"
        hapticManager.vibrate(style: .light)
    }

    func speechDidCancel(utterance: AVSpeechUtterance) {
        statusMessage = "Cancelled"
        progressDescription = "Stopped at \(utterance.speechString.count) characters"
    }

    func speechWillSpeakRange(
        characterRange: NSRange,
        utterance: AVSpeechUtterance
    ) {
        let spoken = characterRange.location + characterRange.length
        let total = utterance.speechString.count
        progressDescription = "Progress: \(spoken) / \(total) characters"
    }
}