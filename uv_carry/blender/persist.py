"""Saving carried images on request.

A carry changes images in memory only; Ctrl+Z undoes it. The UV Carry panel lists the images that carries
changed and that are still unsaved, and its button saves them, each on its own:

- an image from a file: the file on disk is first copied to a backup. Blender's own Image.save then writes
  the image next to the file under a temporary name, and the temporary file replaces the file (os.replace),
  so the file is never left half written. It holds what Image > Save would write: 8-bit PNG and TGA
  exactly; JPEG compresses again; float images go through Blender's colour space and alpha conversions;
- a packed image is packed again from memory (Image.pack); it reaches the disk when the .blend is saved;
- a generated image has no file: it stays listed, for Image > Save As.

Backups go to the extension's user folder (a temporary folder when UV Carry does not run as an extension),
the newest BACKUPS_KEPT per file; they are pruned only after a save that succeeded. A save that fails leaves
the file as it was and the image still unsaved, also for Blender's own warning on quit. Undo does not touch
files on disk. FAULTS (uv_carry.blender.controller) can fail a save at "save_backup:<i>", "save_write:<i>"
and "save_replace:<i>" for tests.
"""

import hashlib
import os
import shutil
import stat
import tempfile
import time

import bpy

from .. import __package__ as BASE_PACKAGE

BACKUPS_KEPT = 5
SAVING = ".uv_carry_saving"         # infix of the temporary file next to an image's file
CHANGED = {}                        # image name -> {"pointer": as_pointer(), "unsaved": bool}


def note_carried(images):
    """Images a carry just wrote."""
    for img in images:
        CHANGED[img.name] = {"pointer": img.as_pointer(), "unsaved": True}


def forget_all():
    CHANGED.clear()


def kind_of(img):
    """"packed", "file" or "generated" (no file to write to)."""
    if img.packed_file is not None:
        return "packed"
    if img.source == "FILE" and img.filepath:
        return "file"
    return "generated"


def pending():
    """[(image, kind)] for the images carries changed that are unsaved: never saved since, or changed again
    in memory (another carry, an undo, painting)."""
    out = []
    for name, entry in list(CHANGED.items()):
        img = bpy.data.images.get(name)
        if img is None or img.as_pointer() != entry["pointer"]:
            del CHANGED[name]
            continue
        if entry["unsaved"] or img.is_dirty:
            out.append((img, kind_of(img)))
    return out


def backup_root():
    try:
        return bpy.utils.extension_path_user(BASE_PACKAGE, path="backups", create=True)
    except (ValueError, KeyError, RuntimeError):      # not running as an installed extension
        root = os.path.join(tempfile.gettempdir(), "uv_carry_backups")
        os.makedirs(root, exist_ok=True)
        return root


def backup_folder(path):
    """The backups of one file: its name and a digest of its full path, so equal names do not mix."""
    name = os.path.basename(path)
    folder = os.path.join(backup_root(), f"{name}_{hashlib.sha1(os.path.normcase(path).encode()).hexdigest()[:10]}")
    os.makedirs(folder, exist_ok=True)
    return folder


def backups(path):
    """Backups of `path`, oldest first."""
    folder = backup_folder(path)
    return [os.path.join(folder, f) for f in sorted(os.listdir(folder))]


def _backup(path):
    """A copy of the file's bytes, not of its permissions: a read-only file still gives a backup that can be
    pruned."""
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10 ** 9:09d}"
    dest = os.path.join(backup_folder(path), f"{stamp}_{os.path.basename(path)}")
    shutil.copyfile(path, dest)
    return dest


def _prune(path):
    """Remove the backups of `path` older than the newest BACKUPS_KEPT. Returns the ones that could not be
    removed; a save that succeeded stays a success."""
    stuck = []
    for old in backups(path)[:-BACKUPS_KEPT]:
        try:
            os.chmod(old, stat.S_IREAD | stat.S_IWRITE)
            os.remove(old)
        except OSError as exc:
            stuck.append(f"{os.path.basename(old)} ({exc.strerror or exc})")
    return stuck


def _mark_unsaved(img):
    """Image.save clears the image's changed flag even when the file then fails to take its place: set it
    again, so that Blender still warns about the image on quit."""
    img.pixels[0] = img.pixels[0]


def _save_file(img, i, faults):
    path = bpy.path.abspath(img.filepath, library=img.library)
    stem, ext = os.path.splitext(os.path.basename(path))
    tmp = os.path.join(os.path.dirname(path), f".{stem}{SAVING}{ext}")
    backup, written = None, False
    try:
        faults.check(f"save_backup:{i}")
        if os.path.exists(path):
            backup = _backup(path)
        faults.check(f"save_write:{i}")
        written = True
        img.save(filepath=tmp)
        faults.check(f"save_replace:{i}")
        os.replace(tmp, path)
    except Exception as exc:            # reported, never hidden; the file keeps what it had
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        if written:
            _mark_unsaved(img)
        kept = f"; backup at {backup}" if backup else ""
        hint = (" (the file or its folder may be read-only, or held open by another program)"
                if isinstance(exc, PermissionError) else "")
        return {"image": img.name, "kind": "file", "outcome": "failed", "path": path, "backup": backup,
                "error": f"{type(exc).__name__}: {exc}",
                "message": f"Saving {img.name} failed{hint}: {exc}; its file on disk is unchanged{kept}"}
    CHANGED[img.name]["unsaved"] = False
    stuck = _prune(path)
    message = f"{img.name} saved" + (" (backup of the previous file kept)" if backup else "")
    if stuck:
        message += f"; old backups not removed: {', '.join(stuck)}"
    return {"image": img.name, "kind": "file", "outcome": "saved", "path": path, "backup": backup,
            "stuck_backups": stuck, "message": message}


def _repack(img, i, faults):
    try:
        faults.check(f"save_write:{i}")
        img.pack()
    except Exception as exc:
        return {"image": img.name, "kind": "packed", "outcome": "failed", "error": f"{type(exc).__name__}: {exc}",
                "message": f"Packing {img.name} again failed ({exc}); the packed copy is unchanged"}
    CHANGED[img.name]["unsaved"] = False
    return {"image": img.name, "kind": "packed", "outcome": "packed",
            "message": f"{img.name} packed again; it is written with the .blend"}


def save(entries, faults):
    """Save each (image, kind) of `entries` on its own. Returns one result per image: outcome "saved",
    "packed", "unsaved" (a generated image) or "failed", and a message."""
    results = []
    for i, (img, kind) in enumerate(entries):
        if kind == "file":
            results.append(_save_file(img, i, faults))
        elif kind == "packed":
            results.append(_repack(img, i, faults))
        else:
            results.append({"image": img.name, "kind": kind, "outcome": "unsaved",
                            "message": f"{img.name} has no file: save it with Image > Save As"})
    return results


def summary(results):
    """(severity, one message) for the panel and the report. Only files written to disk are called saved: a
    packed image reaches the disk with the .blend."""
    failed = [r for r in results if r["outcome"] == "failed"]
    saved = [r["image"] for r in results if r["outcome"] == "saved"]
    packed = [r["image"] for r in results if r["outcome"] == "packed"]
    left = [r for r in results if r["outcome"] == "unsaved"]
    parts = []
    if saved:
        parts.append("Saved " + ", ".join(saved))
    if packed:
        parts.append("Packed " + ", ".join(packed) + " again: saving the .blend writes "
                     + ("it" if len(packed) == 1 else "them") + " to disk")
    parts += [r["message"] for r in failed + left]
    return ("WARNING" if failed or left else "INFO"), ". ".join(parts) + "."
