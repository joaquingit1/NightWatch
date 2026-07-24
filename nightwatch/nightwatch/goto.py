"""go_to_visible_object: navigate to anything the dog can currently see.

Why this exists: stock navigate_with_text's vision branch needs an
ObjectTracking module that zips RGB with a DEPTH stream; the Go2 WebRTC
connection has no depth, so on this robot that branch is permanently dead
(and BBoxNavigationModule publishes goals to a topic nothing consumes).

This skill needs no depth: local moondream (VisionService) finds the
object's bbox in the latest camera frame; the bottom edge is where it meets the floor,
so a flat-floor unprojection through the camera intrinsics gives distance
and bearing; the goal (with a standoff) is published as a PoseStamped on
the planner's goal_request channel (the same input BBoxNavigation uses), so the full
A*/replanning/obstacle-avoidance pipeline drives the motion.
"""

import math
import time
from typing import Any

from dimos.agents.annotation import skill
from dimos.agents.capabilities import CAP_MOVEMENT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.sensor_msgs.CameraInfo import CameraInfo
from dimos.msgs.sensor_msgs.Image import Image
from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec
from dimos.utils.logging_config import setup_logger
from nightwatch.unitree import ensure_motion_ready
from nightwatch.vision import VisionSpec

logger = setup_logger()


class GoToConfig(ModuleConfig):
    camera_info: CameraInfo
    camera_height_m: float = 0.33  # front camera above floor, standing
    standoff_m: float = 0.7  # stop this far short of the object
    max_goal_m: float = 8.0  # sanity cap on computed distance


class GoToSkillContainer(Module):
    config: GoToConfig

    _connection: GO2ConnectionSpec
    _vision: VisionSpec

    color_image: In[Image]
    odom: In[PoseStamped]
    goal_request: Out[PoseStamped]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._latest_image: Image | None = None
        self._latest_odom: PoseStamped | None = None

    @rpc
    def start(self) -> None:
        super().start()
        from reactivex.disposable import Disposable

        self.register_disposable(
            Disposable(self.color_image.subscribe(lambda m: setattr(self, "_latest_image", m)))
        )
        self.register_disposable(
            Disposable(self.odom.subscribe(lambda m: setattr(self, "_latest_odom", m)))
        )

    @skill(uses=[CAP_MOVEMENT])
    def go_to_visible_object(self, query: str) -> str:
        """Walk to an object or person CURRENTLY VISIBLE in the camera.

        Use this to approach anything you can see: "the water bottles",
        "the person in the red shirt", "the doorway". The robot plans a
        path with obstacle avoidance and stops just short of the target.

        Args:
            query: description of the visible object to approach

        Returns:
            Status message.
        """
        image = self._latest_image
        odom = self._latest_odom
        if image is None:
            return "No camera image available."
        if odom is None:
            return "No odometry available."

        # Local moondream detect (VisionService): tested tight pixel boxes.
        # OpenAI models hallucinate coordinates, so they never localize here.
        try:
            boxes = self._vision.moondream_detect(query)
        except Exception:
            logger.exception("vision service detect failed")
            return "My vision system had an error; try again in a moment."
        if not boxes:
            return f"I can't see '{query}' in the current camera view."
        # Several matches ("the water bottles") mean the whole cluster: take
        # the union so the goal centers on the group.
        x1 = min(b[0] for b in boxes)
        x2 = max(b[2] for b in boxes)
        y2 = max(b[3] for b in boxes)
        k = self.config.camera_info.K
        fx, fy, cx0, cy0 = k[0], k[4], k[2], k[5]

        u = (x1 + x2) / 2.0  # horizontal center of the object
        v = y2  # bottom edge = floor contact line
        dv = v - cy0
        if dv <= 5:
            return (
                f"'{query}' does not touch the floor in view (too far or "
                "elevated); I can't estimate a distance safely."
            )

        # Flat-floor pinhole geometry (camera looking forward).
        dist = self.config.camera_height_m * fy / dv
        if dist > self.config.max_goal_m:
            return f"'{query}' looks {dist:.1f} m away, beyond my safe range."
        lateral = (u - cx0) / fx * dist  # +right in camera frame

        d_goal = max(0.3, dist - self.config.standoff_m)
        scale = d_goal / dist
        fwd, right = d_goal, lateral * scale

        yaw = odom.orientation.euler.z
        gx = odom.position.x + fwd * math.cos(yaw) - (-right) * math.sin(yaw)
        gy = odom.position.y + fwd * math.sin(yaw) + (-right) * math.cos(yaw)

        heading = math.atan2(gy - odom.position.y, gx - odom.position.x)
        connection = getattr(self, "_connection", None)
        if connection is not None:
            # Firmware drops planner velocities unless the dog is standing in
            # BalanceStand with joystick listening on; re-arm before the goal.
            ensure_motion_ready(connection)
        self.goal_request.publish(
            PoseStamped(
                ts=time.time(),
                frame_id="map",
                position=Vector3(gx, gy, 0.0),
                orientation=Quaternion.from_euler(Vector3(0.0, 0.0, heading)),
            )
        )
        logger.info(
            "go_to_visible_object goal",
            query=query,
            dist=round(dist, 2),
            lateral=round(lateral, 2),
            goal=(round(gx, 2), round(gy, 2)),
        )
        return (
            f"Found '{query}' about {dist:.1f} m ahead. Navigating to it now "
            "(stopping short). Call stop_navigation to cancel."
        )
