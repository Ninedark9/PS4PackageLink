#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import queue
import re
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import Tk, StringVar, BooleanVar, filedialog, messagebox, Text, END, PhotoImage
from tkinter import ttk

APP_NAME = "PS4 Package Link"
VERSION = "0.6.4"
HTTP_PORT = 8765
DISCOVERY_PORT = 8766
DISCOVERY_MAGIC = b"P4LINK_DISCOVER_V1"
DISCOVERY_REPLY = "P4LINK_HERE_V1|{url}"
PKG_MAGIC = b"\x7fCNT"
PKG_HEADER_SIZE = 0x2000
PKG_ENTRY_SIZE = 0x20
PKG_ENTRY_PARAM_SFO = 0x1000
PKG_ENTRY_ICON0_PNG = 0x1200


def be32(data: bytes, off: int) -> int:
    return struct.unpack_from(">I", data, off)[0]


def be64(data: bytes, off: int) -> int:
    return struct.unpack_from(">Q", data, off)[0]


def parse_sfo(data: bytes) -> dict[str, object]:
    if len(data) < 0x14 or data[:4] != b"\x00PSF":
        return {}
    key_table = struct.unpack_from("<I", data, 0x08)[0]
    data_table = struct.unpack_from("<I", data, 0x0C)[0]
    count = struct.unpack_from("<I", data, 0x10)[0]
    out: dict[str, object] = {}
    ent = 0x14
    for _ in range(count):
        if ent + 0x10 > len(data):
            break
        key_off, fmt = struct.unpack_from("<HH", data, ent)
        length, max_len, data_off = struct.unpack_from("<III", data, ent + 4)
        ent += 0x10
        kpos = key_table + key_off
        if kpos >= len(data):
            continue
        kend = data.find(b"\x00", kpos)
        if kend < 0:
            continue
        key = data[kpos:kend].decode("utf-8", "replace")
        dpos = data_table + data_off
        raw = data[dpos:dpos + min(length, max_len)]
        if fmt in (0x0204, 0x0004):
            out[key] = raw.rstrip(b"\x00").decode("utf-8", "replace")
        elif fmt == 0x0404 and len(raw) >= 4:
            out[key] = struct.unpack_from("<I", raw, 0)[0]
        else:
            out[key] = raw.hex()
    return out


@dataclass
class PackageInfo:
    id: str
    filename: str
    path: str
    title: str
    title_id: str
    content_id: str
    version: str
    size: int
    declared_size: int
    content_type: int
    flags: int
    is_patch: bool
    package_type: str
    digest: str
    icon_offset: int
    icon_size: int
    valid: bool
    error: str = ""

    @property
    def pkg_url_path(self) -> str:
        return f"/pkg/{urllib.parse.quote(self.id)}"

    @property
    def ref_url_path(self) -> str:
        return f"/ref/{urllib.parse.quote(self.id)}.json"

    @property
    def icon_url_path(self) -> str:
        return f"/icon/{urllib.parse.quote(self.id)}.png"


@dataclass
class CatalogApp:
    id: str
    name: str
    category: str
    pkg_url: str
    image: str = ""
    filename: str = ""
    description: str = ""


def safe_filename(text: str, suffix: str = ".pkg") -> str:
    text = re.sub(r"[^A-Za-z0-9._ -]+", "_", text).strip(" ._")
    if not text:
        text = "package"
    if suffix and not text.lower().endswith(suffix.lower()):
        text += suffix
    return text


def catalog_filename(app: CatalogApp) -> str:
    if app.filename:
        return safe_filename(Path(app.filename).name)
    url_name = Path(urllib.parse.urlparse(app.pkg_url).path).name
    if url_name.lower().endswith(".pkg"):
        return safe_filename(url_name)
    return safe_filename(app.name)


