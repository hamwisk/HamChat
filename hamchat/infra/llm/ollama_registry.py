# hamchat/infra/llm/ollama_registry.py
from __future__ import annotations
import json, re, time, logging
from pathlib import Path
from typing import Dict, Any, Optional
import requests

log = logging.getLogger("models")

DEFAULT_OLLAMA = "http://127.0.0.1:11434"
REGISTRY_PATH = Path("settings/models.json")
TRIGGERS_PATH = Path("settings/modality_triggers.json")
OVERRIDES_PATH = Path("settings/context_overrides.json")

VISION_HINTS = re.compile(
    r"(llava|llama.*vision|phi[-_]?3.*vision|qwen.*vl|bakllava|moondream|minicpm[-_]?v)",
    re.IGNORECASE
)
CTX_RE = re.compile(r"\bnum_ctx\s+(\d+)", re.IGNORECASE)
CTX_KEYS = {
  "context_length","num_ctx","n_ctx","ctx",
  "max_context_length","max_ctx","max_seq_len","sequence_length",
  "rope.context","rope_ctx","rope_ctx_train"
}
CTX_LINE_RE = re.compile(
  r"\b(num_ctx|max_?ctx|n_ctx|context_length|max_seq_len|sequence_length|rope(?:\.|_)?ctx(?:_train)?)\D+(\d{3,7})",
  re.IGNORECASE
)


def _extract_context(res: dict) -> Optional[int]:
    # Normalize shapes
    raw_details = res.get("details")
    raw_params  = res.get("parameters")
    raw_mi      = res.get("model_info")
    mf          = res.get("modelfile") or ""

    details = raw_details if isinstance(raw_details, dict) else {}
    params  = raw_params  if isinstance(raw_params,  dict) else {}
    mi      = raw_mi      if isinstance(raw_mi,      dict) else {}

    # 1) direct numeric keys in details/params/model_info
    for src in (details, params, mi):
        if isinstance(src, dict):
            for k, v in src.items():
                if str(k).lower() in CTX_KEYS:
                    try:
                        return int(v)
                    except Exception:
                        pass

    # 2) stringy values that contain ctx hints
    for src in (details, params, mi):
        if isinstance(src, dict):
            for v in src.values():
                if isinstance(v, str):
                    m = CTX_LINE_RE.search(v)
                    if m:
                        try:
                            return int(m.group(2))
                        except Exception:
                            pass

    # 3) scan modelfile lines
    m = CTX_LINE_RE.search(mf)
    if m:
        try: return int(m.group(2))
        except Exception: pass

    return None

