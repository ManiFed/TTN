import UIKit

/// Haptic feedback intensity styles with UIKit generator mapping.
enum HapticStyle: String, CaseIterable, Identifiable, Sendable {
    case light
    case medium
    case heavy
    case soft
    case rigid

    var id: String { rawValue }

    var displayName: String {
        rawValue.capitalized
    }

    var impactStyle: UIImpactFeedbackGenerator.FeedbackStyle {
        switch self {
        case .light: .light
        case .medium: .medium
        case .heavy: .heavy
        case .soft: .soft
        case .rigid: .rigid
        }
    }

    var symbolName: String {
        switch self {
        case .light: "circle.fill"
        case .medium: "circle.circle.fill"
        case .heavy: "circle.hexagongrid.fill"
        case .soft: "cloud.fill"
        case .rigid: "bolt.fill"
        }
    }

    var defaultIntensity: Float {
        switch self {
        case .light: 0.35
        case .medium: 0.55
        case .heavy: 0.85
        case .soft: 0.45
        case .rigid: 0.95
        }
    }

    static func from(intensity: Float) -> HapticStyle {
        switch intensity {
        case ..<0.3: .light
        case ..<0.5: .soft
        case ..<0.7: .medium
        case ..<0.85: .heavy
        default: .rigid
        }
    }
}