def load_catalog(path: Path) -> list[CatalogApp]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"apps.json: {e}") from e
    items = raw.get("apps", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("apps.json must contain an 'apps' array or be an array")
    out: list[CatalogApp] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        pkg_url = str(item.get("pkg_url") or item.get("url") or "").strip()
        if not name or not pkg_url:
            continue
        raw_id = str(item.get("id") or "").strip()
        if not raw_id:
            raw_id = hashlib.sha1((name + "\0" + pkg_url).encode("utf-8", "replace")).hexdigest()[:12]
        out.append(CatalogApp(
            id=raw_id,
            name=name,
            category=str(item.get("category") or "Other").strip() or "Other",
            pkg_url=pkg_url,
            image=str(item.get("image") or item.get("image_url") or "").strip(),
            filename=str(item.get("filename") or "").strip(),
            description=str(item.get("description") or "").strip(),
        ))
    return out




def validate_content_id(content_id: str) -> tuple[bool, str]:
    """Validate the structural Content ID format expected by PS4 BGFT.

    Flatz's remote_pkg_installer requires a 0x24 (36-char) Content ID with
    service id, 9-char title id and 16-char label, e.g.
    UP0000-CUSA00000_00-0000000000000000.
    """
    cid = (content_id or "").strip()
    if len(cid) != 0x24:
        return False, f"CONTENT_ID must be 36 characters (got {len(cid)})"
    if cid[6:7] != "-" or cid[16:17] != "_" or cid[19:20] != "-":
        return False, "CONTENT_ID separators are invalid (expected XX0000-CUSA00000_00-XXXXXXXXXXXXXXXX)"
    service_id = cid[:6]
    title_id = cid[7:16]
    label = cid[20:36]
    if len(title_id) != 9 or len(label) != 16:
        return False, "CONTENT_ID title id/label length is invalid"
    if not re.fullmatch(r"[A-Z]{2}[0-9]{4}", service_id):
        return False, "CONTENT_ID service id must look like IV0000/UP0000/EP0000"
    if not re.fullmatch(r"[A-Z]{4}[0-9]{5}", title_id):
        return False, "TITLE_ID must be 4 uppercase letters followed by 5 digits"
    if any(ord(c) < 0x21 or ord(c) > 0x7E for c in cid):
        return False, "CONTENT_ID contains non-printable/whitespace characters"
    return True, ""

def package_type_from_content_type(ct: int) -> str:
    return {0x1A: "PS4GD", 0x1B: "PS4AC", 0x1C: "PS4AL", 0x1E: "PS4DP"}.get(ct, "PS4GD")


def pkg_id_for(path: Path) -> str:
    h = hashlib.sha1()
    h.update(str(path.resolve()).encode("utf-8", "surrogatepass"))
    h.update(str(path.stat().st_size).encode())
    return h.hexdigest()[:16]


def parse_pkg(path: Path) -> PackageInfo:
    try:
        st = path.stat()
        with path.open("rb") as f:
            hdr = f.read(PKG_HEADER_SIZE)
            if len(hdr) < PKG_HEADER_SIZE or hdr[:4] != PKG_MAGIC:
                raise ValueError("Not a PS4 PKG (bad magic/header)")

            entry_count = be32(hdr, 0x10)
            entry_table_offset = be32(hdr, 0x18)
            content_id = hdr[0x40:0x40 + 0x24].split(b"\x00", 1)[0].decode("ascii", "replace")
            content_type = be32(hdr, 0x74)
            flags = be32(hdr, 0x78)
            declared_size = be64(hdr, 0x430)
            digest = hdr[0xFE0:0xFE0 + 0x20].hex()

            f.seek(entry_table_offset)
            table = f.read(entry_count * PKG_ENTRY_SIZE)
            sfo_offset = sfo_size = 0
            icon_offset = icon_size = 0
            for i in range(entry_count):
                off = i * PKG_ENTRY_SIZE
                if off + PKG_ENTRY_SIZE > len(table):
                    break
                eid = be32(table, off)
                if eid == PKG_ENTRY_PARAM_SFO:
                    sfo_offset = be32(table, off + 0x10)
                    sfo_size = be32(table, off + 0x14)
                elif eid == PKG_ENTRY_ICON0_PNG:
                    icon_offset = be32(table, off + 0x10)
                    icon_size = be32(table, off + 0x14)

            sfo = {}
            if sfo_offset and sfo_size:
                f.seek(sfo_offset)
                sfo = parse_sfo(f.read(sfo_size))

        title = str(sfo.get("TITLE") or path.stem)
        title_id = str(sfo.get("TITLE_ID") or "")
        version = str(sfo.get("APP_VER") or sfo.get("VERSION") or "")
        header_content_id = content_id
        sfo_content_id = str(sfo.get("CONTENT_ID") or "").strip()
        if sfo_content_id:
            content_id = sfo_content_id
        else:
            content_id = header_content_id.strip()

        cid_ok, cid_error = validate_content_id(content_id)
        if not cid_ok:
            raise ValueError(f"Invalid PARAM.SFO CONTENT_ID: {cid_error}; value={content_id!r}")

        patch_mask = 0x00100000 | 0x40000000 | 0x41000000 | 0x60000000
        is_patch = bool(flags & patch_mask)
        return PackageInfo(
            id=pkg_id_for(path), filename=path.name, path=str(path.resolve()), title=title,
            title_id=title_id, content_id=content_id, version=version, size=st.st_size,
            declared_size=declared_size or st.st_size, content_type=content_type, flags=flags,
            is_patch=is_patch, package_type=package_type_from_content_type(content_type),
            digest=digest, icon_offset=icon_offset, icon_size=icon_size, valid=True
        )
    except Exception as e:
        return PackageInfo(
            id=pkg_id_for(path), filename=path.name, path=str(path.resolve()), title=path.stem,
            title_id="", content_id="", version="", size=path.stat().st_size if path.exists() else 0,
            declared_size=0, content_type=0, flags=0, is_patch=False, package_type="PS4GD",
            digest="", icon_offset=0, icon_size=0, valid=False, error=str(e)
        )


class State:
    def __init__(self, library_dir: Path):
        self.library_dir = library_dir
        self.base_dir = library_dir.parent
        self.hidden_path = self.base_dir / "hidden_packages.json"
        self.apps_path = self.base_dir / "apps.json"
        self.lock = threading.RLock()
        self.packages: dict[str, PackageInfo] = {}
        self.hidden_ids: set[str] = set()
        self.pending: dict | None = None
        self.last_ack: dict | None = None
        self.logs: list[str] = []
        self.bytes_sent = 0
        self.active_transfers = 0
        self.last_client = "-"
        self.rate_mbps = 0.0
        self._rate_bytes = 0
        self._rate_t = time.monotonic()
        self._known_clients: set[str] = set()
        self.download_state = {
            "active": False, "name": "", "url": "", "downloaded": 0, "total": 0,
            "rate_mbps": 0.0, "percent": 0.0, "error": "", "path": ""
        }
        self.ui_events: queue.Queue = queue.Queue()
        self._load_hidden()
        self.scan()
        self.log("Companion started")

    def log(self, message: str):
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > 300:
                self.logs = self.logs[-300:]

    def _load_hidden(self):
        try:
            raw = json.loads(self.hidden_path.read_text(encoding="utf-8")) if self.hidden_path.exists() else []
            if isinstance(raw, dict):
                raw = raw.get("hidden", [])
            self.hidden_ids = {str(x) for x in raw if x}
        except Exception:
            self.hidden_ids = set()

    def _save_hidden(self):
        self.hidden_path.write_text(json.dumps({"hidden": sorted(self.hidden_ids)}, indent=2), encoding="utf-8")

    def client_seen(self, ip: str):
        with self.lock:
            self.last_client = ip
            if ip not in self._known_clients:
                self._known_clients.add(ip)
                self.log(f"PS4/client connected: {ip}")

    def transfer_begin(self, ip: str, pkg: str):
        with self.lock:
            self.active_transfers += 1
            self.last_client = ip
        self.log(f"Transfer started -> {ip}: {pkg}")

    def transfer_bytes(self, count: int):
        now = time.monotonic()
        with self.lock:
            self.bytes_sent += count
            self._rate_bytes += count
            dt = now - self._rate_t
            if dt >= 0.45:
                self.rate_mbps = (self._rate_bytes / dt) / (1024 * 1024)
                self._rate_bytes = 0
                self._rate_t = now

    def transfer_end(self, ip: str, pkg: str):
        with self.lock:
            self.active_transfers = max(0, self.active_transfers - 1)
            if self.active_transfers == 0:
                self.rate_mbps = 0.0
                self._rate_bytes = 0
                self._rate_t = time.monotonic()
        self.log(f"Transfer finished -> {ip}: {pkg}")

    def scan(self):
        self.library_dir.mkdir(parents=True, exist_ok=True)
        found: dict[str, PackageInfo] = {}
        for p in sorted(self.library_dir.glob("*.pkg"), key=lambda x: x.name.lower()):
            info = parse_pkg(p)
            found[info.id] = info
        with self.lock:
            self.packages = found
            stale = self.hidden_ids.difference(found.keys())
            if stale:
                self.hidden_ids.difference_update(stale)
                self._save_hidden()

    def visible_packages(self, show_hidden: bool = False) -> list[PackageInfo]:
        with self.lock:
            vals = list(self.packages.values())
            if show_hidden:
                return vals
            return [p for p in vals if p.id not in self.hidden_ids]

    def add_path(self, src: Path):
        if src.parent.resolve() != self.library_dir.resolve():
            dst = self.library_dir / src.name
            if dst.exists() and dst.resolve() != src.resolve():
                stem, suf = dst.stem, dst.suffix
                n = 2
                while dst.exists():
                    dst = self.library_dir / f"{stem}_{n}{suf}"
                    n += 1
            try:
                os.link(src, dst)
            except Exception:
                import shutil
                shutil.copy2(src, dst)
        self.scan()

    def _unique_library_target(self, name: str) -> Path:
        target = self.library_dir / Path(name).name
        if not target.exists():
            return target
        stem, suffix = target.stem, target.suffix
        idx = 2
        while target.exists():
            target = self.library_dir / f"{stem}_{idx}{suffix}"
            idx += 1
        return target

    @staticmethod
    def _find_7zip() -> str | None:
        candidates = [
            shutil.which("7z"), shutil.which("7z.exe"), shutil.which("7zz"),
            os.environ.get("SEVENZIP"),
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
        ]
        for item in candidates:
            if item and Path(item).exists():
                return str(Path(item))
        return None

    @staticmethod
    def _archive_root(path: Path) -> Path:
        m = re.match(r"^(.*)\.part(\d+)\.rar$", path.name, re.I)
        if m:
            return path.with_name(m.group(1) + ".part1.rar")
        m = re.match(r"^(.*\.(?:7z|zip))\.(\d{3})$", path.name, re.I)
        if m:
            return path.with_name(m.group(1) + ".001")
        return path

    @staticmethod
    def _safe_extract_zip(src: Path, dest: Path):
        with zipfile.ZipFile(src, 'r') as zf:
            base = dest.resolve()
            for member in zf.infolist():
                out = (dest / member.filename).resolve()
                if base != out and base not in out.parents:
                    raise ValueError(f"Unsafe ZIP path: {member.filename}")
            zf.extractall(dest)

    @staticmethod
    def _split_pkg_groups(root: Path) -> list[tuple[Path, list[Path]]]:
        groups: dict[str, list[tuple[int, Path]]] = {}
        for f in root.rglob('*'):
            if not f.is_file():
                continue
            m = re.match(r"^(.*\.pkg)\.(\d{1,3})$", f.name, re.I)
            if not m:
                continue
            groups.setdefault(str(f.with_name(m.group(1))), []).append((int(m.group(2)), f))
        out = []
        for base, parts in groups.items():
            parts.sort(key=lambda x: x[0])
            nums = [n for n, _ in parts]
            start = nums[0]
            if nums != list(range(start, start + len(nums))):
                continue
            out.append((Path(base), [f for _, f in parts]))
        return out

    def _reassemble_split_pkgs(self, root: Path) -> list[Path]:
        created: list[Path] = []
        for output, parts in self._split_pkg_groups(root):
            try:
                with parts[0].open('rb') as f:
                    if f.read(4) != PKG_MAGIC:
                        continue
                if output.exists():
                    output = output.with_name(output.stem + "_reassembled.pkg")
                self.log(f"Reassembling split PKG: {output.name} ({len(parts)} parts)")
                with output.open('wb') as dst:
                    for idx, part in enumerate(parts, 1):
                        self.log(f"  part {idx}/{len(parts)}: {part.name}")
                        with part.open('rb') as src:
                            shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
                info = parse_pkg(output)
                if not info.valid:
                    output.unlink(missing_ok=True)
                    self.log(f"Split PKG validation failed: {info.error}")
                    continue
                if info.declared_size and output.stat().st_size < info.declared_size:
                    output.unlink(missing_ok=True)
                    self.log(f"Split PKG incomplete: {output.name}")
                    continue
                created.append(output)
            except Exception as e:
                self.log(f"Split PKG reassembly failed: {e}")
        return created

    def _game_import_worker(self, selections: list[str]):
        temp_dir = Path(tempfile.mkdtemp(prefix='p4link-game-'))
        imported: list[str] = []
        try:
            self.log(f"Game Import started: {len(selections)} selected file(s)")
            selected = [Path(x) for x in selections]
            direct_pkg = [p for p in selected if p.suffix.lower() == '.pkg']
            split_piece = [p for p in selected if re.search(r"\.pkg\.\d{1,3}$", p.name, re.I)]
            if split_piece:
                expanded: dict[str, Path] = {str(p.resolve()): p for p in split_piece if p.exists()}
                for picked in list(split_piece):
                    m = re.match(r"^(.*\.pkg)\.(\d{1,3})$", picked.name, re.I)
                    if not m:
                        continue
                    for sibling in picked.parent.iterdir():
                        if sibling.is_file() and re.match(re.escape(m.group(1)) + r"\.\d{1,3}$", sibling.name, re.I):
                            expanded[str(sibling.resolve())] = sibling
                split_piece = list(expanded.values())
            archives: list[Path] = []
            for p in selected:
                n = p.name.lower()
                if p in direct_pkg or p in split_piece:
                    continue
                if n.endswith(('.zip', '.rar', '.7z', '.7z.001', '.zip.001')) or re.search(r"\.part\d+\.rar$", n):
                    archives.append(self._archive_root(p))

            for src in direct_pkg:
                if not src.exists():
                    continue
                target = self._unique_library_target(src.name)
                try:
                    os.link(src, target)
                except Exception:
                    shutil.copy2(src, target)
                imported.append(target.name)
                self.log(f"Imported PKG: {target.name}")

            if split_piece:
                split_root = temp_dir / 'split'
                split_root.mkdir(parents=True, exist_ok=True)
                for src in split_piece:
                    shutil.copy2(src, split_root / src.name)
                for rebuilt in self._reassemble_split_pkgs(split_root):
                    target = self._unique_library_target(rebuilt.name)
                    shutil.move(str(rebuilt), str(target))
                    imported.append(target.name)
                    self.log(f"Imported rebuilt PKG: {target.name}")

            seen_archives: set[str] = set()
            seven = self._find_7zip()
            for arc in archives:
                key = str(arc.resolve()) if arc.exists() else str(arc)
                if key in seen_archives:
                    continue
                seen_archives.add(key)
                if not arc.exists():
                    raise FileNotFoundError(f"Archive first volume not found: {arc}")
                dest = temp_dir / f"archive_{len(seen_archives)}"
                dest.mkdir(parents=True, exist_ok=True)
                lower = arc.name.lower()
                if lower.endswith('.zip') and not re.search(r"\.zip\.\d{3}$", lower):
                    self.log(f"Extracting ZIP: {arc.name}")
                    self._safe_extract_zip(arc, dest)
                else:
                    if not seven:
                        raise RuntimeError("RAR/7z/multipart archive support requires 7-Zip. Install 7-Zip or set SEVENZIP to 7z.exe.")
                    self.log(f"Extracting with 7-Zip: {arc.name}")
                    proc = subprocess.run([seven, 'x', '-y', f'-o{dest}', str(arc)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
                    if proc.returncode != 0:
                        tail = '\n'.join(proc.stdout.splitlines()[-10:])
                        raise RuntimeError(f"7-Zip extraction failed ({proc.returncode}):\n{tail}")

                rebuilt = self._reassemble_split_pkgs(dest)
                rebuilt_set = {x.resolve() for x in rebuilt}
                pkg_files = [p for p in dest.rglob('*.pkg') if p.resolve() not in rebuilt_set]
                pkg_files.extend(rebuilt)
                if not pkg_files:
                    raise RuntimeError(f"No .pkg files found after extracting {arc.name}")
                self.log(f"Found {len(pkg_files)} PKG file(s) in {arc.name}")
                for src in sorted(pkg_files, key=lambda x: x.name.lower()):
                    info = parse_pkg(src)
                    if not info.valid:
                        self.log(f"Skipped invalid PKG: {src.name}: {info.error}")
                        continue
                    target = self._unique_library_target(src.name)
                    shutil.copy2(src, target)
                    imported.append(target.name)
                    self.log(f"Imported from archive: {target.name}")

            self.scan()
            if not imported:
                raise RuntimeError("No valid PS4 PKG was imported")
            self.log(f"Game Import complete: {len(imported)} PKG file(s)")
            self.ui_events.put(("game_import_done", {"ok": True, "files": imported}))
        except Exception as e:
            self.log(f"Game Import failed: {e}")
            self.ui_events.put(("game_import_done", {"ok": False, "error": str(e)}))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def import_game(self, selections: list[str]):
        if not selections:
            return
        threading.Thread(target=self._game_import_worker, args=(list(selections),), daemon=True, name='game-import').start()

    def hide_package(self, package_id: str) -> PackageInfo:
        with self.lock:
            info = self.packages.get(package_id)
            if not info:
                raise FileNotFoundError("Package is no longer in the library")
            self.hidden_ids.add(package_id)
            self._save_hidden()
        self.log(f"Removed from PC list: {info.title}")
        return info

    def restore_package(self, package_id: str) -> PackageInfo:
        with self.lock:
            info = self.packages.get(package_id)
            if not info:
                raise FileNotFoundError("Package is no longer in the library")
            self.hidden_ids.discard(package_id)
            self._save_hidden()
        self.log(f"Restored to PC list: {info.title}")
        return info

    def delete_package(self, package_id: str) -> PackageInfo:
        with self.lock:
            info = self.packages.get(package_id)
            if not info:
                raise FileNotFoundError("Package is no longer in the library")
            if self.pending and self.pending.get("id") == package_id:
                self.pending = None
                self.log(f"Pending queue cleared before delete: {info.title}")
            path = Path(info.path)
        root = self.library_dir.resolve()
        resolved = path.resolve()
        if resolved.parent != root:
            raise PermissionError("Refusing to delete a PKG outside the companion library folder")
        if not resolved.exists():
            raise FileNotFoundError(str(resolved))
        resolved.unlink()
        with self.lock:
            self.hidden_ids.discard(package_id)
            self._save_hidden()
        self.log(f"Deleted PKG from library: {info.filename}")
        self.scan()
        return info

    def _download_worker(self, url: str, target: Path, display_name: str, overwrite: bool):
        temp = target.with_suffix(target.suffix + ".part")
        try:
            if target.exists() and not overwrite:
                raise FileExistsError(f"{target.name} already exists in the library")
            if temp.exists():
                temp.unlink()
            req = urllib.request.Request(url, headers={"User-Agent": f"PS4PackageLink/{VERSION}"})
            self.log(f"Download started: {display_name} <- {url}")
            with urllib.request.urlopen(req, timeout=30) as resp:
                final_url = resp.geturl()
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                rate_bytes = 0
                rate_t = time.monotonic()
                with self.lock:
                    self.download_state.update(active=True, name=display_name, url=final_url, downloaded=0, total=total, rate_mbps=0.0, percent=0.0, error="", path=str(target))
                with temp.open("wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        rate_bytes += len(chunk)
                        now = time.monotonic()
                        dt = now - rate_t
                        with self.lock:
                            self.download_state["downloaded"] = downloaded
                            self.download_state["total"] = total
                            if total:
                                self.download_state["percent"] = min(100.0, downloaded * 100.0 / total)
                        if dt >= 0.45:
                            with self.lock:
                                self.download_state["rate_mbps"] = (rate_bytes / dt) / (1024 * 1024)
                            rate_bytes = 0
                            rate_t = now
            temp.replace(target)
            self.scan()
            info = next((p for p in self.packages.values() if Path(p.path).resolve() == target.resolve()), None)
            if info and not info.valid:
                self.log(f"Download completed but PKG validation failed: {info.error}")
            else:
                self.log(f"Download completed: {target.name}")
            with self.lock:
                self.download_state.update(active=False, downloaded=target.stat().st_size, total=target.stat().st_size, percent=100.0, rate_mbps=0.0, error="")
            self.ui_events.put(("download_done", {"ok": True, "path": str(target), "name": display_name}))
        except Exception as e:
            try:
                if temp.exists():
                    temp.unlink()
            except Exception:
                pass
            with self.lock:
                self.download_state.update(active=False, rate_mbps=0.0, error=str(e))
            self.log(f"Download failed: {display_name}: {e}")
            self.ui_events.put(("download_done", {"ok": False, "error": str(e), "name": display_name}))

    def download_to_library(self, url: str, filename: str = "", display_name: str = "", overwrite: bool = False) -> Path:
        url = url.strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Use a valid http:// or https:// PKG URL")
        with self.lock:
            if self.download_state.get("active"):
                raise RuntimeError("Another PKG download is already running")
        suggested = Path(filename).name if filename else Path(parsed.path).name
        if not suggested.lower().endswith(".pkg"):
            suggested = safe_filename(display_name or suggested or "download")
        else:
            suggested = safe_filename(suggested)
        target = self.library_dir / suggested
        label = display_name or suggested
        with self.lock:
            self.download_state.update(active=True, name=label, url=url, downloaded=0, total=0, rate_mbps=0.0, percent=0.0, error="", path=str(target))
        threading.Thread(target=self._download_worker, args=(url, target, label, overwrite), daemon=True, name="pkg-download").start()
        return target


def local_ip_for_peer() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


class ApiHandler(BaseHTTPRequestHandler):
    server_version = f"PS4PackageLink/{VERSION}"

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, text: str, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self.state.client_seen(self.client_address[0])
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/v1/status":
            with self.state.lock:
                self._json({"name": APP_NAME, "version": VERSION, "packages": len(self.state.packages),
                            "bytes_sent": self.state.bytes_sent, "rate_mbps": self.state.rate_mbps,
                            "active_transfers": self.state.active_transfers, "last_client": self.state.last_client})
            return
        if path == "/api/v1/status.txt":
            with self.state.lock:
                self._text("\t".join([VERSION, str(len(self.state.packages)), str(self.state.bytes_sent),
                                      f"{self.state.rate_mbps:.2f}", str(self.state.active_transfers), self.state.last_client]))
            return
        if path == "/api/v1/library":
            self.state.scan()
            host = self.headers.get("Host") or f"{local_ip_for_peer()}:{HTTP_PORT}"
            base = f"http://{host}"
            with self.state.lock:
                items = []
                for p in self.state.packages.values():
                    d = asdict(p)
                    d["pkg_url"] = base + p.pkg_url_path
                    d["ref_url"] = base + p.ref_url_path
                    d["icon_url"] = base + p.icon_url_path if p.icon_size else ""
                    items.append(d)
            self._json({"packages": items})
            return
        if path == "/api/v1/library.txt":
            self.state.scan()
            host = self.headers.get("Host") or f"{local_ip_for_peer()}:{HTTP_PORT}"
            base = f"http://{host}"
            rows = []
            with self.state.lock:
                for p in self.state.packages.values():
                    fields = [
                        p.id, urllib.parse.quote(p.title, safe=""), urllib.parse.quote(p.filename, safe=""),
                        p.content_id, p.title_id, p.version, str(p.size), str(p.declared_size),
                        str(p.content_type), "1" if p.is_patch else "0", p.package_type,
                        urllib.parse.quote(base + p.pkg_url_path, safe=":/?&=%"),
                        urllib.parse.quote(base + p.ref_url_path, safe=":/?&=%"),
                        urllib.parse.quote(base + p.icon_url_path, safe=":/?&=%") if p.icon_size else "",
                        "1" if p.valid else "0", urllib.parse.quote(p.error, safe="")
                    ]
                    rows.append("\t".join(fields))
            self._text("\n".join(rows))
            return
        if path == "/api/v1/pending":
            with self.state.lock:
                self._json({"pending": self.state.pending})
            return
        if path == "/api/v1/pending.txt":
            with self.state.lock:
                p = self.state.pending
                if not p:
                    self._text("")
                    return
                pkg = self.state.packages.get(p["id"])
                if not pkg:
                    self._text("")
                    return
                host = self.headers.get("Host") or f"{local_ip_for_peer()}:{HTTP_PORT}"
                base = f"http://{host}"
                fields = [
                    pkg.id, urllib.parse.quote(pkg.title, safe=""), pkg.content_id, str(pkg.declared_size or pkg.size),
                    "1" if pkg.is_patch else "0", pkg.package_type,
                    urllib.parse.quote(base + pkg.ref_url_path, safe=":/?&=%")
                ]
                self._text("\t".join(fields))
            return
        if path.startswith("/ref/") and path.endswith(".json"):
            pid = urllib.parse.unquote(path[len("/ref/"):-len(".json")])
            with self.state.lock:
                pkg = self.state.packages.get(pid)
            if not pkg or not pkg.valid:
                self._json({"error": "package not found"}, 404)
                return
            host = self.headers.get("Host") or f"{local_ip_for_peer()}:{HTTP_PORT}"
            pkg_url = f"http://{host}{pkg.pkg_url_path}"
            body = {
                "originalFileSize": pkg.declared_size or pkg.size,
                "packageDigest": pkg.digest,
                "numberOfSplitFiles": 1,
                "pieces": [{
                    "url": pkg_url,
                    "fileOffset": 0,
                    "fileSize": pkg.size,
                    "hashValue": "0" * 40,
                }],
            }
            self._json(body)
            return
        if path.startswith("/icon/") and path.endswith(".png"):
            pid = urllib.parse.unquote(path[len("/icon/"):-len(".png")])
            with self.state.lock:
                pkg = self.state.packages.get(pid)
            if not pkg or not pkg.icon_offset or not pkg.icon_size:
                self.send_error(404)
                return
            try:
                with Path(pkg.path).open("rb") as f:
                    f.seek(pkg.icon_offset)
                    body = f.read(pkg.icon_size)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except Exception:
                self.send_error(500)
            return
        if path.startswith("/pkg/"):
            pid = urllib.parse.unquote(path[len("/pkg/"):])
            with self.state.lock:
                pkg = self.state.packages.get(pid)
            if not pkg:
                self.send_error(404)
                return
            self._serve_file(Path(pkg.path))
            return
        self.send_error(404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json({"error": "invalid json"}, 400)
            return
        if parsed.path == "/api/v1/pending":
            pid = str(data.get("id", ""))
            with self.state.lock:
                if pid not in self.state.packages:
                    self._json({"error": "unknown package"}, 404)
                    return
                self.state.pending = {"id": pid, "queued_at": time.time()}
                pkg = self.state.packages[pid]
            self.state.log(f"Queued for PS4: {pkg.title}")
            self._json({"ok": True})
            return
        if parsed.path == "/api/v1/pending/ack":
            with self.state.lock:
                self.state.last_ack = data
                if self.state.pending and data.get("id") == self.state.pending.get("id"):
                    self.state.pending = None
            self.state.log(f"PS4 ack: {data.get('status','?')} task={data.get('task_id','-')} {data.get('message','')}")
            self._json({"ok": True})
            return
        self.send_error(404)

    def _serve_file(self, path: Path):
        try:
            size = path.stat().st_size
            range_header = self.headers.get("Range")
            start, end = 0, size - 1
            status = HTTPStatus.OK
            if range_header:
                m = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
                if not m:
                    self.send_error(416)
                    return
                a, b = m.groups()
                if a:
                    start = int(a)
                    end = min(int(b), size - 1) if b else size - 1
                else:
                    suffix = int(b or "0")
                    start = max(0, size - suffix)
                    end = size - 1
                if start < 0 or end >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            ip = self.client_address[0]
            self.state.transfer_begin(ip, path.name)
            try:
                with path.open("rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.state.transfer_bytes(len(chunk))
                        remaining -= len(chunk)
            finally:
                self.state.transfer_end(ip, path.name)
        except BrokenPipeError:
            self.state.log(f"Transfer disconnected: {self.client_address[0]}")
        except Exception as e:
            try:
                self.send_error(500, str(e))
            except Exception:
                pass


class CompanionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state: State):
        super().__init__(address, ApiHandler)
        self.state = state


class DiscoveryThread(threading.Thread):
    daemon = True
    def __init__(self, http_port: int):
        super().__init__(name="discovery")
        self.http_port = http_port
        self.stop_evt = threading.Event()

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", DISCOVERY_PORT))
        sock.settimeout(0.5)
        try:
            while not self.stop_evt.is_set():
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                if data.strip() != DISCOVERY_MAGIC:
                    continue
                # Choose the IP used to reach that PS4.
                route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    route.connect((addr[0], max(1, addr[1])))
                    ip = route.getsockname()[0]
                except OSError:
                    ip = local_ip_for_peer()
                finally:
                    route.close()
                reply = DISCOVERY_REPLY.format(url=f"http://{ip}:{self.http_port}").encode()
                sock.sendto(reply, addr)
        finally:
            sock.close()



class App:
    BG = "#0c1018"
    PANEL = "#141c29"
    PANEL_ALT = "#10141b"
    PANEL_SOFT = "#1b283b"
    BORDER = "#252c3b"
    TEXT = "#e9edf3"
    MUTED = "#96a0b2"
    ACCENT = "#367cf5"
    ACCENT_ALT = "#67a6ff"
    GOOD = "#63c984"
    WARN = "#e0b252"
    DANGER = "#ff8d8d"

    def __init__(self, root: Tk, state: State, server: CompanionServer):
        self.root = root
        self.state = state
        self.server = server
        self.status = StringVar(value="Starting")
        self.server_text = StringVar()
        self.library_text = StringVar(value=str(self.state.library_dir))
        self.pkg_title = StringVar(value="Select a package")
        self.pkg_meta = StringVar(value="No package selected")
        self.pkg_extra = StringVar(value="")
        self.ack_text = StringVar(value="Last ACK: none")
        self.show_hidden = BooleanVar(value=False)
        self.direct_url = StringVar()
        self.direct_name = StringVar()
        self.download_text = StringVar(value="No active download")
        self.download_rate = StringVar(value="0.0 MB/s")
        self.catalog_name = StringVar(value="Select an app")
        self.catalog_meta = StringVar(value="No catalog item selected")
        self.catalog_desc = StringVar(value="")
        self.catalog_filter = StringVar(value="All")
        self.preview = None
        self.catalog_preview = None
        self.catalog: list[CatalogApp] = []
        self.catalog_by_id: dict[str, CatalogApp] = {}
        self.root.title(f"{APP_NAME} {VERSION}")
        self.root.geometry("1320x900")
        self.root.minsize(1080, 760)
        self.root.configure(bg=self.BG)
        self._build_styles()
        self._load_catalog(show_error=False)
        self._build()
        self.refresh()

    def _build_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use('clam')
        except Exception:
            pass
        style.configure('.', background=self.BG, foreground=self.TEXT, fieldbackground=self.PANEL,
                        font=('Segoe UI', 10), borderwidth=0)
        style.configure('App.TFrame', background=self.BG)
        style.configure('Panel.TFrame', background=self.PANEL)
        style.configure('SoftPanel.TFrame', background=self.PANEL_SOFT)
        style.configure('Header.TLabel', background=self.BG, foreground=self.TEXT, font=('Segoe UI', 23, 'bold'))
        style.configure('SubHeader.TLabel', background=self.BG, foreground=self.MUTED, font=('Segoe UI', 10))
        style.configure('CardTitle.TLabel', background=self.PANEL, foreground=self.MUTED, font=('Segoe UI', 10, 'bold'))
        style.configure('CardValue.TLabel', background=self.PANEL, foreground=self.TEXT, font=('Segoe UI', 12, 'bold'))
        style.configure('DetailTitle.TLabel', background=self.PANEL_SOFT, foreground=self.MUTED, font=('Segoe UI', 10, 'bold'))
        style.configure('HeroValue.TLabel', background=self.PANEL_SOFT, foreground=self.TEXT, font=('Segoe UI', 13, 'bold'))
        style.configure('HeroMuted.TLabel', background=self.PANEL_SOFT, foreground=self.MUTED, font=('Segoe UI', 10))
        style.configure('Body.TLabel', background=self.PANEL, foreground=self.TEXT, font=('Segoe UI', 10))
        style.configure('Muted.TLabel', background=self.PANEL, foreground=self.MUTED, font=('Segoe UI', 10))
        style.configure('Primary.TButton', background=self.ACCENT, foreground='white', padding=(14, 9), relief='flat', font=('Segoe UI', 10, 'bold'))
        style.map('Primary.TButton', background=[('active', self.ACCENT_ALT), ('pressed', self.ACCENT_ALT)])
        style.configure('Secondary.TButton', background=self.PANEL_SOFT, foreground=self.TEXT, padding=(12, 9), relief='flat', font=('Segoe UI', 10, 'bold'))
        style.map('Secondary.TButton', background=[('active', '#222b3a'), ('pressed', '#222b3a')])
        style.configure('Danger.TButton', background='#311d22', foreground='#ff9a9a', padding=(12, 9), relief='flat', font=('Segoe UI', 10, 'bold'))
        style.map('Danger.TButton', background=[('active', '#47262d'), ('pressed', '#47262d')])
        style.configure('Dark.TEntry', fieldbackground=self.PANEL_ALT, foreground=self.TEXT, insertcolor=self.TEXT, padding=8)
        style.configure('Placeholder.TEntry', fieldbackground=self.PANEL_ALT, foreground=self.MUTED, insertcolor=self.TEXT, padding=8)
        style.configure('Dark.TCombobox', fieldbackground=self.PANEL_ALT, foreground=self.TEXT, padding=7)
        style.configure('Dark.TCheckbutton', background=self.PANEL, foreground=self.MUTED)
        style.map('Dark.TCheckbutton', background=[('active', self.PANEL)])
        style.configure('Treeview', background=self.PANEL, foreground=self.TEXT, fieldbackground=self.PANEL,
                        rowheight=42, bordercolor=self.BORDER, lightcolor=self.BORDER, darkcolor=self.BORDER)
        style.configure('Treeview.Heading', background=self.PANEL_SOFT, foreground=self.MUTED, relief='flat', font=('Segoe UI', 10, 'bold'), padding=(10, 10))
        style.map('Treeview', background=[('selected', '#243147')], foreground=[('selected', self.TEXT)])
        style.map('Treeview.Heading', background=[('active', '#202838')])
        style.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
        style.configure('Vertical.TScrollbar', background=self.PANEL_SOFT, troughcolor=self.PANEL, bordercolor=self.PANEL, lightcolor=self.PANEL_SOFT, darkcolor=self.PANEL_SOFT, arrowcolor=self.MUTED, arrowsize=12)
        style.configure('Dark.Horizontal.TProgressbar', troughcolor=self.PANEL_ALT, background=self.ACCENT, bordercolor=self.PANEL_ALT, lightcolor=self.ACCENT, darkcolor=self.ACCENT)
        style.configure('Dark.TNotebook', background=self.BG, borderwidth=0, bordercolor=self.BG, lightcolor=self.BG, darkcolor=self.BG, tabmargins=(0, 0, 0, 8))
        style.layout('Dark.TNotebook', [('Notebook.client', {'sticky': 'nswe'})])
        style.configure('Dark.TNotebook.Tab', bordercolor=self.BG, lightcolor=self.BG, darkcolor=self.BG, focuscolor=self.PANEL_SOFT, background=self.PANEL_SOFT, foreground=self.MUTED, padding=(18, 10), font=('Segoe UI', 10, 'bold'))
        style.map('Dark.TNotebook.Tab', background=[('selected', '#243147'), ('active', '#202838')], foreground=[('selected', self.TEXT)])

    def _panel(self, parent, style='Panel.TFrame', padding=(18, 16)):
        f = ttk.Frame(parent, style=style, padding=padding)
        return f

    def _set_placeholder(self, entry, variable, placeholder: str):
        variable.set(placeholder)
        entry.configure(style='Placeholder.TEntry')

        def on_focus_in(_event=None):
            if variable.get() == placeholder:
                variable.set('')
                entry.configure(style='Dark.TEntry')

        def on_focus_out(_event=None):
            if not variable.get().strip():
                variable.set(placeholder)
                entry.configure(style='Placeholder.TEntry')

        entry.bind('<FocusIn>', on_focus_in)
        entry.bind('<FocusOut>', on_focus_out)

    def _build(self):
        outer = ttk.Frame(self.root, style='App.TFrame', padding=24)
        outer.pack(fill='both', expand=True)

        header = ttk.Frame(outer, style='App.TFrame')
        header.pack(fill='x')
        left_head = ttk.Frame(header, style='App.TFrame')
        left_head.pack(side='left')
        ttk.Label(left_head, text=APP_NAME, style='Header.TLabel').pack(anchor='w')
        hero = ttk.Frame(header, style='SoftPanel.TFrame', padding=(16, 10))
        hero.pack(side='right')
        ttk.Label(hero, textvariable=self.server_text, style='HeroValue.TLabel').pack(anchor='e')

        # Console is packed before the expandable notebook, so it is visible immediately at every supported window size.
        console_panel = self._panel(outer)
        console_panel.pack(side='bottom', fill='x', pady=(14, 0))
        console_head = ttk.Frame(console_panel, style='Panel.TFrame')
        console_head.pack(fill='x')
        ttk.Label(console_head, text='Activity', style='CardTitle.TLabel').pack(side='left')
        console_wrap = ttk.Frame(console_panel, style='Panel.TFrame')
        console_wrap.pack(fill='x', pady=(10, 0))
        self.console = Text(console_wrap, height=5, wrap='none', font=('Consolas', 10), state='disabled',
                            bg=self.PANEL_ALT, fg=self.TEXT, insertbackground=self.TEXT,
                            selectbackground='#243147', selectforeground=self.TEXT,
                            relief='flat', borderwidth=0)
        console_scroll = ttk.Scrollbar(console_wrap, orient='vertical', command=self.console.yview)
        self.console.configure(yscrollcommand=console_scroll.set)
        self.console.pack(side='left', fill='both', expand=True)
        console_scroll.pack(side='right', fill='y')

        self.notebook = ttk.Notebook(outer, style='Dark.TNotebook')
        self.notebook.pack(fill='both', expand=True, pady=(16, 0))
        self.local_tab = ttk.Frame(self.notebook, style='App.TFrame', padding=(0, 10, 0, 0))
        self.catalog_tab = ttk.Frame(self.notebook, style='App.TFrame', padding=(0, 10, 0, 0))
        self.notebook.add(self.local_tab, text='Local library')
        self.notebook.add(self.catalog_tab, text='Downloads & catalog')
        self._build_local_tab()
        self._build_catalog_tab()

    def _build_local_tab(self):
        metrics = ttk.Frame(self.local_tab, style='App.TFrame')
        metrics.pack(fill='x', pady=(0, 12))
        self.metric_cards = {}
        for key, title in [('client', 'PS4 client'), ('rate', 'Upload to PS4'), ('pending', 'Queue'), ('library', 'Local library')]:
            card = self._panel(metrics)
            card.pack(side='left', fill='x', expand=True, padx=(0, 10) if key != 'library' else 0)
            ttk.Label(card, text=title, style='CardTitle.TLabel').pack(anchor='w')
            val = ttk.Label(card, text='-', style='CardValue.TLabel')
            val.pack(anchor='w', pady=(8, 0))
            self.metric_cards[key] = val

        body = ttk.Frame(self.local_tab, style='App.TFrame')
        body.pack(fill='both', expand=True)
        left = self._panel(body, padding=(16, 14))
        left.pack(side='left', fill='both', expand=True)
        right = self._panel(body, style='SoftPanel.TFrame', padding=(18, 16))
        right.pack(side='right', fill='y', padx=(14, 0))
        right.configure(width=350)

        top = ttk.Frame(left, style='Panel.TFrame')
        top.pack(fill='x', pady=(0, 10))
        ttk.Label(top, text='Your packages', style='CardTitle.TLabel').pack(side='left')
        ttk.Checkbutton(top, text='Show hidden', variable=self.show_hidden, style='Dark.TCheckbutton', command=lambda: self.refresh(False)).pack(side='right')

        actions = ttk.Frame(left, style='Panel.TFrame')
        actions.pack(fill='x', pady=(0, 10))
        ttk.Button(actions, text='Add PKG…', style='Primary.TButton', command=self.add_pkg).pack(side='left')
        ttk.Button(actions, text='Add Game…', style='Secondary.TButton', command=self.add_game).pack(side='left', padx=(8, 0))
        ttk.Button(actions, text='Rescan', style='Secondary.TButton', command=lambda: self.refresh(False)).pack(side='left', padx=(8, 0))
        ttk.Label(left, text='Import PKG files or archives. Select a package to see its details.', style='Muted.TLabel').pack(anchor='w', pady=(0, 10))

        tree_wrap = ttk.Frame(left, style='Panel.TFrame')
        tree_wrap.pack(fill='both', expand=True)
        cols = ('title', 'title_id', 'version', 'size', 'type', 'valid')
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show='headings', selectmode='browse')
        widths = [('title', 'Title', 360), ('title_id', 'Title ID', 100), ('version', 'Version', 76), ('size', 'Size', 95), ('type', 'Type', 82), ('valid', 'Status', 78)]
        for c, label, width in widths:
            self.tree.heading(c, text=label, anchor='w')
            self.tree.column(c, width=width, minwidth=55, anchor='w', stretch=(c=='title'))
        self.tree.tag_configure('hidden', foreground='#6f7787')
        yscroll = ttk.Scrollbar(tree_wrap, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        yscroll.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self._selection_changed)
        self.tree.bind('<Delete>', lambda _e: self.remove_from_list())
        self.tree.bind('<Shift-Delete>', lambda _e: self.delete_from_library())
        self.tree.bind('<Button-3>', self._show_context_menu)
        self.context_menu = __import__('tkinter').Menu(self.root, tearoff=0, bg=self.PANEL_SOFT, fg=self.TEXT, activebackground='#243147', activeforeground=self.TEXT)
        self.context_menu.add_command(label='Queue for PS4', command=self.queue_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label='Remove from List', command=self.remove_from_list)
        self.context_menu.add_command(label='Restore to List', command=self.restore_to_list)
        self.context_menu.add_command(label='Delete PKG from Library', command=self.delete_from_library)

        ttk.Label(right, text='Package details', style='DetailTitle.TLabel').pack(anchor='w')
        self.preview_label = ttk.Label(right, text='No icon', anchor='center', background=self.PANEL_SOFT, foreground=self.MUTED)
        self.preview_label.pack(fill='x', pady=(12, 10), ipady=12)
        ttk.Label(right, textvariable=self.pkg_title, style='HeroValue.TLabel', wraplength=310, justify='left').pack(anchor='w')
        ttk.Label(right, textvariable=self.pkg_meta, style='HeroMuted.TLabel', wraplength=310, justify='left').pack(anchor='w', pady=(6, 2))
        ttk.Label(right, textvariable=self.pkg_extra, style='HeroMuted.TLabel', wraplength=310, justify='left').pack(anchor='w')
        ttk.Separator(right, orient='horizontal').pack(fill='x', pady=14)
        ttk.Label(right, text='Manage package', style='DetailTitle.TLabel').pack(anchor='w')
        ttk.Button(right, text='Queue for PS4', style='Primary.TButton', command=self.queue_selected).pack(fill='x', pady=(10, 6))
        ttk.Button(right, text='Remove from List', style='Secondary.TButton', command=self.remove_from_list).pack(fill='x', pady=3)
        ttk.Button(right, text='Restore to List', style='Secondary.TButton', command=self.restore_to_list).pack(fill='x', pady=3)
        ttk.Button(right, text='Delete PKG from Library', style='Danger.TButton', command=self.delete_from_library).pack(fill='x', pady=(3, 0))
        ttk.Label(right, textvariable=self.ack_text, style='HeroMuted.TLabel', wraplength=310, justify='left').pack(anchor='w', pady=(16, 0))

    def _build_catalog_tab(self):
        direct = self._panel(self.catalog_tab)
        direct.pack(fill='x', pady=(0, 12))
        ttk.Label(direct, text='DIRECT PKG URL', style='CardTitle.TLabel').grid(row=0, column=0, sticky='w', columnspan=4)
        ttk.Label(direct, text='Download a PKG you are authorized to use directly into the local library.', style='Muted.TLabel').grid(row=1, column=0, sticky='w', columnspan=4, pady=(4, 12))

        ttk.Label(direct, text='PKG LINK', style='CardTitle.TLabel').grid(row=2, column=0, sticky='w', padx=(0, 8), pady=(0, 5))
        ttk.Label(direct, text='PKG NAME (OPTIONAL)', style='CardTitle.TLabel').grid(row=2, column=1, sticky='w', padx=(0, 8), pady=(0, 5))
        self.direct_url_entry = ttk.Entry(direct, textvariable=self.direct_url, style='Dark.TEntry')
        self.direct_url_entry.grid(row=3, column=0, sticky='ew', padx=(0, 8))
        self.direct_name_entry = ttk.Entry(direct, textvariable=self.direct_name, style='Dark.TEntry', width=28)
        self.direct_name_entry.grid(row=3, column=1, sticky='ew', padx=(0, 8))
        ttk.Button(direct, text='Download to Library', style='Primary.TButton', command=self.download_direct_url).grid(row=3, column=2, sticky='ew')
        self._set_placeholder(self.direct_url_entry, self.direct_url, 'Paste direct PKG URL...')
        self._set_placeholder(self.direct_name_entry, self.direct_name, 'Name your PKG...')
        direct.columnconfigure(0, weight=1)
        direct.columnconfigure(1, weight=0)
        direct.columnconfigure(2, weight=0)

        download = self._panel(self.catalog_tab)
        download.pack(fill='x', pady=(0, 12))
        top = ttk.Frame(download, style='Panel.TFrame')
        top.pack(fill='x')
        ttk.Label(top, text='DOWNLOAD', style='CardTitle.TLabel').pack(side='left')
        ttk.Label(top, textvariable=self.download_rate, style='Muted.TLabel').pack(side='right')
        ttk.Label(download, textvariable=self.download_text, style='Body.TLabel').pack(anchor='w', pady=(8, 8))
        self.download_bar = ttk.Progressbar(download, maximum=100.0, style='Dark.Horizontal.TProgressbar')
        self.download_bar.pack(fill='x')

        body = ttk.Frame(self.catalog_tab, style='App.TFrame')
        body.pack(fill='both', expand=True)
        left = self._panel(body)
        left.pack(side='left', fill='both', expand=True)
        right = self._panel(body, style='SoftPanel.TFrame')
        right.pack(side='right', fill='y', padx=(14, 0))
        right.configure(width=360)

        catalog_head = ttk.Frame(left, style='Panel.TFrame')
        catalog_head.pack(fill='x', pady=(0, 10))
        ttk.Label(catalog_head, text='APPS.JSON LIBRARY', style='CardTitle.TLabel').pack(side='left')
        ttk.Button(catalog_head, text='Open apps.json', style='Secondary.TButton', command=self.open_apps_json).pack(side='right')
        ttk.Button(catalog_head, text='Reload', style='Secondary.TButton', command=lambda: self._load_catalog(show_error=True)).pack(side='right', padx=(0, 8))
        self.category_box = ttk.Combobox(catalog_head, textvariable=self.catalog_filter, state='readonly', style='Dark.TCombobox', width=18)
        self.category_box.pack(side='right', padx=(0, 8))
        self.category_box.bind('<<ComboboxSelected>>', lambda _e: self._refresh_catalog_tree())

        tree_wrap = ttk.Frame(left, style='Panel.TFrame')
        tree_wrap.pack(fill='both', expand=True)
        cols = ('name', 'category', 'status')
        self.catalog_tree = ttk.Treeview(tree_wrap, columns=cols, show='headings', selectmode='browse')
        for c, label, width in [('name', 'Name', 430), ('category', 'Category', 170), ('status', 'Status', 120)]:
            self.catalog_tree.heading(c, text=label)
            self.catalog_tree.column(c, width=width, anchor='w')
        sy = ttk.Scrollbar(tree_wrap, orient='vertical', command=self.catalog_tree.yview)
        self.catalog_tree.configure(yscrollcommand=sy.set)
        self.catalog_tree.pack(side='left', fill='both', expand=True)
        sy.pack(side='right', fill='y')
        self.catalog_tree.bind('<<TreeviewSelect>>', self._catalog_selection_changed)

        ttk.Label(right, text='CATALOG APP', style='CardTitle.TLabel').pack(anchor='w')
        self.catalog_preview_label = ttk.Label(right, text='No image', anchor='center', background=self.PANEL_SOFT, foreground=self.MUTED)
        self.catalog_preview_label.pack(fill='x', pady=(12, 10), ipady=14)
        ttk.Label(right, textvariable=self.catalog_name, style='HeroValue.TLabel', wraplength=320, justify='left').pack(anchor='w')
        ttk.Label(right, textvariable=self.catalog_meta, style='HeroMuted.TLabel', wraplength=320, justify='left').pack(anchor='w', pady=(6, 4))
        ttk.Label(right, textvariable=self.catalog_desc, style='HeroMuted.TLabel', wraplength=320, justify='left').pack(anchor='w')
        ttk.Button(right, text='Install to Library', style='Primary.TButton', command=self.install_catalog_selected).pack(fill='x', pady=(16, 6))
        ttk.Button(right, text='Queue for PS4', style='Secondary.TButton', command=self.queue_catalog_selected).pack(fill='x')
        self._refresh_catalog_tree()

    @staticmethod
    def size_text(n: int) -> str:
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        v = float(n)
        for u in units:
            if v < 1024 or u == units[-1]:
                return f"{v:.1f} {u}" if u != 'B' else f"{int(v)} B"
            v /= 1024

    def _selection_changed(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        with self.state.lock:
            p = self.state.packages.get(sel[0])
            hidden = sel[0] in self.state.hidden_ids
        if not p:
            return
        self.pkg_title.set(p.title)
        type_text = 'Patch' if p.is_patch else ('Application' if p.package_type == 'PS4GD' else p.package_type)
        self.pkg_meta.set(f"{p.title_id or 'NO TITLE ID'}  •  v{p.version or '-'}  •  {self.size_text(p.size)}")
        extra = [f"Type: {type_text}", f"Content ID: {p.content_id or '-'}", f"PC list: {'Hidden' if hidden else 'Visible'}", f"File: {p.filename}"]
        if p.error:
            extra.append(f"Error: {p.error}")
        self.pkg_extra.set("\n".join(extra))
        self.preview = None
        if p.icon_offset and p.icon_size:
            try:
                with Path(p.path).open('rb') as f:
                    f.seek(p.icon_offset)
                    raw = f.read(p.icon_size)
                img = PhotoImage(data=base64.b64encode(raw).decode('ascii'))
                factor = max(1, max(img.width(), img.height()) // 230)
                self.preview = img.subsample(factor, factor) if factor > 1 else img
                self.preview_label.configure(image=self.preview, text='')
                return
            except Exception:
                pass
        self.preview_label.configure(image='', text='No icon')

    def _show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.tree.focus(row)
            self._selection_changed()
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def add_pkg(self):
        filename = filedialog.askopenfilename(title='Select PS4 PKG', filetypes=[('PS4 package', '*.pkg'), ('All files', '*.*')])
        if filename:
            try:
                self.state.add_path(Path(filename))
                self.state.log(f'Added to library: {Path(filename).name}')
                self.refresh(False)
            except Exception as e:
                messagebox.showerror(APP_NAME, str(e))

    def add_game(self):
        filenames = filedialog.askopenfilenames(
            title='Add Game to Local Library',
            filetypes=[
                ('Game packages / archives', ('*.pkg', '*.zip', '*.rar', '*.7z', '*.001', '*.002', '*.003', '*.004', '*.005', '*.006', '*.007', '*.008', '*.009')),
                ('PS4 package', '*.pkg'),
                ('Archives', '*.zip *.rar *.7z *.001'),
                ('All files', '*.*'),
            ],
        )
        if not filenames:
            return
        self.state.log(f'Game Import queued from GUI: {len(filenames)} selected file(s)')
        self.state.import_game(list(filenames))

    def remove_from_list(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, 'Select a package first.')
            return
        try:
            info = self.state.hide_package(sel[0])
            self.state.log(f"GUI Remove from List: {info.title}")
            self.refresh(False)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def restore_to_list(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, 'Enable Show hidden and select a hidden package first.')
            return
        try:
            info = self.state.restore_package(sel[0])
            self.state.log(f"GUI Restore to List: {info.title}")
            self.refresh(False)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def delete_from_library(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, 'Select a package first.')
            return
        pid = sel[0]
        with self.state.lock:
            p = self.state.packages.get(pid)
        if not p:
            return
        msg = (f"Permanently delete '{p.title}' from the local PKG library?\n\n"
               f"File: {p.filename}\n\n"
               "This deletes the .pkg file from the packages folder. This action cannot be undone by PS4 Package Link.")
        if not messagebox.askyesno(APP_NAME, msg, icon='warning'):
            return
        try:
            deleted = self.state.delete_package(pid)
            self.preview = None
            self.preview_label.configure(image='', text='No icon')
            self.pkg_title.set('Select a package')
            self.pkg_meta.set('No package selected')
            self.pkg_extra.set('')
            self.state.log(f"GUI Delete from Library: {deleted.title}")
            self.refresh(False)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Delete failed:\n{e}")

    def queue_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, 'Select a package first.')
            return
        self._queue_package_id(sel[0])

    def _queue_package_id(self, pid: str):
        with self.state.lock:
            p = self.state.packages.get(pid)
            if not p or not p.valid:
                messagebox.showerror(APP_NAME, f"Invalid package: {p.error if p else 'not found'}")
                return
            self.state.pending = {'id': pid, 'queued_at': time.time()}
        self.state.log(f'Queued from GUI: {p.title}')
        self.refresh(False)

    def _load_catalog(self, show_error=True):
        try:
            self.catalog = load_catalog(self.state.apps_path)
            self.catalog_by_id = {a.id: a for a in self.catalog}
            if hasattr(self, 'category_box'):
                cats = ['All'] + sorted({a.category for a in self.catalog}, key=str.lower)
                self.category_box['values'] = cats
                if self.catalog_filter.get() not in cats:
                    self.catalog_filter.set('All')
                self._refresh_catalog_tree()
            self.state.log(f"apps.json loaded: {len(self.catalog)} apps")
        except Exception as e:
            self.catalog = []
            self.catalog_by_id = {}
            self.state.log(str(e))
            if show_error:
                messagebox.showerror(APP_NAME, str(e))

    def _refresh_catalog_tree(self):
        if not hasattr(self, 'catalog_tree'):
            return
        previous = self.catalog_tree.selection()[0] if self.catalog_tree.selection() else None
        for row in self.catalog_tree.get_children():
            self.catalog_tree.delete(row)
        chosen = self.catalog_filter.get() or 'All'
        for app in self.catalog:
            if chosen != 'All' and app.category != chosen:
                continue
            target = self.state.library_dir / catalog_filename(app)
            status = 'In Library' if target.exists() else 'Remote'
            self.catalog_tree.insert('', 'end', iid=app.id, values=(app.name, app.category, status))
        if previous and self.catalog_tree.exists(previous):
            self.catalog_tree.selection_set(previous)
            self.catalog_tree.focus(previous)

    def _catalog_selection_changed(self, _event=None):
        sel = self.catalog_tree.selection()
        if not sel:
            return
        app = self.catalog_by_id.get(sel[0])
        if not app:
            return
        target = self.state.library_dir / catalog_filename(app)
        self.catalog_name.set(app.name)
        self.catalog_meta.set(f"{app.category}  •  {'In Local Library' if target.exists() else 'Remote'}\n{catalog_filename(app)}")
        self.catalog_desc.set(app.description or app.pkg_url)
        self.catalog_preview = None
        self.catalog_preview_label.configure(image='', text='Loading image…' if app.image else 'No image')
        if app.image:
            self._load_catalog_image_async(app)

    def _load_catalog_image_async(self, app: CatalogApp):
        token = app.id
        def worker():
            try:
                src = app.image
                parsed = urllib.parse.urlparse(src)
                if parsed.scheme in ('http', 'https'):
                    req = urllib.request.Request(src, headers={'User-Agent': f'{APP_NAME}/{VERSION}'})
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        data = resp.read(8 * 1024 * 1024)
                else:
                    image_path = Path(src)
                    if not image_path.is_absolute():
                        image_path = self.state.base_dir / image_path
                    data = image_path.read_bytes()
                self.root.after(0, lambda d=data, t=token: self._set_catalog_preview(t, d))
            except Exception as e:
                self.root.after(0, lambda t=token, err=str(e): self._catalog_image_failed(t, err))
        threading.Thread(target=worker, daemon=True, name='catalog-image').start()

    def _set_catalog_preview(self, token: str, data: bytes):
        sel = self.catalog_tree.selection()
        if not sel or sel[0] != token:
            return
        try:
            img = PhotoImage(data=base64.b64encode(data).decode('ascii'))
            factor = max(1, max(img.width(), img.height()) // 250)
            self.catalog_preview = img.subsample(factor, factor) if factor > 1 else img
            self.catalog_preview_label.configure(image=self.catalog_preview, text='')
        except Exception:
            self.catalog_preview_label.configure(image='', text='Image format unsupported\n(use PNG/GIF, or install Pillow for more formats)')

    def _catalog_image_failed(self, token: str, error: str):
        sel = self.catalog_tree.selection()
        if sel and sel[0] == token:
            self.catalog_preview_label.configure(image='', text='Image unavailable')
            self.state.log(f"Catalog image failed: {error}")

    def install_catalog_selected(self):
        sel = self.catalog_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, 'Select an app from apps.json first.')
            return
        app = self.catalog_by_id.get(sel[0])
        if not app:
            return
        filename = catalog_filename(app)
        target = self.state.library_dir / filename
        overwrite = False
        if target.exists():
            if not messagebox.askyesno(APP_NAME, f"{filename} is already in the library. Download and replace it?"):
                return
            overwrite = True
        try:
            self.state.download_to_library(app.pkg_url, filename=filename, display_name=app.name, overwrite=overwrite)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def queue_catalog_selected(self):
        sel = self.catalog_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, 'Select an app first.')
            return
        app = self.catalog_by_id.get(sel[0])
        if not app:
            return
        target = (self.state.library_dir / catalog_filename(app)).resolve()
        self.state.scan()
        with self.state.lock:
            match = next((p for p in self.state.packages.values() if Path(p.path).resolve() == target), None)
        if not match:
            messagebox.showinfo(APP_NAME, 'This app is not downloaded to the local library yet. Use Install to Library first.')
            return
        self._queue_package_id(match.id)
        self.notebook.select(self.local_tab)

    def download_direct_url(self):
        url = self.direct_url.get().strip()
        name = self.direct_name.get().strip()
        if url == 'Paste direct PKG URL...':
            url = ''
        if name == 'Name your PKG...':
            name = ''
        if not url:
            messagebox.showinfo(APP_NAME, 'Paste a PKG URL first.')
            return
        try:
            parsed = urllib.parse.urlparse(url)
            suggested = Path(urllib.parse.unquote(parsed.path)).name
            if name:
                filename = safe_filename(name)
            elif suggested.lower().endswith('.pkg'):
                filename = safe_filename(suggested)
            else:
                filename = 'download.pkg'
            target = self.state.library_dir / filename
            overwrite = False
            if target.exists():
                if not messagebox.askyesno(APP_NAME, f"{filename} already exists. Replace it?"):
                    return
                overwrite = True
            self.state.download_to_library(url, filename=filename, display_name=name or filename, overwrite=overwrite)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def open_apps_json(self):
        path = self.state.apps_path
        if not path.exists():
            path.write_text(json.dumps({'version': 1, 'apps': []}, indent=2), encoding='utf-8')
        try:
            if os.name == 'nt':
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(['xdg-open', str(path)])
        except Exception as e:
            messagebox.showinfo(APP_NAME, f"apps.json path:\n{path}\n\nOpen it in your text editor.\n\n{e}")

    def _process_ui_events(self):
        while True:
            try:
                kind, payload = self.state.ui_events.get_nowait()
            except queue.Empty:
                break
            if kind == 'download_done':
                if payload.get('ok'):
                    self.status.set(f"Downloaded: {payload.get('name', '')}")
                    self._refresh_catalog_tree()
                else:
                    messagebox.showerror(APP_NAME, f"Download failed:\n{payload.get('error', 'Unknown error')}")
            elif kind == 'game_import_done':
                if payload.get('ok'):
                    files = payload.get('files') or []
                    self.status.set(f"Game Import complete: {len(files)} PKG")
                    self.refresh(False)
                    messagebox.showinfo(APP_NAME, "Game Import complete.\n\nImported:\n" + "\n".join(files))
                else:
                    messagebox.showerror(APP_NAME, f"Game Import failed:\n{payload.get('error', 'Unknown error')}")

    def refresh(self, schedule=True):
        self._process_ui_events()
        previous = self.tree.selection()[0] if hasattr(self, 'tree') and self.tree.selection() else None
        self.state.scan()
        if hasattr(self, 'tree'):
            for i in self.tree.get_children():
                self.tree.delete(i)
            packages = self.state.visible_packages(show_hidden=self.show_hidden.get())
            with self.state.lock:
                pending = self.state.pending['id'] if self.state.pending else None
                last_ack = self.state.last_ack
                hidden = set(self.state.hidden_ids)
            for p in packages:
                pkg_type = 'Patch' if p.is_patch else 'App'
                status = 'HIDDEN' if p.id in hidden else ('OK' if p.valid else 'ERR')
                tags = ('hidden',) if p.id in hidden else ()
                self.tree.insert('', 'end', iid=p.id, values=(p.title, p.title_id, p.version or '-', self.size_text(p.size), pkg_type, status), tags=tags)
            if previous and self.tree.exists(previous):
                self.tree.selection_set(previous)
                self.tree.focus(previous)
                self._selection_changed()

            with self.state.lock:
                rate = self.state.rate_mbps
                client = self.state.last_client
                active = self.state.active_transfers
                logs = list(self.state.logs[-160:])
            visible_by_id = {p.id: p for p in self.state.packages.values()}
            pending_text = visible_by_id[pending].title if pending and pending in visible_by_id else 'No queued package'
            self.metric_cards['client'].configure(text=client if client != '-' else 'No client yet')
            self.metric_cards['rate'].configure(text=f"{rate:.1f} MB/s" if active else 'Idle')
            self.metric_cards['pending'].configure(text=pending_text)
            self.metric_cards['library'].configure(text=f"{len(self.state.packages)} PKG")
            self.library_text.set(str(self.state.library_dir))
            if last_ack:
                self.ack_text.set(f"Last ACK: {last_ack.get('status','?')}  •  task {last_ack.get('task_id', '-') }  •  {last_ack.get('message','')}")
            else:
                self.ack_text.set('Last ACK: none')

            self.console.configure(state='normal')
            self.console.delete('1.0', END)
            self.console.insert(END, '\n'.join(logs))
            self.console.see(END)
            self.console.configure(state='disabled')

        server_url = f"http://{local_ip_for_peer()}:{self.server.server_address[1]}"
        self.server_text.set(server_url)
        with self.state.lock:
            d = dict(self.state.download_state)
        if d.get('active'):
            pct = float(d.get('percent') or 0.0)
            self.download_bar['value'] = pct
            total = int(d.get('total') or 0)
            got = int(d.get('downloaded') or 0)
            self.download_text.set(f"{d.get('name','PKG')}  •  {self.size_text(got)} / {self.size_text(total) if total else '?'}  •  {pct:.1f}%")
            self.download_rate.set(f"{float(d.get('rate_mbps') or 0.0):.1f} MB/s")
            self.status.set('Downloading PKG to local library')
        else:
            if d.get('error'):
                self.download_text.set(f"Last download failed: {d.get('error')}")
            elif float(d.get('percent') or 0) >= 100:
                self.download_text.set(f"Completed: {d.get('name','PKG')}")
            else:
                self.download_text.set('No active download')
            self.download_rate.set('0.0 MB/s')
            self.download_bar['value'] = float(d.get('percent') or 0.0)
            if not self.status.get().startswith('Downloaded:'):
                self.status.set('Ready')
        if schedule:
            self.root.after(700, self.refresh)

def run_server(state: State, port: int):
    server = CompanionServer(("0.0.0.0", port), state)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="http")
    t.start()
    d = DiscoveryThread(port)
    d.start()
    return server, d


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--library", default=str(Path.cwd() / "packages"))
    ap.add_argument("--port", type=int, default=HTTP_PORT)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    state = State(Path(args.library).resolve())
    server, discovery = run_server(state, args.port)
    state.log(f"Library: {state.library_dir}")
    state.log(f"HTTP server: http://{local_ip_for_peer()}:{args.port}")
    state.log(f"UDP discovery: *:{DISCOVERY_PORT}")
    if args.headless:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        root = Tk()
        App(root, state, server)
        try:
            root.mainloop()
        finally:
            server.shutdown()
            discovery.stop_evt.set()


if __name__ == "__main__":
    main()
