import type { Metadata } from "next";
import "@/styles/globals.css";

export const metadata: Metadata = { title: "Marketplace", description: "Multi-vendor marketplace" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
