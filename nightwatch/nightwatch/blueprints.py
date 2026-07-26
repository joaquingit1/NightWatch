"""Night Watch blueprints.

scout: floor-scouting stack for premapping the venue, tuned for a
minimum-spec MacBook (8-core M3, 16 GB: DimOS' documented minimum).

What differs from stock `unitree-go2-agentic`, and why (each verified
against source or docs, July 23):

1. Rerun vis module replaced (autoconnect keeps the newest duplicate) with a
   throttled config. Stock ships `world/color_image` UNTHROTTLED (14 Hz) into
   a never-drop pipeline (10k-message LCM queue, 128 MiB gRPC buffer), which
   produced 20+ s camera lag on this laptop. 5 Hz to the viewer is plenty;
   perception consumes the raw stream, not this one.
2. Lidar dropped from the viewer entirely (stock only HIDES it, still paying
   encode/ship costs). memory_limit 10% instead of 25% (shorter history
   replay when a viewer attaches, less GC thrash). TimePanel collapsed
   instead of hidden so a stale/paused timeline is VISIBLE.
3. SpatialMemory throttled (stock: 1 frame/s CLIP) and new_memory=False:
   stock DEFAULT WIPES the spatial DB (and thus tagged locations) on every
   restart, which would have destroyed the scout's work on the first crash.
4. Agent model on the OpenAgents gateway (kimi-k2.5; tool calling verified).
5. PersonFollow + Navigation containers subclassed: stock hardcodes DashScope
   Qwen VL (needs ALIBABA_API_KEY); ours uses Gemini (verified, see vl.py).
6. n_workers 10 (stock spatial=8, go2=10; 14 was oversized for 8 cores).

Manual override lives OUTSIDE the stack: dimos-viewer itself has built-in
keyboard teleop and click-to-navigate; `python -m nightwatch.teleop` is the
backup e-stop window (websocket to :3030/ws, no zenoh dependency).

Run: source robot.env && dimos run nightwatch.scout
After every stack restart: close ALL old viewer windows first (a stale
viewer on port 9876 silently prevents the new stack from opening one, and
old viewers never reconnect).
"""

import os
from pathlib import Path
import signal as _signal
import sys
import threading as _threading
import time as _time
from typing import Any

from dimos.agents.mcp.mcp_server import McpServer
from dimos.core.coordination.blueprints import autoconnect
from dimos.core.global_config import global_config
from dimos.core.transport import JpegLcmTransport
from dimos.mapping.costmapper import CostMapper
from dimos.mapping.pointclouds.occupancy import HeightCostConfig
from dimos.mapping.voxels import VoxelGridMapper
from dimos.msgs.sensor_msgs.Image import Image
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.perception.fiducial.marker_detection_stream_module import (
    MarkerDetectionStreamModule,
)
from dimos.perception.fiducial.marker_tf_module import MarkerTfModule
# Same import paths the rerun bridge itself uses (see bridge.py).
from dimos.protocol.pubsub.impl.lcmpubsub import LCM
from dimos.protocol.pubsub.impl.zenohpubsub import Zenoh
from dimos.robot.unitree.go2.blueprints.basic.unitree_go2_basic import (
    _convert_camera_info,
    _convert_global_map,
    _convert_navigation_costmap,
    _static_base_link,
)
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2 import unitree_go2
from dimos.robot.unitree.go2.connection import GO2Connection
from dimos.visualization.vis_module import vis_module
from nightwatch.agent import McpClient  # resilient: survives LLM errors
from nightwatch.connection import GO2Connection as NightwatchGO2Connection
from nightwatch.curiosity import CuriositySupervisor
from nightwatch.follow import NightwatchNavigation, NightwatchPersonFollow
from nightwatch.goto import GoToSkillContainer
from nightwatch.escort import EscortSkill  # uninterruptible escort to nearest sleeping area
from nightwatch.intervene import InterventionSkill  # suspicious protocol waves
from nightwatch.map_stream import MapStreamer  # live-map WebSocket for the booth /lidar page
from nightwatch.memory import (
    NightwatchMapRecorder,
    NightwatchRelocalization,
    NightwatchSpatialMemory,
)
from nightwatch.navigation import PatrollingModule, WavefrontFrontierExplorer
from nightwatch.persona import NIGHTWATCH_SYSTEM_PROMPT
from nightwatch.perceive import (
    PerceiveLoopSkill,
)  # via VisionService; stock crashes on MPS
from nightwatch.speak import SpeakSkill  # local `say` TTS; stock 404s + 5s stall
from nightwatch.unitree import UnitreeSkillContainer  # re-arms motion pre-move
from nightwatch.vision import VisionService  # single shared moondream (MPS-fixed)
from nightwatch.webchat import NightwatchWebInput
from nightwatch.world_model import NightwatchWorldModel

