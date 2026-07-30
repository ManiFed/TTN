import AVFoundation
import Charts
import CoreLocation
import Foundation
import Observation
import Security
import SwiftUI
import UIKit
import UserNotifications

// MARK: - App Entry View

struct ContentView: View {
    @State private var appState = MemberAppState()

    var body: some View {
        Group {
            switch appState.status {
            case .unknown:
                ProgressView()
                    .task { await appState.bootstrap() }
            case .signedOut:
                LoginView(appState: appState)
            case .signedIn:
                HomeShell(appState: appState)
            }
        }
        .preferredColorScheme(.dark)
        .tint(AppTheme.accent)
        .task { await appState.bootstrap() }
    }
}

// MARK: - Configuration

enum AppConfig {
    static let apiPrefix = "/api/v1"
    static let productionBase = "https://api.thetelescope.net"

    static var apiBase: String {
        Bundle.main.object(forInfoDictionaryKey: "BS_API_BASE") as? String
            ?? productionBase
    }

    static func url(_ path: String, query: [String: CustomStringConvertible] = [:]) -> URL {
        var components = URLComponents(string: apiBase + apiPrefix + path)!
        if !query.isEmpty {
            components.queryItems = query.map { URLQueryItem(name: $0.key, value: "\($0.value)") }
        }
        return components.url!
    }
}

enum AppTheme {
    static let night = Color(red: 15 / 255, green: 17 / 255, blue: 20 / 255)
    static let surface = Color(red: 23 / 255, green: 26 / 255, blue: 30 / 255)
    static let surface2 = Color(red: 29 / 255, green: 33 / 255, blue: 38 / 255)
    static let ink = Color(red: 235 / 255, green: 236 / 255, blue: 238 / 255)
    static let ink2 = ink.opacity(0.68)
    static let ink3 = ink.opacity(0.42)
    static let accent = Color(red: 127 / 255, green: 166 / 255, blue: 199 / 255)
    static let sky = accent
    static let warm = Color(red: 170 / 255, green: 163 / 255, blue: 143 / 255)
    static let danger = Color(red: 192 / 255, green: 140 / 255, blue: 140 / 255)
}

// MARK: - JSON Helpers

enum JSONValue: Decodable, Hashable, CustomStringConvertible {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            self = .array((try? container.decode([JSONValue].self)) ?? [])
        }
    }

    var description: String {
        switch self {
        case .string(let value): value
        case .number(let value): value.formatted()
        case .bool(let value): value ? "true" : "false"
        case .object(let value): value.map { "\($0.key): \($0.value)" }.sorted().joined(separator: ", ")
        case .array(let value): value.map(\.description).joined(separator: ", ")
        case .null: ""
        }
    }

    var stringValue: String {
        if case .string(let value) = self { return value }
        return description
    }
}

func asInt(_ value: Int?) -> Int { value ?? 0 }
func asDouble(_ value: Double?) -> Double { value ?? 0 }
func compactDate(_ raw: String) -> String {
    raw.replacingOccurrences(of: "T", with: " ").replacingOccurrences(of: "Z", with: "")
}

// MARK: - Models

struct Member: Decodable {
    let userId: String
    let email: String
    let role: String
    let displayName: String
    let country: String

    enum CodingKeys: String, CodingKey {
        case userId = "user_id", email, role, displayName = "display_name", country
    }
}

struct PreviousLocation: Decodable, Identifiable, Hashable {
    var id: String { "\(lat),\(lon),\(lastUsed)" }
    let lat: Double
    let lon: Double
    let city: String
    let siteName: String
    let lastUsed: String

    enum CodingKeys: String, CodingKey {
        case lat, lon, city, siteName = "site_name", lastUsed = "last_used"
    }

    var label: String {
        if !siteName.isEmpty, !city.isEmpty { return "\(siteName), \(city)" }
        return siteName.isEmpty ? city : siteName
    }
}

struct MemberNode: Decodable, Identifiable {
    var id: String { nodeId }
    let nodeId: String
    let telescopeModel: String
    let city: String
    let country: String
    let status: String
    let lastHeartbeat: String
    let online: Bool
    let portable: Bool
    let vacationUntil: String
    let sessionCity: String
    let sessionSiteName: String
    let previousLocations: [PreviousLocation]

    enum CodingKeys: String, CodingKey {
        case nodeId = "node_id", telescopeModel = "telescope_model", city, country, status
        case lastHeartbeat = "last_heartbeat", online, portable, vacationUntil = "vacation_until"
        case sessionCity = "session_city", sessionSiteName = "session_site_name"
        case previousLocations = "previous_locations"
    }

    var location: String {
        [city, country].filter { !$0.isEmpty }.joined(separator: ", ").ifEmpty("Location unknown")
    }

    var isSleeping: Bool { status == "sleeping" }
    var isOnVacation: Bool { status == "vacation" }
}

struct TelescopeSpec: Decodable, Identifiable, Hashable {
    var id: String { key }
    let key: String
    let displayName: String
    let apertureMm: Double
    let focalLengthMm: Double
    let focalRatio: Double
    let pixelScaleArcsec: Double
    let fovDeg: Double
    let mountType: String
    let tier: Int
    let cameraModel: String

    enum CodingKeys: String, CodingKey {
        case key
        case displayName = "telescope_model"
        case apertureMm = "aperture_mm"
        case focalLengthMm = "focal_length_mm"
        case focalRatio = "focal_ratio"
        case pixelScaleArcsec = "pixel_scale_arcsec"
        case fovDeg = "fov_deg"
        case mountType = "mount_type"
        case tier
        case cameraModel = "camera_model"
    }

    var specPayload: [String: Any] {
        var payload: [String: Any] = [:]
        if apertureMm > 0 { payload["aperture_mm"] = apertureMm }
        if focalLengthMm > 0 { payload["focal_length_mm"] = focalLengthMm }
        if pixelScaleArcsec > 0 { payload["pixel_scale_arcsec"] = pixelScaleArcsec }
        if fovDeg > 0 { payload["fov_deg"] = fovDeg }
        if !mountType.isEmpty { payload["mount_type"] = mountType }
        if !cameraModel.isEmpty { payload["camera_model"] = cameraModel }
        return payload
    }
}

struct MemberStats: Decodable {
    let totalObservations: Int
    let aavsoSubmitted: Int
    let targetsObserved: Int
    let clearNights: Int
    let nodeCount: Int

    enum CodingKeys: String, CodingKey {
        case totalObservations = "total_observations", aavsoSubmitted = "aavso_submitted"
        case targetsObserved = "targets_observed", clearNights = "clear_nights", nodeCount = "node_count"
    }

    static let empty = MemberStats(totalObservations: 0, aavsoSubmitted: 0, targetsObserved: 0, clearNights: 0, nodeCount: 0)
}

struct ObservationRecord: Decodable, Identifiable {
    var id: String { "\(nodeId)-\(targetName)-\(bjd)" }
    let nodeId: String
    let targetName: String
    let bjd: Double
    let magnitude: Double
    let uncertainty: Double
    let filter: String
    let qualityFlag: String
    let aavsoSubmitted: Bool
    let receivedAt: String

    enum CodingKeys: String, CodingKey {
        case nodeId = "node_id", targetName = "target_name", bjd, magnitude, uncertainty, filter
        case qualityFlag = "quality_flag", aavsoSubmitted = "aavso_submitted", receivedAt = "received_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nodeId = try c.decodeIfPresent(String.self, forKey: .nodeId) ?? ""
        targetName = try c.decodeIfPresent(String.self, forKey: .targetName) ?? ""
        bjd = try c.decodeIfPresent(Double.self, forKey: .bjd) ?? 0
        magnitude = try c.decodeIfPresent(Double.self, forKey: .magnitude) ?? 0
        uncertainty = try c.decodeIfPresent(Double.self, forKey: .uncertainty) ?? 0
        filter = try c.decodeIfPresent(String.self, forKey: .filter) ?? ""
        qualityFlag = try c.decodeIfPresent(String.self, forKey: .qualityFlag) ?? ""
        receivedAt = try c.decodeIfPresent(String.self, forKey: .receivedAt) ?? ""
        if let bool = try? c.decode(Bool.self, forKey: .aavsoSubmitted) {
            aavsoSubmitted = bool
        } else {
            aavsoSubmitted = (try c.decodeIfPresent(Int.self, forKey: .aavsoSubmitted) ?? 0) == 1
        }
    }
}

struct LightCurvePoint: Decodable, Identifiable {
    var id: String { "\(nodeId)-\(bjd)-\(magnitude)" }
    let nodeId: String
    let bjd: Double
    let magnitude: Double
    let uncertainty: Double
    let filter: String
    let snr: Double
    let qualityFlag: String
    let aavsoSubmitted: Bool

    enum CodingKeys: String, CodingKey {
        case nodeId = "node_id", bjd, magnitude, uncertainty, filter, snr
        case qualityFlag = "quality_flag", aavsoSubmitted = "aavso_submitted"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nodeId = try c.decodeIfPresent(String.self, forKey: .nodeId) ?? ""
        bjd = try c.decodeIfPresent(Double.self, forKey: .bjd) ?? 0
        magnitude = try c.decodeIfPresent(Double.self, forKey: .magnitude) ?? 0
        uncertainty = try c.decodeIfPresent(Double.self, forKey: .uncertainty) ?? 0
        filter = try c.decodeIfPresent(String.self, forKey: .filter) ?? ""
        snr = try c.decodeIfPresent(Double.self, forKey: .snr) ?? 0
        qualityFlag = try c.decodeIfPresent(String.self, forKey: .qualityFlag) ?? ""
        if let bool = try? c.decode(Bool.self, forKey: .aavsoSubmitted) {
            aavsoSubmitted = bool
        } else {
            aavsoSubmitted = (try c.decodeIfPresent(Int.self, forKey: .aavsoSubmitted) ?? 0) == 1
        }
    }
}

struct Target: Decodable, Identifiable, Hashable {
    var id: String { targetId.isEmpty ? name : targetId }
    let targetId: String
    let name: String
    let targetType: String
    let scienceProgram: String
    let mag: Double?
    let magBand: String
    let priority: Double
    let bestScore: Double?
    let nMeasurements: Int
    let scoreExplanation: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case targetId = "target_id", name, targetType = "target_type", scienceProgram = "science_program"
        case mag, magBand = "mag_band", priority, bestScore = "best_score", nMeasurements = "n_measurements"
        case scoreExplanation = "score_explanation"
    }
}

struct TimelineItem: Decodable, Identifiable {
    var id: String { "\(nodeId)-\(target)-\(startTime)" }
    let nodeId: String
    let target: String
    let targetId: String
    let startTime: String
    let score: Double
    let ra: Double
    let dec: Double
    let expDur: Double
    let expCount: Int
    let filter: String
    let notes: String
    let explanation: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case node, target, targetId = "target_id", startTime, score, ra, dec, expDur, expCount, filter, notes, explanation
    }
    enum NodeKeys: String, CodingKey { case nodeId = "node_id" }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let node = try? c.nestedContainer(keyedBy: NodeKeys.self, forKey: .node)
        nodeId = (try? node?.decode(String.self, forKey: .nodeId)) ?? ""
        target = try c.decodeIfPresent(String.self, forKey: .target) ?? ""
        targetId = try c.decodeIfPresent(String.self, forKey: .targetId) ?? ""
        startTime = try c.decodeIfPresent(String.self, forKey: .startTime) ?? ""
        score = try c.decodeIfPresent(Double.self, forKey: .score) ?? 0
        ra = try c.decodeIfPresent(Double.self, forKey: .ra) ?? 0
        dec = try c.decodeIfPresent(Double.self, forKey: .dec) ?? 0
        expDur = try c.decodeIfPresent(Double.self, forKey: .expDur) ?? 0
        expCount = try c.decodeIfPresent(Int.self, forKey: .expCount) ?? 0
        filter = try c.decodeIfPresent(String.self, forKey: .filter) ?? ""
        notes = try c.decodeIfPresent(String.self, forKey: .notes) ?? ""
        explanation = try c.decodeIfPresent([String: JSONValue].self, forKey: .explanation) ?? [:]
    }

    var estimatedMinutes: Double { expDur * Double(expCount) / 60 }
    var reason: String { explanation["summary"]?.stringValue ?? "" }
}

