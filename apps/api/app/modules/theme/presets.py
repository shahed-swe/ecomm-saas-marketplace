"""Platform theme presets. Every preset must pass the contrast guard (tested)."""

from app.modules.theme.schema import ThemeDocument

_DARK_BASE = {
    "bg": "2 6 23",
    "surface": "15 23 42",
    "fg": "241 245 249",
    "muted": "148 163 184",
    "border": "30 41 59",
    "danger": "248 113 113",
    "success": "74 222 128",
}

PRESETS: dict[str, dict] = {
    "minimal": {
        "name": "Minimal",
        "tokens": {
            "colors": {
                "primary": "15 23 42",
                "primary-fg": "255 255 255",
                "secondary": "71 85 105",
                "accent": "14 116 144",
                "bg": "255 255 255",
                "surface": "248 250 252",
                "fg": "15 23 42",
                "muted": "100 116 139",
                "border": "226 232 240",
                "danger": "185 28 28",
                "success": "21 128 61",
            },
            "dark": {
                **_DARK_BASE,
                "primary": "241 245 249",
                "primary-fg": "15 23 42",
                "secondary": "148 163 184",
                "accent": "34 211 238",
            },
            "radius": "0.25rem",
            "font_body": "inter-hind-siliguri",
            "font_heading": "inter-hind-siliguri",
            "button_style": "solid",
            "density": "comfortable",
        },
    },
    "bazaar": {
        "name": "Bazaar",
        "tokens": {
            "colors": {
                "primary": "194 65 12",
                "primary-fg": "255 255 255",
                "secondary": "22 101 52",
                "accent": "202 138 4",
                "bg": "255 251 245",
                "surface": "255 247 237",
                "fg": "28 25 23",
                "muted": "120 113 108",
                "border": "231 229 228",
                "danger": "185 28 28",
                "success": "21 128 61",
            },
            "dark": {
                **_DARK_BASE,
                "primary": "251 146 60",
                "primary-fg": "28 25 23",
                "secondary": "74 222 128",
                "accent": "250 204 21",
            },
            "radius": "0.75rem",
            "font_body": "poppins-noto-sans-bengali",
            "font_heading": "poppins-noto-sans-bengali",
            "button_style": "pill",
            "density": "compact",
        },
    },
    "fashion": {
        "name": "Fashion",
        "tokens": {
            "colors": {
                "primary": "136 19 55",
                "primary-fg": "255 255 255",
                "secondary": "30 27 75",
                "accent": "190 24 93",
                "bg": "255 255 255",
                "surface": "253 242 248",
                "fg": "24 24 27",
                "muted": "113 113 122",
                "border": "244 228 236",
                "danger": "185 28 28",
                "success": "21 128 61",
            },
            "dark": {
                **_DARK_BASE,
                "primary": "244 114 182",
                "primary-fg": "24 24 27",
                "secondary": "165 180 252",
                "accent": "251 113 133",
            },
            "radius": "0rem",
            "font_body": "lora-noto-serif-bengali",
            "font_heading": "lora-noto-serif-bengali",
            "button_style": "outline",
            "density": "comfortable",
        },
    },
}


def preset_document(key: str) -> ThemeDocument:
    p = PRESETS[key]
    return ThemeDocument(preset=key, tokens=p["tokens"])
