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

/// A previous observing location stored on a portable node.
class PreviousLocation {
  final double lat;
  final double lon;
  final String city;
  final String siteName;
  final String lastUsed;

  const PreviousLocation({
    required this.lat,
    required this.lon,
    required this.city,
    required this.siteName,
    required this.lastUsed,
  });

  factory PreviousLocation.fromJson(Map<String, dynamic> j) => PreviousLocation(
        lat: _asDouble(j['lat']),
        lon: _asDouble(j['lon']),
        city: _asStr(j['city']),
        siteName: _asStr(j['site_name']),
        lastUsed: _asStr(j['last_used']),
      );

  String get label {
    if (siteName.isNotEmpty && city.isNotEmpty) return '$siteName, $city';
    return siteName.isNotEmpty ? siteName : city;
  }
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
  final bool portable;
  final String vacationUntil;
  final String sessionCity;
  final String sessionSiteName;
  final List<PreviousLocation> previousLocations;

  const Node({
    required this.nodeId,
    required this.telescopeModel,
    required this.city,
    required this.country,
    required this.status,
    required this.lastHeartbeat,
    required this.online,
    required this.portable,
    required this.vacationUntil,
    required this.sessionCity,
    required this.sessionSiteName,
    required this.previousLocations,
  });

  factory Node.fromJson(Map<String, dynamic> j) {
    final locList = (j['previous_locations'] as List? ?? [])
        .map((e) => PreviousLocation.fromJson(e as Map<String, dynamic>))
        .toList();
    return Node(
      nodeId: _asStr(j['node_id']),
      telescopeModel: _asStr(j['telescope_model']),
      city: _asStr(j['city']),
      country: _asStr(j['country']),
      status: _asStr(j['status']),
      lastHeartbeat: _asStr(j['last_heartbeat']),
      online: j['online'] == true,
      portable: j['portable'] == true,
      vacationUntil: _asStr(j['vacation_until']),
      sessionCity: _asStr(j['session_city']),
      sessionSiteName: _asStr(j['session_site_name']),
      previousLocations: locList,
    );
  }

  bool get isSleeping => status == 'sleeping';
  bool get isOnVacation => status == 'vacation';

  String get location {
    final parts = [city, country].where((p) => p.isNotEmpty).toList();
    return parts.isEmpty ? 'Location unknown' : parts.join(', ');
  }
}

/// One entry in the telescope spec catalog (GET /telescopes).
///
/// Mirrors `telescope_specs.catalog_list()` on the cloud: physical specs plus
/// the derived parameters the connect-flow confirmation card shows.
class TelescopeSpec {
  final String key;
  final String displayName;
  final double apertureMm;
  final double focalLengthMm;
  final double focalRatio;
  final double pixelScaleArcsec;
  final double fovDeg;
  final String mountType;
  final int tier;
  final String cameraModel;

  const TelescopeSpec({
    required this.key,
    required this.displayName,
    required this.apertureMm,
    required this.focalLengthMm,
    required this.focalRatio,
    required this.pixelScaleArcsec,
    required this.fovDeg,
    required this.mountType,
    required this.tier,
    required this.cameraModel,
  });

  bool get isCustom => key == 'custom';

  factory TelescopeSpec.fromJson(Map<String, dynamic> j) => TelescopeSpec(
        key: _asStr(j['key']),
        displayName: _asStr(j['telescope_model']).isNotEmpty
            ? _asStr(j['telescope_model'])
            : _asStr(j['display_name']),
        apertureMm: _asDouble(j['aperture_mm']),
        focalLengthMm: _asDouble(j['focal_length_mm']),
        focalRatio: _asDouble(j['focal_ratio']),
        pixelScaleArcsec: _asDouble(j['pixel_scale_arcsec']),
        fovDeg: _asDouble(j['fov_deg']),
        mountType: _asStr(j['mount_type']),
        tier: _asInt(j['tier']),
        cameraModel: _asStr(j['camera_model']),
      );