struct NightSummary: Decodable, Identifiable {
    var id: String { "\(nodeId)-\(night)" }
    let nodeId: String
    let night: String
    let nTargets: Int
    let nObservations: Int
    let nSubmitted: Int
    let generatedAt: String
    let receipt: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case nodeId = "node_id", night, nTargets = "n_targets", nObservations = "n_observations"
        case nSubmitted = "n_submitted", generatedAt = "generated_at", receipt
    }

    var wasClear: Bool { nObservations > 0 }
    var receiptTitle: String { receipt["title"]?.stringValue ?? "" }
}

struct AppNotification: Decodable, Identifiable {
    let id: Int
    let type: String
    let payload: [String: JSONValue]
    let sentAt: String
    let readAt: String?

    enum CodingKeys: String, CodingKey {
        case id, type, payload, sentAt = "sent_at", readAt = "read_at"
    }

    var read: Bool { readAt != nil }
    var title: String {
        payload["title"]?.stringValue
            ?? payload["message"]?.stringValue
            ?? payload["target"]?.stringValue
            ?? type.replacingOccurrences(of: "_", with: " ")
    }
}

// MARK: - API

enum AuthStatus { case unknown, signedOut, signedIn }

struct ApiError: LocalizedError {
    let statusCode: Int
    let message: String
    var errorDescription: String? { message }
    var isUnauthorized: Bool { statusCode == 401 }
}

@MainActor
@Observable
final class AuthStore {
    private let tokenKey = "bs_auth_token"
    private let userIdKey = "bs_user_id"
    var token: String?
    var userId: String?

    var isLoggedIn: Bool { token?.isEmpty == false }

    func load() {
        token = read(tokenKey)
        userId = read(userIdKey)
    }

    func save(token: String, userId: String) {
        self.token = token
        self.userId = userId
        write(token, for: tokenKey)
        write(userId, for: userIdKey)
    }

    func clear() {
        token = nil
        userId = nil
        remove(tokenKey)
        remove(userIdKey)
    }

    private func read(_ key: String) -> String? {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: "net.thetelescope.node",
            kSecAttrAccount: key,
            kSecReturnData: true,
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private func write(_ value: String, for key: String) {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: "net.thetelescope.node",
            kSecAttrAccount: key,
        ]
        let attributes = [kSecValueData: Data(value.utf8)] as CFDictionary
        if SecItemUpdate(query as CFDictionary, attributes) == errSecItemNotFound {
            var insert = query
            insert[kSecValueData] = Data(value.utf8)
            SecItemAdd(insert as CFDictionary, nil)
        }
    }

    private func remove(_ key: String) {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: "net.thetelescope.node",
            kSecAttrAccount: key,
        ]
        SecItemDelete(query as CFDictionary)
    }
}

@MainActor
final class ApiClient {
    private let auth: AuthStore
    private let session: URLSession
    private let decoder = JSONDecoder()

    init(auth: AuthStore, session: URLSession = .shared) {
        self.auth = auth
        self.session = session
    }

    private var headers: [String: String] {
        var headers = ["Content-Type": "application/json"]
        if let token = auth.token, !token.isEmpty {
            headers["Authorization"] = "Bearer \(token)"
        }
        return headers
    }

    private func request(_ method: String, _ path: String, query: [String: CustomStringConvertible] = [:], body: [String: Any]? = nil) async throws -> Data {
        var request = URLRequest(url: AppConfig.url(path, query: query))
        request.httpMethod = method
        headers.forEach { request.setValue($0.value, forHTTPHeaderField: $0.key) }
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: request)
        let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(statusCode) else {
            let message = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["error"] as? String
            throw ApiError(statusCode: statusCode, message: message ?? "Request failed (\(statusCode)).")
        }
        return data
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        try decoder.decode(T.self, from: data.isEmpty ? Data("{}".utf8) : data)
    }

    private struct TokenResponse: Decodable { let token: String; let userId: String; enum CodingKeys: String, CodingKey { case token, userId = "user_id" } }
    private struct NodesResponse: Decodable { let nodes: [MemberNode] }
    private struct ObservationsResponse: Decodable { let observations: [ObservationRecord] }
    private struct TimelineResponse: Decodable { let items: [TimelineItem] }
    private struct NotificationsResponse: Decodable { let notifications: [AppNotification]; let unread: Int }
    private struct TelescopesResponse: Decodable { let telescopes: [TelescopeSpec] }
    private struct ActivationResponse: Decodable { let code: String }
    private struct NightsResponse: Decodable { let nights: [NightSummary] }
    private struct TargetsResponse: Decodable { let targets: [Target] }
    private struct LightCurveResponse: Decodable { let points: [LightCurvePoint] }

    func register(email: String, password: String, displayName: String) async throws {
        let data = try await request("POST", "/auth/register", body: ["email": email, "password": password, "display_name": displayName])
        let token = try decode(TokenResponse.self, from: data)
        await auth.save(token: token.token, userId: token.userId)
    }

    func login(email: String, password: String) async throws {
        let data = try await request("POST", "/auth/login", body: ["email": email, "password": password])
        let token = try decode(TokenResponse.self, from: data)
        await auth.save(token: token.token, userId: token.userId)
    }

    func me() async throws -> Member { try decode(Member.self, from: try await request("GET", "/me")) }
    func stats() async throws -> MemberStats { try decode(MemberStats.self, from: try await request("GET", "/me/stats")) }
    func nodes() async throws -> [MemberNode] { try decode(NodesResponse.self, from: try await request("GET", "/me/nodes")).nodes }
    func observations(days: Int = 90, limit: Int = 200) async throws -> [ObservationRecord] {
        try decode(ObservationsResponse.self, from: try await request("GET", "/me/observations", query: ["days": days, "limit": limit])).observations
    }
    func timeline() async throws -> [TimelineItem] { try decode(TimelineResponse.self, from: try await request("GET", "/me/timeline")).items }
    func notifications(limit: Int = 50) async throws -> ([AppNotification], Int) {
        let result = try decode(NotificationsResponse.self, from: try await request("GET", "/me/notifications", query: ["limit": limit]))
        return (result.notifications, result.unread)
    }
    func markNotificationRead(_ id: Int) async throws { _ = try await request("POST", "/me/notifications/\(id)/read", body: [:]) }
    func telescopes() async throws -> [TelescopeSpec] { try decode(TelescopesResponse.self, from: try await request("GET", "/telescopes")).telescopes }
    func claimNode(nodeId: String, apiKey: String) async throws { _ = try await request("POST", "/me/nodes/\(nodeId)", body: ["api_key": apiKey]) }
    func generateActivationCode(
        locationName: String,
        lat: Double?,
        lon: Double?,
        telescopeModel: String,
        telescopeSpecs: [String: Any] = [:],
        portable: Bool
    ) async throws -> String {
        var body: [String: Any] = ["portable": portable]
        if !locationName.isEmpty { body["location_name"] = locationName }
        if let lat, let lon { body["latitude"] = lat; body["longitude"] = lon }
        if !telescopeModel.isEmpty { body["telescope_model"] = telescopeModel }
        if !telescopeSpecs.isEmpty { body["telescope_specs"] = telescopeSpecs }
        return try decode(ActivationResponse.self, from: try await request("POST", "/me/activation-code", body: body)).code
    }
    func startNodeSession(_ nodeId: String, lat: Double, lon: Double, city: String, siteName: String) async throws {
        _ = try await request("POST", "/me/nodes/\(nodeId)/session", body: ["lat": lat, "lon": lon, "city": city, "site_name": siteName])
    }
    func endNodeSession(_ nodeId: String) async throws { _ = try await request("DELETE", "/me/nodes/\(nodeId)/session") }
    func setNodeVacation(_ nodeId: String, untilDate: String) async throws { _ = try await request("PUT", "/me/nodes/\(nodeId)/vacation", body: ["until_date": untilDate]) }
    func cancelNodeVacation(_ nodeId: String) async throws { _ = try await request("DELETE", "/me/nodes/\(nodeId)/vacation") }
    func disconnectNode(_ nodeId: String) async throws { _ = try await request("DELETE", "/me/nodes/\(nodeId)") }
    func skyQuality(lat: Double, lon: Double) async throws -> SkyQuality {
        try decode(SkyQuality.self, from: try await request("GET", "/sky-quality", query: ["lat": lat, "lon": lon]))
    }
    func pushActivationCode(pairingToken: String, activationCode: String) async throws {
        _ = try await request("POST", "/nodes/pair", body: ["pairing_token": pairingToken.trimmed.uppercased(), "activation_code": activationCode.trimmed.uppercased()])
    }
    func setNotificationPrefs(email: Bool? = nil, push: Bool? = nil, pushToken: String? = nil) async throws {
        var body: [String: Any] = [:]
        if let email { body["notification_email"] = email }
        if let push { body["notification_push"] = push }
        if let pushToken { body["push_token"] = pushToken }
        _ = try await request("PUT", "/me/notifications/prefs", body: body)
    }
    func deleteAccount() async throws { _ = try await request("DELETE", "/me", body: ["confirm": true]) }
    func nights(limit: Int = 30) async throws -> [NightSummary] { try decode(NightsResponse.self, from: try await request("GET", "/me/nights", query: ["limit": limit])).nights }
    func targets() async throws -> [Target] { try decode(TargetsResponse.self, from: try await request("GET", "/targets")).targets }
    func lightCurve(targetName: String, days: Int = 90) async throws -> [LightCurvePoint] {
        try decode(LightCurveResponse.self, from: try await request("GET", "/lightcurves/\(targetName.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? targetName)", query: ["days": days])).points
    }
}

// MARK: - App State

@MainActor
@Observable
final class MemberAppState {
    let auth = AuthStore()
    lazy var api = ApiClient(auth: auth)

    var status: AuthStatus = .unknown
    var member: Member?
    var lastError: String?
    var hasNode: Bool?
    var pendingTab: Int?
    var unreadNotifications = 0

    func bootstrap() async {
        guard status == .unknown else { return }
        auth.load()
        guard auth.isLoggedIn else {
            status = .signedOut
            return
        }
        do {
            member = try await api.me()
            status = .signedIn
            await refreshNodes()
            await refreshUnreadNotifications()
            await PushService.initialize(api: api) { [weak self] tab in self?.pendingTab = tab }
        } catch let error as ApiError where error.isUnauthorized {
            auth.clear()
            status = .signedOut
        } catch {
            status = .signedIn
        }
    }

    func login(email: String, password: String) async -> Bool {
        await authFlow { try await api.login(email: email, password: password) }
    }

    func register(email: String, password: String, name: String) async -> Bool {
        await authFlow { try await api.register(email: email, password: password, displayName: name) }
    }

    private func authFlow(_ action: () async throws -> Void) async -> Bool {
        lastError = nil
        do {
            try await action()
            member = try await api.me()
            status = .signedIn
            await refreshNodes()
            await refreshUnreadNotifications()
            await PushService.initialize(api: api) { [weak self] tab in self?.pendingTab = tab }
            return true
        } catch let error as ApiError {
            lastError = error.message
            return false
        } catch {
            lastError = "Could not reach the network. Check your connection and try again."
            return false
        }
    }

    func refreshNodes() async {
        do { hasNode = try await api.nodes().isEmpty == false } catch { hasNode = true }
    }

    func refreshUnreadNotifications() async {
        do { unreadNotifications = try await api.notifications(limit: 1).1 } catch {}
    }

