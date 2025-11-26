# hamchat/ui/theme.py
from __future__ import annotations
from pathlib import Path
import json
from typing import Dict
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtWidgets import QApplication

DEFAULT_THEME: Dict = {
    "name": "HamChat Default",
    "version": 1,
    "author": "HamChat",
    "variants": {
        "light": {
            "colors": {
                # Base surfaces
                "bg": "#F3F4F6",
                "bg_alt": "#EDEFF2",
                "surface": "#FFFFFF",
                "surface_alt": "#F6F7F9",
                "chat_bg": "#E9EAED",

                # Chat message colors
                "msg_user_bg": "#DCF7C5",
                "msg_user_text": "#10210A",
                "msg_assistant_bg": "#E6EEF9",
                "msg_assistant_text": "#0E1A2B",
                "msg_system_bg": "#FFF7D6",
                "msg_system_text": "#312500",

                # Text and accents
                "text": "#1F2937",
                "text_muted": "#6B7280",
                "text_inv": "#F9FAFB",
                "accent": "#3B82F6",
                "accent_hover": "#2563EB",
                "accent_active": "#1D4ED8",
                "on_accent": "#FFFFFF",

                # Links
                "link": "#2563EB",
                "link_visited": "#7C3AED",

                # Code blocks
                "code_bg": "#F4F6F8",
                "code_border": "#D8DEE5",

                # Selection
                "selection_bg": "#C7D2FE",
                "selection_text": "#111827",

                # Utility
                "border": "#D1D5DB",
                "border_strong": "#9CA3AF",
                "success": "#10B981",
                "warning": "#F59E0B",
                "danger":  "#EF4444",
                "info":    "#0EA5E9",
                "edge": "#30343B",
                "input_bg": "#FFFFFF",
                "scrim": "#66000000"  # 40% black
            },
            "metrics": {
                "radius_sm": 6, "radius_md": 12, "radius_lg": 18,
                "pad_sm": 6, "pad_md": 10, "pad_lg": 14
            }
        },
        "dark": {
            "colors": {
                # Base surfaces
                "bg": "#121417",
                "bg_alt": "#161A1E",
                "surface": "#1C2127",
                "surface_alt": "#252B33",
                "chat_bg": "#2B3037",

                # Chat message colors
                "msg_user_bg": "#1E2A1E",
                "msg_user_text": "#DFF7D6",
                "msg_assistant_bg": "#1F2733",
                "msg_assistant_text": "#E6EEF9",
                "msg_system_bg": "#2B2412",
                "msg_system_text": "#FDECC8",

                # Text and accents
                "text": "#E5E7EB",
                "text_muted": "#9CA3AF",
                "text_inv": "#0B0D0F",
                "accent": "#60A5FA",
                "accent_hover": "#3B82F6",
                "accent_active": "#2563EB",
                "on_accent": "#0B0D0F",

                # Links
                "link": "#60A5FA",
                "link_visited": "#A78BFA",

                # Code blocks
                "code_bg": "#20262D",
                "code_border": "#2D3642",

                # Selection
                "selection_bg": "#334155",
                "selection_text": "#E5E7EB",

                # Utility
                "border": "#2F3742",
                "border_strong": "#465161",
                "success": "#34D399",
                "warning": "#FBBF24",
                "danger":  "#F87171",
                "info":    "#38BDF8",
                "edge": "#30343B",
                "input_bg": "#20262D",
                "scrim": "#66000000"
            },
            "metrics": {
                "radius_sm": 6, "radius_md": 12, "radius_lg": 18,
                "pad_sm": 6, "pad_md": 10, "pad_lg": 14
            }
        }
    }
}


# --- merge core ---
def _merge_defaults(user_val, default_val):
    """
    Recursive, type-aware merge:
      - Dicts: deep merge (user keys override; missing keys filled from default).
      - Mismatched types: prefer default to avoid runtime surprises.
      - Scalars: prefer user when type matches; otherwise default.
    """
    if isinstance(user_val, dict) and isinstance(default_val, dict):
        merged = {}
        # ensure all default keys exist
        for k, dv in default_val.items():
            if k in user_val:
                merged[k] = _merge_defaults(user_val[k], dv)
            else:
                merged[k] = dv
        # carry through any extra user keys (extensions)
        for k, uv in user_val.items():
            if k not in merged:
                merged[k] = uv
        return merged

    if user_val is not None and (type(user_val) is type(default_val)):
        return user_val

    return default_val

def merge_theme_with_defaults(theme: Dict) -> Dict:
    """
    Public helper: hydrate any theme dict with DEFAULT_THEME.
    Safe for custom theme JSONs that may omit keys.
    """
    return _merge_defaults(theme, DEFAULT_THEME)

