import type { NextConfig } from "next";

const apiBase = process.env.API_BASE_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiBase}/api/:path*` },
      { source: "/video_feed/:path*", destination: `${apiBase}/video_feed/:path*` },
      { source: "/text_stream/:path*", destination: `${apiBase}/text_stream/:path*` },
    ];
  },
};

export default nextConfig;
