from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from math import hypot
from pathlib import Path

import cv2

from common.attention import (
    AttentionCalibrator,
    AttentionGeometry,
    extract_attention_geometry,
)
from common.mediapipe_face import FaceLandmarkDetector
from common.metrics import (
    DrowsinessMonitor,
    FatigueState,
    FatigueThresholds,
    extract_face_metrics,
)
from common.visuals import (
    draw_landmark_subset,
    make_video_writer,
    parse_source,
)
from common.yolo_face import FaceBox, YOLOFaceDetector, padded_box

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_YOLO_MODEL = (
    PROJECT_ROOT / "02_yolov8face_mediapipe" / "models" / "yolov8n-face-lindevs.pt"
)
DEFAULT_LANDMARK_MODEL = (
    PROJECT_ROOT / "02_yolov8face_mediapipe" / "models" / "face_landmarker.task"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Open a computer camera and detect real-time fatigue using "
            "YOLOv8-Face, MediaPipe, EAR, MAR, PERCLOS, head pose and gaze."
        )
    )
    parser.add_argument(
        "--source",
        default="0",
        help="Camera index (0, 1, ...), video path, or stream URL. Default: 0.",
    )
    parser.add_argument("--width", type=int, default=1280, help="Requested camera width.")
    parser.add_argument("--height", type=int, default=720, help="Requested camera height.")
    parser.add_argument("--camera-fps", type=float, default=30.0)
    parser.add_argument(
        "--mirror",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mirror the image. Enabled by default; use --no-mirror to disable.",
    )
    parser.add_argument("--device", default="auto", help="auto, cpu, mps, 0, cuda:0, ...")
    parser.add_argument("--yolo-model", type=Path, default=DEFAULT_YOLO_MODEL)
    parser.add_argument("--landmark-model", type=Path, default=DEFAULT_LANDMARK_MODEL)
    parser.add_argument("--image-size", type=int, default=640)
    parser.add_argument("--detection-confidence", type=float, default=0.35)
    parser.add_argument(
        "--max-faces",
        type=int,
        default=20,
        help="Maximum number of faces to process in one frame.",
    )
    parser.add_argument(
        "--minimum-face-size",
        type=int,
        default=30,
        help="Ignore face boxes narrower or shorter than this pixel size.",
    )
    parser.add_argument("--face-padding", type=float, default=0.15)
    parser.add_argument("--ear-threshold", type=float, default=0.20)
    parser.add_argument("--mar-threshold", type=float, default=0.35)
    parser.add_argument("--eye-alarm-seconds", type=float, default=1.20)
    parser.add_argument("--yawn-alarm-seconds", type=float, default=1.50)
    parser.add_argument("--perclos-threshold", type=float, default=0.28)
    parser.add_argument("--perclos-window", type=float, default=30.0)
    parser.add_argument(
        "--calibration-frames",
        type=int,
        default=20,
        help="Usable frames per face for its neutral head-pose/gaze baseline.",
    )
    parser.add_argument("--head-yaw-threshold", type=float, default=25.0)
    parser.add_argument("--head-down-threshold", type=float, default=18.0)
    parser.add_argument("--gaze-horizontal-threshold", type=float, default=0.45)
    parser.add_argument("--gaze-vertical-threshold", type=float, default=0.45)
    parser.add_argument(
        "--threshold-config",
        type=Path,
        help=(
            "Optional JSON produced by "
            "02_yolov8face_mediapipe/tune_uta_thresholds.py. Its tuned "
            "instantaneous EAR/MAR/head/gaze thresholds override CLI defaults."
        ),
    )
    parser.add_argument("--attention-alarm-seconds", type=float, default=2.0)
    parser.add_argument("--head-down-alarm-seconds", type=float, default=1.5)
    parser.add_argument("--nod-angle", type=float, default=12.0)
    parser.add_argument("--nod-alarm-count", type=int, default=2)
    parser.add_argument("--output", type=Path, help="Optional annotated MP4 output.")
    parser.add_argument(
        "--no-display",
        action="store_true",
        help="Run without an OpenCV window. Useful for automated tests.",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="Stop after N frames. Zero means run until the source ends or q is pressed.",
    )
    return parser


def apply_threshold_config(args: argparse.Namespace) -> None:
    if args.threshold_config is None:
        return
    payload = json.loads(args.threshold_config.read_text(encoding="utf-8"))
    parameters = payload.get("best_parameters")
    if not isinstance(parameters, dict):
        raise ValueError(
            f"No best_parameters object in {args.threshold_config}"
        )
    mapping = {
        "ear_closed": "ear_threshold",
        "mar_yawn": "mar_threshold",
        "head_down_delta": "head_down_threshold",
        "head_yaw_delta": "head_yaw_threshold",
        "gaze_horizontal_delta": "gaze_horizontal_threshold",
        "gaze_vertical_delta": "gaze_vertical_threshold",
    }
    for source_name, argument_name in mapping.items():
        if source_name in parameters:
            setattr(args, argument_name, float(parameters[source_name]))


