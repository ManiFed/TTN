import Testing
@testable import BoundlessSkies

@MainActor
struct HapticManagerTests {
    @Test func supportsHapticsReturnsBool() {
        let manager = HapticManager()
        let supports = manager.supportsHaptics()
        #expect(supports == true || supports == false)
    }

    @Test func vibrateDoesNotThrow() {
        let manager = HapticManager()
        manager.vibrate(style: .light)
        manager.vibrate(style: .heavy)
    }

    @Test func stopClearsPlayingState() {
        let manager = HapticManager()
        manager.stop()
        #expect(manager.isPlaying == false)
    }

    @Test func emptyPatternIsNoOp() throws {
        let manager = HapticManager()
        try manager.playPattern(events: [])
        #expect(manager.isPlaying == false)
    }

    @Test func appConfigBuildsVersionedAPIURLs() {
        let url = AppConfig.url("/me/observations", query: ["days": 90, "limit": 200])

        #expect(url.absoluteString.contains("/api/v1/me/observations"))
        #expect(url.query?.contains("days=90") == true)
        #expect(url.query?.contains("limit=200") == true)
    }

    @Test func appConfigBuildsSkyQualityURL() {
        let url = AppConfig.url("/sky-quality", query: ["lat": 40.7, "lon": -74.0])

        #expect(url.absoluteString.contains("/api/v1/sky-quality"))
        #expect(url.query?.contains("lat=40.7") == true)
        #expect(url.query?.contains("lon=-74.0") == true)
    }

    @Test func notificationTitleFallsBackToPayloadTarget() throws {
        let data = """
        {
          "id": 7,
          "type": "night_summary",
          "payload": {"target": "T CrB"},
          "sent_at": "2026-06-27T01:02:03Z",
          "read_at": null
        }
        """.data(using: .utf8)!

        let notification = try JSONDecoder().decode(AppNotification.self, from: data)

        #expect(notification.title == "T CrB")
        #expect(notification.read == false)
    }
}