    func signOut() {
        auth.clear()
        member = nil
        hasNode = nil
        unreadNotifications = 0
        status = .signedOut
    }

    func deleteAccount() async {
        try? await api.deleteAccount()
        signOut()
    }

    func handleAuthError(_ error: Error) {
        if (error as? ApiError)?.isUnauthorized == true { signOut() }
    }
}

// MARK: - Push Notifications

@MainActor
enum PushService {
    static func initialize(api: ApiClient, onNotificationTab: @escaping @MainActor (Int) -> Void) async {
        let center = UNUserNotificationCenter.current()
        let granted = (try? await center.requestAuthorization(options: [.alert, .badge, .sound])) ?? false
        try? await api.setNotificationPrefs(push: true)
        if granted {
            UIApplication.shared.registerForRemoteNotifications()
        }
        NotificationCenter.default.addObserver(forName: .nativePushTokenUpdated, object: nil, queue: .main) { note in
            guard let token = note.object as? String else { return }
            Task { try? await api.setNotificationPrefs(push: true, pushToken: token) }
        }
        NotificationCenter.default.addObserver(forName: .nativeNightSummaryTapped, object: nil, queue: .main) { _ in
            Task { @MainActor in onNotificationTab(3) }
        }
    }
}

extension Notification.Name {
    static let nativeNightSummaryTapped = Notification.Name("nativeNightSummaryTapped")
    static let nativePushTokenUpdated = Notification.Name("nativePushTokenUpdated")
    static let nativePushRegistrationFailed = Notification.Name("nativePushRegistrationFailed")
}

// MARK: - Location

struct ObservingLocation: Hashable {
    let latitude: Double
    let longitude: Double
    let city: String
    let siteName: String
}

struct SkyQuality: Decodable, Hashable {
    let mpsas: Double
    let bortle: Int
    let source: String

    enum CodingKeys: String, CodingKey {
        case mpsas, bortle, source
    }

    init(mpsas: Double = 0, bortle: Int = 0, source: String = "") {
        self.mpsas = mpsas
        self.bortle = bortle
        self.source = source
    }
}

struct LocationSearchResult: Identifiable, Decodable, Hashable {
    var id: String { "\(lat)-\(lon)-\(displayName)" }
    let lat: String
    let lon: String
    let displayName: String
    let address: Address?

    enum CodingKeys: String, CodingKey {
        case lat, lon, displayName = "display_name", address
    }

    struct Address: Decodable, Hashable {
        let city: String?
        let town: String?
        let village: String?
        let state: String?
        let country: String?
    }

    var latitude: Double? { Double(lat) }
    var longitude: Double? { Double(lon) }
    var cityName: String {
        address?.city ?? address?.town ?? address?.village ?? displayName
    }
}

@MainActor
final class LocationService: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var continuation: CheckedContinuation<CLLocation, Error>?

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    func currentObservingLocation() async throws -> ObservingLocation {
        let location = try await currentLocation()
        let city = await reverseCity(for: location)
        return ObservingLocation(
            latitude: location.coordinate.latitude,
            longitude: location.coordinate.longitude,
            city: city,
            siteName: ""
        )
    }

    private func currentLocation() async throws -> CLLocation {
        if manager.authorizationStatus == .notDetermined {
            manager.requestWhenInUseAuthorization()
        }
        return try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            manager.requestLocation()
        }
    }

    private func reverseCity(for location: CLLocation) async -> String {
        let geocoder = CLGeocoder()
        guard let placemark = try? await geocoder.reverseGeocodeLocation(location).first else {
            return ""
        }
        return [placemark.locality, placemark.administrativeArea, placemark.country]
            .compactMap { $0 }
            .filter { !$0.isEmpty }
            .joined(separator: ", ")
    }

    func search(_ query: String) async throws -> [LocationSearchResult] {
        var components = URLComponents(string: "https://nominatim.openstreetmap.org/search")!
        components.queryItems = [
            URLQueryItem(name: "q", value: query),
            URLQueryItem(name: "format", value: "json"),
            URLQueryItem(name: "limit", value: "5"),
            URLQueryItem(name: "addressdetails", value: "1"),
        ]
        var request = URLRequest(url: components.url!)
        request.setValue("TheTelescopeNetApp/1.0", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw ApiError(statusCode: (response as? HTTPURLResponse)?.statusCode ?? 0, message: "Location lookup failed. Try again or use the GPS button.")
        }
        return try JSONDecoder().decode([LocationSearchResult].self, from: data)
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last else { return }
        Task { @MainActor in
            continuation?.resume(returning: location)
            continuation = nil
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in
            continuation?.resume(throwing: error)
            continuation = nil
        }
    }
}

// MARK: - Login

struct LoginView: View {
    @Bindable var appState: MemberAppState
    @State private var isRegistering = false
    @State private var email = ""
    @State private var password = ""
    @State private var displayName = ""
    @State private var isSubmitting = false

    var body: some View {
        ZStack {
            AppTheme.night.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("The Telescope Net")
                            .font(.system(size: 40, weight: .bold, design: .rounded))
                        Text("Your autonomous observatory, tuned for accessible astronomy.")
                            .foregroundStyle(AppTheme.ink2)
                    }
                    .accessibilityElement(children: .combine)

                    panel {
                        Text(isRegistering ? "Create your member account" : "Welcome back")
                            .font(.title2.bold())
                        if isRegistering {
                            TextField("Display name", text: $displayName)
                                .textContentType(.name)
                                .nativeField()
                        }
                        TextField("Email", text: $email)
                            .textInputAutocapitalization(.never)
                            .keyboardType(.emailAddress)
                            .textContentType(.emailAddress)
                            .nativeField()
                        SecureField("Password", text: $password)
                            .textContentType(isRegistering ? .newPassword : .password)
                            .nativeField()

                        if let error = appState.lastError {
                            Text(error)
                                .foregroundStyle(AppTheme.danger)
                                .accessibilityLabel("Error: \(error)")
                        }

                        Button {
                            Task { await submit() }
                        } label: {
                            HStack {
                                if isSubmitting { ProgressView().tint(AppTheme.night) }
                                Text(isRegistering ? "Register" : "Sign in").fontWeight(.semibold)
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                        .disabled(isSubmitting || email.trimmed.isEmpty || password.isEmpty || (isRegistering && displayName.trimmed.isEmpty))

                        Button(isRegistering ? "Already have an account? Sign in" : "New here? Create an account") {
                            withAnimation { isRegistering.toggle() }
                        }
                    }
                }
                .padding(24)
            }
        }
    }

    private func submit() async {
        isSubmitting = true
        defer { isSubmitting = false }
        if isRegistering {
            _ = await appState.register(email: email, password: password, name: displayName)
        } else {
            _ = await appState.login(email: email, password: password)
        }
    }
}

// MARK: - Home

struct HomeShell: View {
    @Bindable var appState: MemberAppState
    @State private var selectedTab = 0
    @State private var showingMe = false

    var body: some View {
        Group {
            if appState.hasNode == nil {
                ProgressView()
                    .task { await appState.refreshNodes() }
            } else if appState.hasNode == false {
                SetupWallView(appState: appState)
            } else {
                ZStack {
                    OperationsBackdrop()
                    VStack(spacing: 0) {
                        OpsShellHeader(
                            title: selectedTitle,
                            subtitle: selectedSubtitle,
                            unread: appState.unreadNotifications,
                            onAccount: { showingMe = true }
                        )
                        currentScreen
                        OpsTabRail(
                            selectedTab: $selectedTab,
                            unread: appState.unreadNotifications
                        )
                    }
                }
            }
        }
        .onChange(of: appState.pendingTab) { _, value in
            if let value {
                selectedTab = value
                appState.pendingTab = nil
            }
        }
        .sheet(isPresented: $showingMe) {
            MeView(appState: appState)
        }
    }

    private var selectedTitle: String {
        switch selectedTab {
        case 1:
            return "Telescopes"
        case 2:
            return "Evidence"
        case 3:
            return "Alerts"
        default:
            return "Tonight Brief"
        }
    }

    private var selectedSubtitle: String {
        switch selectedTab {
        case 1:
            return "Node state, consequence, and next action"
        case 2:
            return "Measurements with provenance"
        case 3:
            return appState.unreadNotifications == 0 ? "No unread transmissions" : "\(appState.unreadNotifications) unread transmission\(appState.unreadNotifications == 1 ? "" : "s")"
        default:
            return "Readiness, plan, and science output"
        }
    }

    @ViewBuilder
    private var currentScreen: some View {
        switch selectedTab {
        case 1:
            NodesView(appState: appState, onAccount: { showingMe = true })
        case 2:
            ObservationsView(appState: appState, onAccount: { showingMe = true })
        case 3:
            NotificationsView(appState: appState, onAccount: { showingMe = true })
        default:
            DashboardView(appState: appState, selectedTab: $selectedTab, onAccount: { showingMe = true })
        }
    }
}

struct OperationsBackdrop: View {
    var body: some View {
        ZStack {
            AppTheme.night.ignoresSafeArea()
            LinearGradient(
                colors: [
                    AppTheme.accent.opacity(0.12),
                    .clear,
                    AppTheme.sky.opacity(0.08),
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .ignoresSafeArea()
            GridOverlay()
                .stroke(AppTheme.ink.opacity(0.045), lineWidth: 0.6)
                .ignoresSafeArea()
        }
    }
}

struct GridOverlay: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        let step: CGFloat = 24
        var x = rect.minX
        while x <= rect.maxX {
            path.move(to: CGPoint(x: x, y: rect.minY))
            path.addLine(to: CGPoint(x: x, y: rect.maxY))
            x += step
        }
        var y = rect.minY
        while y <= rect.maxY {
            path.move(to: CGPoint(x: rect.minX, y: y))
            path.addLine(to: CGPoint(x: rect.maxX, y: y))
            y += step
        }
        return path
    }
}

struct OpsShellHeader: View {
    let title: String
    let subtitle: String
    let unread: Int
    let onAccount: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("TTN OPERATIONS")
                    .opsLabel()
                Text(title)
                    .font(.title3.weight(.black))
                    .foregroundStyle(AppTheme.ink)
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(AppTheme.ink2)
                    .lineLimit(1)
            }
            Spacer()
            if unread > 0 {
                SignalPill(text: "\(unread) ALERT\(unread == 1 ? "" : "S")", color: AppTheme.danger)
            } else {
                SignalPill(text: "READY", color: AppTheme.accent)
            }
            Button(action: onAccount) {
                Image(systemName: "person.crop.circle")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(AppTheme.ink)
                    .frame(width: 38, height: 38)
                    .background(AppTheme.ink.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).stroke(AppTheme.ink.opacity(0.12)))
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 10)
        .padding(.bottom, 12)
        .background(AppTheme.surface.opacity(0.94))
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(AppTheme.ink.opacity(0.12))
                .frame(height: 0.7)
        }
    }
}

struct OpsTabRail: View {
    @Binding var selectedTab: Int
    let unread: Int

    private let tabs: [(Int, String, String, Color)] = [
        (0, "Tonight", "sparkles", AppTheme.accent),
        (1, "Scopes", "dot.radiowaves.left.and.right", AppTheme.sky),
        (2, "Data", "waveform.path.ecg", AppTheme.warm),
        (3, "Alerts", "bell", AppTheme.danger),
    ]

