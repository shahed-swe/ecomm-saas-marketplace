"""Custom CSS sandbox (architecture §5.5). Parse, allow-list, scope, re-serialise.

- only style rules and @media blocks; every other at-rule is rejected (@import, @font-face, @keyframes…)
- no url() except this tenant's own media paths; no expression/behavior/-moz-binding/javascript:
- no position:fixed (overlay phishing), no huge z-index
- every selector is prefixed with [data-tenant-css]; :root/html/body map to that scope
- the storefront never includes it on checkout, payment or account-security pages
"""

import re

import tinycss2

from app.core.errors import AppError

MAX_BYTES = 50_000
BANNED_TOKENS = (
    "expression",
    "behavior",
    "-moz-binding",
    "javascript:",
    "vbscript:",
    "@import",
    "\\",
)
SCOPE = "[data-tenant-css]"


class CssRejected(AppError):
    status = 422
    code = "css_rejected"


def _check_declarations(tokens, tenant_id: str) -> str:
    decls = tinycss2.parse_declaration_list(tokens, skip_comments=True, skip_whitespace=True)
    out = []
    for d in decls:
        if d.type == "error":
            raise CssRejected(f"Invalid declaration: {d.message}")
        if d.type != "declaration":
            raise CssRejected("Only declarations are allowed inside rules")
        name = d.lower_name
        value = tinycss2.serialize(d.value).strip()
        low = value.lower()
        if any(b in low for b in BANNED_TOKENS) or name.startswith("-ms-behavior"):
            raise CssRejected(f"'{name}' uses a blocked construct")
        for tok in d.value:
            if tok.type == "url" or (
                tok.type == "function" and tok.lower_name in ("url", "image-set", "src")
            ):
                target = (
                    tok.value
                    if tok.type == "url"
                    else tinycss2.serialize(tok.arguments).strip("'\" ")
                )
                if not target.startswith(f"/media/t/{tenant_id}/"):
                    raise CssRejected("url() may only point at images uploaded to this store")
        if name == "position" and "fixed" in low:
            raise CssRejected("position: fixed is not allowed")
        if name == "z-index" and re.search(r"\d{4,}", low):
            raise CssRejected("z-index above 999 is not allowed")
        if name == "content" and ("attr(" in low):
            raise CssRejected("content: attr() is not allowed")
        out.append(f"{name}:{value}{' !important' if d.important else ''}")
    return ";".join(out)


def _scope_selector(prelude) -> str:
    raw = tinycss2.serialize(prelude).strip()
    if not raw or "{" in raw or "}" in raw:
        raise CssRejected("Invalid selector")
    scoped = []
    for part in raw.split(","):
        sel = part.strip()
        if not sel:
            raise CssRejected("Empty selector")
        sel = re.sub(r"^(:root|html|body)\b", "", sel).strip()
        scoped.append(f"{SCOPE} {sel}".strip() if sel else SCOPE)
    return ",".join(scoped)


def _rules(rules, tenant_id: str, depth: int = 0) -> list[str]:
    out = []
    for r in rules:
        if r.type in ("whitespace", "comment"):
            continue
        if r.type == "error":
            raise CssRejected(f"Invalid CSS: {r.message}")
        if r.type == "at-rule":
            if r.lower_at_keyword != "media" or depth > 0 or r.content is None:
                raise CssRejected(f"@{r.lower_at_keyword} is not allowed")
            media = tinycss2.serialize(r.prelude).strip()
            if not re.fullmatch(r"[a-zA-Z0-9\s():,.\-]+", media):
                raise CssRejected("Invalid @media query")
            inner = tinycss2.parse_rule_list(r.content, skip_comments=True, skip_whitespace=True)
            out.append(f"@media {media}{{{''.join(_rules(inner, tenant_id, depth + 1))}}}")
            continue
        if r.type != "qualified-rule":
            raise CssRejected("Unsupported CSS")
        out.append(f"{_scope_selector(r.prelude)}{{{_check_declarations(r.content, tenant_id)}}}")
    return out


def sanitize_css(css: str, tenant_id: str) -> str:
    if len(css.encode()) > MAX_BYTES:
        raise CssRejected("Custom CSS must be 50 KB or smaller")
    if "</" in css or "<!--" in css:
        raise CssRejected("HTML is not allowed in CSS")
    rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    return "\n".join(_rules(rules, tenant_id))
