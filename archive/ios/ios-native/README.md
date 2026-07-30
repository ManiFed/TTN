# Boundless Skies — Native iOS (SwiftUI)

Pure native Swift replacement for the Flutter `flutter_tts` and `vibration` plugins.
No CocoaPods, SPM, or Carthage dependencies — only Apple frameworks
(`AVFoundation`, `CoreHaptics`, `UIKit`, `SwiftUI`).

## Open in Xcode

```bash
open app/ios-native/BoundlessSkies.xcodeproj
```

Set your **Development Team** in Signing & Capabilities, then run on a device or
simulator (⌘R).

## Requirements

| Setting | Value |
|---------|-------|
| iOS Deployment Target | 17.0+ |
| Swift Language Mode | 6.0 |
| Strict Concurrency | Complete |
| External Dependencies | None |

## Architecture

```
BoundlessSkies/
├── BoundlessSkiesApp.swift      App entry (@main)
├── ContentView.swift            Root navigation + layout
├── Managers/
│   ├── TextToSpeechManager.swift   AVSpeechSynthesizer wrapper
│   └── HapticManager.swift         CoreHaptics + UIKit fallback
├── Models/
│   ├── HapticEvent.swift
│   └── HapticStyle.swift
├── ViewModels/
│   └── SpeechHapticsViewModel.swift   MVVM coordinator
└── Views/
    ├── SpeechControlsSection.swift
    ├── HapticTestSection.swift
    └── SpeakingStatusView.swift
```

## Flutter → Native mapping

| Flutter plugin | Native replacement |
|----------------|-------------------|
| `flutter_tts` | `TextToSpeechManager` (`AVSpeechSynthesizer`) |
| `vibration` | `HapticManager` (`CHHapticEngine` + `UIImpactFeedbackGenerator`) |

The Flutter app used TTS and haptics in `target_detail_screen.dart` for
accessibility ("Hear" / "Feel" light-curve data). This native project provides
the same core capabilities as a standalone foundation for porting the rest of the
app.

## Build from CLI

```bash
cd app/ios-native
xcodebuild -scheme BoundlessSkies \
  -destination 'platform=iOS Simulator,name=iPhone 16' \
  build test
```

## Tests

Unit tests live in `BoundlessSkiesTests/` and use the Swift Testing framework.
Run with ⌘U in Xcode or via `xcodebuild test`.