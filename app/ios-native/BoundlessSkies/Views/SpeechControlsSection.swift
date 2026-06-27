import SwiftUI

struct SpeechControlsSection: View {
    @Bindable var viewModel: SpeechHapticsViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Text to Speech", systemImage: "text.bubble.fill")
                .font(.headline)

            TextField("Enter text to speak", text: $viewModel.speechText, axis: .vertical)
                .lineLimit(3...6)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("Speech text")
                .accessibilityHint("Enter the text you want spoken aloud")

            Picker("Language", selection: $viewModel.selectedLanguage) {
                ForEach(viewModel.availableLanguages, id: \.self) { code in
                    Text(languageDisplayName(code))
                        .tag(code)
                }
            }
            .accessibilityLabel("Speech language")
            .accessibilityHint("Select the language for text to speech")

            VStack(alignment: .leading, spacing: 4) {
                Slider(value: $viewModel.speechRate, in: 0...1)
                    .accessibilityLabel("Speech rate")
                    .accessibilityValue("\(Int(viewModel.speechRate * 100)) percent")
                Text("Rate: \(viewModel.speechRate, format: .number.precision(.fractionLength(2)))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: 4) {
                Slider(value: $viewModel.speechPitch, in: 0.5...2.0)
                    .accessibilityLabel("Speech pitch")
                    .accessibilityValue("\(viewModel.speechPitch, format: .number.precision(.fractionLength(1)))")
                Text("Pitch: \(viewModel.speechPitch, format: .number.precision(.fractionLength(2)))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: 4) {
                Slider(value: $viewModel.speechVolume, in: 0...1)
                    .accessibilityLabel("Speech volume")
                    .accessibilityValue("\(Int(viewModel.speechVolume * 100)) percent")
                Text("Volume: \(viewModel.speechVolume, format: .number.precision(.fractionLength(2)))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 12) {
                Button {
                    viewModel.speakWithHaptic()
                } label: {
                    Label("Speak", systemImage: "play.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .accessibilityLabel("Speak text")
                .accessibilityHint("Speaks the entered text with a haptic tap")

                Button {
                    viewModel.stopSpeech()
                } label: {
                    Label("Stop", systemImage: "stop.fill")
                }
                .buttonStyle(.bordered)
                .disabled(!viewModel.isSpeaking && !viewModel.isPaused)
                .accessibilityLabel("Stop speech")
            }

            HStack(spacing: 12) {
                Button {
                    viewModel.pauseSpeech()
                } label: {
                    Label("Pause", systemImage: "pause.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .disabled(!viewModel.isSpeaking || viewModel.isPaused)
                .accessibilityLabel("Pause speech")

                Button {
                    viewModel.resumeSpeech()
                } label: {
                    Label("Resume", systemImage: "playpause.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .disabled(!viewModel.isPaused)
                .accessibilityLabel("Resume speech")
            }

            if !viewModel.voicesForSelectedLanguage().isEmpty {
                DisclosureGroup("Available Voices (\(viewModel.voicesForSelectedLanguage().count))") {
                    ForEach(viewModel.voicesForSelectedLanguage(), id: \.identifier) { voice in
                        Text(voice.name)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .font(.caption)
            }
        }
    }

    private func languageDisplayName(_ code: String) -> String {
        Locale.current.localizedString(forIdentifier: code) ?? code
    }
}