# The stack is commonly launched as ``.venv/bin/dimos`` without activating
# the venv.  Rerun locates ``dimos-viewer`` through PATH, so that launch style
# made the viewer silently fail even though it was installed beside Python.
_runtime_bin = str(Path(sys.executable).parent)
if _runtime_bin not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _runtime_bin + os.pathsep + os.environ.get("PATH", "")

# Forensics for the July 24 silent-shutdown bug: the coordinator's loop()
# swallows KeyboardInterrupt, so a stray SIGINT tears the stack down with
# zero log output. Log the delivery (with pid and time) before the default
# behavior proceeds, so a recurrence is attributable instead of invisible.


def _log_sigint(signum: int, frame: object) -> None:
    print(
        f"[nightwatch-forensics] SIGINT delivered pid={os.getpid()} "
        f"t={_time.time():.3f} main_thread={_threading.current_thread().name}",
        file=sys.stderr,
        flush=True,
    )
    raise KeyboardInterrupt


if _threading.current_thread() is _threading.main_thread():
    try:
        _signal.signal(_signal.SIGINT, _log_sigint)
    except ValueError:
        pass

# Real OpenAI via the openai-next relay (OPENAI_BASE_URL in robot.env).
# gpt-4o: verified 2.7s full tool round trip; kimi-k2.5 on the OpenAgents
# gateway took 40s+ of thinking per turn.
AGENT_MODEL = "openai:gpt-4o"

# Long frontier legs need the same time budget as known-area patrol legs.
# The supervisor independently detects a genuinely stalled robot from pose
# displacement, so a short fixed timeout only cancels healthy cross-room paths.
FRONTIER_GOAL_TIMEOUT_S = 75.0


def _scout_rerun_blueprint() -> Any:
    import rerun.blueprint as rrb

    return rrb.Blueprint(
        rrb.Horizontal(
            rrb.Spatial2DView(origin="world/color_image", name="Camera"),
            rrb.Spatial3DView(
                origin="world",
                name="3D",
                background=rrb.Background(kind="SolidColor", color=[0, 0, 0]),
            ),
            column_shares=[1, 2],
        ),
        # Collapsed (not hidden): a paused/stale timeline must be visible.
        rrb.TimePanel(state="collapsed"),
        rrb.SelectionPanel(state="hidden"),
    )


class _ScoutZenoh(Zenoh):
    """A picklable Zenoh for the rerun-bridge pubsub list.

    The bridge is a dedicated worker, so its constructor kwargs (this pubsubs
    list included) are pickled and shipped over the forkserver pipe. A bare
    dimos Zenoh() is NOT picklable: it references the process-wide session pool,
    which holds a threading.Lock, so putting one here aborts the deploy with
    "cannot pickle '_thread.lock' object" (verified 2026-07-24). Reconstruct a
    fresh peer-mode Zenoh in the worker instead; it opens its own session on
    start(), exactly what the transport-default path produced before. LCM()
    already survives pickling on its own, so only Zenoh needs this. isinstance
    checks against Zenoh still pass, so the bridge and _resolve_pubsubs treat it
    as an ordinary Zenoh backend.
    """

    def __reduce__(self):  # type: ignore[no-untyped-def]
        return (self.__class__, ())


# Bounds viewer ingest and render cost regardless of map size.
_MAX_VIEWER_CLOUD_POINTS = 150_000


