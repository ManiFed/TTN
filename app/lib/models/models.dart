/// Data models mirroring the cloud member API (cloud/server.py).
///
/// Each `fromJson` is defensive: the cloud occasionally returns null for
/// aggregate columns, so numeric fields fall back to 0 / 0.0.
library;

int _asInt(dynamic v) => v is int ? v : (v is num ? v.toInt() : int.tryParse('$v') ?? 0);
double _asDouble(dynamic v) =>
    v is double ? v : (v is num ? v.toDouble() : double.tryParse('$v') ?? 0.0);
String _asStr(dynamic v) => v == null ? '' : '$v';

/// Logged-in member profile (GET /me).
class Member {
  final String userId;
  final String email;
  final String role;
  final String displayName;
  final String country;

  const Member({
    required this.userId,
    required this.email,
    required this.role,
    required this.displayName,
    required this.country,
  });

  factory Member.fromJson(Map<String, dynamic> j) => Member(
        userId: _asStr(j['user_id']),
        email: _asStr(j['email']),
        role: _asStr(j['role']),
        displayName: _asStr(j['display_name']),
        country: _asStr(j['country']),
      );
}

/// A telescope node the member has claimed (GET /me/nodes).
class Node {
  final String nodeId;
  final String telescopeModel;
  final String city;
  final String country;
  final String status;
  final String lastHeartbeat;
  final bool online;

  const Node({
    required this.nodeId,
    required this.telescopeModel,
    required this.city,
    required this.country,
    required this.status,
    required this.lastHeartbeat,
    required this.online,
  });

  factory Node.fromJson(Map<String, dynamic> j) => Node(
        nodeId: _asStr(j['node_id']),
        telescopeModel: _asStr(j['telescope_model']),
        city: _asStr(j['city']),
        country: _asStr(j['country']),
        status: _asStr(j['status']),
        lastHeartbeat: _asStr(j['last_heartbeat']),
        online: j['online'] == true,
      );

  String get location {
    final parts = [city, country].where((p) => p.isNotEmpty).toList();
    return parts.isEmpty ? 'Location unknown' : parts.join(', ');
  }
}

/// Cumulative member statistics (GET /me/stats).
class MemberStats {
  final int totalObservations;
  final int aavsoSubmitted;
  final int targetsObserved;
  final int clearNights;
  final int nodeCount;

  const MemberStats({
    required this.totalObservations,
    required this.aavsoSubmitted,
    required this.targetsObserved,
    required this.clearNights,
    required this.nodeCount,
  });

  const MemberStats.empty()
      : totalObservations = 0,
        aavsoSubmitted = 0,
        targetsObserved = 0,
        clearNights = 0,
        nodeCount = 0;

  factory MemberStats.fromJson(Map<String, dynamic> j) => MemberStats(
        totalObservations: _asInt(j['total_observations']),
        aavsoSubmitted: _asInt(j['aavso_submitted']),
        targetsObserved: _asInt(j['targets_observed']),
        clearNights: _asInt(j['clear_nights']),
        nodeCount: _asInt(j['node_count']),
      );
}

/// A single photometric measurement (GET /me/observations).
class Observation {
  final String nodeId;
  final String targetName;
  final double bjd;
  final double magnitude;
  final double uncertainty;
  final String filter;
  final String qualityFlag;
  final bool aavsoSubmitted;
  final String receivedAt;

  const Observation({
    required this.nodeId,
    required this.targetName,
    required this.bjd,
    required this.magnitude,
    required this.uncertainty,
    required this.filter,
    required this.qualityFlag,
    required this.aavsoSubmitted,
    required this.receivedAt,
  });

  factory Observation.fromJson(Map<String, dynamic> j) => Observation(
        nodeId: _asStr(j['node_id']),
        targetName: _asStr(j['target_name']),
        bjd: _asDouble(j['bjd']),
        magnitude: _asDouble(j['magnitude']),
        uncertainty: _asDouble(j['uncertainty']),
        filter: _asStr(j['filter']),
        qualityFlag: _asStr(j['quality_flag']),
        aavsoSubmitted: _asInt(j['aavso_submitted']) == 1 || j['aavso_submitted'] == true,
        receivedAt: _asStr(j['received_at']),
      );
}

/// One point on a target's photometric light curve (GET /lightcurves/<name>).
class LightCurvePoint {
  final String nodeId;
  final double bjd;
  final double magnitude;
  final double uncertainty;
  final String filter;
  final double snr;
  final String qualityFlag;
  final bool aavsoSubmitted;

  const LightCurvePoint({
    required this.nodeId,
    required this.bjd,
    required this.magnitude,
    required this.uncertainty,
    required this.filter,
    required this.snr,
    required this.qualityFlag,
    required this.aavsoSubmitted,
  });

  factory LightCurvePoint.fromJson(Map<String, dynamic> j) => LightCurvePoint(
        nodeId: _asStr(j['node_id']),
        bjd: _asDouble(j['bjd']),
        magnitude: _asDouble(j['magnitude']),
        uncertainty: _asDouble(j['uncertainty']),
        filter: _asStr(j['filter']),
        snr: _asDouble(j['snr']),
        qualityFlag: _asStr(j['quality_flag']),
        aavsoSubmitted:
            _asInt(j['aavso_submitted']) == 1 || j['aavso_submitted'] == true,
      );
}

/// An active observation target (GET /targets).
class Target {
  final String targetId;
  final String name;
  final String targetType;
  final double? mag;
  final String magBand;
  final double priority;
  final double? bestScore;
  final int nMeasurements;

  const Target({
    required this.targetId,
    required this.name,
    required this.targetType,
    required this.mag,
    required this.magBand,
    required this.priority,
    required this.bestScore,
    required this.nMeasurements,
  });

  factory Target.fromJson(Map<String, dynamic> j) => Target(
        targetId: _asStr(j['target_id']),
        name: _asStr(j['name']),
        targetType: _asStr(j['target_type']),
        mag: j['mag'] == null ? null : _asDouble(j['mag']),
        magBand: _asStr(j['mag_band']),
        priority: _asDouble(j['priority']),
        bestScore: j['best_score'] == null ? null : _asDouble(j['best_score']),
        nMeasurements: _asInt(j['n_measurements']),
      );
}

/// An in-app notification (GET /me/notifications).
class AppNotification {
  final int id;
  final String type;
  final Map<String, dynamic> payload;
  final String sentAt;
  final bool read;

  const AppNotification({
    required this.id,
    required this.type,
    required this.payload,
    required this.sentAt,
    required this.read,
  });

  factory AppNotification.fromJson(Map<String, dynamic> j) => AppNotification(
        id: _asInt(j['id']),
        type: _asStr(j['type']),
        payload: (j['payload'] is Map)
            ? Map<String, dynamic>.from(j['payload'] as Map)
            : <String, dynamic>{},
        sentAt: _asStr(j['sent_at']),
        read: j['read_at'] != null,
      );

  /// Best-effort human-readable line from the payload.
  String get title {
    final t = payload['title'] ?? payload['message'] ?? payload['target'];
    if (t != null) return '$t';
    return type.replaceAll('_', ' ');
  }
}
