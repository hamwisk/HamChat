# hamchat/media_helper.py
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple
import hashlib, base64, imghdr, os, tempfile, shutil
from PIL import Image  # Pillow

THUMB_SIZE = 96  # square

def _sha256_file(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _mime_guess(p: str) -> str:
    kind = imghdr.what(p) or ""
    return {"png":"image/png","jpeg":"image/jpeg","gif":"image/gif","bmp":"image/bmp","tiff":"image/tiff"}.get(kind, "application/octet-stream")

def _make_thumb(src: str, dst: str, size: int = THUMB_SIZE) -> Tuple[int,int]:
    im = Image.open(src).convert("RGBA")
    # letterbox into square
    w, h = im.size
    scale = min(size / w, size / h)
    nw, nh = max(1,int(w*scale)), max(1,int(h*scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    bg = Image.new("RGBA", (size, size), (0,0,0,0))
    bg.paste(im, ((size-nw)//2, (size-nh)//2))
    bg.save(dst)
    return size, size

def _to_base64(p: str) -> str:
    with open(p, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")

def process_images(paths: List[str], *, ephemeral: bool, db=None, session=None):
    """
    Returns dict: {stored:[...], thumbs:[...], llm_parts:[...]}
    - guest/admin → copy into temp dir
    - user → CAS: insert-or-ignore by sha256; return file_id (requires db_ops)
    """
    results = {"stored": [], "thumbs": [], "llm_parts": []}
    work_dir = tempfile.mkdtemp(prefix="hamchat_") if ephemeral else None

    for idx, p in enumerate(paths):
        src = p[7:] if p.lower().startswith("file://") else p
        sha = _sha256_file(src)
        mime = _mime_guess(src)

        if ephemeral:
            # copy into our temp vault
            base = os.path.basename(src)
            dst = os.path.join(work_dir, f"{sha}_{base}")
            shutil.copy2(src, dst)
            stored_path = dst
            file_id = None
        else:
            # CAS store via db_ops (implements de-dupe by sha256)
            from hamchat import db_ops as dbo
            file_id = dbo.cas_put(db, sha256=sha, mime=mime, src_path=src)  # you’ll add this
            stored_path = None

        # thumbnail
        thumb_path = os.path.join(work_dir or os.path.dirname(src), f"thumb_{sha}.png")
        _make_thumb(src, thumb_path, THUMB_SIZE)

        # llm part (base64 from the stored copy if ephemeral; else from src or cas fetch)
        b64 = _to_base64(src) if ephemeral else _to_base64(src)  # CAS fetch raw if you prefer
        results["stored"].append({"file_id": file_id, "tmp_path": stored_path, "sha256": sha, "mime": mime})
        results["thumbs"].append({"path": thumb_path, "w": THUMB_SIZE, "h": THUMB_SIZE})
        results["llm_parts"].append({"type": "image", "media_type": mime, "data_base64": b64})
    return results
