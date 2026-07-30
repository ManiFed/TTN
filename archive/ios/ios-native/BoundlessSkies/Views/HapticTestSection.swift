import SwiftUI

struct HapticTestSection: View {
    let supportsHaptics: Bool
    let onStyleTapped: (HapticStyle) -> Void
    let onPatternTapped: () -> Void

    private let columns = [
        GridItem(.flexible()),
        GridItem(.flexible()),
        GridItem(.flexible()),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Haptic Feedback", systemImage: "hand.tap.fill")
                .font(.headline)

            if !supportsHaptics {
                Text("Advanced haptics are limited on this device. UIKit feedback generators will be used as a fallback.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            LazyVGrid(columns: columns, spacing: 10) {
                ForEach(HapticStyle.allCases) { style in
                    Button {
                        onStyleTapped(style)
                    } label: {
                        VStack(spacing: 6) {
                            Image(systemName: style.symbolName)
                                .font(.title3)
                            Text(style.displayName)
                                .font(.caption)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                    }
                    .buttonStyle(.bordered)
                    .accessibilityLabel("\(style.displayName) haptic test")
                    .accessibilityHint("Plays a \(style.displayName.lowercased()) impact haptic")
                }
            }

            Button {
                onPatternTapped()
            } label: {
                Label("Play Complex Pattern", systemImage: "waveform.path")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .accessibilityLabel("Play complex haptic pattern")
            .accessibilityHint("Plays a multi-event haptic sequence")
        }
    }
}