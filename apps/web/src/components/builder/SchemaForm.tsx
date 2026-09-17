"use client";
// Generates a settings form from the section's JSON Schema (served by GET /api/v1/theme/sections).
// Supports the subset the registry uses: string, integer, boolean, enum, arrays, nested objects, $ref, anyOf-null.
type Schema = Record<string, any>;

function resolve(schema: Schema, root: Schema): Schema {
  if (schema?.$ref) return resolve(root.$defs?.[schema.$ref.split("/").pop()!] ?? {}, root);
  if (schema?.anyOf) {
    const nonNull = schema.anyOf.find((s: Schema) => s.type !== "null");
    return { ...resolve(nonNull, root), nullable: true, title: schema.title, default: schema.default };
  }
  return schema ?? {};
}

function defaultFor(schema: Schema, root: Schema): any {
  const s = resolve(schema, root);
  if (s.default !== undefined) return structuredClone(s.default);
  if (s.nullable) return null;
  if (s.enum) return s.enum[0];
  switch (s.type) {
    case "object": return Object.fromEntries(Object.entries(s.properties ?? {}).map(([k, v]) => [k, defaultFor(v as Schema, root)]));
    case "array": return Array.from({ length: s.minItems ?? 0 }, () => defaultFor(s.items, root));
    case "integer": case "number": return s.minimum ?? 0;
    case "boolean": return false;
    default: return "";
  }
}

export function defaultsFromSchema(schema: Schema) {
  return defaultFor(schema, schema);
}

function Field({ name, schema, root, value, onChange }: { name: string; schema: Schema; root: Schema; value: any; onChange: (v: any) => void }) {
  const s = resolve(schema, root);
  const label = s.title ?? name.replace(/_/g, " ");
  const input = "w-full rounded-theme border border-border bg-bg px-2 py-1 text-sm";
  if (s.nullable && value === null) {
    return <button type="button" className="text-xs text-primary underline" onClick={() => onChange(defaultFor({ ...s, nullable: false, default: undefined }, root))}>+ Add {label}</button>;
  }
  if (s.enum) {
    return <label className="block text-xs">{label}<select className={input} value={value} onChange={(e) => onChange(isNaN(+e.target.value) ? e.target.value : +e.target.value)}>
      {s.enum.map((o: any) => <option key={o} value={o}>{String(o)}</option>)}</select></label>;
  }
  if (s.type === "boolean") return <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />{label}</label>;
  if (s.type === "integer" || s.type === "number") {
    return <label className="block text-xs">{label}<input type="number" className={input} value={value ?? ""} min={s.minimum} max={s.maximum} onChange={(e) => onChange(e.target.value === "" ? null : +e.target.value)} /></label>;
  }
  if (s.type === "object") {
    return <fieldset className="space-y-2 rounded-theme border border-border p-2"><legend className="px-1 text-xs font-semibold">{label}</legend>
      {Object.entries(s.properties ?? {}).map(([k, sub]) => (
        <Field key={k} name={k} schema={sub as Schema} root={root} value={value?.[k]} onChange={(v) => onChange({ ...value, [k]: v })} />))}
      {s.nullable && <button type="button" className="text-xs text-danger" onClick={() => onChange(null)}>Remove</button>}
    </fieldset>;
  }
  if (s.type === "array") {
    const items = (value ?? []) as any[];
    return <fieldset className="space-y-2"><legend className="text-xs font-semibold">{label}</legend>
      {items.map((it, i) => (
        <div key={i} className="flex gap-1">
          <div className="flex-1"><Field name={`${label} ${i + 1}`} schema={s.items} root={root} value={it} onChange={(v) => onChange(items.map((x, j) => (j === i ? v : x)))} /></div>
          <button type="button" aria-label="Remove item" className="text-danger" onClick={() => onChange(items.filter((_, j) => j !== i))}>×</button>
        </div>))}
      {(s.maxItems === undefined || items.length < s.maxItems) && (
        <button type="button" className="text-xs text-primary underline" onClick={() => onChange([...items, defaultFor(s.items, root)])}>+ Add</button>)}
    </fieldset>;
  }
  const long = (s.maxLength ?? 0) > 500;
  return <label className="block text-xs">{label}{long
    ? <textarea className={input} rows={4} value={value ?? ""} maxLength={s.maxLength} onChange={(e) => onChange(e.target.value)} />
    : <input className={input} value={value ?? ""} maxLength={s.maxLength} onChange={(e) => onChange(e.target.value)} />}</label>;
}

export function SchemaForm({ schema, value, onChange }: { schema: Schema; value: any; onChange: (v: any) => void }) {
  return <Field name="settings" schema={schema} root={schema} value={value} onChange={onChange} />;
}
