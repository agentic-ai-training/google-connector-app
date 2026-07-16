import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  turbopack: {
    root: process.cwd(),
  },
  async rewrites() {
    const backend = process.env.BACKEND_API_URL;
    if (!backend) return [];
    return [{source: "/api/:path*", destination: `${backend}/:path*`}];
  },
};

export default nextConfig;
