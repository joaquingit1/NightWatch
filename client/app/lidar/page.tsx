import type { Metadata } from "next";

import { LidarViewer } from "@/components/lidar/LidarViewer";

export const metadata: Metadata = {
  title: "Night Watch | Live Map",
  description: "Real-time LIDAR point cloud streamed from the Go2 scout.",
};

export default function LidarPage() {
  return (
    <main className="fixed inset-0 bg-[#04060c]">
      <LidarViewer />
    </main>
  );
}
