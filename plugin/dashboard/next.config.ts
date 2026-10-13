import type { NextConfig } from "next";
import path from "path";

// Pin Turbopack's workspace root to this dashboard directory. When the
// plugin is installed via npx (e.g. ~/.npm/_npx/<hash>/node_modules/...),
// Next would otherwise detect the npx parent's package-lock.json as the
// workspace root and panic in `next build` ("Failed to write app endpoint
// /page"). Pinning the root avoids that misdetection.
const nextConfig: NextConfig = {
  experimental: process.env.CI ? {} : { cpus: 4 },
  turbopack: {
    root: path.resolve(__dirname),
  },
};

export default nextConfig;
