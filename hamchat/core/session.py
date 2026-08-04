# hamchat/core/session.py
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Optional
from PyQt6.QtCore import QObject, pyqtSignal
from pathlib import Path
from hamchat.paths import settings_dir
from hamchat.core.settings import Settings
from hamchat.infra.llm.ollama_registry import update_model_capability

@dataclass
class Preferences:
    theme_variant: str = "dark"          # "light" | "dark"
    spellcheck_enabled: bool = True
    locale: str = "en_GB"
    model_id: str = "gpt-oss:latest"     # NEW: default LLM model


@dataclass
class SessionData:
    user_id: Optional[int] = None
    username: str = "Guest"
    role: str = "guest"                  # "guest" | "user" | "admin"
    runtime_mode: str = "solo"           # "solo" | "snout" | "ham"
    server_url: Optional[str] = None
    prefs: Preferences = Preferences()
    current_model: str = None
    vision: bool = False
    profile_id: Optional[int] = None


class SessionManager(QObject):
    sessionChanged = pyqtSignal(object)   # emits SessionData
    prefsChanged   = pyqtSignal(object)   # emits Preferences
    modelContextAllocationChanged = pyqtSignal(str, str)  # model_id, allocation mode

    OLLAMA_CONTEXT_ALLOCATIONS = {
        "auto": None,
        "low": 4096,
        "mid": 8192,
        "high": 16384,
    }

    def __init__(self, settings: Settings, runtime_mode: str, server_url: Optional[str]):
        super().__init__()
        self.settings = settings
        # Registry reads are file-backed, but learned capabilities must take
        # effect immediately for this live session as well.
        self._model_capability_overrides: dict[str, dict[str, tuple[bool, Optional[str]]]] = {}
        self.current = SessionData(runtime_mode=runtime_mode, server_url=server_url)
        # hydrate guest prefs from app settings
        self.current.prefs.theme_variant      = settings.get("theme_variant", "dark")
        self.current.prefs.spellcheck_enabled = bool(settings.get("spellcheck_enabled", True))
        self.current.prefs.locale             = settings.get("locale", "en_GB")
        self.current.prefs.model_id           = settings.get("model_id", "gpt-oss:latest")
        # compute initial vision flag from model capabilities
        self._refresh_current_capabilities()
        self.prefsChanged.emit(self.current.prefs)

    # --- Minimal auth/account helpers (MVP storage in Settings) ---

    def signup_requires_approval(self) -> bool:
        """Return whether signups must be submitted for admin approval."""
        return bool(self.settings.get("auth", {}).get("signup_submit", False))

    def set_signup_requires_approval(self, enabled: bool) -> None:
        """Update the policy in memory + persist via Settings."""
        auth = self.settings.get("auth", {}) or {}
        auth["signup_submit"] = bool(enabled)
        self.settings.set("auth", auth)

    def mark_has_admin(self, value: bool = True) -> None:
        """Flag that an admin now exists (UI hint; DB remains source of truth)."""
        auth = self.settings.get("auth", {}) or {}
        auth["has_admin"] = bool(value)
        self.settings.set("auth", auth)

    def has_admin(self) -> bool | None:
        auth = (self.settings.get("auth") or {})
        return auth.get("has_admin", None)  # None | True | False

    def _save_accounts(self, acc: dict):
        self.settings.set("accounts", acc)

    def _set_has_admin_flag(self, value: bool | None):
        auth = dict(self.settings.get("auth") or {})
        if auth.get("has_admin") != value:
            auth["has_admin"] = value
            self.settings.set("auth", auth)

    def create_admin(self, username: str) -> int:
        acc = self.settings.get("accounts", {}) or {}
        acc.setdefault("users", [])
        acc["admin"] = {"id": 1, "username": username}
        acc["next_user_id"] = max(2, int(acc.get("next_user_id", 2)))
        self._save_accounts(acc)
        self._set_has_admin_flag(True)  # <- reflect reality
        return 1

    def signup_user(self, username: str) -> int:
        acc = self.settings.get("accounts", {}) or {"users": [], "next_user_id": 2}
        uid = int(acc.get("next_user_id", 2))
        acc.setdefault("users", []).append({"id": uid, "username": username})
        acc["next_user_id"] = uid + 1
        self._save_accounts(acc)
        return uid

    def login_user(self, username: str) -> tuple[int, str, dict]:
        acc = self.settings.get("accounts", {}) or {}
        if acc.get("admin", {}).get("username") == username:
            return 1, "admin", {}
        for u in acc.get("users", []):
            if u.get("username") == username:
                return int(u["id"]), "user", {}
        raise ValueError("User not found")

    def logout(self):
        # Reset to guest; keep prefs and runtime info
        cur = self.current
        self.current = SessionData(
            user_id=None, username="Guest", role="guest",
            runtime_mode=cur.runtime_mode, server_url=cur.server_url, prefs=cur.prefs
        )
        # Recompute capabilities for this session's model
        self._refresh_current_capabilities()
        self.sessionChanged.emit(self.current)

    # called after a real login later
    def load_user(self, user_id: int, username: str, role: str, user_prefs: dict):
        self.current.user_id = user_id
        self.current.username = username
        self.current.role = role
        self.current.profile_id = None
        # merge user prefs (fallback to current)
        p = self.current.prefs
        p.theme_variant      = user_prefs.get("theme_variant", p.theme_variant)
        p.spellcheck_enabled = user_prefs.get("spellcheck_enabled", p.spellcheck_enabled)
        p.locale             = user_prefs.get("locale", p.locale)
        p.model_id           = user_prefs.get("model_id", p.model_id)   # NEW
        # NEW: recompute vision for this user's model
        self._refresh_current_capabilities()

        self.sessionChanged.emit(self.current)
        self.prefsChanged.emit(self.current.prefs)

    # unified mutators (persist + signal)
    def set_theme_variant(self, variant: str):
        self.current.prefs.theme_variant = variant
        self.settings.set("theme_variant", variant)
        self.prefsChanged.emit(self.current.prefs)

    def set_spell_enabled(self, on: bool):
        self.current.prefs.spellcheck_enabled = bool(on)
        self.settings.set("spellcheck_enabled", bool(on))
        self.prefsChanged.emit(self.current.prefs)

    def set_locale(self, locale: str):
        self.current.prefs.locale = locale
        self.settings.set("locale", locale)
        self.prefsChanged.emit(self.current.prefs)

    # --- Profile helpers ---
    def get_profile_id(self) -> Optional[int]:
        return getattr(self.current, "profile_id", None)

    def set_profile_id(self, profile_id: Optional[int]) -> None:
        try:
            self.current.profile_id = int(profile_id) if profile_id is not None else None
        except Exception:
            self.current.profile_id = None
        self.sessionChanged.emit(self.current)

    # --- Model helpers ---

    def get_model_id(self) -> str:
        return self.current.prefs.model_id

    def set_model_id(self, model_id: str):
        self.current.prefs.model_id = model_id
        self.settings.set("model_id", model_id)
        caps = self.get_model_capabilities(model_id)
        self.set_model_vision(bool(caps.get("vision", False)))
        self.prefsChanged.emit(self.current.prefs)

    def is_ollama_model(self, model_id: str) -> bool:
        """Models without an explicit backend retain HamChat's local-Ollama default."""
        return (self.get_model_backend(model_id) or "ollama") == "ollama"

    def get_model_context_allocation(self, model_id: str) -> str:
        if not self.is_ollama_model(model_id):
            return "auto"
        saved = self.settings.get("ollama_context_allocations", {}) or {}
        mode = saved.get(model_id, "auto") if isinstance(saved, dict) else "auto"
        return mode if mode in self.OLLAMA_CONTEXT_ALLOCATIONS else "auto"

    def get_model_context_num_ctx(self, model_id: str) -> Optional[int]:
        return self.OLLAMA_CONTEXT_ALLOCATIONS[self.get_model_context_allocation(model_id)]

    def set_model_context_allocation(self, model_id: str, mode: str) -> None:
        if not self.is_ollama_model(model_id):
            return
        if mode not in self.OLLAMA_CONTEXT_ALLOCATIONS:
            raise ValueError(f"Unknown Ollama context allocation: {mode}")
        saved = self.settings.get("ollama_context_allocations", {}) or {}
        saved = dict(saved) if isinstance(saved, dict) else {}
        if saved.get(model_id, "auto") == mode:
            return
        saved[model_id] = mode
        self.settings.set("ollama_context_allocations", saved)
        self.modelContextAllocationChanged.emit(model_id, mode)

    def _refresh_current_capabilities(self) -> None:
        """
        Recompute capabilities (currently just 'vision') for the active model
        and store them on self.current.
        """
        model_id = self.get_model_id()
        caps = self.get_model_capabilities(model_id)
        self.current.vision = bool(caps.get("vision", False))

    def _load_all_models(self) -> list[dict]:
        """
        Load and merge models from:
          - settings/models.json      (Ollama registry, auto-generated)
          - settings/models-x.json    (external/API models, user-managed)

        Returns a flat list of model dicts.
        """
        base = settings_dir()
        all_models: list[dict] = []

        for fname in ("models.json", "models-x.json"):
            path: Path = base.joinpath(fname)
            try:
                text = path.read_text("utf-8")
                data = json.loads(text)
            except Exception:
                continue

            models = data.get("models")
            if isinstance(models, list):
                all_models.extend(models)

        return all_models

    def get_model_backend(self, model_id: str) -> Optional[str]:
        """
        Return backend string for model_id, e.g. 'ollama' or 'openai'.
        Returns None if not specified.
        """
        try:
            for m in self._load_all_models():
                if m.get("name") == model_id:
                    backend = m.get("backend")
                    if isinstance(backend, str) and backend.strip():
                        return backend.strip().lower()
        except Exception:
            pass
        return None

    def get_model_choices(self) -> list[tuple[str, str]]:
        """
        Return a list of (model_id, label) tuples, based on the merged registry:
        - settings/models.json  (local/Ollama, auto-generated)
        - settings/models-x.json (external/API models, user-managed)

        Falls back to a small static list if anything goes wrong.
        """
        try:
            models = self._load_all_models()
        except Exception:
            models = []

        result: list[tuple[str, str]] = []
        for m in models:
            # external entries might omit 'available' → treat as True by default
            if m.get("available", True) is False:
                continue

            name = m.get("name")
            if not name:
                continue

            label = name  # keep it simple for now
            result.append((name, label))

        # Safety: don't ever return an empty list
        if not result:
            return [
                ("gpt-oss:latest", "gpt-oss:latest"),
                ("mistral:latest", "mistral:latest"),
                ("phi:latest", "phi:latest"),
            ]

        return result

    def get_model_capabilities(self, model_id: str) -> dict:
        """Return capabilities{} for model_id from merged model registries, else {}."""
        try:
            for m in self._load_all_models():
                if m.get("name") == model_id:
                    capabilities = dict(m.get("capabilities") or {})
                    overrides = self._model_capability_overrides.get(model_id, {})
                    digest = m.get("digest")
                    for capability, (value, learned_digest) in list(overrides.items()):
                        if learned_digest == digest:
                            capabilities[capability] = value
                        else:
                            # A registry refresh found a new build under the
                            # same tag, so do not retain stale learned state.
                            del overrides[capability]
                    return capabilities
        except Exception:
            pass
        return {
            capability: value
            for capability, (value, _digest) in self._model_capability_overrides.get(model_id, {}).items()
        }

    def set_model_capability(self, model_id: str, capability: str, value: bool) -> bool:
        """Persist a learned Ollama capability and apply it to this session."""
        if not isinstance(value, bool):
            raise ValueError("Model capability values must be boolean")
        if not self.is_ollama_model(model_id):
            return False
        current = self.get_model_capabilities(model_id)
        digest = self.get_model_metadata(model_id).get("digest")
        if current.get(capability) is value:
            self._model_capability_overrides.setdefault(model_id, {})[capability] = (value, digest)
            return False
        changed = update_model_capability(
            model_id, capability, value,
            registry_path=settings_dir() / "models.json",
        )
        self._model_capability_overrides.setdefault(model_id, {})[capability] = (value, digest)
        if model_id == self.get_model_id():
            self._refresh_current_capabilities()
        return changed

    def get_model_metadata(self, model_id: str) -> dict:
        """Return registry metadata, including Ollama's reported family when known."""
        try:
            for model in self._load_all_models():
                if model.get("name") == model_id:
                    return dict(model)
        except Exception:
            pass
        return {}

    def set_model_vision(self, enabled: bool):
        self.current.vision = bool(enabled)
        self.sessionChanged.emit(self.current)