def _convert_big_cloud(msg: Any) -> Any:
    """Subsample an oversized map cloud before conversion.

    The merged/global clouds grow with mapping coverage. Converting and logging
    a full-venue cloud at arrival rate starves the camera stream, which shares
    the viewer ingest queue (observed live July 24 as ever-growing camera
    latency). Cap the point count first, then hand off to the stock converter.
    Any failure falls back to the unmodified conversion.
    """
    try:
        points = msg.points_f32()
        if len(points) <= _MAX_VIEWER_CLOUD_POINTS:
            return _convert_global_map(msg)
        # Uniform stride keeps the cloud's spatial coverage while bounding size.
        stride = -(-len(points) // _MAX_VIEWER_CLOUD_POINTS)
        reduced = PointCloud2.from_numpy(
            points[::stride],
            frame_id=msg.frame_id,
            timestamp=msg.ts,
        )
        return _convert_global_map(reduced)
    except Exception:
        return _convert_global_map(msg)


def _convert_saved_map(msg: Any) -> Any:
    """Render the complete saved floor as a distinct Rerun reference layer.

    The stock global-map converter removes every point below ``z == 0``. That
    is reasonable for a single live mapper frame, but an exported venue map
    accumulates small floor-height drift across a long run. At the legacy
    venue the cutoff removed about 81% of the canonical premap, leaving a
    sparse remainder that looked like the map had not loaded. Keep all saved
    points and use small fixed-color markers so the reference stays distinct
    without obscuring the live height-colored map.
    """
    cloud = msg
    try:
        points = msg.points_f32()
        if len(points) > _MAX_VIEWER_CLOUD_POINTS:
            stride = -(-len(points) // _MAX_VIEWER_CLOUD_POINTS)
            cloud = PointCloud2.from_numpy(
                points[::stride],
                frame_id=msg.frame_id,
                timestamp=msg.ts,
            )
    except Exception:
        cloud = msg
    return cloud.to_rerun(
        voxel_size=0.025,
        colors=[72, 166, 255],
        mode="points",
        bottom_cutoff=None,
    )


def _convert_saved_map_preview(msg: Any) -> Any:
    """Render the unaligned saved MAP frame as an explicit amber fallback."""
    cloud = msg
    try:
        points = msg.points_f32()
        if len(points) > _MAX_VIEWER_CLOUD_POINTS:
            stride = -(-len(points) // _MAX_VIEWER_CLOUD_POINTS)
            cloud = PointCloud2.from_numpy(
                points[::stride],
                frame_id=msg.frame_id,
                timestamp=msg.ts,
            )
    except Exception:
        cloud = msg
    return cloud.to_rerun(
        voxel_size=0.02,
        colors=[245, 166, 35],
        mode="points",
        bottom_cutoff=None,
    )


_scout_rerun_config: dict[str, Any] = {
    "blueprint": _scout_rerun_blueprint,
    # 6% (about 1 GB) caps the viewer history buffer. 10% still tipped the
    # 16 GB machine into swap once real camera frames flowed to the viewer.
    "memory_limit": "6%",
    # The stack's global transport is zenoh, but color_image rides
    # JpegLcmTransport (see the .transports() override below). The rerun bridge
    # only listens on the backends passed here, so a zenoh-only bridge never
    # sees the LCM camera frames and the Camera panel stays empty. List both,
    # Zenoh first (the global transport) and LCM second (the camera). vis_module
    # does setdefault("pubsubs", [LCM()]), which does NOT override this explicit
    # key; and the bridge's _resolve_pubsubs only swaps out a lone legacy
    # [LCM()] default, so this two-element list is honored verbatim.
    "pubsubs": [_ScoutZenoh(), LCM()],
    "visual_override": {
        "world/camera_info": _convert_camera_info,
        "world/global_map": _convert_big_cloud,
        "world/merged_map": _convert_big_cloud,
        # The canonical MAP-frame stream feeds the web UI but is suppressed
        # here: without a MAP TF Rerun cannot place it under the WORLD view.
        "world/loaded_map": None,
        # This explicit WORLD-attached copy is an amber provisional fallback.
        # The relocalizer clears it as soon as the authoritative blue aligned
        # layer appears. It never enters navigation.
        "world/preview_loaded_map": _convert_saved_map_preview,
        "world/aligned_loaded_map": _convert_saved_map,
        "world/navigation_costmap": _convert_navigation_costmap,
        "world/lidar": None,  # drop before conversion; hiding still ships it
    },
    "max_hz": {
        # Five visual frames/s is fluid enough for teleoperation while the
        # bridge still drops intermediate frames instead of queueing them.
        # Was 10: once the camera actually reached the viewer (the LCM
        # pubsub fix), 10 Hz of decoded frames plus the default history
        # budget exhausted physical RAM in ~20 minutes on the 16 GB M3 and
        # the whole machine started swapping (observed live July 24, camera
        # latency spike at minute 22 of the session).
        "world/color_image": 5,
        # The merged/global clouds grow with coverage; unthrottled they starve
        # the camera stream in the viewer ingest queue (observed live July 24
        # as ever-growing camera latency), so cap their publish rate hard.
        "world/global_map": 1,
        "world/merged_map": 0.2,
        "world/global_costmap": 2,
        "world/preview_loaded_map": 0.5,
        # The saved premap changes only on relock; 1 frame every 2 s of a
        # static cloud is plenty and costs nothing at the viewer.
        "world/aligned_loaded_map": 0.5,
    },
    "static": {
        "world/tf/base_link": _static_base_link,
    },
}


scout = (
    autoconnect(
        unitree_go2,
        # same module name as stock -> dedupe replaces it; adds lidar switch-on.
        # Go2 fw 1.1.7+ boots into MCF. Preserve that controller (the live
        # robot rejects SelectMode("normal") with status 7004) and use MCF's
        # documented no-reply sport Move packets instead of joystick emulation.
        NightwatchGO2Connection.blueprint(
            motion_mode=None,
            velocity_api=True,
        ),
        # Keep DimensionalOS' 5 cm navigation resolution. The former 10 cm
        # shortcut plus one lidar update/s was too coarse and stale around
        # chair legs and narrow furniture. The connection bounds sensor load
        # before this mapper, so intermediate frames never form a backlog.
        # Unitree publishes a complete rolling 6.4 m voxel snapshot, not a raw
        # scan. Replace the overlapping window so people removed from a later
        # snapshot also leave the global map, while preserving history outside.
        VoxelGridMapper.blueprint(
            voxel_size=0.05,
            emit_every=1,
            replace_window=True,
        ),
        # A Go2 can deliberately step over the venue's low doorway strips.
        # Keep slope as a graded cost up to 18 cm; taller steps, drop edges,
        # chair legs, and walls remain lethal costs. The stock generic 15 cm
        # cutoff turned harmless threshold noise into an impassable wall.
        CostMapper.blueprint(
            config=HeightCostConfig(
                resolution=0.05,
                can_pass_under=0.60,
                can_climb=0.18,
                ignore_noise=0.04,
                smoothing=1.0,
            )
        ),
        # run_scout.sh exports the canonical premap after every session and
        # points NIGHTWATCH_PREMAP at it by default (NIGHTWATCH_PREMAP=off
        # opts out). CostMapper continues using the live map until ICP accepts
        # an alignment, then consumes the merged premap plus live map. Dynamic
        # local obstacles remain live while static floor knowledge survives
        # restarts. A missing or corrupt premap degrades to live-map-only.
        # Stock gives ICP 5 attempts at 30 s spacing, which expires ~2.5
        # minutes into a session while the live map still covers one corner.
        # More, slower attempts let coverage grow between tries. Historical
        # scores on this repetitive corridor clustered at 0.45-0.52; the live
        # 0.460 acceptance immediately erased a reachable frontier and produced
        # an unreachable (-17.9, -35.2) patrol goal. Require a clearly stronger
        # match. Until then navigation safely remains on the live map.
        NightwatchRelocalization.blueprint(
            map_file=os.environ.get("NIGHTWATCH_PREMAP") or None,
            fitness_threshold=0.55,
            max_failed_attempts=12,
            retry_interval_s=45.0,
            # Show the remembered floor in the viewer from second one, not
            # only after the first ICP lock.
            publish_loaded_map=True,
        ),
        # Replace the stock rolling-map completion and forgetful coverage
        # behavior. Novelty/failure regions and patrol visits now survive every
        # short follow, expression, and operator preemption.
        # min_frontier_perimeter 0.9 (stock 0.5): the scout skims. Sub-meter
        # map holes are not worth a dedicated trip; they get filled
        # incidentally while wandering. Give a progressing leg enough time to
        # reach a frontier across a room. The subclass wakes immediately on
        # planner failure, liveness recovery, and goals whose unknown area
        # lidar already resolved en route, so the timeout is only a final
        # backstop; making it shorter caused healthy long paths to be replaced
        # mid-stride and produced avoidable zig-zagging.
        WavefrontFrontierExplorer.blueprint(
            min_frontier_perimeter=0.9,
            goal_timeout=FRONTIER_GOAL_TIMEOUT_S,
        ),
        PatrollingModule.blueprint(),
        # listed after unitree_go2 so it replaces the stock unthrottled vis module
        vis_module(
            viewer_backend=global_config.viewer, rerun_config=_scout_rerun_config
        ),
        NightwatchSpatialMemory.blueprint(
            min_time_threshold=5.0,
            min_distance_threshold=0.75,
            new_memory=False,
            db_path=os.environ.get(
                "NIGHTWATCH_SPATIAL_DB_PATH",
                "assets/output/memory/spatial_memory/chromadb_data",
            ),
            visual_memory_path=os.environ.get(
                "NIGHTWATCH_VISUAL_MEMORY_PATH",
                "assets/output/memory/spatial_memory/visual_memory.pkl",
            ),
            output_dir=os.environ.get(
                "NIGHTWATCH_SPATIAL_OUTPUT_DIR",
                "assets/output/memory/spatial_memory",
            ),
        ),
        # Saves lidar + trajectory to a DimensionalOS memory2 database. Existing
        # sessions are backed up, so every run remains exportable/replayable.
        NightwatchMapRecorder.blueprint(
            backup_keep_last=20,
            db_path=os.environ.get(
                "NIGHTWATCH_MAP_DB_PATH",
                "assets/output/maps/nightwatch_map.db",
            ),
        ),
        # Streams world/global_map + odom over a local WebSocket (:8010) for
        # the booth FastAPI relay (/ws/lidar) and the /lidar 3D viewer.
        MapStreamer.blueprint(
            port=int(os.environ.get("NIGHTWATCH_MAP_WS_PORT", "8010")),
        ),
        # AprilTags are stable manual anchors (0=checkpoint, 1=rest, 2=sleep by
        # default). The world model also self-tags areas from persistent 3D
        # object evidence and stores sparse failure/keyframe experience.
        MarkerDetectionStreamModule.blueprint(
            marker_length_m=float(os.environ.get("NIGHTWATCH_MARKER_SIZE_M", "0.10")),
            camera_info=GO2Connection.camera_info_static,
            quality_window_s=0.5,
        ),
        MarkerTfModule.blueprint(),
        NightwatchWorldModel.blueprint(
            experience_db_path=os.environ.get(
                "NIGHTWATCH_EXPERIENCE_DB_PATH",
                "assets/output/memory/nightwatch_experience.db",
            ),
            world_db_path=os.environ.get(
                "NIGHTWATCH_WORLD_DB_PATH",
                "assets/output/memory/nightwatch_world.sqlite3",
            ),
        ),
        PerceiveLoopSkill.blueprint(),
        McpServer.blueprint(),
        McpClient.blueprint(model=AGENT_MODEL, system_prompt=NIGHTWATCH_SYSTEM_PROMPT),
        NightwatchNavigation.blueprint(),
        GoToSkillContainer.blueprint(camera_info=GO2Connection.camera_info_static),
        # Suspicious protocol: approach + verified waves + raised camera for the
        # facial pipeline. Holds an intervene lease (priority 40) while active.
        InterventionSkill.blueprint(camera_info=GO2Connection.camera_info_static),
        # Uninterruptible escort: holds an escort lease (priority 60) that
        # outranks intervene (40), follow, and curiosity until the person
        # reaches the nearest sleeping area. Shows as behavior=escort on the
        # operator dashboard.
        EscortSkill.blueprint(),
        NightwatchPersonFollow.blueprint(camera_info=GO2Connection.camera_info_static),
        UnitreeSkillContainer.blueprint(),
        NightwatchWebInput.blueprint(),
        SpeakSkill.blueprint(),
        # Always-on base behavior. It keeps exploring while the LLM thinks and
        # yields only to a real motion owner or an explicit/safety hold.
        # curious_follow_enabled=False for the venue night: constant close-person
        # follow triggers at a packed event kept cancelling long exploration
        # paths (the dog looked glued to home). Explicit follow_person commands
        # and potential_detected still work; only the automatic follow is off.
        # Mapping owns the mission until coverage converges. Scheduled tricks
        # cancel the active frontier leg, throw away planner progress, and
        # consume scarce battery; operator-requested expressions remain
        # available from the console.
        CuriositySupervisor.blueprint(
            curious_follow_enabled=False,
            dog_expressions_while_exploring=False,
        ),
        # one shared local moondream for goto/follow/lookout localization
        VisionService.blueprint(),
    )
    # Person follow is autonomous navigation, not raw robot control. Routing
    # it through MovementManager makes keyboard/web teleop an unconditional
    # override and prevents two independent velocity publishers from fighting.
    .remappings([(NightwatchPersonFollow, "cmd_vel", "nav_cmd_vel")])
    # Raw 1280x720 Image messages over Zenoh shared memory produced watchdog
    # overruns and intermittent LCM decode failures on macOS. The documented
    # Go2 smart transport is JPEG-in-LCM: bounded payloads, preserved capture
    # timestamps, and no shared-memory corruption.
    .transports(
        {
            ("color_image", Image): JpegLcmTransport("/color_image", Image),
        }
    )
    # Treat the 30 cm body as a 42 cm safety footprint. nerf_speed scales
    # BOTH cruise and the rotation cap in the local planner: 0.65 made every
    # in-place turn take 4-9 s and capped travel at 0.36 m/s, which is why a
    # 20-room venue took an hour per room. 0.9 gives ~0.50 m/s cruise and
    # ~0.50 rad/s turns, still below the dimos stock default (0.55) and far
    # below Go2 capability, with the 42 cm footprint doing the safety work.
    .global_config(
        n_workers=11,
        robot_model="unitree_go2",
        robot_width=0.42,
        robot_rotation_diameter=0.72,
        nerf_speed=0.9,
    )
)
