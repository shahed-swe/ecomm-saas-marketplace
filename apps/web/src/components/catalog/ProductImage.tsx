// AVIF -> WebP -> JPEG with responsive widths and a blur placeholder in a reserved box (CLS 0).
type Renditions = { avif?: Record<string, string>; webp?: Record<string, string>; jpeg?: Record<string, string> };

function srcset(map?: Record<string, string>) {
  return map ? Object.entries(map).sort((a, b) => +a[0] - +b[0]).map(([w, u]) => `${u} ${w}w`).join(", ") : undefined;
}

export function ProductImage({ renditions, blur, alt, sizes, width = 4, height = 5, priority = false }: {
  renditions?: Renditions | null; blur?: string | null; alt: string; sizes: string;
  width?: number; height?: number; priority?: boolean;
}) {
  const fallback = renditions?.jpeg ? Object.values(renditions.jpeg)[0] : undefined;
  return (
    <div className="relative overflow-hidden rounded-theme bg-surface" style={{ aspectRatio: `${width} / ${height}` }}>
      {blur && <img src={blur} alt="" aria-hidden className="absolute inset-0 h-full w-full scale-110 object-cover blur-lg" />}
      {fallback && (
        <picture>
          <source type="image/avif" srcSet={srcset(renditions?.avif)} sizes={sizes} />
          <source type="image/webp" srcSet={srcset(renditions?.webp)} sizes={sizes} />
          <img src={fallback} alt={alt} loading={priority ? "eager" : "lazy"} fetchPriority={priority ? "high" : "auto"}
               decoding="async" className="absolute inset-0 h-full w-full object-cover" />
        </picture>
      )}
    </div>
  );
}