    var body: some View {
        HStack(spacing: 8) {
            ForEach(tabs, id: \.0) { tab in
                Button { selectedTab = tab.0 } label: {
                    VStack(spacing: 5) {
                        ZStack(alignment: .topTrailing) {
                            Image(systemName: tab.2)
                                .font(.headline.weight(.bold))
                            if tab.0 == 3 && unread > 0 {
                                Circle()
                                    .fill(AppTheme.danger)
                                    .frame(width: 8, height: 8)
                                    .offset(x: 7, y: -5)
                            }
                        }
                        Text(tab.1)
                            .font(.caption2.weight(.black))
                    }
                    .foregroundStyle(selectedTab == tab.0 ? tab.3 : AppTheme.ink3)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .background(
                        (selectedTab == tab.0 ? tab.3.opacity(0.13) : AppTheme.ink.opacity(0.03)),
                        in: RoundedRectangle(cornerRadius: 11)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 11)
                            .stroke(selectedTab == tab.0 ? tab.3.opacity(0.38) : AppTheme.ink.opacity(0.08))
                    )
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 10)
        .padding(.bottom, 10)
        .background(AppTheme.surface.opacity(0.96))
        .overlay(alignment: .top) {
            Rectangle()
                .fill(AppTheme.ink.opacity(0.12))
                .frame(height: 0.7)
        }
    }
}

// MARK: - Dashboard

struct DashboardView: View {
    @Bindable var appState: MemberAppState
    @Binding var selectedTab: Int
    let onAccount: () -> Void
    @State private var stats = MemberStats.empty
    @State private var timeline: [TimelineItem] = []
    @State private var targets: [Target] = []
    @State private var notifications: [AppNotification] = []
    @State private var isLoading = true
    @State private var error: String?
    @State private var selectedTarget: Target?
    @State private var showingTargetsList = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    hero
                    readinessPanel
                    timelinePanel
                    evidencePanel
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
            }
            .background(AppTheme.night)
            .navigationTitle("Tonight Brief")
            .toolbar(.hidden, for: .navigationBar)
            .toolbar {
                refreshButton { await load() }
                ToolbarItem(placement: .topBarLeading) {
                    Button(action: onAccount) {
                        Label("Account", systemImage: "person.crop.circle")
                    }
                }
            }
            .refreshable { await load() }
            .task { await load() }
            .sheet(item: $selectedTarget) { target in
                TargetDetailView(api: appState.api, targetName: target.name)
            }
            .sheet(isPresented: $showingTargetsList) {
                TargetsListView(targets: targets) { target in
                    showingTargetsList = false
                    selectedTarget = target
                }
            }
        }
    }

    private var hero: some View {
        MissionControlHero(
            headline: headline,
            summary: summary,
            online: onlineNodes,
            totalNodes: stats.nodeCount,
            observations: stats.totalObservations,
            submitted: stats.aavsoSubmitted,
            alerts: unreadCount,
            targets: Array(targets.sorted { $0.priority > $1.priority }.prefix(3)),
            accent: readinessColor
        )
    }

    private var readinessPanel: some View {
        OpsPanel(accent: actionCount == 0 ? AppTheme.accent : AppTheme.warm) {
            VStack(alignment: .leading, spacing: 12) {
                OpsHeader(kicker: "READINESS", title: "What needs attention", systemImage: "checklist")
                IntentRow(
                    systemImage: actionCount == 0 ? "checkmark.seal.fill" : "exclamationmark.triangle.fill",
                    color: actionCount == 0 ? AppTheme.accent : AppTheme.warm,
                    title: actionCount == 0 ? "No blocking actions" : "\(actionCount) thing\(actionCount == 1 ? "" : "s") to check",
                    subtitle: actionCount == 0 ? "Your network can accept tonight's assignments." : "Open Telescopes or Alerts before the next observing window."
                )
                Button { selectedTab = unreadCount > 0 ? 3 : 1 } label: {
                    HStack {
                        Text(unreadCount > 0 ? "Review alerts" : "Review telescopes")
                            .font(.subheadline.weight(.bold))
                        Spacer()
                        Image(systemName: "arrow.right")
                    }
                    .padding(12)
                    .foregroundStyle(AppTheme.night)
                    .background(AppTheme.ink, in: RoundedRectangle(cornerRadius: 10))
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var timelinePanel: some View {
        OpsPanel(accent: AppTheme.sky) {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    OpsHeader(kicker: "PLAN", title: "What the network will try", systemImage: "point.topleft.down.curvedto.point.bottomright.up")
                    Spacer()
                    Button("Targets") { showingTargetsList = true }
                        .font(.caption.weight(.bold))
                }
                if timeline.isEmpty {
                    ForEach(targets.prefix(4)) { target in
                        Button { selectedTarget = target } label: {
                            IntentRow(
                                systemImage: "scope",
                                color: target.priority > 0.7 ? AppTheme.warm : AppTheme.sky,
                                title: target.name,
                                subtitle: "\(target.scienceProgram.ifEmpty(target.targetType)) · priority \(Int(target.priority * 100)) · \(target.nMeasurements) measurements"
                            )
                        }
                        .buttonStyle(.plain)
                    }
                    if targets.isEmpty { EmptyLine("No planned observations yet.") }
                } else {
                    ForEach(timeline.prefix(5)) { item in
                        Button {
                            if let target = targets.first(where: { $0.name == item.target }) { selectedTarget = target }
                        } label: {
                            IntentRow(
                                systemImage: "clock",
                                color: AppTheme.sky,
                                title: item.target.ifEmpty("Scheduled target"),
                                subtitle: "\(compactDate(item.startTime)) · \(item.expCount)x \(Int(item.expDur))s\(item.filter.isEmpty ? "" : " · \(item.filter)")\(item.reason.isEmpty ? "" : " · \(item.reason)")"
                            )
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }

    private var evidencePanel: some View {
        OpsPanel(accent: AppTheme.accent) {
            VStack(alignment: .leading, spacing: 12) {
                OpsHeader(kicker: "EVIDENCE", title: "What changed in the sky", systemImage: "waveform.path.ecg")
                HStack(spacing: 10) {
                    OpsMetric(title: "Targets", value: "\(stats.targetsObserved)", color: AppTheme.warm)
                    OpsMetric(title: "Clear nights", value: "\(stats.clearNights)", color: AppTheme.sky)
                }
                if notifications.isEmpty {
                    EmptyLine("No recent alerts or receipts.")
                } else {
                    ForEach(notifications.prefix(3)) { notification in
                        IntentRow(
                            systemImage: notification.read ? "envelope.open" : "dot.radiowaves.left.and.right",
                            color: notification.read ? AppTheme.ink3 : AppTheme.accent,
                            title: notification.title,
                            subtitle: "\(compactDate(notification.sentAt)) · \(notification.read ? "read" : "new")"
                        )
                    }
                    Button("Open all alerts") { selectedTab = 3 }
                        .font(.caption.weight(.bold))
                }
            }
        }
    }

    private var onlineNodes: Int { appState.hasNode == true ? max(stats.nodeCount, 1) : 0 }
    private var unreadCount: Int { notifications.filter { !$0.read }.count }
    private var actionCount: Int { (appState.hasNode == true ? 0 : 1) + unreadCount }
    private var readinessColor: Color { actionCount == 0 ? AppTheme.accent : AppTheme.warm }
    private var headline: String {
        if appState.hasNode != true { return "Connect a telescope to start observing." }
        if actionCount > 0 { return "Tonight needs attention before it runs." }
        if stats.totalObservations > 0 { return "Your network is producing useful measurements." }
        return "Your telescopes are ready for tonight."
    }
    private var summary: String {
        if let target = targets.sorted(by: { $0.priority > $1.priority }).first {
            return "\(target.name) is the highest-priority target in the current queue."
        }
        return "The app now focuses on readiness, intent, and evidence instead of generic status cards."
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            async let loadedStats = appState.api.stats()
            async let loadedTimeline = appState.api.timeline()
            async let loadedTargets = appState.api.targets()
            async let loadedNotifications = appState.api.notifications(limit: 5)
            stats = try await loadedStats
            timeline = try await loadedTimeline
            targets = try await loadedTargets
            let result = try await loadedNotifications
            notifications = result.0
            appState.unreadNotifications = result.1
            error = nil
        } catch {
            appState.handleAuthError(error)
            self.error = error.localizedDescription
        }
    }
}

struct TargetsListView: View {
    let targets: [Target]
    let onSelect: (Target) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var selectedProgram = ""

    private let programs = [
        ("All", ""),
        ("Variable Stars", "variable_stars"),
        ("Exoplanets", "exoplanet_transits"),
        ("Transients", "transient_follow_up"),
    ]

    private var filteredTargets: [Target] {
        let sorted = targets.sorted { $0.priority > $1.priority }
        guard !selectedProgram.isEmpty else { return sorted }
        return sorted.filter { $0.scienceProgram == selectedProgram }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(programs, id: \.1) { label, program in
                            Button(label) { selectedProgram = program }
                                .buttonStyle(.bordered)
                                .tint(selectedProgram == program ? programColor(program) : AppTheme.ink2)
                        }
                    }
                    .padding(.horizontal)
                    .padding(.vertical, 8)
                }

                List(filteredTargets) { target in
                    Button {
                        onSelect(target)
                    } label: {
                        row(
                            title: target.name,
                            subtitle: "\(target.scienceProgram.ifEmpty(target.targetType)) • priority \(String(format: "%.1f", target.priority)) • \(target.nMeasurements) measurements",
                            accessory: target.bestScore.map { String(format: "%.0f", $0) } ?? ""
                        )
                    }
                    .buttonStyle(.plain)
                }
                .scrollContentBackground(.hidden)
            }
            .background(AppTheme.night)
            .navigationTitle("Network Targets")
            .toolbar { Button("Done") { dismiss() } }
        }
    }

    private func programColor(_ program: String) -> Color {
        switch program {
        case "exoplanet_transits": AppTheme.accent
        case "transient_follow_up": AppTheme.danger
        case "variable_stars": AppTheme.warm
        default: AppTheme.ink2
        }
    }
}

struct SetupWallView: View {
    @Bindable var appState: MemberAppState
    @State private var downloaded = false
    @State private var showingClaim = false

    private var downloadURL: URL {
        URL(string: "\(AppConfig.apiBase)/download/node-agent")!
    }

    private var steps: [(String, String, String)] {
        if downloaded {
            return [
                ("1", "Run the installer", "Open the downloaded .pkg and follow the steps."),
                ("2", "Node software starts", "The installer starts the node software automatically when done."),
                ("3", "Come back here and tap Connect", "Paste the activation code into the Node Agent dashboard."),
            ]
        }
        return [
            ("1", "Download & install", "Run the installer on the Mac connected to your telescope."),
            ("2", "Node software starts", "The installer starts the node software automatically."),
            ("3", "Connect your telescope", "Come back here and tap Connect telescope to link your account."),
        ]
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 24) {
                    Image(systemName: "dot.radiowaves.left.and.right")
                        .font(.system(size: 44, weight: .semibold))
                        .foregroundStyle(AppTheme.accent)
                        .frame(width: 76, height: 76)
                        .background(AppTheme.surface, in: Circle())

                    VStack(spacing: 10) {
                        Text("Connect your telescope")
                            .font(.title.bold())
                        Text("Install the Node Software on your telescope's Mac. It runs silently; this app is where you see everything.")
                            .foregroundStyle(AppTheme.ink2)
                            .multilineTextAlignment(.center)
                    }

                    panel {
                        ForEach(steps, id: \.0) { number, title, detail in
                            HStack(alignment: .top, spacing: 14) {
                                Text(number)
                                    .font(.headline)
                                    .foregroundStyle(AppTheme.accent)
                                    .frame(width: 36, height: 36)
                                    .background(AppTheme.accent.opacity(0.12), in: Circle())
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(title).font(.headline)
                                    Text(detail).font(.subheadline).foregroundStyle(AppTheme.ink2)
                                }
                            }
                        }
                    }

                    if downloaded {
                        Button("Connect telescope") { showingClaim = true }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.large)
                        Button("Download again") { openDownload() }
                    } else {
                        Button("Download Node Software") { openDownload() }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.large)
                        Button("Already installed - connect telescope") { showingClaim = true }
                    }

                    Button("Already connected? Tap to refresh") {
                        Task { await appState.refreshNodes() }
                    }
                    .font(.caption)
                    .foregroundStyle(AppTheme.ink3)
                }
                .padding(24)
            }
            .background(AppTheme.night)
            .toolbar {
                Button("Sign out") { appState.signOut() }
            }
            .sheet(isPresented: $showingClaim) {
                ClaimNodeView(api: appState.api) { await appState.refreshNodes() }
            }
        }
    }

    private func openDownload() {
        downloaded = true
        UIApplication.shared.open(downloadURL)
    }
}

