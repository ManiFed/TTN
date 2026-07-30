import SwiftUI

struct SpeakingStatusView: View {
    let isSpeaking: Bool
    let isPaused: Bool
    let statusMessage: String
    let progressDescription: String

    private var stateLabel: String {
        if isPaused { "Paused" }
        else if isSpeaking { "Speaking" }
        else { "Idle" }
    }

    private var stateColor: Color {
        if isPaused { .orange }
        else if isSpeaking { .green }
        else { .secondary }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Image(systemName: isSpeaking ? "waveform" : "waveform.slash")
                    .symbolEffect(.variableColor.iterative, isActive: isSpeaking && !isPaused)
                    .foregroundStyle(stateColor)
                    .font(.title2)
                    .accessibilityHidden(true)

                VStack(alignment: .leading, spacing: 2) {
                    Text(stateLabel)
                        .font(.headline)
                    Text(statusMessage)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }

                Spacer()

                Circle()
                    .fill(stateColor)
                    .frame(width: 12, height: 12)
                    .accessibilityLabel("Speech state indicator")
                    .accessibilityValue(stateLabel)
            }

            if !progressDescription.isEmpty {
                Text(progressDescription)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Speech progress")
                    .accessibilityValue(progressDescription)
            }
        }
        .padding()
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
    }
}