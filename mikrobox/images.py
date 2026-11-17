"""Importing vendor images the user supplies (experimental platforms).

Accepts what the vendor hands out and what people usually have lying around:
a .zip holding an .ova, an .ova (a tar holding a .vmdk), a .vmdk, or a .qcow2.
The disk is converted to qcow2 and stored as <data>/images/<platform image name>.

    python -m mikrobox.images add aoscx ArubaOS-CX_10_15_1020_ova.zip
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path

from .config import Settings
from .models import NodeKind
from .platforms import PLATFORMS, Platform

DISK_SUFFIXES = (".vmdk", ".qcow2", ".img", ".raw")
MAX_UPLOAD = 16 * 1024**3


class ImageError(Exception):
    pass


def sniff(path: Path) -> str:
    """zip, tar, qcow2, vmdk or unknown, from the first bytes of the file."""
    with path.open("rb") as f:
        head = f.read(512)
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"QFI\xfb"):
        return "qcow2"
    if head.startswith(b"KDMV") or head.startswith(b"# Disk DescriptorFile"):
        return "vmdk"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return "unknown"


def _pick(names_sizes: list[tuple[str, int]], suffixes: tuple[str, ...]) -> str | None:
    """The largest member whose name ends in one of suffixes (disks dwarf everything else)."""
    hits = [(size, name) for name, size in names_sizes if name.lower().endswith(suffixes)]
    return max(hits)[1] if hits else None


def _extract_disk(src: Path, work: Path, depth: int = 0) -> Path:
    """Follow zip and tar wrappers down to a disk file. Members are copied to names we choose."""
    kind = sniff(src)
    if kind in ("qcow2", "vmdk"):
        return src
    if depth > 3:
        raise ImageError("The archive is nested too deeply to find a disk in it.")
    out = work / f"layer{depth}"
    if kind == "zip":
        with zipfile.ZipFile(src) as z:
            infos = [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]
            name = _pick(infos, DISK_SUFFIXES) or _pick(infos, (".ova", ".tar"))
            if name is None:
                raise ImageError("No .ova, .vmdk or .qcow2 found inside the .zip.")
            with z.open(name) as member, out.open("wb") as dst:
                shutil.copyfileobj(member, dst, 1024 * 1024)
    elif kind == "tar":
        with tarfile.open(src) as t:
            infos = [(m.name, m.size) for m in t.getmembers() if m.isfile()]
            name = _pick(infos, DISK_SUFFIXES)
            if name is None:
                raise ImageError("No .vmdk or .qcow2 found inside the .ova.")
            member = t.extractfile(name)
            with member, out.open("wb") as dst:
                shutil.copyfileobj(member, dst, 1024 * 1024)
    else:
        raise ImageError("That file is not a .zip, .ova, .vmdk or .qcow2.")
    return _extract_disk(out, work, depth + 1)


def _convert(disk: Path, dest: Path) -> None:
    fmt = sniff(disk)
    proc = subprocess.run(["qemu-img", "convert", "-f", fmt, "-O", "qcow2", str(disk), str(dest)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise ImageError(f"qemu-img could not convert the disk: {proc.stderr.strip()[-300:]}")


def guess_version(filename: str) -> str | None:
    m = re.search(r"(\d{2})[._](\d{2})[._](\d{4})", filename or "")
    return ".".join(m.groups()) if m else None


def _nodes_of(settings: Settings, kind: NodeKind):
    from .store import Store

    store = Store(settings.data_dir / "state.json")
    return [n for n in store.state.all_nodes() if n.kind == kind]


def _running(settings: Settings, node) -> bool:
    from .network import pid_alive

    return pid_alive(settings.run_dir / f"n{node.id}.pid", "qemu") is not None


def install(settings: Settings, p: Platform, src: Path, original_name: str = "") -> dict:
    """Unpack and convert src into the platform's image. src is left in place.

    Every existing device of this platform is reset to factory defaults, because its
    disk only records changes against the image being replaced."""
    nodes = _nodes_of(settings, p.kind)
    busy = [n.name for n in nodes if _running(settings, n)]
    if busy:
        raise ImageError(f"Stop {', '.join(busy)} first: the image is in use.")
    images = settings.data_dir / "images"
    images.mkdir(parents=True, exist_ok=True)
    need = src.stat().st_size * 3
    if shutil.disk_usage(images).free < need:
        raise ImageError(f"Not enough disk space: about {need // 1024**3 + 1} GB free is needed.")
    with tempfile.TemporaryDirectory(dir=images, prefix=".import-") as tmp:
        work = Path(tmp)
        disk = _extract_disk(src, work)
        converted = work / "converted.qcow2"
        _convert(disk, converted)
        dest = p.image_path(settings)
        os.replace(converted, dest)
    for n in nodes:
        (settings.data_dir / "disks" / f"n{n.id}.qcow2").unlink(missing_ok=True)
    version = guess_version(original_name or src.name)
    version_file = dest.with_suffix(".version")
    if version:
        version_file.write_text(version + "\n")
    else:
        version_file.unlink(missing_ok=True)
    return {"path": str(dest), "size": dest.stat().st_size, "version": version, "reset": len(nodes)}


class Importer:
    """Runs one import at a time per platform in the background, for the web upload."""

    def __init__(self, settings: Settings, on_installed=None) -> None:
        self.s = settings
        self.on_installed = on_installed  # called with the platform after a successful import
        self.status: dict[str, dict] = {}
        self.lock = threading.Lock()

    def busy(self, kind: NodeKind) -> bool:
        return self.status.get(kind.value, {}).get("state") in ("uploading", "converting")

    def upload_path(self, kind: NodeKind) -> Path:
        return self.s.data_dir / "images" / f".upload-{kind.value}"

    def begin(self, kind: NodeKind) -> None:
        with self.lock:
            if self.busy(kind):
                raise ImageError("An import for this platform is already running.")
            self.status[kind.value] = {"state": "uploading", "message": "Receiving the file"}

    def fail(self, kind: NodeKind, message: str) -> None:
        self.status[kind.value] = {"state": "error", "message": message}
        self.upload_path(kind).unlink(missing_ok=True)

    def start(self, kind: NodeKind, original_name: str) -> None:
        self.status[kind.value] = {"state": "converting", "message": "Unpacking and converting the disk"}
        threading.Thread(target=self._run, args=(kind, original_name), daemon=True).start()

    def _run(self, kind: NodeKind, original_name: str) -> None:
        p = PLATFORMS[kind]
        src = self.upload_path(kind)
        try:
            result = install(self.s, p, src, original_name)
            if self.on_installed:
                self.on_installed(p)
            version = f" {result['version']}" if result["version"] else ""
            self.status[kind.value] = {"state": "done", "message": f"{p.label}{version} is ready."}
        except (ImageError, OSError, zipfile.BadZipFile, tarfile.TarError) as e:
            self.status[kind.value] = {"state": "error", "message": str(e)}
        finally:
            src.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    kinds = {p.kind.value: p for p in PLATFORMS.values() if p.experimental}
    if len(args) != 3 or args[0] != "add" or args[1] not in kinds:
        print(f"usage: python -m mikrobox.images add {{{','.join(kinds)}}} FILE", file=sys.stderr)
        return 1
    src = Path(args[2])
    if not src.is_file():
        print(f"{src} is not a file.", file=sys.stderr)
        return 1
    try:
        result = install(Settings.from_env(), kinds[args[1]], src, src.name)
    except (ImageError, zipfile.BadZipFile, tarfile.TarError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Installed {result['path']} ({result['size'] // 1024**2} MB"
          + (f", version {result['version']}" if result["version"] else "") + ").")
    if result["reset"]:
        print(f"{result['reset']} existing {kinds[args[1]].label} device(s) were reset to factory defaults.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