// MARK: - Nodes

struct NodesView: View {
    @Bindable var appState: MemberAppState
    let onAccount: () -> Void
    @State private var nodes: [MemberNode] = []
    @State private var isLoading = true
    @State private var showingClaim = false
    @State private var managingNode: MemberNode?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    OpsPanel(accent: nodesReady ? AppTheme.accent : AppTheme.warm) {
                        VStack(alignment: .leading, spacing: 10) {
                            HStack(alignment: .center) {
                                OpsHeader(kicker: "TELESCOPES", title: nodesReady ? "Network ready" : "Connect or review nodes", systemImage: "dot.radiowaves.left.and.right")
                                Spacer()
                                Button { showingClaim = true } label: {
                                    Label("Connect", systemImage: "plus")
                                        .font(.caption.weight(.black))
                                        .padding(.horizontal, 10)
                                        .padding(.vertical, 8)
                                        .foregroundStyle(AppTheme.night)
                                        .background(AppTheme.ink, in: RoundedRectangle(cornerRadius: 8))
                                }
                                .buttonStyle(.plain)
                            }
                            Text(nodesReady ? "\(nodes.filter { $0.online }.count) of \(nodes.count) telescopes are live." : "Every node card explains its state, consequence, and next useful action.")
                                .font(.subheadline)
                                .foregroundStyle(AppTheme.ink2)
                        }
                    }
                    if nodes.isEmpty && !isLoading {
                        EmptyLine("No telescopes connected yet.")
                            .padding(.top, 12)
                    }
                    ForEach(nodes) { node in
                        Button { managingNode = node } label: { NodeCard(node: node) }
                            .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
            }
            .background(AppTheme.night)
            .navigationTitle("Telescopes")
            .toolbar(.hidden, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(action: onAccount) {
                        Label("Account", systemImage: "person.crop.circle")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showingClaim = true } label: { Label("Connect", systemImage: "plus") }
                }
            }
            .refreshable { await load() }
            .task { await load() }
            .sheet(isPresented: $showingClaim) {
                ClaimNodeView(api: appState.api) { await load(); await appState.refreshNodes() }
            }
            .sheet(item: $managingNode) { node in
                ManageNodeView(api: appState.api, node: node) { await load(); await appState.refreshNodes() }
            }
        }
    }

    private var nodesReady: Bool {
        !nodes.isEmpty && nodes.allSatisfy { $0.online && !$0.isOnVacation }
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            nodes = try await appState.api.nodes()
            appState.hasNode = !nodes.isEmpty
        } catch {
            appState.handleAuthError(error)
        }
    }
}

struct NodeCard: View {
    let node: MemberNode

    var body: some View {
        OpsPanel(accent: statusColor) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "dot.radiowaves.left.and.right")
                        .font(.title3.weight(.bold))
                        .foregroundStyle(statusColor)
                        .frame(width: 42, height: 42)
                        .background(statusColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                    VStack(alignment: .leading, spacing: 4) {
                        Text(node.telescopeModel.ifEmpty("Telescope node"))
                            .font(.headline.weight(.bold))
                            .foregroundStyle(AppTheme.ink)
                        Text(node.location)
                            .font(.subheadline)
                            .foregroundStyle(AppTheme.ink2)
                    }
                    Spacer()
                    SignalPill(text: statusText, color: statusColor)
                }
                IntentRow(systemImage: actionIcon, color: statusColor, title: actionTitle, subtitle: actionDetail)
                HStack(spacing: 8) {
                    MiniDatum(label: "heartbeat", value: compactDate(node.lastHeartbeat).ifEmpty("unknown"))
                    MiniDatum(label: "node", value: node.nodeId)
                    if node.portable {
                        MiniDatum(label: "mode", value: node.sessionCity.isEmpty ? "portable" : node.sessionCity)
                    }
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var statusColor: Color {
        if !node.online { return AppTheme.danger }
        if node.isOnVacation { return AppTheme.warm }
        if node.isSleeping { return AppTheme.sky }
        return AppTheme.accent
    }

    private var statusText: String { node.status.ifEmpty(node.online ? "online" : "offline").uppercased() }
    private var actionIcon: String {
        if !node.online { return "powerplug" }
        if node.portable && node.isSleeping { return "location.viewfinder" }
        if node.isOnVacation { return "pause.circle" }
        return "checkmark.seal"
    }
    private var actionTitle: String {
        if !node.online { return "Node needs attention" }
        if node.portable && node.isSleeping { return "Set tonight's observing site" }
        if node.isOnVacation { return "Vacation mode is active" }
        return "Accepting assignments"
    }
    private var actionDetail: String {
        if !node.online { return "Check power, network, or the node software before dark." }
        if node.portable && node.isSleeping { return "Portable nodes need a current location before they can run." }
        if node.isOnVacation { return node.vacationUntil.isEmpty ? "This telescope is paused." : "Paused until \(node.vacationUntil)." }
        return "This telescope can be scheduled for useful observations."
    }
}

struct ManageNodeView: View {
    let api: ApiClient
    let node: MemberNode
    let onChanged: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var nights: [NightSummary] = []
    @State private var showingVacation = false
    @State private var showingStart = false
    @State private var confirmingDisconnect = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            List {
                Section("Node") {
                    NodeCard(node: node)
                    Button("Start tonight's observing session") { showingStart = true }
                        .disabled(!node.portable)
                    Button("End current session") { Task { await perform { try await api.endNodeSession(node.nodeId) } } }
                        .disabled(!node.portable || node.sessionCity.isEmpty)
                    Button(node.isOnVacation ? "Cancel vacation" : "Set vacation") {
                        if node.isOnVacation { Task { await perform { try await api.cancelNodeVacation(node.nodeId) } } }
                        else { showingVacation = true }
                    }
                    Button("Disconnect node", role: .destructive) { confirmingDisconnect = true }
                }
                Section("Recent nights") {
                    ForEach(nights.filter { $0.nodeId == node.nodeId }.prefix(8)) { night in
                        row(title: night.night, subtitle: "\(night.nTargets) targets • \(night.nObservations) observations", accessory: "\(night.nSubmitted) sent")
                    }
                }
                if let error {
                    Section { Text(error).foregroundStyle(AppTheme.danger) }
                }
            }
            .navigationTitle("Manage node")
            .toolbar { Button("Done") { dismiss() } }
            .task { nights = (try? await api.nights()) ?? [] }
            .sheet(isPresented: $showingVacation) {
                VacationView(api: api, node: node) { await onChanged(); dismiss() }
            }
            .sheet(isPresented: $showingStart) {
                StartSessionView(api: api, node: node) { await onChanged(); dismiss() }
            }
            .alert("Disconnect telescope?", isPresented: $confirmingDisconnect) {
                Button("Disconnect", role: .destructive) {
                    Task { await perform { try await api.disconnectNode(node.nodeId) } }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("This removes \(node.telescopeModel.isEmpty ? "this telescope" : node.telescopeModel) from your account. The node software will keep running but you won't see it here anymore.")
            }
        }
    }

    private func perform(_ action: () async throws -> Void) async {
        do {
            try await action()
            await onChanged()
            dismiss()
        } catch { self.error = error.localizedDescription }
    }
}