# --- default theme file management ---
def ensure_theme(themes_dir: Path) -> Dict:
    """
    Ensure the default theme file exists.
    If it exists, read, merge with defaults (for forward-compat),
    persist any fixes, and return the hydrated dict.
    """
    themes_dir.mkdir(parents=True, exist_ok=True)
    f = themes_dir / "default_theme.json"

    if not f.exists():
        f.write_text(json.dumps(DEFAULT_THEME, indent=2), encoding="utf-8")
        return DEFAULT_THEME

    try:
        data = json.load(f.open("r", encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Theme JSON root must be a dict")

        merged = merge_theme_with_defaults(data)

        # Persist if we filled in anything missing or corrected types
        if merged != data:
            f.write_text(json.dumps(merged, indent=2), encoding="utf-8")

        return merged
    except Exception:
        # If corrupt, reset to defaults
        f.write_text(json.dumps(DEFAULT_THEME, indent=2), encoding="utf-8")
        return DEFAULT_THEME

# --- custom theme loading ---
def load_theme(path: Path) -> Dict:
    """
    Load a custom theme JSON from 'path' and merge with DEFAULT_THEME
    so missing keys are auto-filled. Does not modify the file on disk.
    """
    data = json.load(path.open("r", encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Theme JSON root must be a dict")
    return merge_theme_with_defaults(data)

def select_variant(theme: Dict, variant: str) -> Dict:
    v = variant.lower()
    if v not in ("light", "dark"):
        v = "dark"
    return theme["variants"][v]["colors"]

def apply_theme(app: QApplication, window, colors: Dict) -> None:
    """
    Apply palette baseline + scoped QSS using our semantic tokens.
    We target objectNames set on our panels for precise styling.
    """
    # --- QPalette baseline ---
    pal = app.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor(colors["bg"]))
    pal.setColor(QPalette.ColorRole.Base, QColor(colors["surface"]))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(colors["surface_alt"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(colors["text"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(colors["text"]))
    pal.setColor(QPalette.ColorRole.Button, QColor(colors["surface"]))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(colors["text"]))

    # Selection and link colors
    pal.setColor(QPalette.ColorRole.Highlight, QColor(colors["selection_bg"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(colors["selection_text"]))
    pal.setColor(QPalette.ColorRole.Link, QColor(colors["link"]))
    pal.setColor(QPalette.ColorRole.LinkVisited, QColor(colors["link_visited"]))

    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(colors["surface"]))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(colors["text"]))
    app.setPalette(pal)


    # --- QSS scoped to our object names ---
    qss = f"""
    /* Panels */
    #SidePanel {{
        background: {colors["surface"]};
        color: {colors["text"]};
        border-right: 1px solid {colors["border"]};
    }}
    #ChatPanel {{
        background: {colors["surface"]};
        color: {colors["text"]};
        border-left: 1px solid {colors["border"]};
    }}
    #TopPanel {{
        background: {colors["surface_alt"]};
        color: {colors["text"]};
        border-bottom: 1px solid {colors["border"]};
    }}
    #ChatDisplay {{
        background: {colors["bg"]};
        color: {colors["text"]};
    }}
    #Transcript {{
        background: {colors["chat_bg"]};
        color: {colors["text"]};
        selection-background-color: {colors["selection_bg"]};
        selection-color: {colors["selection_text"]};
    }}
    /* Edge bars */
    #EdgeBarLeft, #EdgeBarRight {{
        background: {colors["edge"]};
    }}
    /* Inputs & buttons (generic) */
    QLineEdit {{
        background: {colors["input_bg"]};
        color: {colors["text"]};
        border: 1px solid {colors["border"]};
        border-radius: 6px;
        padding: 4px 6px;
    }}
    QPushButton {{
        background: {colors["surface"]};
        color: {colors["text"]};
        border: 1px solid {colors["border"]};
        border-radius: 8px;
        padding: 4px 10px;
    }}
    QPushButton:hover {{
        border-color: {colors["border_strong"]};
    }}
    QPushButton:pressed {{
        background: {colors["surface_alt"]};
    }}
    /* Accent (opt-in via property) */
    QPushButton[accent="true"] {{
        background: {colors["accent"]};
        color: {colors["on_accent"]};
        border-color: {colors["accent_active"]};
    }}
    QStatusBar {{
        background: {colors["surface"]};
        color: {colors["text"]};
        border-top: 1px solid {colors["border"]};
    }}
    QTextEdit#PromptInput {{
    background: {colors["input_bg"]};
    color: {colors["text"]};
    border: 1px solid {colors["border"]};
    border-radius: 8px;
    padding: 6px 8px;
    }}
    QPushButton#SendButton[accent="true"] {{
        background: {colors["accent"]};
        color: {colors["on_accent"]};
        border: 1px solid {colors["accent_active"]};
    }}
    QPushButton#SendButton[accent="true"]:hover {{
        background: {colors["accent_hover"]};
    }}
    QPushButton#SendButton[accent="true"]:pressed {{
        background: {colors["accent_active"]};
    }}  
    """

    window.setStyleSheet(qss)

def export_qml_tokens(colors: Dict) -> Dict:
    keys = [
        "bg","bg_alt","surface","surface_alt","chat_bg",
        "text","text_muted","text_inv",
        "accent","accent_hover","accent_active","on_accent",
        "border","border_strong",
        "success","warning","danger","info",
        "edge","input_bg","scrim",
        # chat-specific
        "msg_user_bg","msg_user_text",
        "msg_assistant_bg","msg_assistant_text",
        "msg_system_bg","msg_system_text",
        "code_bg","code_border",
        "link","link_visited",
        "selection_bg","selection_text",
    ]
    return {k: colors[k] for k in keys if k in colors}

