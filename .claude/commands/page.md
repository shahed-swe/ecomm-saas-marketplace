---
description: Build a Next.js route with the right rendering strategy, states, and accessibility.
argument-hint: <route path — what the user sees>
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Build: **$ARGUMENTS**

Use the `nextjs-engineer` agent.

Decide and state up front: Server or Client, static/ISR/dynamic, and what
revalidates it. Then build:

- `page.tsx`, plus `loading.tsx` and `error.tsx` for the segment.
- `generateMetadata` with real title, description, OG image, and canonical.
  Product pages also emit Product/Offer JSON-LD; listing pages emit
  BreadcrumbList.
- Skeletons that match the final layout so nothing shifts.
- Empty state with an action, not just a sentence.
- Mobile-first Tailwind; check 360px, 768px, 1280px.
- Keyboard path through every control; visible focus; labelled inputs.

Report the route, the rendering strategy, the first-load JS delta from
`next build`, and any new component added to `components/ui/`.
