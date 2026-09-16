// platform route group: its own layout and auth boundary (never shared with other groups).
export default function Layout({ children }: { children: React.ReactNode }) {
  return <div data-surface="platform" className="min-h-screen bg-bg">{children}</div>;
}
