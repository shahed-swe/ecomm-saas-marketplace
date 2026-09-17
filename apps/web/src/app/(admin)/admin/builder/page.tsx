"use client";
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from "@dnd-kit/core";
import { SortableContext, arrayMove, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SchemaForm, defaultsFromSchema } from "@/components/builder/SchemaForm";
import { ApiError, api } from "@/lib/client-api";

type Section = { id: string; type: string; hidden: boolean; settings: Record<string, any> };
type SectionType = { type: string; pages: string[]; max_per_page: number; settings_schema: any };
type Doc = { templates: Record<string, any>; pages: any[]; [k: string]: any };
type Target = { kind: "template"; key: "home" | "category_top" | "product_bottom"; page: string } | { kind: "page"; index: number };

const TEMPLATES = [
  { key: "home", page: "home", label: "Home page" },
  { key: "category_top", page: "category", label: "Category pages (top)" },
  { key: "product_bottom", page: "product", label: "Product pages (bottom)" },
] as const;

// crypto.randomUUID needs a secure context; getRandomValues works everywhere.
const newId = () => Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b.toString(16).padStart(2, "0")).join("");

function Row({ s, selected, onSelect, onToggle, onRemove }: { s: Section; selected: boolean; onSelect: () => void; onToggle: () => void; onRemove: () => void }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: s.id });
  return (
    <li ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition }}
        className={`flex items-center gap-2 rounded-theme border px-2 py-2 text-sm ${selected ? "border-primary" : "border-border"} ${isDragging ? "opacity-60" : ""} bg-bg`}>
      <button {...attributes} {...listeners} aria-label={`Drag ${s.type}`} className="cursor-grab px-1 text-muted">⋮⋮</button>
      <button onClick={onSelect} className={`flex-1 text-left ${s.hidden ? "text-muted line-through" : ""}`}>{s.type.replace(/_/g, " ")}</button>
      <button onClick={onToggle} className="text-xs text-muted" aria-label={s.hidden ? "Show" : "Hide"}>{s.hidden ? "show" : "hide"}</button>
      <button onClick={onRemove} className="text-xs text-danger" aria-label="Delete">×</button>
    </li>
  );
}