  /// The custom-spec payload sent with the activation code (only set fields).
  Map<String, dynamic> toSpecPayload() => {
        if (apertureMm > 0) 'aperture_mm': apertureMm,
        if (focalLengthMm > 0) 'focal_length_mm': focalLengthMm,
        if (pixelScaleArcsec > 0) 'pixel_scale_arcsec': pixelScaleArcsec,
        if (fovDeg > 0) 'fov_deg': fovDeg,
        if (mountType.isNotEmpty) 'mount_type': mountType,
        if (cameraModel.isNotEmpty) 'camera_model': cameraModel,
      };
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
  final String scienceProgram;
  final double? mag;
  final String magBand;
  final double priority;
  final double? bestScore;
  final int nMeasurements;
  final Map<String, dynamic> scoreExplanation;

  const Target({
    required this.targetId,
    required this.name,
    required this.targetType,
    this.scienceProgram = '',
    required this.mag,
    required this.magBand,
    required this.priority,
    required this.bestScore,
    required this.nMeasurements,
    required this.scoreExplanation,
  });

  factory Target.fromJson(Map<String, dynamic> j) => Target(
        targetId: _asStr(j['target_id']),
        name: _asStr(j['name']),
        targetType: _asStr(j['target_type']),
        scienceProgram: _asStr(j['science_program']),
        mag: j['mag'] == null ? null : _asDouble(j['mag']),
        magBand: _asStr(j['mag_band']),
        priority: _asDouble(j['priority']),
        bestScore: j['best_score'] == null ? null : _asDouble(j['best_score']),
        nMeasurements: _asInt(j['n_measurements']),
        scoreExplanation: (j['score_explanation'] is Map)
            ? Map<String, dynamic>.from(j['score_explanation'] as Map)
            : <String, dynamic>{},
      );
}

/// One planned observation in tonight's timeline (GET /me/timeline).
class TimelineItem {
  final String nodeId;
  final String target;
  final String targetId;
  final String startTime;
  final double score;
  final double ra;
  final double dec;
  final double expDur;
  final int expCount;
  final String filter;
  final String notes;
  final Map<String, dynamic> explanation;

  const TimelineItem({
    required this.nodeId,
    required this.target,
    required this.targetId,
    required this.startTime,
    required this.score,
    required this.ra,
    required this.dec,
    required this.expDur,
    required this.expCount,
    required this.filter,
    required this.notes,
    required this.explanation,
  });

  factory TimelineItem.fromJson(Map<String, dynamic> j) {
    final node = j['node'] is Map ? Map<String, dynamic>.from(j['node'] as Map) : {};
    return TimelineItem(
      nodeId: _asStr(node['node_id']),
      target: _asStr(j['target']),
      targetId: _asStr(j['target_id']),
      startTime: _asStr(j['startTime']),
      score: _asDouble(j['score']),
      ra: _asDouble(j['ra']),
      dec: _asDouble(j['dec']),
      expDur: _asDouble(j['expDur']),
      expCount: _asInt(j['expCount']),
      filter: _asStr(j['filter']),
      notes: _asStr(j['notes']),
      explanation: (j['explanation'] is Map)
          ? Map<String, dynamic>.from(j['explanation'] as Map)
          : <String, dynamic>{},
    );
  }

  double get estimatedMinutes => expDur * expCount / 60.0;
  String get reason => _asStr(explanation['summary']);
}

/// One night's summary for a node (GET /me/nights).
class NightSummary {
  final String nodeId;
  final String night;
  final int nTargets;
  final int nObservations;
  final int nSubmitted;
  final String generatedAt;
  final Map<String, dynamic> receipt;

  const NightSummary({
    required this.nodeId,
    required this.night,
    required this.nTargets,
    required this.nObservations,
    required this.nSubmitted,
    required this.generatedAt,
    required this.receipt,
  });

  factory NightSummary.fromJson(Map<String, dynamic> j) => NightSummary(
        nodeId: _asStr(j['node_id']),
        night: _asStr(j['night']),
        nTargets: _asInt(j['n_targets']),
        nObservations: _asInt(j['n_observations']),
        nSubmitted: _asInt(j['n_submitted']),
        generatedAt: _asStr(j['generated_at']),
        receipt: (j['receipt'] is Map)
            ? Map<String, dynamic>.from(j['receipt'] as Map)
            : <String, dynamic>{},
      );

  bool get wasClear => nObservations > 0;
  String get receiptTitle => _asStr(receipt['title']);
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
