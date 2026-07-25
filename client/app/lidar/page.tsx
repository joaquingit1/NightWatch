import type { Metadata } from "next";

import { LidarViewer } from "@/components/lidar/LidarViewer";

export const metadata: Metadata = {
  title: "守夜犬 Night Watch | 三维雷达地图与 Bedroom 标定",
  description:
    "实时显示 Go2 激光雷达点云、累计地图和机器人位姿，并标定全空间唯一 Bedroom。",
};

export default function LidarPage() {
  return (
    <main className="fixed inset-0 bg-[#04060c]">
      <LidarViewer />
    </main>
  );
}
