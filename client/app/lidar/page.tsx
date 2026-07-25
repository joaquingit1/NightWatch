import type { Metadata } from "next";

import { LidarViewer } from "@/components/lidar/LidarViewer";

export const metadata: Metadata = {
  title: "守夜犬 Night Watch | 三维雷达地图与 Bedroom 标定",
  description:
    "实时显示 Go2 激光雷达点云、累计地图和机器人位姿，并标定全空间唯一 Bedroom。",
};

export default async function LidarPage({
  searchParams,
}: {
  searchParams: Promise<{ embed?: string; lang?: string }>;
}) {
  const params = await searchParams;
  const initialLanguage = params.lang === "en" ? "en" : params.lang === "zh" ? "zh" : undefined;
  return (
    <main className="fixed inset-0 bg-[#04060c]">
      <LidarViewer
        embedded={params.embed === "1"}
        initialLanguage={initialLanguage}
      />
    </main>
  );
}
