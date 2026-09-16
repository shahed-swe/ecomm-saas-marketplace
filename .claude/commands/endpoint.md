---
description: Add one API endpoint to an existing module, with schemas, service logic, tests, and OpenAPI examples.
argument-hint: <METHOD /path — what it does>
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Add: **$ARGUMENTS**

Use the `fastapi-architect` agent.

- Find the owning module under `app/modules/`; create it only if none fits.
- Router handles HTTP only; the rule lives in `service.py`, the query in
  `repository.py`.
- Declare `response_model`, status code, and the error responses in the
  decorator so OpenAPI is accurate.
- Add the auth dependency explicitly, even for public routes (`Depends(optional_user)`)
  so intent is visible.
- Paginate if it returns a list. Filter by owner if it returns user data.
- Write the integration test in the same change: happy path, 401, 403, 422.

Report the final route signature, the OpenAPI example payloads, and whether a
migration was needed.
