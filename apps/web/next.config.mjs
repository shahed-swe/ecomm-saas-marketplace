/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  poweredByHeader: false,
  images: { formats: ["image/avif", "image/webp"] },
};
export default nextConfig;
