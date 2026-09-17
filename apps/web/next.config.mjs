/** @type {import('next').NextConfig} */
const API = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const nextConfig = {
  output: "standalone",
  poweredByHeader: false,
  images: { formats: ["image/avif", "image/webp"] },
  // In production Caddy routes /api/v1 and /media straight to the API; this keeps dev identical.
  async rewrites() {
    return [
      { source: "/api/v1/:path*", destination: `${API}/api/v1/:path*` },
      { source: "/media/:path*", destination: `${API}/media/:path*` },
    ];
  },
  async headers() {
    return [{ source: "/admin/:path*", headers: [{ key: "X-Frame-Options", value: "DENY" }, { key: "Cache-Control", value: "no-store" }] }];
  },
};
export default nextConfig;
