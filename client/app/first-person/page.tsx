import type { Metadata } from "next";

import { FirstPersonViewer } from "@/components/FirstPersonViewer";

export const metadata: Metadata = {
  title: "Night Watch | First Person View",
  description: "Full-screen live view from the Night Watch robot camera.",
};

export default function FirstPersonPage() {
  return <FirstPersonViewer />;
}
