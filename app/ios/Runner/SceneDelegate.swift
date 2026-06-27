import UIKit
import Flutter

class SceneDelegate: FlutterSceneDelegate {
  override func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
    super.scene(scene, willConnectTo: session, options: connectionOptions)

    guard let windowScene = scene as? UIWindowScene else { return }

    // Use the implicit Flutter engine provided by FlutterAppDelegate/FlutterSceneDelegate
    let flutterViewController = FlutterViewController(engine: self.flutterEngine, nibName: nil, bundle: nil)

    let window = UIWindow(windowScene: windowScene)
    window.rootViewController = flutterViewController
    self.window = window
    window.makeKeyAndVisible()
  }
}
