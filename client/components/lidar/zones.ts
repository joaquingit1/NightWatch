/**
 * keep_in / keep_out constrain where the robot may walk. "sleeping" declares
 * an escort destination: the robot's world model turns the polygon centroid
 * into a navigable sleeping area, and navigation ignores it as a constraint.
 */
export type ZoneKind = "keep_in" | "keep_out" | "sleeping";
export type MapPoint = [number, number];

export interface MapZone {
  id: string;
  name: string;
  kind: ZoneKind;
  points_frame?: "map" | "world";
  /** Persistent MAP-frame polygon. */
  points: MapPoint[];
  /** Immediate current-session WORLD-frame fallback. */
  world_points: MapPoint[];
  /** Odometry epoch the world_points snapshot was captured in. */
  world_epoch?: string | null;
  /**
   * Server-computed: true when world_points belongs to the running robot
   * session, false when it provably belongs to a previous session, null/
   * undefined when unknowable (legacy entry).
   */
  world_frame_valid?: boolean | null;
  created_at: number;
  active: boolean;
}

export interface WorldToMap {
  x: number;
  y: number;
  yaw: number;
}

export function worldToMapPoint(
  point: MapPoint,
  transform: WorldToMap | null
): MapPoint {
  if (!transform) return point;
  const cos = Math.cos(transform.yaw);
  const sin = Math.sin(transform.yaw);
  return [
    cos * point[0] - sin * point[1] + transform.x,
    sin * point[0] + cos * point[1] + transform.y,
  ];
}

export function mapToWorldPoint(
  point: MapPoint,
  transform: WorldToMap | null
): MapPoint {
  if (!transform) return point;
  const dx = point[0] - transform.x;
  const dy = point[1] - transform.y;
  const cos = Math.cos(transform.yaw);
  const sin = Math.sin(transform.yaw);
  return [cos * dx + sin * dy, -sin * dx + cos * dy];
}

/**
 * Where the zone sits in the CURRENTLY DISPLAYED world frame, or [] when its
 * frame cannot be validated for this session (never draw a saved zone at a
 * guessed position; the polygon reappears as soon as a live transform arrives
 * or the robot re-anchors and revalidates its world snapshot).
 */
export function zoneWorldPoints(
  zone: MapZone,
  transform: WorldToMap | null
): MapPoint[] {
  if (zone.points_frame === "world") {
    // A world drawing is only placeable while its snapshot provably belongs
    // to the session being displayed. Legacy entries (undefined) keep their
    // historical behavior.
    return zone.world_frame_valid === false ? [] : zone.world_points;
  }
  if (transform) {
    return zone.points.map((point) => mapToWorldPoint(point, transform));
  }
  // MAP-frame polygon with no transform: the stored snapshot is trustworthy
  // only when the server confirmed it was refreshed in the current session.
  return zone.world_frame_valid === true ? zone.world_points : [];
}
