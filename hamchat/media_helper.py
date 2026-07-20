# hamchat/media_helper.py
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple
import hashlib, base64, imghdr, os, tempfile, shutil
from PIL import Image  # Pillow

THUMB_SIZE = 96  # square
AVATAR_SIZE = 64  # default logical avatar size for profiles (square)

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
    if not ephemeral and db is None:
        # fail safe: without DB we fallback to temp-only handling
        ephemeral = True
    work_dir = tempfile.mkdtemp(prefix="hamchat_")

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
            file_id = dbo.cas_put(db, sha256=sha, mime=mime, src_path=src)
            stored_path = None

        # thumbnail
        thumb_path = os.path.join(work_dir, f"thumb_{sha}.png")
        _make_thumb(src, thumb_path, THUMB_SIZE)
        thumb_sha = _sha256_file(thumb_path)
        thumb_mime = "image/png"

        if not ephemeral and db is not None:
            thumb_file_id = dbo.cas_put(db, sha256=thumb_sha, mime=thumb_mime, src_path=thumb_path)
        else:
            thumb_file_id = None

        # llm part (base64 from the stored copy if ephemeral; else from src or cas fetch)
        b64 = _to_base64(src)  # CAS fetch raw if you prefer
        results["stored"].append({"file_id": file_id, "tmp_path": stored_path, "sha256": sha, "mime": mime})
        results["thumbs"].append({"path": thumb_path, "w": THUMB_SIZE, "h": THUMB_SIZE, "file_id": thumb_file_id, "sha256": thumb_sha, "mime": thumb_mime})
        results["llm_parts"].append({"type": "image", "media_type": mime, "data_base64": b64})
    return results

def store_profile_avatar(src: str, *, db, size: int = AVATAR_SIZE) -> str:
    """
    Normalise a user-selected avatar image into a square thumbnail, store it in CAS,
    and return a filesystem path that the UI can load.

    - src: path or file:// URL chosen by the user.
    - db: open SQLite connection used by db_ops.
    - size: final avatar square size in pixels.
    """
    if db is None:
        # Fallback: just give back the original; avoids crashing in weird states.
        return src

    # Normalise source path (strip file:// for drag-drop style paths).
    src_path = src[7:] if src.lower().startswith("file://") else src
    if not os.path.exists(src_path):
        return src

    # 1) Create a temporary normalised thumbnail on disk.
    tmp_dir = tempfile.mkdtemp(prefix="hamchat_avatar_")
    tmp_avatar = os.path.join(tmp_dir, "avatar.png")
    try:
        # This does the square letterbox + resize; we override the default 96px here.
        _make_thumb(src_path, tmp_avatar, size=size)

        # 2) Push the normalised avatar into CAS so strict-mode encryption is honoured.
        from hamchat import db_ops as dbo

        sha = _sha256_file(tmp_avatar)
        mime = _mime_guess(tmp_avatar)
        file_id = dbo.cas_put(db, sha256=sha, mime=mime, src_path=tmp_avatar)

        # 3) Resolve a readable path for the UI (handles strict vs lite).
        cas_path = dbo.cas_path_for_file(db, file_id)
        if cas_path is None:
            # Fallback to original path if something went sideways.
            return src_path
        return str(cas_path)
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass

def cleanup_profile_avatar(db, old_avatar_path: str) -> None:
    """
    Best-effort cleanup for a *previous* avatar image that was stored in CAS.

    - Only runs if the path looks like a CAS-style SHA filename.
    - Never deletes if the underlying file is still referenced by:
        * any message_files row, or
        * any ai_profiles.avatar row.
    - Deletes both cas/ and cas_tmp/ variants if safe, plus the files row.
    """
    if db is None or not old_avatar_path:
        return

    try:
        from pathlib import Path
        p = Path(old_avatar_path)
        sha_hex = p.name
        # Only touch if it looks like a 64-char hex SHA
        if len(sha_hex) != 64:
            return
        try:
            sha_bytes = bytes.fromhex(sha_hex)
        except ValueError:
            return

        cur = db.cursor()
        try:
            # Find the CAS metadata row
            cur.execute("SELECT id FROM files WHERE sha256 = ?", (sha_bytes,))
            row = cur.fetchone()
            if not row:
                # No DB row; at most try to delete the on-disk file and bail.
                base_dir = os.path.dirname(os.path.dirname(old_avatar_path))
                cas_dirs = [
                    os.path.join(base_dir, "cas"),
                    os.path.join(base_dir, "cas_tmp"),
                ]
                for d in cas_dirs:
                    candidate = os.path.join(d, sha_hex)
                    try:
                        if os.path.exists(candidate):
                            os.remove(candidate)
                    except Exception:
                        pass
                return

            file_id = int(row[0])

            # 1) Is any message still referencing this file?
            cur.execute("SELECT COUNT(*) FROM message_files WHERE file_id = ?", (file_id,))
            msg_count = cur.fetchone()[0] or 0

            # 2) Is any profile still using this avatar path (other than the one we just updated)?
            # We match on the SHA suffix to catch both cas/ and cas_tmp/ variants.
            cur.execute("SELECT COUNT(*) FROM ai_profiles WHERE avatar LIKE ?", (f"%{sha_hex}",))
            prof_count = cur.fetchone()[0] or 0

            if msg_count > 0 or prof_count > 0:
                # Still in use somewhere → do not delete.
                return

            # At this point, no messages and no profiles reference this SHA.
            base_dir = os.path.dirname(os.path.dirname(old_avatar_path))
            cas_dirs = [
                os.path.join(base_dir, "cas"),
                os.path.join(base_dir, "cas_tmp"),
            ]
            for d in cas_dirs:
                candidate = os.path.join(d, sha_hex)
                try:
                    if os.path.exists(candidate):
                        os.remove(candidate)
                except Exception:
                    # Best-effort only
                    pass

            # Remove metadata row
            cur.execute("DELETE FROM files WHERE id = ?", (file_id,))
            db.commit()
        finally:
            cur.close()
    except Exception:
        # Absolutely non-critical; never let avatar cleanup crash the app.
        pass