def _probe_model(base_url: str, name: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        res = requests.post(f"{base_url}/api/show", json={"name": name}, timeout=3).json()
    except Exception as e:
        log.warning("Failed to probe %s: %s", name, e)
        return out

    rd = res.get("details")
    details = rd if isinstance(rd, dict) else {}
    out["family"] = details.get("family")

    # context discovery (expanded)
    out["context"] = _extract_context(res)

    # vision via triggers (as you already added)
    triggers = _load_triggers()
    out["vision"] = _infer_vision(name, out["family"], details, triggers)
    return out

def _load_ctx_overrides() -> dict:
    try:
        with OVERRIDES_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["_rx"] = [(re.compile(p, re.IGNORECASE), v) for p, v in (data.get("by_regex") or {}).items()]
        return data
    except Exception:
        return {"by_name": {}, "_rx": [], "default_by_family": {}}

def _apply_context_overrides(entry: dict) -> None:
    if entry.get("context"):
        return
    name = entry.get("name","")
    family = (entry.get("family") or "").lower()
    o = _load_ctx_overrides()
    # exact by_name
    if name in o.get("by_name", {}):
        entry["context"] = int(o["by_name"][name]); return
    # regex patterns
    for rx, val in o.get("_rx", []):
        if rx.search(name):
            entry["context"] = int(val); return
    # conservative family default
    if family and family in o.get("default_by_family", {}):
        entry["context"] = int(o["default_by_family"][family]); return

def _load_triggers() -> dict:
    try:
        with TRIGGERS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # precompile regexes for speed
        data["_rx_mm"] = [re.compile(p, re.IGNORECASE) for p in data.get("regex_multimodal", [])]
        return data
    except Exception as e:
        log.warning("No modality triggers found (%s); defaulting to text-only.", e)
        return {"defaults_to": "text", "_rx_mm": []}

def _infer_vision(name: str, family: Optional[str], details: Dict[str, Any], triggers: dict) -> bool:
    """
    Port of your 1.0 logic: exact overrides -> regex -> name hints -> family hints -> families[].
    Returns True if multimodal/vision.
    """
    name_l = (name or "").lower()
    family_l = (family or "").lower()

    # 1) exact override
    override = (triggers.get("model_overrides") or {}).get(name)
    if override and override.lower() == "multimodal":
        return True

    # 2) regex on name
    for rx in triggers.get("_rx_mm", []):
        if rx.search(name_l):
            return True

    # 3) simple name tokens
    for tok in triggers.get("name_contains_multimodal", []):
        if tok.lower() in name_l:
            return True

    # 4) family match
    if family_l and family_l in {x.lower() for x in triggers.get("families_multimodal", [])}:
        return True

    # 5) ollama-reported families array
    fams = (details.get("families") or [])
    mm_markers = {"clip", "llava", "moondream", "bakllava", "glm-vision"}
    if any(str(f).lower() in mm_markers for f in fams):
        return True

    return False

def refresh_registry(base_url: str = DEFAULT_OLLAMA, registry_path: Path = REGISTRY_PATH) -> Dict[str, Any]:
    reg = _load_registry(registry_path)
    now = int(time.time())

    try:
        tags = requests.get(f"{base_url}/api/tags", timeout=2).json()
        runtime = {m["name"]: m.get("digest") for m in tags.get("models", [])}
    except Exception as e:
        log.warning("Ollama not reachable (%s). Using cached models.", e)
        # mark all unavailable and persist timestamp
        for m in reg["models"]:
            m["available"] = False
            m["last_seen"] = now
        reg["last_refresh"] = _iso_now()
        _save_registry(registry_path, reg)
        return reg

    # index cache by name
    cache = {m["name"]: m for m in reg["models"]}

    # new or changed models -> probe once
    for name, digest in runtime.items():
        cached = cache.get(name)
        if (cached is None) or (cached.get("digest") != digest):
            info = _probe_model(base_url, name)
            context = info.get("context")
            vision = info.get("vision", False)
            family = info.get("family")

            entry = cached or {
                "name": name, "first_seen": now
            }
            entry.update({
                "digest": digest,
                "capabilities": {"vision": bool(vision)},
                "context": context,
                "family": family,
                "last_seen": now,
                "available": True
            })
            if not entry.get("context"):
                _apply_context_overrides(entry)
            # Label the source
            entry["ctx_source"] = "extracted" if context else ("override" if entry.get("context") else None)
            cache[name] = entry
            log.info("Indexed model %s (ctx=%s, vision=%s)", name, context, vision)
        else:
            cached["available"] = True
            cached["last_seen"] = now
            if not cached.get("context"):
                _apply_context_overrides(cached)
                if cached.get("context") and not cached.get("ctx_source"):
                    cached["ctx_source"] = "override"

    # models removed from runtime -> mark unavailable
    for name, entry in list(cache.items()):
        if name not in runtime:
            entry["available"] = False
            entry["last_seen"] = now
            _apply_context_overrides(entry)

    # write back in stable order
    reg["source"] = f"ollama@{base_url}"
    reg["last_refresh"] = _iso_now()
    reg["models"] = sorted(cache.values(), key=lambda m: (not m["available"], m["name"].lower()))
    _save_registry(registry_path, reg)
    return reg

def _load_registry(path: Path) -> Dict[str, Any]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"schema": 1, "source": "", "last_refresh": _iso_now(), "models": []}
        _save_registry(path, data)
        return data
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def _save_registry(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
    tmp.replace(path)

def measure_context(base_url: str, name: str, seed: int = 8192, upper: int = 131072, timeout=4) -> Optional[int]:
    """
    Binary-search the max acceptable prompt 'size'. Token counting isn't exposed,
    so we use a synthetic prompt that scales length and detect failure.
    Cached per model after first real use.
    """
    def _can(n: int) -> bool:
        # ~approx n "tokens": repeat a short token-like chunk to grow prompt length
        chunk = " ham" * (n // 2)
        try:
            r = requests.post(
                f"{base_url}/api/generate",
                json={
                    "model": name,
                    "prompt": chunk,
                    "stream": False,
                    "options": {"temperature": 0},
                    "max_tokens": 1
                },
                timeout=timeout
            )
            if r.status_code == 200:
                return True
            try:
                msg = r.json().get("error", "")
            except Exception:
                msg = r.text
            # Treat classic overflow messages as failure
            msg_l = (msg or "").lower()
            return not any(k in msg_l for k in ("context", "too long", "exceed"))
        except Exception:
            # Be conservative on transport failures
            return False

    lo, hi = 1024, seed
    # Grow until it fails or we cap at upper
    while _can(hi) and hi < upper:
        lo, hi = hi, min(hi * 2, upper)

    # If even 'upper' is allowed, give up and return None (very large ctx)
    if hi >= upper and _can(upper):
        return None

    # Binary search the edge
    while lo + 256 <= hi:
        mid = (lo + hi) // 2
        if _can(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo

def _iso_now() -> str:
    import datetime as dt
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