struct ClaimNodeView: View {
    let api: ApiClient
    let onChanged: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var nodeId = ""
    @State private var apiKey = ""
    @State private var pairingToken = ""
    @State private var locationName = ""
    @State private var latitude = ""
    @State private var longitude = ""
    @State private var telescopeModel = ""
    @State private var customTelescopeModel = ""
    @State private var portable = false
    @State private var activationCode = ""
    @State private var telescopes: [TelescopeSpec] = []
    @State private var locationResults: [LocationSearchResult] = []
    @State private var skyQuality: SkyQuality?
    @State private var codeCopied = false
    @State private var checkingConnection = false
    @State private var connected = false
    @State private var locationService = LocationService()
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Claim an existing node") {
                    TextField("Node ID", text: $nodeId)
                        .textInputAutocapitalization(.characters)
                    SecureField("API key", text: $apiKey)
                    Button("Claim node") { Task { await claim() } }
                        .disabled(nodeId.trimmed.isEmpty || apiKey.trimmed.isEmpty)
                }
                if connected {
                    Section {
                        Label("Telescope connected!", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(AppTheme.accent)
                        Button("Done") { dismiss() }
                    }
                } else {
                    Section("Generate activation code") {
                        Picker("Telescope", selection: $telescopeModel) {
                            Text("Choose model").tag("")
                            ForEach(telescopes) { spec in Text(spec.displayName).tag(spec.displayName) }
                        }
                        TextField("Custom telescope model", text: $customTelescopeModel)
                        Button("Use custom telescope") {
                            telescopeModel = ""
                        }
                        .disabled(customTelescopeModel.trimmed.isEmpty)
                        Toggle("Portable node", isOn: $portable)
                        TextField("Location name", text: $locationName)
                            .onSubmit { Task { await lookupLocation() } }
                        HStack {
                            TextField("Latitude", text: $latitude)
                                .keyboardType(.numbersAndPunctuation)
                            TextField("Longitude", text: $longitude)
                                .keyboardType(.numbersAndPunctuation)
                        }
                        Button("Use current location") { Task { await detectLocation() } }
                        Button("Look up location") { Task { await lookupLocation() } }
                            .disabled(locationName.trimmed.isEmpty)
                        if !locationResults.isEmpty {
                            ForEach(locationResults) { result in
                                Button(result.displayName) { selectLocation(result) }
                            }
                        }
                        if let skyQuality {
                            SkyQualityView(sky: skyQuality)
                        }
                        Button("Generate code") { Task { await generate() } }
                    }
                    if !activationCode.isEmpty {
                        Section("Your activation code") {
                            Text("Open http://localhost:5173 on the computer running the Node Agent, then paste this code.")
                                .font(.caption)
                                .foregroundStyle(AppTheme.ink2)
                            HStack {
                                Text(activationCode)
                                    .font(.system(size: 28, weight: .bold, design: .monospaced))
                                    .accessibilityLabel("Activation code \(activationCode)")
                                Spacer()
                                Button(codeCopied ? "Copied" : "Copy") {
                                    UIPasteboard.general.string = activationCode
                                    codeCopied = true
                                }
                            }
                            Button(checkingConnection ? "Checking..." : "I've pasted it - check connection") {
                                Task { await checkConnected() }
                            }
                            .disabled(checkingConnection)
                            Button("Start over") { resetActivationFlow() }
                        }
                    }
                    Section("Push code to node") {
                        TextField("Pairing token", text: $pairingToken)
                            .textInputAutocapitalization(.characters)
                        Button("Send activation code") { Task { await pushCode() } }
                            .disabled(pairingToken.trimmed.isEmpty || activationCode.isEmpty)
                    }
                }
                if let error { Section { Text(error).foregroundStyle(AppTheme.danger) } }
            }
            .navigationTitle("Connect node")
            .toolbar { Button("Done") { dismiss() } }
            .task { telescopes = (try? await api.telescopes()) ?? [] }
        }
    }

    private func claim() async {
        do {
            try await api.claimNode(nodeId: nodeId.trimmed, apiKey: apiKey.trimmed)
            await onChanged()
            dismiss()
        } catch { error = error.localizedDescription }
    }

    private func generate() async {
        do {
            let selectedSpec = telescopes.first { $0.displayName == telescopeModel }
            let model = selectedSpec?.displayName ?? customTelescopeModel.trimmed
            activationCode = try await api.generateActivationCode(
                locationName: locationName,
                lat: Double(latitude),
                lon: Double(longitude),
                telescopeModel: model,
                telescopeSpecs: selectedSpec?.specPayload ?? [:],
                portable: portable
            )
            codeCopied = false
            error = nil
        } catch { error = error.localizedDescription }
    }

    private func detectLocation() async {
        do {
            let loc = try await locationService.currentObservingLocation()
            latitude = String(format: "%.6f", loc.latitude)
            longitude = String(format: "%.6f", loc.longitude)
            if locationName.trimmed.isEmpty {
                locationName = loc.city
            }
            await fetchSky()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func lookupLocation() async {
        do {
            let results = try await locationService.search(locationName.trimmed)
            guard !results.isEmpty else {
                error = "No location found for \"\(locationName)\". Try a more specific name."
                return
            }
            locationResults = results
            if results.count == 1 {
                selectLocation(results[0])
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func selectLocation(_ result: LocationSearchResult) {
        guard let lat = result.latitude, let lon = result.longitude else { return }
        latitude = String(format: "%.6f", lat)
        longitude = String(format: "%.6f", lon)
        locationName = result.cityName
        locationResults = []
        Task { await fetchSky() }
    }

    private func fetchSky() async {
        guard let lat = Double(latitude), let lon = Double(longitude) else { return }
        skyQuality = try? await api.skyQuality(lat: lat, lon: lon)
    }

    private func pushCode() async {
        do {
            try await api.pushActivationCode(pairingToken: pairingToken, activationCode: activationCode)
            await onChanged()
            dismiss()
        } catch { error = error.localizedDescription }
    }

    private func checkConnected() async {
        checkingConnection = true
        defer { checkingConnection = false }
        do {
            connected = try await !api.nodes().isEmpty
            if connected {
                await onChanged()
            } else {
                error = "Not linked yet. Enter the code in the node software, wait a moment, then try again."
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func resetActivationFlow() {
        activationCode = ""
        pairingToken = ""
        codeCopied = false
        connected = false
        error = nil
    }
}

struct VacationView: View {
    let api: ApiClient
    let node: MemberNode
    let onChanged: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var untilDate = Calendar.current.date(byAdding: .day, value: 7, to: Date()) ?? Date()
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                DatePicker("Vacation until", selection: $untilDate, displayedComponents: .date)
                Button("Set vacation") { Task { await submit() } }
                if let error { Text(error).foregroundStyle(AppTheme.danger) }
            }
            .navigationTitle("Vacation")
            .toolbar { Button("Cancel") { dismiss() } }
        }
    }

    private func submit() async {
        do {
            let text = ISO8601DateFormatter.dateOnly.string(from: untilDate)
            try await api.setNodeVacation(node.nodeId, untilDate: text)
            await onChanged()
            dismiss()
        } catch { error = error.localizedDescription }
    }
}

struct StartSessionView: View {
    let api: ApiClient
    let node: MemberNode
    let onChanged: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var city = ""
    @State private var siteName = ""
    @State private var lat = ""
    @State private var lon = ""
    @State private var locationResults: [LocationSearchResult] = []
    @State private var skyQuality: SkyQuality?
    @State private var locationService = LocationService()
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                if !node.previousLocations.isEmpty {
                    Section("Previous locations") {
                        ForEach(node.previousLocations) { loc in
                            Button(loc.label) {
                                city = loc.city
                                siteName = loc.siteName
                                lat = "\(loc.lat)"
                                lon = "\(loc.lon)"
                            }
                        }
                    }
                }
                Section("Tonight's site") {
                    TextField("City", text: $city)
                        .onSubmit { Task { await lookupLocation() } }
                    TextField("Site name", text: $siteName)
                    TextField("Latitude", text: $lat).keyboardType(.numbersAndPunctuation)
                    TextField("Longitude", text: $lon).keyboardType(.numbersAndPunctuation)
                    Button("Use current location") { Task { await detectLocation() } }
                    Button("Look up location") { Task { await lookupLocation() } }
                        .disabled(city.trimmed.isEmpty)
                    if !locationResults.isEmpty {
                        ForEach(locationResults) { result in
                            Button(result.displayName) { selectLocation(result) }
                        }
                    }
                    if let skyQuality {
                        SkyQualityView(sky: skyQuality)
                    }
                    Button("Start session") { Task { await submit() } }
                        .disabled(Double(lat) == nil || Double(lon) == nil || city.trimmed.isEmpty)
                }
                if let error { Text(error).foregroundStyle(AppTheme.danger) }
            }
            .navigationTitle("Start tonight")
            .toolbar { Button("Cancel") { dismiss() } }
        }
    }

    private func submit() async {
        guard let lat = Double(lat), let lon = Double(lon) else { return }
        do {
            try await api.startNodeSession(node.nodeId, lat: lat, lon: lon, city: city, siteName: siteName)
            await onChanged()
            dismiss()
        } catch { error = error.localizedDescription }
    }

    private func detectLocation() async {
        do {
            let loc = try await locationService.currentObservingLocation()
            lat = String(format: "%.6f", loc.latitude)
            lon = String(format: "%.6f", loc.longitude)
            if city.trimmed.isEmpty {
                city = loc.city
            }
            await fetchSky()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func lookupLocation() async {
        do {
            let results = try await locationService.search(city.trimmed)
            guard !results.isEmpty else {
                error = "No location found for \"\(city)\". Try a more specific name."
                return
            }
            locationResults = results
            if results.count == 1 {
                selectLocation(results[0])
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func selectLocation(_ result: LocationSearchResult) {
        guard let resultLat = result.latitude, let resultLon = result.longitude else { return }
        lat = String(format: "%.6f", resultLat)
        lon = String(format: "%.6f", resultLon)
        city = result.cityName
        locationResults = []
        Task { await fetchSky() }
    }

    private func fetchSky() async {
        guard let lat = Double(lat), let lon = Double(lon) else { return }
        skyQuality = try? await api.skyQuality(lat: lat, lon: lon)
    }
}

// MARK: - Observations

struct ObservationsView: View {
    @Bindable var appState: MemberAppState
    let onAccount: () -> Void
    @State private var observations: [ObservationRecord] = []
    @State private var nights: [NightSummary] = []
    @State private var selectedTarget: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    OpsPanel(accent: AppTheme.accent) {
                        VStack(alignment: .leading, spacing: 10) {
                            OpsHeader(kicker: "EVIDENCE", title: "Measurements with provenance", systemImage: "waveform.path.ecg")
                            Text("Each record shows target, magnitude, uncertainty, node, quality, and submission state.")
                                .font(.subheadline)
                                .foregroundStyle(AppTheme.ink2)
                        }
                    }
                    if !nights.isEmpty {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 10) {
                                ForEach(nights.prefix(12)) { night in
                                    VStack(alignment: .leading, spacing: 6) {
                                        Text(night.night)
                                            .font(.caption.weight(.bold))
                                            .foregroundStyle(night.wasClear ? AppTheme.ink : AppTheme.ink3)
                                        Text("\(night.nObservations) obs")
                                            .font(.caption2.weight(.bold))
                                            .foregroundStyle(night.wasClear ? AppTheme.accent : AppTheme.ink3)
                                    }
                                    .frame(width: 86, alignment: .leading)
                                    .padding(11)
                                    .background(AppTheme.surface2, in: RoundedRectangle(cornerRadius: 10))
                                    .overlay(RoundedRectangle(cornerRadius: 10).stroke((night.wasClear ? AppTheme.accent : AppTheme.ink).opacity(0.16)))
                                }
                            }
                        }
                    }
                    if observations.isEmpty {
                        EmptyLine("No observations yet.")
                            .padding(.top, 12)
                    }
                    ForEach(observations) { observation in
                        Button { selectedTarget = observation.targetName } label: {
                            ObservationEvidenceCard(observation: observation)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
            }
            .background(AppTheme.night)
            .navigationTitle("Data")
            .toolbar(.hidden, for: .navigationBar)
            .toolbar {
                Button(action: onAccount) {
                    Label("Account", systemImage: "person.crop.circle")
                }
            }
            .refreshable { await load() }
            .task { await load() }
            .sheet(
                isPresented: Binding(
                    get: { selectedTarget != nil },
                    set: { if !$0 { selectedTarget = nil } }
                )
            ) {
                if let selectedTarget {
                    TargetDetailView(api: appState.api, targetName: selectedTarget)
                }
            }
        }
    }

    private func load() async {
        do {
            async let obs = appState.api.observations()
            async let nightData = appState.api.nights()
            observations = try await obs
            nights = try await nightData
        } catch { appState.handleAuthError(error) }
    }
}

struct ObservationEvidenceCard: View {
    let observation: ObservationRecord

    var body: some View {
        OpsPanel(accent: magColor) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "sparkle.magnifyingglass")
                        .font(.title3.weight(.bold))
                        .foregroundStyle(magColor)
                        .frame(width: 42, height: 42)
                        .background(magColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                    VStack(alignment: .leading, spacing: 4) {
                        Text(observation.targetName.ifEmpty("Unknown target"))
                            .font(.headline.weight(.bold))
                            .foregroundStyle(AppTheme.ink)
                        Text("\(String(format: "%.3f", observation.magnitude)) mag")
                            .font(.system(size: 28, weight: .black, design: .rounded))
                            .foregroundStyle(magColor)
                    }
                    Spacer()
                    SignalPill(text: observation.aavsoSubmitted ? "AAVSO" : observation.qualityFlag.ifEmpty("PENDING").uppercased(), color: observation.aavsoSubmitted ? AppTheme.accent : AppTheme.warm)
                }
                HStack(spacing: 8) {
                    MiniDatum(label: "uncertainty", value: "± \(String(format: "%.3f", observation.uncertainty))")
                    MiniDatum(label: "node", value: observation.nodeId.ifEmpty("unknown"))
                    MiniDatum(label: "filter", value: observation.filter.ifEmpty("none"))
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var magColor: Color {
        if observation.magnitude < 8 { return AppTheme.warm }
        if observation.magnitude < 11 { return AppTheme.sky }
        return AppTheme.ink2
    }
}

// MARK: - Notifications

struct NotificationsView: View {
    @Bindable var appState: MemberAppState
    let onAccount: () -> Void
    @State private var notifications: [AppNotification] = []

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    OpsPanel(accent: notifications.contains { !$0.read } ? AppTheme.danger : AppTheme.accent) {
                        VStack(alignment: .leading, spacing: 10) {
                            OpsHeader(kicker: "TRANSMISSIONS", title: notifications.contains { !$0.read } ? "Actionable alerts" : "All quiet", systemImage: "bell.and.waves.left.and.right")
                            Text(notifications.isEmpty ? "When the network needs you, it will show up here." : "Tap an incoming transmission to mark it received.")
                                .font(.subheadline)
                                .foregroundStyle(AppTheme.ink2)
                        }
                    }
                    if notifications.isEmpty {
                        EmptyLine("No alerts yet.")
                            .padding(.top, 12)
                    }
                    ForEach(notifications) { notification in
                        Button { Task { await markRead(notification) } } label: {
                            IntentRow(
                                systemImage: notification.read ? "envelope.open" : "dot.radiowaves.left.and.right",
                                color: notification.read ? AppTheme.ink3 : AppTheme.danger,
                                title: notification.title,
                                subtitle: "\(compactDate(notification.sentAt)) · \(notification.read ? "read" : "new")"
                            )
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
            }
            .background(AppTheme.night)
            .navigationTitle("Alerts")
            .toolbar(.hidden, for: .navigationBar)
            .toolbar {
                Button(action: onAccount) {
                    Label("Account", systemImage: "person.crop.circle")
                }
            }
            .refreshable { await load() }
            .task { await load() }
        }
    }

    private func load() async {
        do {
            let result = try await appState.api.notifications()
            notifications = result.0
            appState.unreadNotifications = result.1
        } catch { appState.handleAuthError(error) }
    }

    private func markRead(_ notification: AppNotification) async {
        guard !notification.read else { return }
        try? await appState.api.markNotificationRead(notification.id)
        await load()
    }
}

// MARK: - Target Detail

enum TargetViewMode: String, CaseIterable, Identifiable {
    case chart = "Chart"
    case table = "Table"
    case audio = "Audio"
    var id: String { rawValue }
}

struct TargetDetailView: View {
    let api: ApiClient
    let targetName: String
    @Environment(\.dismiss) private var dismiss
    @State private var points: [LightCurvePoint] = []
    @State private var mode = TargetViewMode.chart
    @State private var tts = TextToSpeechManager()
    @State private var haptics = HapticManager()
    @State private var error: String?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("View mode", selection: $mode) {
                    ForEach(TargetViewMode.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .padding()

                Group {
                    switch mode {
                    case .chart: chartView
                    case .table: tableView
                    case .audio: audioView
                    }
                }
                Divider()
                accessibilityBar
            }
            .background(AppTheme.night)
            .navigationTitle(targetName)
            .toolbar { Button("Done") { dismiss() } }
            .task { await load() }
        }
    }

    private var chartView: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                summary
                Chart(points) { point in
                    LineMark(x: .value("BJD", point.bjd), y: .value("Magnitude", point.magnitude))
                    PointMark(x: .value("BJD", point.bjd), y: .value("Magnitude", point.magnitude))
                }
                .chartYScale(domain: .automatic(reversed: true))
                .frame(height: 260)
                .padding()
                .background(AppTheme.surface, in: RoundedRectangle(cornerRadius: 8))
                .accessibilityLabel(lightCurveDescription)
            }
            .padding()
        }
    }

    private var tableView: some View {
        List(points) { point in
            VStack(alignment: .leading) {
                Text("BJD \(point.bjd, specifier: "%.5f")")
                    .font(.headline)
                Text("Magnitude \(point.magnitude, specifier: "%.3f") ± \(point.uncertainty, specifier: "%.3f") • \(point.filter) • SNR \(point.snr, specifier: "%.1f")")
                    .foregroundStyle(AppTheme.ink2)
            }
            .accessibilityElement(children: .combine)
        }
        .scrollContentBackground(.hidden)
    }

    private var audioView: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                summary
                Text(lightCurveDescription)
                    .foregroundStyle(AppTheme.ink2)
            }
            .padding()
        }
    }

    private var accessibilityBar: some View {
        HStack(spacing: 12) {
            Button {
                toggleSpeech()
            } label: {
                Label(tts.isSpeaking ? "Stop" : "Hear", systemImage: tts.isSpeaking ? "stop.fill" : "speaker.wave.2.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .accessibilityLabel(tts.isSpeaking ? "Stop audio description" : "Hear audio description")

            Button {
                if haptics.isPlaying { haptics.stop() }
                else { playHaptics() }
            } label: {
                Label(haptics.isPlaying ? "Stop" : "Feel", systemImage: haptics.isPlaying ? "stop.fill" : "waveform.path")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .accessibilityLabel(haptics.isPlaying ? "Stop haptic light curve" : "Feel the light curve")
        }
        .padding()
        .background(AppTheme.surface)
    }

    private var summary: some View {
        HStack {
            StatTile(title: "Points", value: "\(points.count)")
            StatTile(title: "Brightest", value: points.map(\.magnitude).min().map { String(format: "%.2f", $0) } ?? "--")
            StatTile(title: "Faintest", value: points.map(\.magnitude).max().map { String(format: "%.2f", $0) } ?? "--")
        }
    }

    private var lightCurveDescription: String {
        let sorted = points.sorted { $0.bjd < $1.bjd }
        guard let first = sorted.first, let latest = sorted.last else {
            return "No observations recorded for \(targetName) yet."
        }

        let count = sorted.count
        let daySpan = Int((latest.bjd - first.bjd).rounded())
        let mags = sorted.map(\.magnitude)
        let minMag = mags.min() ?? 0
        let maxMag = mags.max() ?? 0
        let range = maxMag - minMag
        let brightestIndex = mags.firstIndex(of: minMag) ?? 0
        let daysAgo = Int((latest.bjd - sorted[brightestIndex].bjd).rounded())
        let submitted = sorted.filter(\.aavsoSubmitted).count
        let good = sorted.filter { $0.qualityFlag == "good" }.count

        var trend = ""
        if sorted.count >= 4 {
            let firstAvg = sorted.prefix(2).map(\.magnitude).reduce(0, +) / 2
            let lastAvg = sorted.suffix(2).map(\.magnitude).reduce(0, +) / 2
            if lastAvg < firstAvg - 0.1 {
                trend = "Overall the star has been getting brighter. "
            } else if lastAvg > firstAvg + 0.1 {
                trend = "Overall the star has been getting fainter. "
            } else {
                trend = "The brightness has been roughly steady. "
            }
        }

        let observationWord = count == 1 ? "observation" : "observations"
        let dayWord = daysAgo == 1 ? "day" : "days"
        let peakWhen = daysAgo == 0 ? "reached tonight" : "\(daysAgo) \(dayWord) ago"
        let aavsoStatus = latest.aavsoSubmitted ? "Submitted to AAVSO. " : "Pending AAVSO submission. "
        return "\(targetName). \(count) \(observationWord) over \(daySpan) days. "
            + "Most recent magnitude: \(String(format: "%.3f", latest.magnitude)), "
            + "uncertainty plus or minus \(String(format: "%.3f", latest.uncertainty)). "
            + "Quality: \(latest.qualityFlag). "
            + aavsoStatus
            + "Peak brightness was magnitude \(String(format: "%.2f", minMag)), \(peakWhen). "
            + "The star varied by \(String(format: "%.2f", range)) magnitudes in total. "
            + trend
            + "\(submitted) of \(count) measurements have been accepted by AAVSO. "
            + "\(good) passed quality checks as good."
    }

    private func load() async {
        do { points = try await api.lightCurve(targetName: targetName) }
        catch { self.error = error.localizedDescription }
    }

    private func toggleSpeech() {
        if tts.isSpeaking {
            tts.stop()
        } else {
            try? tts.speak(text: lightCurveDescription, language: "en-US", rate: 0.43)
        }
    }

    private func playHaptics() {
        guard !points.isEmpty else { return }
        let sorted = points.sorted { $0.bjd < $1.bjd }
        let mags = sorted.map(\.magnitude)
        let minMag = mags.min() ?? 0
        let maxMag = mags.max() ?? minMag + 1
        let span = max(maxMag - minMag, 0.01)
        let events = sorted.enumerated().map { index, point in
            let brightness = Float((maxMag - point.magnitude) / span)
            return HapticEvent(relativeTime: Double(index) * 0.32 + 0.10, intensity: max(0.24, brightness), sharpness: 0.55, duration: 0.22)
        }
        try? haptics.playPattern(events: events)
    }
}

// MARK: - Me

struct MeView: View {
    @Bindable var appState: MemberAppState
    @State private var stats = MemberStats.empty
    @State private var confirmDelete = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            List {
                Section("Account") {
                    Text(appState.member?.displayName.ifEmpty(appState.member?.email ?? "") ?? "")
                    Text(appState.member?.email ?? "").foregroundStyle(AppTheme.ink2)
                }
                Section("Stats") {
                    StatTile(title: "Observations", value: "\(stats.totalObservations)")
                    StatTile(title: "AAVSO submissions", value: "\(stats.aavsoSubmitted)")
                    StatTile(title: "Nodes", value: "\(stats.nodeCount)")
                }
                Section {
                    Button("Sign out") { appState.signOut() }
                    Button("Delete account", role: .destructive) { confirmDelete = true }
                }
                if let error {
                    Section { Text(error).foregroundStyle(AppTheme.danger) }
                }
            }
            .scrollContentBackground(.hidden)
            .background(AppTheme.night)
            .navigationTitle("Me")
            .task { stats = (try? await appState.api.stats()) ?? .empty }
            .alert("Delete account?", isPresented: $confirmDelete) {
                Button("Delete my account", role: .destructive) { Task { await deleteAccount() } }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("This permanently deletes your account and all your data. Your telescopes will stop contributing to the network. This cannot be undone.")
            }
        }
    }

    private func deleteAccount() async {
        do {
            try await appState.api.deleteAccount()
            appState.signOut()
        } catch {
            self.error = "Could not delete account: \(error.localizedDescription)"
        }
    }
}

