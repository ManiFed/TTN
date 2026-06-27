import AVFoundation
import Observation

// MARK: - Delegate

protocol TextToSpeechManagerDelegate: AnyObject {
    func speechDidStart(utterance: AVSpeechUtterance)
    func speechDidFinish(utterance: AVSpeechUtterance)
    func speechDidCancel(utterance: AVSpeechUtterance)
    func speechWillSpeakRange(
        characterRange: NSRange,
        utterance: AVSpeechUtterance
    )
}

extension TextToSpeechManagerDelegate {
    func speechDidStart(utterance: AVSpeechUtterance) {}
    func speechDidFinish(utterance: AVSpeechUtterance) {}
    func speechDidCancel(utterance: AVSpeechUtterance) {}
    func speechWillSpeakRange(
        characterRange: NSRange,
        utterance: AVSpeechUtterance
    ) {}
}

// MARK: - Errors

enum TextToSpeechError: LocalizedError, Sendable {
    case emptyText
    case audioSessionFailed(String)
    case synthesizerBusy

    var errorDescription: String? {
        switch self {
        case .emptyText:
            "Speech text cannot be empty."
        case .audioSessionFailed(let message):
            "Audio session configuration failed: \(message)"
        case .synthesizerBusy:
            "Speech synthesizer is not ready."
        }
    }
}

// MARK: - Manager

/// Native text-to-speech backed by `AVSpeechSynthesizer`.
@MainActor
@Observable
final class TextToSpeechManager: NSObject {
    private(set) var isSpeaking = false
    private(set) var isPaused = false
    private(set) var lastError: TextToSpeechError?
    private(set) var spokenCharacterIndex = 0

    weak var delegate: TextToSpeechManagerDelegate?

    private let synthesizer = AVSpeechSynthesizer()
    private var currentUtterance: AVSpeechUtterance?

    override init() {
        super.init()
        synthesizer.delegate = self
        try? configureAudioSession()
    }

    // MARK: - Public API

    func speak(
        text: String,
        language: String? = nil,
        rate: Float = 0.5,
        pitch: Float = 1.0,
        volume: Float = 1.0
    ) throws {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            lastError = .emptyText
            throw TextToSpeechError.emptyText
        }

        try configureAudioSession()

        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }

        let utterance = AVSpeechUtterance(string: trimmed)
        utterance.rate = mappedSpeechRate(rate)
        utterance.pitchMultiplier = clamped(pitch, min: 0.5, max: 2.0)
        utterance.volume = clamped(volume, min: 0.0, max: 1.0)

        if let language {
            utterance.voice = preferredVoice(for: language)
        }

        currentUtterance = utterance
        spokenCharacterIndex = 0
        isPaused = false
        lastError = nil
        synthesizer.speak(utterance)
    }

    func stop() {
        guard synthesizer.isSpeaking || synthesizer.isPaused else { return }
        synthesizer.stopSpeaking(at: .immediate)
        isSpeaking = false
        isPaused = false
        spokenCharacterIndex = 0
    }

    func pause() {
        guard synthesizer.isSpeaking, !synthesizer.isPaused else { return }
        synthesizer.pauseSpeaking(at: .word)
        isPaused = true
    }

    func resume() {
        guard synthesizer.isPaused else { return }
        synthesizer.continueSpeaking()
        isPaused = false
    }

    func getAvailableLanguages() -> [String] {
        Set(AVSpeechSynthesisVoice.speechVoices().map(\.language))
            .sorted()
    }

    func getAvailableVoices(for language: String) -> [AVSpeechSynthesisVoice] {
        AVSpeechSynthesisVoice.speechVoices()
            .filter { $0.language.hasPrefix(language) || $0.language == language }
            .sorted { $0.name < $1.name }
    }

    // MARK: - Private

    private func configureAudioSession() throws {
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(
                .playback,
                mode: .spokenAudio,
                options: [.duckOthers, .interruptSpokenAudioAndMixWithOthers]
            )
            try session.setActive(true, options: .notifyOthersOnDeactivation)
        } catch {
            let message = error.localizedDescription
            lastError = .audioSessionFailed(message)
            throw TextToSpeechError.audioSessionFailed(message)
        }
    }

    private func mappedSpeechRate(_ normalizedRate: Float) -> Float {
        let clampedRate = clamped(normalizedRate, min: 0.0, max: 1.0)
        let minimum = AVSpeechUtteranceMinimumSpeechRate
        let maximum = AVSpeechUtteranceMaximumSpeechRate
        return minimum + clampedRate * (maximum - minimum)
    }

    private func preferredVoice(for language: String) -> AVSpeechSynthesisVoice? {
        getAvailableVoices(for: language).first
            ?? AVSpeechSynthesisVoice(language: language)
    }

    private func clamped(_ value: Float, min: Float, max: Float) -> Float {
        Swift.min(Swift.max(value, min), max)
    }

    private func updateSpeakingState() {
        isSpeaking = synthesizer.isSpeaking
        isPaused = synthesizer.isPaused
    }
}

// MARK: - AVSpeechSynthesizerDelegate

extension TextToSpeechManager: AVSpeechSynthesizerDelegate {
    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer,
        didStart utterance: AVSpeechUtterance
    ) {
        Task { @MainActor in
            updateSpeakingState()
            delegate?.speechDidStart(utterance: utterance)
        }
    }

    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer,
        didFinish utterance: AVSpeechUtterance
    ) {
        Task { @MainActor in
            isSpeaking = false
            isPaused = false
            spokenCharacterIndex = 0
            currentUtterance = nil
            delegate?.speechDidFinish(utterance: utterance)
        }
    }

    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer,
        didCancel utterance: AVSpeechUtterance
    ) {
        Task { @MainActor in
            isSpeaking = false
            isPaused = false
            spokenCharacterIndex = 0
            currentUtterance = nil
            delegate?.speechDidCancel(utterance: utterance)
        }
    }

    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer,
        willSpeakRange characterRange: NSRange,
        utterance: AVSpeechUtterance
    ) {
        Task { @MainActor in
            spokenCharacterIndex = characterRange.location + characterRange.length
            delegate?.speechWillSpeakRange(
                characterRange: characterRange,
                utterance: utterance
            )
        }
    }
}