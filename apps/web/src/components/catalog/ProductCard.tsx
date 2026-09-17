import Link from "next/link";
import { taka } from "@/lib/money";
import { ProductImage } from "./ProductImage";

export type Card = {
  id: string; slug: string; title_en: string; title_bn?: string | null; min_price?: string | null;
  max_price?: string | null; in_stock: boolean; vendor_name: string; image?: Record<string, Record<string, string>> | null;
  blur_data?: string | null;
};

export function ProductCard({ p, priority = false }: { p: Card; priority?: boolean }) {
  return (
    <Link href={`/p/${p.slug}`} className="group block">
      <ProductImage renditions={p.image} blur={p.blur_data} alt={p.title_en} priority={priority}
                    sizes="(min-width: 1024px) 25vw, 50vw" />
      <div className="mt-2 space-y-0.5">
        <p className="line-clamp-2 text-sm text-fg group-hover:underline">{p.title_en}</p>
        <p className="text-xs text-muted">{p.vendor_name}</p>
        <p className="font-semibold text-fg">
          {taka(p.min_price)}{p.max_price && p.max_price !== p.min_price ? ` – ${taka(p.max_price)}` : ""}
        </p>
        {!p.in_stock && <p className="text-xs text-danger">Out of stock</p>}
      </div>
    </Link>
  );
}