// MARK: - Shared UI

struct MissionControlHero: View {
    let headline: String
    let summary: String
    let online: Int
    let totalNodes: Int
    let observations: Int
    let submitted: Int
    let alerts: Int
    let targets: [Target]
    let accent: Color

    var body: some View {
        ZStack(alignment: .topLeading) {
            AppTheme.surface.opacity(0.92)
            MissionField(accent: accent, alerts: alerts)
                .opacity(0.95)
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("LIVE SKY CONTROL")
                            .font(.caption2.weight(.black))
                            .kerning(2.2)
                            .foregroundStyle(accent)
                        Text(headline.uppercased())
                            .font(.system(size: 32, weight: .black, design: .monospaced))
                            .foregroundStyle(AppTheme.ink)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 8) {
                        SignalPill(text: alerts > 0 ? "\(alerts) ALERTS" : "CLEAR", color: alerts > 0 ? AppTheme.danger : accent)
                        Text("\(online)/\(max(totalNodes, online)) NODES")
                            .font(.caption2.weight(.black))
                            .monospacedDigit()
                            .foregroundStyle(AppTheme.ink2)
                    }
                }

                Text(summary)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.ink2)
                    .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 9) {
                    ControlReadout(label: "OBS", value: "\(observations)", color: AppTheme.sky)
                    ControlReadout(label: "AAVSO", value: "\(submitted)", color: AppTheme.accent)
                    ControlReadout(label: "QUEUE", value: "\(targets.count)", color: AppTheme.warm)
                }

                if !targets.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("PRIORITY TRACKS")
                            .opsLabel()
                        ForEach(targets) { target in
                            HStack(spacing: 8) {
                                Circle()
                                    .fill(target.priority > 0.7 ? AppTheme.warm : AppTheme.sky)
                                    .frame(width: 7, height: 7)
                                Text(target.name)
                                    .font(.caption.weight(.black))
                                    .foregroundStyle(AppTheme.ink)
                                    .lineLimit(1)
                                Spacer()
                                Text("\(Int(target.priority * 100))")
                                    .font(.caption2.weight(.black))
                                    .monospacedDigit()
                                    .foregroundStyle(AppTheme.ink3)
                            }
                            .padding(.vertical, 6)
                            .padding(.horizontal, 8)
                            .background(AppTheme.night.opacity(0.42), in: RoundedRectangle(cornerRadius: 7))
                        }
                    }
                }
            }
            .padding(18)
        }
        .clipShape(RoundedRectangle(cornerRadius: 4))
        .overlay(RoundedRectangle(cornerRadius: 4).stroke(accent.opacity(0.42), lineWidth: 1))
        .overlay(alignment: .topTrailing) {
            Text("// TTN //")
                .font(.caption2.weight(.black))
                .kerning(1.4)
                .foregroundStyle(AppTheme.ink3)
                .padding(10)
        }
    }
}

struct MissionField: View {
    let accent: Color
    let alerts: Int

    private let stars: [(CGFloat, CGFloat, CGFloat, Color)] = [
        (0.16, 0.25, 2.0, AppTheme.sky),
        (0.30, 0.64, 3.2, AppTheme.accent),
        (0.47, 0.35, 2.4, AppTheme.ink),
        (0.62, 0.77, 2.6, AppTheme.sky),
        (0.78, 0.48, 3.8, AppTheme.warm),
        (0.88, 0.22, 1.8, AppTheme.ink),
    ]

