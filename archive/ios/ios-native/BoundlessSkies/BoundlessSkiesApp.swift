import SwiftUI
import UIKit
import UserNotifications

#if canImport(FirebaseCore) && canImport(FirebaseMessaging)
import FirebaseCore
import FirebaseMessaging
#endif

@main
struct BoundlessSkiesApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        #if canImport(FirebaseCore) && canImport(FirebaseMessaging)
        FirebaseApp.configure()
        Messaging.messaging().delegate = self
        #endif

        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        #if canImport(FirebaseMessaging)
        Messaging.messaging().apnsToken = deviceToken
        Messaging.messaging().token { token, error in
            if let token {
                NotificationCenter.default.post(name: .nativePushTokenUpdated, object: token)
            } else if let error {
                NotificationCenter.default.post(name: .nativePushRegistrationFailed, object: error)
            }
        }
        #else
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        NotificationCenter.default.post(name: .nativePushTokenUpdated, object: token)
        #endif
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        NotificationCenter.default.post(name: .nativePushRegistrationFailed, object: error)
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let userInfo = response.notification.request.content.userInfo
        if userInfo["type"] as? String == "night_summary" {
            NotificationCenter.default.post(name: .nativeNightSummaryTapped, object: nil)
        }
    }
}

#if canImport(FirebaseMessaging)
extension AppDelegate: MessagingDelegate {
    func messaging(_ messaging: Messaging, didReceiveRegistrationToken fcmToken: String?) {
        guard let fcmToken else { return }
        NotificationCenter.default.post(name: .nativePushTokenUpdated, object: fcmToken)
    }
}
#endif