def box_iou(first: FaceBox, second: FaceBox) -> float:
    intersection_width = max(0, min(first.x2, second.x2) - max(first.x1, second.x1))
    intersection_height = max(0, min(first.y2, second.y2) - max(first.y1, second.y1))
    intersection = intersection_width * intersection_height
    union = first.area + second.area - intersection
    return intersection / union if union > 0 else 0.0


def normalized_center_distance(first: FaceBox, second: FaceBox) -> float:
    first_center = ((first.x1 + first.x2) / 2, (first.y1 + first.y2) / 2)
    second_center = ((second.x1 + second.x2) / 2, (second.y1 + second.y2) / 2)
    distance = hypot(
        first_center[0] - second_center[0],
        first_center[1] - second_center[1],
    )
    scale = max(
        first.x2 - first.x1,
        first.y2 - first.y1,
        second.x2 - second.x1,
        second.y2 - second.y1,
        1,
    )
    return distance / scale


@dataclass
class FaceTrack:
    track_id: int
    box: FaceBox
    monitor: DrowsinessMonitor
    attention_calibrator: AttentionCalibrator
    last_seen_timestamp: float
    last_landmark_timestamp: float | None = None
    state: FatigueState | None = None
    direct_look_frames: int = 0
    away_frames: int = 0
    looking_at_camera: bool = False


class MultiFaceTracker:
    """Associate face boxes between frames and keep one fatigue history per person."""

    def __init__(
        self,
        thresholds: FatigueThresholds,
        *,
        maximum_missing_seconds: float = 1.5,
        calibration_frames: int = 20,
    ) -> None:
        self.thresholds = thresholds
        self.maximum_missing_seconds = maximum_missing_seconds
        self.calibration_frames = calibration_frames
        self.tracks: dict[int, FaceTrack] = {}
        self.next_track_id = 1

    def update(
        self, detections: list[FaceBox], timestamp: float
    ) -> list[tuple[FaceTrack, FaceBox]]:
        candidates: list[tuple[float, int, int]] = []
        for track_id, track in self.tracks.items():
            for detection_index, detection in enumerate(detections):
                overlap = box_iou(track.box, detection)
                center_distance = normalized_center_distance(track.box, detection)
                if overlap >= 0.05 or center_distance <= 1.0:
                    score = overlap + 0.25 * max(0.0, 1.0 - center_distance)
                    candidates.append((score, track_id, detection_index))

        assigned_tracks: set[int] = set()
        assigned_detections: set[int] = set()
        assignments: dict[int, FaceTrack] = {}
        for _, track_id, detection_index in sorted(candidates, reverse=True):
            if track_id in assigned_tracks or detection_index in assigned_detections:
                continue
            track = self.tracks[track_id]
            detection = detections[detection_index]
            track.box = detection
            track.last_seen_timestamp = timestamp
            assigned_tracks.add(track_id)
            assigned_detections.add(detection_index)
            assignments[detection_index] = track

        for detection_index, detection in enumerate(detections):
            if detection_index in assigned_detections:
                continue
            track = FaceTrack(
                track_id=self.next_track_id,
                box=detection,
                monitor=DrowsinessMonitor(self.thresholds),
                attention_calibrator=AttentionCalibrator(self.calibration_frames),
                last_seen_timestamp=timestamp,
            )
            self.tracks[track.track_id] = track
            assignments[detection_index] = track
            self.next_track_id += 1

        stale_ids = [
            track_id
            for track_id, track in self.tracks.items()
            if timestamp - track.last_seen_timestamp > self.maximum_missing_seconds
        ]
        for track_id in stale_ids:
            del self.tracks[track_id]

        return [
            (assignments[index], detection)
            for index, detection in enumerate(detections)
        ]


