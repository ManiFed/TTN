import AVFoundation
import Testing
@testable import BoundlessSkies

@MainActor
struct TextToSpeechManagerTests {
    @Test func availableLanguagesAreNonEmpty() {
        let manager = TextToSpeechManager()
        let languages = manager.getAvailableLanguages()
        #expect(!languages.isEmpty)
    }

    @Test func availableVoicesMatchLanguage() {
        let manager = TextToSpeechManager()
        let voices = manager.getAvailableVoices(for: "en-US")
        #expect(voices.allSatisfy { $0.language.hasPrefix("en") })
    }

    @Test func emptyTextThrows() {
        let manager = TextToSpeechManager()
        #expect(throws: TextToSpeechError.emptyText) {
            try manager.speak(text: "   ")
        }
    }

    @Test func initialStateIsNotSpeaking() {
        let manager = TextToSpeechManager()
        #expect(manager.isSpeaking == false)
        #expect(manager.isPaused == false)
    }
}