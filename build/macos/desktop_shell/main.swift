import Cocoa
import WebKit

/// Native shell for The Telescope Net control UI.
/// Loads the local node agent dashboard (127.0.0.1:5173) in a real app window.
/// The headless Python agent remains a separate background process/service.

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    private var webView: WKWebView!
    private var statusLabel: NSTextField!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let frame = NSRect(x: 0, y: 0, width: 1280, height: 840)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "The Telescope Net"
        window.center()
        window.setFrameAutosaveName("TelescopeNetMain")
        window.minSize = NSSize(width: 960, height: 640)

        let root = NSView(frame: frame)
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor(calibratedRed: 0.01, green: 0.02, blue: 0.05, alpha: 1).cgColor

        statusLabel = NSTextField(labelWithString: "Starting local node…")
        statusLabel.font = NSFont.systemFont(ofSize: 14, weight: .medium)
        statusLabel.textColor = NSColor(calibratedWhite: 0.75, alpha: 1)
        statusLabel.alignment = .center
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(statusLabel)

        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.setValue(false, forKey: "drawsBackground")
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.isHidden = true
        root.addSubview(webView)

        NSLayoutConstraint.activate([
            statusLabel.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            statusLabel.centerYAnchor.constraint(equalTo: root.centerYAnchor),
            statusLabel.leadingAnchor.constraint(greaterThanOrEqualTo: root.leadingAnchor, constant: 40),
            statusLabel.trailingAnchor.constraint(lessThanOrEqualTo: root.trailingAnchor, constant: -40),

            webView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            webView.topAnchor.constraint(equalTo: root.topAnchor),
            webView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])

        window.contentView = root
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        ensureAgentThenLoad(attempt: 0)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    // MARK: - Agent + load

    private func ensureAgentThenLoad(attempt: Int) {
        if attempt == 0 || attempt % 4 == 0 {
            launchAgentIfNeeded()
        }

        let url = URL(string: "http://127.0.0.1:5173/")!
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5

        let task = URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self else { return }
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            let ok = (200..<400).contains(code)
            DispatchQueue.main.async {
                if ok {
                    self.statusLabel.isHidden = true
                    self.webView.isHidden = false
                    self.webView.load(URLRequest(url: url))
                    return
                }
                if attempt >= 60 {
                    self.statusLabel.stringValue =
                        "Could not reach the local node service on port 5173.\nReinstall The Telescope Net or open Support."
                    return
                }
                self.statusLabel.stringValue = "Waiting for local node service…"
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                    self.ensureAgentThenLoad(attempt: attempt + 1)
                }
            }
        }
        task.resume()
    }

    private func launchAgentIfNeeded() {
        // Prefer launchd service; fall back to launching the bundled agent app.
        let openAgent = Process()
        openAgent.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        openAgent.arguments = ["-a", "/Applications/TelescopeNetNode.app", "--args", "--no-browser"]
        openAgent.standardOutput = FileHandle.nullDevice
        openAgent.standardError = FileHandle.nullDevice
        try? openAgent.run()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