def put_label(
    frame,
    text: str,
    origin: tuple[int, int],
    color: tuple[int, int, int],
    *,
    scale: float = 0.52,
) -> None:
    x, y = origin
    (width, height), baseline = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1
    )
    x = max(0, min(frame.shape[1] - width - 4, x))
    y = max(height + 4, min(frame.shape[0] - baseline - 2, y))
    cv2.rectangle(
        frame,
        (x - 2, y - height - 3),
        (x + width + 2, y + baseline + 2),
        (20, 20, 20),
        -1,
    )
    cv2.putText(
        frame,
        text,
        (x, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color,
        1,
        cv2.LINE_AA,
    )


class CameraFatigueAnalyzer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.face_detector = YOLOFaceDetector(
            args.yolo_model,
            confidence=args.detection_confidence,
            image_size=args.image_size,
            device=args.device,
        )
        self.landmark_detector = FaceLandmarkDetector(args.landmark_model)
        thresholds = FatigueThresholds(
            ear_closed=args.ear_threshold,
            mar_yawn=args.mar_threshold,
            eye_alarm_seconds=args.eye_alarm_seconds,
            yawn_alarm_seconds=args.yawn_alarm_seconds,
            perclos_window_seconds=args.perclos_window,
            perclos_alarm=args.perclos_threshold,
            head_yaw_away_degrees=args.head_yaw_threshold,
            head_down_pitch_degrees=args.head_down_threshold,
            gaze_horizontal_away=args.gaze_horizontal_threshold,
            gaze_vertical_away=args.gaze_vertical_threshold,
            attention_alarm_seconds=args.attention_alarm_seconds,
            head_down_alarm_seconds=args.head_down_alarm_seconds,
            nod_pitch_degrees=args.nod_angle,
            nod_alarm_count=args.nod_alarm_count,
        )
        self.tracker = MultiFaceTracker(
            thresholds,
            calibration_frames=args.calibration_frames,
        )
        self.ear_threshold = args.ear_threshold
        self.face_padding = args.face_padding
        self.max_faces = args.max_faces
        self.minimum_face_size = args.minimum_face_size

    def close(self) -> None:
        self.landmark_detector.close()

    def analyze(
        self, frame, timestamp: float
    ) -> list[tuple[int, FatigueState | None]]:
        boxes = [
            box
            for box in self.face_detector.detect(frame)
            if box.x2 - box.x1 >= self.minimum_face_size
            and box.y2 - box.y1 >= self.minimum_face_size
        ]
        boxes = sorted(boxes, key=lambda candidate: candidate.confidence, reverse=True)[
            : self.max_faces
        ]
        tracked_faces = self.tracker.update(boxes, timestamp)
        results: list[tuple[int, FatigueState | None]] = []

        for track, detected_box in tracked_faces:
            crop_box = padded_box(detected_box, frame.shape, self.face_padding)
            crop = frame[crop_box.y1 : crop_box.y2, crop_box.x1 : crop_box.x2]
            landmarks = self.landmark_detector.detect(crop)
            state = track.state
            if landmarks is not None:
                if (
                    track.last_landmark_timestamp is not None
                    and timestamp - track.last_landmark_timestamp > 1.0
                ):
                    track.monitor.reset()
                crop_height, crop_width = crop.shape[:2]
                ear, mar = extract_face_metrics(landmarks, (crop_width, crop_height))
                geometry = extract_attention_geometry(
                    landmarks,
                    (crop_width, crop_height),
                )
                gaze_is_reliable = ear > self.ear_threshold * 1.10
                if geometry is not None and not gaze_is_reliable:
                    geometry = AttentionGeometry(
                        pitch=geometry.pitch,
                        yaw=geometry.yaw,
                        roll=geometry.roll,
                        gaze_horizontal=None,
                        gaze_vertical=None,
                    )
                relative_geometry = (
                    track.attention_calibrator.update(geometry)
                    if geometry is not None
                    else None
                )
                state = track.monitor.update(
                    timestamp,
                    ear,
                    mar,
                    head_pitch=(
                        relative_geometry.pitch
                        if relative_geometry is not None
                        else None
                    ),
                    head_yaw=(
                        relative_geometry.yaw
                        if relative_geometry is not None
                        else None
                    ),
                    head_roll=(
                        relative_geometry.roll
                        if relative_geometry is not None
                        else None
                    ),
                    gaze_horizontal=(
                        relative_geometry.gaze_horizontal
                        if relative_geometry is not None
                        else None
                    ),
                    gaze_vertical=(
                        relative_geometry.gaze_vertical
                        if relative_geometry is not None
                        else None
                    ),
                )
                track.state = state
                track.last_landmark_timestamp = timestamp
                draw_landmark_subset(
                    frame,
                    landmarks,
                    offset=(crop_box.x1, crop_box.y1),
                    source_size=(crop_width, crop_height),
                )

            calibrating = not track.attention_calibrator.ready
            if state is not None and state.drowsy:
                status, color = "DROWSY", (40, 40, 240)
            elif state is not None and state.inattentive:
                status, color = "INATTENTIVE", (0, 165, 255)
            elif calibrating:
                status, color = "CALIBRATING", (220, 190, 40)
            else:
                status, color = "ALERT", (50, 210, 70)
            cv2.rectangle(
                frame,
                (detected_box.x1, detected_box.y1),
                (detected_box.x2, detected_box.y2),
                color,
                2,
            )
            score = state.fatigue_score if state is not None else 0.0
            label_y = detected_box.y1 - 7 if detected_box.y1 >= 28 else detected_box.y1 + 20
            put_label(
                frame,
                f"ID {track.track_id} {status} {score:.0f}",
                (detected_box.x1, label_y),
                color,
            )
            if state is not None:
                if detected_box.y2 + 38 <= frame.shape[0] - 36:
                    metrics_y = detected_box.y2 + 18
                    pose_y = detected_box.y2 + 36
                else:
                    metrics_y = max(20, detected_box.y2 - 22)
                    pose_y = max(20, detected_box.y2 - 4)
                put_label(
                    frame,
                    f"EAR {state.ear:.2f} MAR {state.mar:.2f} P {state.perclos:.0%}",
                    (detected_box.x1, metrics_y),
                    color,
                    scale=0.43,
                )
                if calibrating:
                    attention_text = (
                        f"Pose/gaze calibration "
                        f"{track.attention_calibrator.progress:.0%}"
                    )
                else:
                    pitch_text = (
                        f"{state.head_pitch:+.0f}"
                        if state.head_pitch is not None
                        else "NA"
                    )
                    yaw_text = (
                        f"{state.head_yaw:+.0f}"
                        if state.head_yaw is not None
                        else "NA"
                    )
                    gaze_horizontal_text = (
                        f"{state.gaze_horizontal:+.2f}"
                        if state.gaze_horizontal is not None
                        else "NA"
                    )
                    gaze_vertical_text = (
                        f"{state.gaze_vertical:+.2f}"
                        if state.gaze_vertical is not None
                        else "NA"
                    )
                    attention_text = (
                        f"Pitch {pitch_text} Yaw {yaw_text} "
                        f"Gaze {gaze_horizontal_text}/{gaze_vertical_text} "
                        f"Nods {state.recent_nod_count}"
                    )
                put_label(
                    frame,
                    attention_text,
                    (detected_box.x1, pose_y),
                    color,
                    scale=0.39,
                )
            results.append((track.track_id, state))

        return results


def main() -> int:
    args = build_parser().parse_args()
    apply_threshold_config(args)
    source = parse_source(args.source)
    is_camera = isinstance(source, int)
    capture = cv2.VideoCapture(source)
    if is_camera:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        capture.set(cv2.CAP_PROP_FPS, args.camera_fps)
    if not capture.isOpened():
        raise RuntimeError(
            f"Unable to open source {args.source!r}. For a macOS camera, grant camera "
            "permission to the terminal application in System Settings > Privacy & Security."
        )

    source_fps = capture.get(cv2.CAP_PROP_FPS)
    if not source_fps or source_fps != source_fps:
        source_fps = args.camera_fps

    analyzer = CameraFatigueAnalyzer(args)
    writer = None
    started = time.perf_counter()
    frame_count = 0
    last_results: list[tuple[int, FatigueState | None]] = []
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if args.mirror:
                frame = cv2.flip(frame, 1)

            timestamp = (
                time.perf_counter() - started
                if is_camera
                else frame_count / max(source_fps, 1.0)
            )
            last_results = analyzer.analyze(frame, timestamp)

            elapsed = max(1e-6, time.perf_counter() - started)
            processing_fps = (frame_count + 1) / elapsed
            drowsy_count = sum(
                state is not None and state.drowsy
                for _, state in last_results
            )
            inattentive_count = sum(
                state is not None and state.inattentive and not state.drowsy
                for _, state in last_results
            )
            cv2.putText(
                frame,
                (
                    f"Faces {len(last_results)} Drowsy {drowsy_count} "
                    f"Away {inattentive_count} | "
                    f"FPS {processing_fps:.1f} | q/Esc"
                ),
                (16, frame.shape[0] - 18),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

            if args.output:
                if writer is None:
                    height, width = frame.shape[:2]
                    writer = make_video_writer(args.output, source_fps, (width, height))
                writer.write(frame)

            if not args.no_display:
                cv2.imshow("Real-time camera fatigue detection", frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break

            frame_count += 1
            if args.max_frames and frame_count >= args.max_frames:
                break
    finally:
        capture.release()
        analyzer.close()
        if writer is not None:
            writer.release()
        if not args.no_display:
            cv2.destroyAllWindows()

    elapsed = max(1e-6, time.perf_counter() - started)
    print(
        json.dumps(
            {
                "source": args.source,
                "threshold_config": (
                    str(args.threshold_config.resolve())
                    if args.threshold_config is not None
                    else None
                ),
                "frames": frame_count,
                "average_processing_fps": frame_count / elapsed,
                "people": [
                    {
                        "track_id": track_id,
                        "state": state.to_dict() if state is not None else None,
                    }
                    for track_id, state in last_results
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
