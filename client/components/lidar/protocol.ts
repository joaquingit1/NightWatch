// Wire protocol shared with nightwatch/nightwatch/map_stream.py and
// scripts/fake_map_stream.py. Binary frames are little-endian:
//   offset 0   uint32   magic: 0x4E575043 ("NWPC") accumulated global map
//                              0x4E57534E ("NWSN") live lidar scan
//                              0x4E57504D ("NWPM") saved premap
//   offset 4   uint32   seq (per-stream counter)
//   offset 8   float64  timestamp_s
//   offset 16  uint32   point_count N
//   offset 20  float32  x, y, z interleaved (N * 3 floats, Z-up world frame)

export const CLOUD_MAGIC = 0x4e575043;
export const SCAN_MAGIC = 0x4e57534e;
export const PREMAP_MAGIC = 0x4e57504d;
export const HEADER_BYTES = 20;

export type CloudKind = "map" | "scan" | "premap";

export interface CloudFrame {
  kind: CloudKind;
  seq: number;
  timestamp: number;
  count: number;
  /** View into the message buffer: x, y, z interleaved, dimos world frame (Z-up). */
  positions: Float32Array;
}

export interface PoseMessage {
  type: "pose";
  x: number;
  y: number;
  z: number;
  yaw: number;
  t: number;
}

export interface StatusMessage {
  type: "status";
  connected: boolean;
  retry_s?: number;
}

export interface PremapMessage {
  type: "premap";
  aligned: boolean;
}

export type TextMessage = PoseMessage | StatusMessage | PremapMessage;

export function parseCloudFrame(buffer: ArrayBuffer): CloudFrame | null {
  if (buffer.byteLength < HEADER_BYTES) return null;
  const view = new DataView(buffer);
  const magic = view.getUint32(0, true);
  const kind: CloudKind | null =
    magic === CLOUD_MAGIC
      ? "map"
      : magic === SCAN_MAGIC
        ? "scan"
        : magic === PREMAP_MAGIC
          ? "premap"
          : null;
  if (kind === null) return null;
  const seq = view.getUint32(4, true);
  const timestamp = view.getFloat64(8, true);
  const count = view.getUint32(16, true);
  if (buffer.byteLength !== HEADER_BYTES + count * 12) return null;
  return {
    kind,
    seq,
    timestamp,
    count,
    positions: new Float32Array(buffer, HEADER_BYTES, count * 3),
  };
}

export function parseTextMessage(raw: string): TextMessage | null {
  try {
    const msg = JSON.parse(raw);
    if (
      msg &&
      (msg.type === "pose" || msg.type === "status" || msg.type === "premap")
    ) {
      return msg as TextMessage;
    }
  } catch {
    // fall through: malformed frames are dropped
  }
  return null;
}
