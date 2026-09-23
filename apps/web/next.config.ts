import type { NextConfig } from "next";

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  // Large source uploads stream through the /api/ezra proxy route handler.
  experimental: { serverActions: { bodySizeLimit: "4gb" } },
};

export default config;