export default function Builder() {
  const router = useRouter();
  const [doc, setDoc] = useState<Doc | null>(null);
  const [types, setTypes] = useState<SectionType[]>([]);
  const [target, setTarget] = useState<Target>({ kind: "template", key: "home", page: "home" });
  const [selected, setSelected] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [previewUrl, setPreviewUrl] = useState("");
  const [device, setDevice] = useState<"desktop" | "mobile">("desktop");
  const [versions, setVersions] = useState<any[]>([]);
  const dirty = useRef(false);
  const sensors = useSensors(useSensor(PointerSensor), useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }));

  const load = useCallback(async () => {
    try {
      const [theme, reg, vs, tok] = await Promise.all([
        api<{ draft: Doc }>("/admin/theme"), api<SectionType[]>("/theme/sections"),
        api<any[]>("/admin/theme/versions"), api<{ token: string }>("/admin/theme/preview-token", { method: "POST" })]);
      setDoc(theme.draft); setTypes(reg); setVersions(vs);
      setPreviewUrl(`/?preview=${encodeURIComponent(tok.token)}`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) router.push("/admin/login"); else setStatus("Could not load the theme");
    }
  }, [router]);
  useEffect(() => { load(); }, [load]);

  const sections: Section[] = useMemo(() => {
    if (!doc) return [];
    return target.kind === "template" ? doc.templates[target.key] : doc.pages[target.index]?.sections ?? [];
  }, [doc, target]);
  const pageName = target.kind === "template" ? target.page : "custom";

  function setSections(next: Section[]) {
    if (!doc) return;
    dirty.current = true;
    if (target.kind === "template") setDoc({ ...doc, templates: { ...doc.templates, [target.key]: next } });
    else setDoc({ ...doc, pages: doc.pages.map((p, i) => (i === target.index ? { ...p, sections: next } : p)) });
  }

  // Autosave the draft 800 ms after the last change, then refresh the preview.
  useEffect(() => {
    if (!doc || !dirty.current) return;
    const h = setTimeout(async () => {
      try {
        setStatus("Saving…");
        await api("/admin/theme/draft", { method: "PUT", json: doc });
        dirty.current = false;
        setStatus("Draft saved");
        setPreviewUrl((u) => u.replace(/&r=\d+$/, "") + `&r=${Date.now()}`);
      } catch (e) {
        setStatus(e instanceof ApiError ? `Not saved: ${e.detail}` : "Not saved");
      }
    }, 800);
    return () => clearTimeout(h);
  }, [doc]);

  function onDragEnd(e: DragEndEvent) {
    if (!e.over || e.active.id === e.over.id) return;
    const from = sections.findIndex((s) => s.id === e.active.id);
    const to = sections.findIndex((s) => s.id === e.over!.id);
    setSections(arrayMove(sections, from, to));
  }

  function add(type: SectionType) {
    const s = { id: newId(), type: type.type, hidden: false, settings: defaultsFromSchema(type.settings_schema) };
    setSections([...sections, s]);
    setSelected(s.id);
  }

  async function publish() {
    try {
      setStatus("Publishing…");
      const v = await api<{ number: number }>("/admin/theme/publish", { method: "POST", json: {} });
      setStatus(`Published version ${v.number}`);
      setVersions(await api<any[]>("/admin/theme/versions"));
    } catch (e) { setStatus(e instanceof ApiError ? e.detail : "Publish failed"); }
  }

  async function restore(id: string) {
    await api(`/admin/theme/versions/${id}/restore`, { method: "POST" });
    dirty.current = false;
    await load();
    setStatus("Rolled back");
  }

  if (!doc) return <main className="p-6 text-muted">{status || "Loading…"}</main>;
  const current = sections.find((s) => s.id === selected);
  const currentType = types.find((t) => t.type === current?.type);
  const addable = types.filter((t) => t.pages.includes(pageName) && sections.filter((s) => s.type === t.type).length < t.max_per_page);

  return (
    <div className="grid h-screen grid-cols-[280px_1fr_320px] bg-surface text-fg">
      <aside className="space-y-4 overflow-y-auto border-r border-border p-3">
        <select className="w-full rounded-theme border border-border bg-bg p-2 text-sm" aria-label="Page"
          value={target.kind === "template" ? target.key : `page:${target.index}`}
          onChange={(e) => {
            const v = e.target.value; setSelected(null);
            if (v.startsWith("page:")) setTarget({ kind: "page", index: +v.slice(5) });
            else { const tpl = TEMPLATES.find((x) => x.key === v)!; setTarget({ kind: "template", key: tpl.key, page: tpl.page }); }
          }}>
          {TEMPLATES.map((x) => <option key={x.key} value={x.key}>{x.label}</option>)}
          {doc.pages.map((p, i) => <option key={p.slug} value={`page:${i}`}>Page: /{p.slug}</option>)}
        </select>
        <button className="text-xs text-primary underline" onClick={() => {
          const slug = prompt("Page link, e.g. about-us")?.trim().toLowerCase();
          if (!slug) return;
          dirty.current = true;
          setDoc({ ...doc, pages: [...doc.pages, { slug, title: { en: slug, bn: "" }, sections: [], seo: { title: {}, description: {} }, published: true }] });
          setTarget({ kind: "page", index: doc.pages.length });
        }}>+ New page</button>
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
          <SortableContext items={sections.map((s) => s.id)} strategy={verticalListSortingStrategy}>
            <ul className="space-y-2">
              {sections.map((s) => (
                <Row key={s.id} s={s} selected={s.id === selected} onSelect={() => setSelected(s.id)}
                  onToggle={() => setSections(sections.map((x) => (x.id === s.id ? { ...x, hidden: !x.hidden } : x)))}
                  onRemove={() => { setSections(sections.filter((x) => x.id !== s.id)); if (selected === s.id) setSelected(null); }} />
              ))}
            </ul>
          </SortableContext>
        </DndContext>
        <details><summary className="cursor-pointer text-sm font-semibold">+ Add section</summary>
          <ul className="mt-2 grid grid-cols-2 gap-1">{addable.map((t) => (
            <li key={t.type}><button onClick={() => add(t)} className="w-full rounded-theme border border-border px-2 py-1 text-left text-xs">{t.type.replace(/_/g, " ")}</button></li>))}</ul>
        </details>
      </aside>

      <section className="flex flex-col">
        <div className="flex items-center gap-3 border-b border-border px-4 py-2 text-sm">
          <span aria-live="polite" className="flex-1 text-muted">{status}</span>
          <button onClick={() => setDevice(device === "desktop" ? "mobile" : "desktop")} className="rounded-theme border border-border px-2 py-1">{device}</button>
          <a href={previewUrl} target="_blank" rel="noreferrer" className="rounded-theme border border-border px-2 py-1">Preview link</a>
          <button onClick={publish} className="rounded-theme bg-primary px-3 py-1 text-primary-fg">Publish</button>
        </div>
        <div className="flex flex-1 justify-center overflow-auto bg-bg p-4">
          <iframe title="Preview" src={target.kind === "page" ? previewUrl.replace("/?", `/pages/${doc.pages[target.index]?.slug}?`) : previewUrl}
            className={`h-full rounded-theme border border-border bg-white ${device === "mobile" ? "w-[390px]" : "w-full"}`} />
        </div>
      </section>

      <aside className="space-y-4 overflow-y-auto border-l border-border p-3">
        {current && currentType ? (
          <div className="space-y-2">
            <h2 className="font-semibold">{current.type.replace(/_/g, " ")}</h2>
            <SchemaForm schema={currentType.settings_schema} value={current.settings}
              onChange={(v) => setSections(sections.map((s) => (s.id === current.id ? { ...s, settings: v } : s)))} />
          </div>
        ) : <p className="text-sm text-muted">Select a section to edit it. Drag the handle to reorder.</p>}
        <div>
          <h2 className="mb-2 text-sm font-semibold">Versions</h2>
          <ul className="space-y-1 text-xs">{versions.map((v) => (
            <li key={v.id} className="flex items-center justify-between">
              <span>v{v.number}{v.is_published ? " · live" : ""}</span>
              {!v.is_published && <button className="text-primary underline" onClick={() => restore(v.id)}>Restore</button>}
            </li>))}</ul>
        </div>
      </aside>
    </div>
  );
}
