"""WCAG 2.x contrast guard: a palette that makes the store unreadable cannot be published."""

from dataclasses import dataclass

from app.modules.theme.schema import Palette, Tokens


def _channel(c: int) -> float:
    s = c / 255
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def luminance(rgb: str) -> float:
    r, g, b = (int(x) for x in rgb.split())
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def ratio(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return round((la + 0.05) / (lb + 0.05), 2)


# (foreground, background, minimum ratio). 4.5 = body text AA; 3.0 = large text / UI components.
PAIRS = [
    ("fg", "bg", 4.5),
    ("fg", "surface", 4.5),
    ("primary_fg", "primary", 4.5),
    ("muted", "bg", 3.0),
    ("danger", "bg", 3.0),
]


@dataclass
class ContrastIssue:
    mode: str
    foreground: str
    background: str
    ratio: float
    required: float


def check_palette(p: Palette, mode: str) -> list[ContrastIssue]:
    issues = []
    for fg, bg, need in PAIRS:
        r = ratio(getattr(p, fg), getattr(p, bg))
        if r < need:
            issues.append(ContrastIssue(mode, fg.replace("_", "-"), bg, r, need))
    return issues


def check_tokens(t: Tokens) -> list[ContrastIssue]:
    return check_palette(t.colors, "light") + check_palette(t.dark, "dark")
