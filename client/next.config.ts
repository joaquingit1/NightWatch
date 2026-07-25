import type { NextConfig } from "next";

const apiBase = process.env.API_BASE_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // The public questionnaire is served through the Tencent host's reverse
  // proxy, so /_next/* assets arrive with that Host header. Next 15.5 flags
  // (and Next 16 blocks) cross-origin dev-asset requests unless listed.
  allowedDevOrigins: ["82.157.96.225", "*.local"],
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiBase}/api/:path*` },
      { source: "/video_feed/:path*", destination: `${apiBase}/video_feed/:path*` },
      { source: "/text_stream/:path*", destination: `${apiBase}/text_stream/:path*` },
    ];
  },
};

export default nextConfig;