    var body: some View {
        GeometryReader { proxy in
            ZStack {
                LinearGradient(
                    colors: [accent.opacity(0.20), .clear, AppTheme.night.opacity(0.55)],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
                ForEach([CGFloat(0.35), CGFloat(0.58), CGFloat(0.82)], id: \.self) { scale in
                    Ellipse()
                        .stroke(AppTheme.ink.opacity(0.08), lineWidth: 1)
                        .frame(width: proxy.size.width * scale, height: proxy.size.height * scale * 0.62)
                        .position(x: proxy.size.width * 0.67, y: proxy.size.height * 0.44)
                }
                Path { path in
                    path.move(to: CGPoint(x: proxy.size.width * 0.08, y: proxy.size.height * 0.72))
                    path.addLine(to: CGPoint(x: proxy.size.width * 0.33, y: proxy.size.height * 0.39))
                    path.addLine(to: CGPoint(x: proxy.size.width * 0.55, y: proxy.size.height * 0.52))
                    path.addLine(to: CGPoint(x: proxy.size.width * 0.78, y: proxy.size.height * 0.25))
                }
                .stroke(accent.opacity(0.32), style: StrokeStyle(lineWidth: 2, dash: [7, 7]))
                ForEach(Array(stars.enumerated()), id: \.offset) { _, star in
                    Circle()
                        .fill(star.3)
                        .frame(width: star.2 * 2, height: star.2 * 2)
                        .shadow(color: star.3.opacity(0.8), radius: star.2 * 3)
                        .position(x: proxy.size.width * star.0, y: proxy.size.height * star.1)
                }
                if alerts > 0 {
                    Circle()
                        .stroke(AppTheme.danger, lineWidth: 2)
                        .frame(width: 42, height: 42)
                        .position(x: proxy.size.width * 0.78, y: proxy.size.height * 0.48)
                }
            }
        }
    }
}

struct ControlReadout: View {
    let label: String
    let value: String
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.caption2.weight(.black))
                .kerning(1.0)
                .foregroundStyle(AppTheme.ink3)
            Text(value)
                .font(.system(size: 26, weight: .black, design: .monospaced))
                .monospacedDigit()
                .foregroundStyle(color)
        }
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppTheme.night.opacity(0.58), in: RoundedRectangle(cornerRadius: 3))
        .overlay(RoundedRectangle(cornerRadius: 3).stroke(color.opacity(0.30)))
    }
}

struct NetworkRadarView: View {
    let online: Int
    let targets: Int
    let alerts: Int
    let accent: Color

    private let points: [(CGFloat, CGFloat, Color)] = [
        (0.18, 0.36, AppTheme.sky),
        (0.34, 0.68, AppTheme.accent),
        (0.56, 0.28, AppTheme.warm),
        (0.78, 0.55, AppTheme.sky),
        (0.66, 0.78, AppTheme.accent),
    ]

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 14)
                .fill(AppTheme.night.opacity(0.78))
            GeometryReader { proxy in
                let size = min(proxy.size.width, proxy.size.height)
                let center = CGPoint(x: proxy.size.width * 0.5, y: proxy.size.height * 0.54)
                ZStack {
                    ForEach([CGFloat(0.28), CGFloat(0.48), CGFloat(0.68), CGFloat(0.88)], id: \.self) { scale in
                        Circle()
                            .stroke(AppTheme.ink.opacity(0.08), lineWidth: 1)
                            .frame(width: size * scale, height: size * scale)
                            .position(center)
                    }
                    Path { path in
                        path.move(to: CGPoint(x: center.x, y: center.y - size * 0.44))
                        path.addLine(to: CGPoint(x: center.x, y: center.y + size * 0.44))
                        path.move(to: CGPoint(x: center.x - size * 0.44, y: center.y))
                        path.addLine(to: CGPoint(x: center.x + size * 0.44, y: center.y))
                    }
                    .stroke(AppTheme.ink.opacity(0.10), style: StrokeStyle(lineWidth: 1, dash: [4, 6]))
                    ForEach(Array(points.enumerated()), id: \.offset) { _, point in
                        Circle()
                            .fill(point.2)
                            .frame(width: 8, height: 8)
                            .shadow(color: point.2.opacity(0.75), radius: 7)
                            .position(x: proxy.size.width * point.0, y: proxy.size.height * point.1)
                    }
                    if alerts > 0 {
                        Circle()
                            .stroke(AppTheme.danger, lineWidth: 2)
                            .frame(width: 30, height: 30)
                            .position(x: proxy.size.width * 0.78, y: proxy.size.height * 0.55)
                    }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 14))

            VStack {
                HStack {
                    Text("NETWORK SKY PLOT")
                        .opsLabel()
                    Spacer()
                    SignalPill(text: alerts > 0 ? "\(alerts) ALERTS" : "CLEAR", color: alerts > 0 ? AppTheme.danger : accent)
                }
                Spacer()
                HStack(spacing: 8) {
                    MiniDatum(label: "online", value: "\(online)")
                    MiniDatum(label: "targets", value: "\(targets)")
                    MiniDatum(label: "state", value: alerts > 0 ? "review" : "ready")
                }
            }
            .padding(12)
        }
        .frame(height: 190)
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(accent.opacity(0.24)))
    }
}

struct OpsPanel<Content: View>: View {
    let accent: Color
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            ZStack(alignment: .leading) {
                AppTheme.surface.opacity(0.96)
                accent.opacity(0.055)
                Rectangle()
                    .fill(accent)
                    .frame(width: 3)
            },
            in: RoundedRectangle(cornerRadius: 4)
        )
        .overlay(RoundedRectangle(cornerRadius: 4).stroke(accent.opacity(0.28)))
        .overlay(alignment: .topTrailing) {
            HStack(spacing: 4) {
                Rectangle().fill(accent.opacity(0.55)).frame(width: 18, height: 2)
                Rectangle().fill(accent.opacity(0.25)).frame(width: 8, height: 2)
            }
            .padding(10)
        }
        .overlay(alignment: .bottomLeading) {
            HStack(spacing: 4) {
                Rectangle().fill(accent.opacity(0.28)).frame(width: 8, height: 2)
                Rectangle().fill(accent.opacity(0.55)).frame(width: 18, height: 2)
            }
            .padding(10)
        }
        .shadow(color: .black.opacity(0.28), radius: 18, x: 0, y: 10)
    }
}

struct OpsHeader: View {
    let kicker: String
    let title: String
    let systemImage: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: systemImage)
                .font(.subheadline.weight(.bold))
                .foregroundStyle(AppTheme.ink2)
                .frame(width: 32, height: 32)
                .background(AppTheme.ink.opacity(0.06), in: RoundedRectangle(cornerRadius: 3))
            VStack(alignment: .leading, spacing: 2) {
                Text(kicker)
                    .opsLabel()
                Text(title)
                    .font(.headline.weight(.black))
                    .foregroundStyle(AppTheme.ink)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

struct IntentRow: View {
    let systemImage: String
    let color: Color
    let title: String
    let subtitle: String

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: systemImage)
                .font(.subheadline.weight(.bold))
                .foregroundStyle(color)
                .frame(width: 32, height: 32)
                .background(color.opacity(0.11), in: RoundedRectangle(cornerRadius: 3))
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.subheadline.weight(.bold))
                    .foregroundStyle(AppTheme.ink)
                    .lineLimit(1)
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(AppTheme.ink2)
                    .lineLimit(3)
            }
            Spacer(minLength: 4)
        }
        .padding(10)
        .background(color.opacity(0.045), in: RoundedRectangle(cornerRadius: 4))
        .overlay(RoundedRectangle(cornerRadius: 4).stroke(color.opacity(0.16)))
        .accessibilityElement(children: .combine)
    }
}

struct SignalPill: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.caption2.weight(.black))
            .kerning(0.7)
            .foregroundStyle(color)
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(color.opacity(0.34)))
    }
}

struct OpsMetric: View {
    let title: String
    let value: String
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption2)
                .foregroundStyle(AppTheme.ink3)
            Text(value)
                .font(.title3.weight(.black))
                .foregroundStyle(color)
                .monospacedDigit()
        }
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppTheme.ink.opacity(0.035), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(AppTheme.ink.opacity(0.10)))
        .accessibilityElement(children: .combine)
    }
}

struct MiniDatum: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption2)
                .foregroundStyle(AppTheme.ink3)
                .lineLimit(1)
            Text(value)
                .font(.caption.weight(.bold))
                .foregroundStyle(AppTheme.ink2)
                .lineLimit(1)
        }
        .padding(9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppTheme.ink.opacity(0.035), in: RoundedRectangle(cornerRadius: 9))
        .overlay(RoundedRectangle(cornerRadius: 9).stroke(AppTheme.ink.opacity(0.10)))
    }
}

func panel<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
    VStack(alignment: .leading, spacing: 12, content: content)
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppTheme.surface, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(AppTheme.ink.opacity(0.14)))
}

func row(title: String, subtitle: String, accessory: String) -> some View {
    HStack(alignment: .top, spacing: 12) {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).foregroundStyle(AppTheme.ink).font(.body.weight(.semibold))
            Text(subtitle).foregroundStyle(AppTheme.ink2).font(.caption)
        }
        Spacer()
        if !accessory.isEmpty {
            Text(accessory).font(.caption.weight(.semibold)).foregroundStyle(AppTheme.accent)
        }
    }
    .accessibilityElement(children: .combine)
}

struct PanelHeader: View {
    let title: String
    let systemImage: String
    var body: some View {
        Label(title, systemImage: systemImage)
            .font(.headline)
            .foregroundStyle(AppTheme.ink)
    }
}

struct StatTile: View {
    let title: String
    let value: String
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(value).font(.title2.bold()).foregroundStyle(AppTheme.ink)
            Text(title).font(.caption).foregroundStyle(AppTheme.ink2)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppTheme.surface2, in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .combine)
    }
}

struct StatusBadge: View {
    let text: String
    let good: Bool
    var body: some View {
        Label(text.ifEmpty("unknown"), systemImage: good ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
            .font(.caption.weight(.semibold))
            .foregroundStyle(good ? AppTheme.accent : AppTheme.warm)
    }
}

struct SkyQualityView: View {
    let sky: SkyQuality

    var body: some View {
        HStack {
            Label("Bortle \(sky.bortle)", systemImage: "moon.haze")
            if sky.mpsas > 0 {
                Text("\(sky.mpsas, specifier: "%.1f") mpsas")
            }
            if !sky.source.isEmpty {
                Text(sky.source).foregroundStyle(AppTheme.ink3)
            }
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(AppTheme.sky)
        .accessibilityElement(children: .combine)
    }
}

struct EmptyLine: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View { Text(text).foregroundStyle(AppTheme.ink2) }
}

func refreshButton(_ action: @escaping () async -> Void) -> some ToolbarContent {
    ToolbarItem(placement: .topBarTrailing) {
        Button { Task { await action() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
    }
}

extension View {
    func opsLabel() -> some View {
        self
            .font(.caption2.weight(.black))
            .kerning(1.1)
            .foregroundStyle(AppTheme.ink3)
    }

    func nativeField() -> some View {
        self
            .padding(14)
            .background(AppTheme.surface2, in: RoundedRectangle(cornerRadius: 8))
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
    }
}

extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
    func ifEmpty(_ fallback: String) -> String { isEmpty ? fallback : self }
}

extension Optional where Wrapped == String {
    func ifEmpty(_ fallback: String) -> String { (self ?? "").isEmpty ? fallback : (self ?? "") }
}

extension ISO8601DateFormatter {
    static let dateOnly: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withFullDate]
        return formatter
    }()
}

#Preview {
    ContentView()
}
