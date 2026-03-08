#!/usr/bin/env python3
from __future__ import annotations

"""
Generate enhanced_manifest.json (version 2.0) by merging:
  1. classic_and_stylish_layouts.json (classic + stylish from app export)
  2. All .svg files in a folder (parsed like SVGLayoutParser: viewBox + path/polygon/rect/circle/ellipse)

Also generates thumbnail images (one per layout) in thumbnails/.

Usage:
  python generate_enhanced_manifest.py --base-url "https://raw.githubusercontent.com/OWNER/REPO/main"
"""

import argparse
import json
import math
import os
import re
import sys
from io import BytesIO
from pathlib import Path

# SVG parsing is string/regex-based (mirrors SVGLayoutParser); svgelements not required

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

try:
    import cairosvg
except ImportError:
    cairosvg = None


# -----------------------------------------------------------------------------
# Classic + Stylish from JSON
# -----------------------------------------------------------------------------

def load_classic_stylish_layouts(json_path: Path, base_url: str) -> list:
    """Load classic_and_stylish_layouts.json and add type + thumbnailURL."""
    base_url = base_url.rstrip("/")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    layouts = []
    for key in ("classic_layouts", "stylish_layouts"):
        for layout in data.get(key, []):
            layout = dict(layout)
            # Ensure we only keep fields needed in enhanced manifest
            slot_list = layout.get("slots", [])
            has_path_data = any(s.get("path_data") for s in slot_list)
            layout["type"] = "organic" if has_path_data else "grid"
            layout["thumbnailURL"] = f"{base_url}/thumbnails/{layout['id']}.jpg"
            # Drop slot_count if present (optional; app can use len(slots))
            layout.pop("slot_count", None)
            # Normalize slots: ensure id and n_rect; keep path_data if present
            slots_out = []
            for i, slot in enumerate(slot_list):
                s = {"id": slot.get("id", f"slot_{i}"), "n_rect": slot["n_rect"]}
                if slot.get("path_data"):
                    s["path_data"] = slot["path_data"]
                slots_out.append(s)
            layout["slots"] = slots_out
            layouts.append(layout)
    return layouts


# -----------------------------------------------------------------------------
# SVG parsing (mirror SVGLayoutParser + SVGCollageLayout: viewBox, path, polygon, rect, circle, ellipse)
# -----------------------------------------------------------------------------

def _ellipse_path_d(cx: float, cy: float, rx: float, ry: float, steps: int = 32) -> str:
    """SVG path d for ellipse (polygon approximation) so thumbnails draw ovals; parser uses M/L/Z."""
    if rx <= 0 or ry <= 0:
        return ""
    pts = []
    for i in range(steps + 1):
        t = 2 * math.pi * i / steps
        pts.append((cx + rx * math.cos(t), cy + ry * math.sin(t)))
    return "M " + " ".join(f"{x} {y}" for x, y in pts) + " Z"


def _parse_viewbox_from_svg_string(svg_string: str) -> tuple[float, float, float, float]:
    """Mirror SVGLayoutParser.parseViewBox: viewBox=\"([^\"]+)\" then 4 components."""
    m = re.search(r'viewBox=["\']([^"\']+)["\']', svg_string, re.IGNORECASE)
    if not m:
        return (0.0, 0.0, 500.0, 500.0)
    parts = m.group(1).split()
    if len(parts) < 4:
        return (0.0, 0.0, 500.0, 500.0)
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
    except ValueError:
        return (0.0, 0.0, 500.0, 500.0)


def _path_bounds_from_d(path_d: str) -> tuple[float, float, float, float] | None:
    """Compute bounding box of path d by flattening to points (same logic as app path.bounds)."""
    polys = _flatten_path_to_polygons(path_d, viewbox=None)  # work in raw coords
    if not polys or not polys[0]:
        return None
    all_x = [p[0] for poly in polys for p in poly]
    all_y = [p[1] for poly in polys for p in poly]
    if not all_x or not all_y:
        return None
    return (min(all_x), min(all_y), max(all_x) - min(all_x), max(all_y) - min(all_y))


def _parse_layout_from_svg_string(svg_string: str) -> list[tuple[str, float, float, float, float, str | None]]:
    """Mirror SVGLayoutParser.parseLayout order: path (id), path (no id), polygon, g rect, xml rect, xml circle, ellipse. Returns [(frame_id, x, y, w, h, path_d or None)]."""
    frames: list[tuple[str, float, float, float, float, str | None]] = []
    # 1) Path with id: <path ... id="..." ... d="...">
    for m in re.finditer(r'<path[^>]*?id\s*=\s*["\']([^"\']+)["\'][^>]*?d\s*=\s*["\']([^"\']+)["\'][^>]*?/?>', svg_string, re.IGNORECASE | re.DOTALL):
        fid, d = m.group(1), m.group(2)
        b = _path_bounds_from_d(d)
        if b:
            frames.append((fid, b[0], b[1], b[2], b[3], d))
    # 2) Path without id
    for i, m in enumerate(re.finditer(r'<path(?![^>]*id)[^>]*d\s*=\s*["\']([^"\']+)["\'][^>]*?/?>', svg_string, re.IGNORECASE | re.DOTALL)):
        d = m.group(1)
        b = _path_bounds_from_d(d)
        if b:
            frames.append((f"path_noid_{i+1}", b[0], b[1], b[2], b[3], d))
    # 3) Polygon
    for i, m in enumerate(re.finditer(r'<polygon[^>]*?(?:id\s*=\s*["\']([^"\']*)["\'])?[^>]*?points\s*=\s*["\']([^"\']+)["\'][^>]*?/?>', svg_string, re.IGNORECASE | re.DOTALL)):
        pid = m.group(1).strip() if m.group(1) else f"polygon_{i+1}"
        points_str = m.group(2).strip()
        # Convert points to path d "M x1 y1 L x2 y2 ... Z" for bounds and storage
        coords = re.findall(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", points_str)
        if len(coords) >= 6 and len(coords) % 2 == 0:
            pts = [(float(coords[j]), float(coords[j+1])) for j in range(0, len(coords), 2)]
            if len(pts) >= 3:
                path_d = "M " + " ".join(f"{x} {y}" for x, y in pts) + " Z"
                b = _path_bounds_from_d(path_d)
                if b:
                    frames.append((pid, b[0], b[1], b[2], b[3], path_d))
    # 4) Rect in <g id="..."> (group)
    for m in re.finditer(r'<g\s+id="([^"]+)">(.*?)</g>', svg_string, re.DOTALL):
        gid, content = m.group(1), m.group(2)
        rm = re.search(r'<rect\s+(?:x="([^"]*)")?\s*(?:y="([^"]*)")?\s*(?:width="([^"]+)")\s*(?:height="([^"]+)")', content)
        if rm:
            x = float(rm.group(1) or 0)
            y = float(rm.group(2) or 0)
            w = float(rm.group(3))
            h = float(rm.group(4))
            frames.append((gid, x, y, w, h, None))
    # 5) Rect via XML (mirror app RectXMLParser)
    # 6) Circle via XML (mirror app CircleXMLParser)
    # 7) Ellipse via regex (mirror app ellipseRegex)
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(svg_string)

        def _local_tag(tag: str) -> str:
            return tag.split("}")[-1] if "}" in tag else tag

        for i, el in enumerate(root.iter()):
            local = _local_tag(el.tag)
            if local == "rect":
                x = float(el.get("x", 0))
                y = float(el.get("y", 0))
                w = float(el.get("width", 0))
                h = float(el.get("height", 0))
                frames.append((f"rect_xml_{i+1}", x, y, w, h, None))
            elif local == "circle":
                cx, cy = float(el.get("cx", 0)), float(el.get("cy", 0))
                r = float(el.get("r", 0))
                path_d = _ellipse_path_d(cx, cy, r, r) if r > 0 else None
                frames.append((f"circle_xml_{i+1}", cx - r, cy - r, r * 2, r * 2, path_d))
    except Exception:
        pass
    # 7) Ellipse regex (app uses regex, not XML)
    for i, m in enumerate(re.finditer(r'<ellipse[^>]*?cx\s*=\s*["\']?([^"\'\s>]*)["\']?[^>]*?cy\s*=\s*["\']?([^"\'\s>]*)["\']?[^>]*?rx\s*=\s*["\']?([^"\'\s>]*)["\']?[^>]*?ry\s*=\s*["\']?([^"\'\s>]*)["\']?[^>]*?/?>', svg_string, re.IGNORECASE | re.DOTALL)):
        cx, cy = float(m.group(1) or 0), float(m.group(2) or 0)
        rx, ry = float(m.group(3) or 0), float(m.group(4) or 0)
        path_d = _ellipse_path_d(cx, cy, rx, ry) if rx > 0 and ry > 0 else None
        frames.append((f"ellipse_{i+1}", cx - rx, cy - ry, rx * 2, ry * 2, path_d))
    return frames


def parse_svg_file(svg_path: Path, base_url: str, id_prefix: str = "svg_") -> dict | None:
    """Parse one SVG file (mirror app: viewBox + parseLayout order) and return layout dict."""
    base_url = base_url.rstrip("/")
    stem = svg_path.stem
    layout_id = f"{id_prefix}{stem}" if id_prefix else stem
    name = stem.replace("_", " ").replace("-", " ").title()

    try:
        svg_string = svg_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"Error reading {svg_path}: {e}", file=sys.stderr)
        return None

    vbx, vby, vbw, vbh = _parse_viewbox_from_svg_string(svg_string)
    if vbw <= 0 or vbh <= 0:
        vbw, vbh = 500.0, 500.0

    raw_frames = _parse_layout_from_svg_string(svg_string)
    if not raw_frames:
        print(f"  No slots extracted from {svg_path}", file=sys.stderr)
        return None

    slots = []
    is_organic = False
    for i, (fid, x, y, w, h, path_d) in enumerate(raw_frames):
        n_x = round(x / vbw, 4)
        n_y = round(y / vbh, 4)
        n_w = round(w / vbw, 4)
        n_h = round(h / vbh, 4)
        slot = {"id": f"slot_{i}", "n_rect": [n_x, n_y, n_w, n_h]}
        if path_d:
            slot["path_data"] = path_d
            is_organic = True
        slots.append(slot)

    result = {
        "id": layout_id,
        "name": name,
        "category": "Stylish",
        "isPremium": False,
        "type": "organic" if is_organic else "grid",
        "thumbnailURL": f"{base_url}/thumbnails/{layout_id}.jpg",
        "slots": slots,
    }
    result["__viewbox"] = (0, 0, vbw, vbh)
    return result


def parse_svg_folder(svg_dir: Path, base_url: str, id_prefix: str = "svg_") -> list:
    """Parse all .svg files in svg_dir and return list of layout dicts."""
    layouts = []
    if not svg_dir.is_dir():
        print(f"SVG dir not found: {svg_dir}", file=sys.stderr)
        return layouts
    for path in sorted(svg_dir.glob("*.svg")):
        layout = parse_svg_file(path, base_url, id_prefix=id_prefix)
        if layout:
            layouts.append(layout)
    return layouts


# -----------------------------------------------------------------------------
# Thumbnails (Pillow: rects + path parsing for organic shapes)
# -----------------------------------------------------------------------------

# Distinct colors per slot index (for grid preview)
THUMB_COLORS = [
    (255, 82, 126),   # pink
    (78, 205, 196),   # teal
    (255, 180, 82),   # orange
    (160, 120, 255),  # purple
    (82, 180, 255),   # blue
    (255, 220, 100),  # yellow
    (180, 255, 120),  # green
    (255, 120, 160),  # rose
]


# Path tokenizer matching SVGLayoutParser: same regex (command letter OR number)
_PATH_DATA_REGEX = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|(-?\d*\.?\d+(?:[eE][+-]?\d+)?)")


def _path_components(d: str) -> list[str]:
    """Flat list of path tokens like Swift: command letters and numbers in order."""
    components = []
    for m in _PATH_DATA_REGEX.finditer(d):
        comp = m.group(1) or m.group(2)
        if comp and comp.strip():
            components.append(comp.strip())
    return components


def _flatten_path_to_polygons(d: str, viewbox: tuple[float, float, float, float] | None) -> list[list[tuple[float, float]]]:
    """Convert path d to polygons (0-1 coords). Mirrors SVGLayoutParser.createBezierPath + bounds in viewBox."""
    vbx, vby, vbw, vbh = viewbox if viewbox else (0.0, 0.0, 1.0, 1.0)
    if vbw <= 0 or vbh <= 0:
        vbw, vbh = 1.0, 1.0

    def norm(x: float, y: float) -> tuple[float, float]:
        return ((x - vbx) / vbw, (y - vby) / vbh)

    comps = _path_components(d)
    polygons: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    cur_x, cur_y = 0.0, 0.0
    start_x, start_y = 0.0, 0.0
    last_cp_x, last_cp_y = 0.0, 0.0  # for S/s
    n_steps = 24
    i = 0
    last_command = ""  # original letter so we know relative (m/l/c etc) vs absolute (M/L/C)

    def read_float() -> float | None:
        nonlocal i
        if i < len(comps):
            try:
                v = float(comps[i])
                i += 1
                return v
            except ValueError:
                pass
        return None

    while i < len(comps):
        c = comps[i]
        if len(c) == 1 and c in "MmLlHhVvCcSsQqTtAaZz":
            cmd_upper = c.upper()
            if cmd_upper == "A":
                pass  # keep last_command, skip arc
            else:
                last_command = c  # keep original for relative (m/l/c/s/q)
            if cmd_upper == "Z":
                if current:
                    current.append(current[0])
                    polygons.append(current)
                current = []
                cur_x, cur_y = start_x, start_y
                i += 1
                continue
            i += 1
            continue

        # Consume numbers for last_command (mirror Swift switch; support relative m/l/h/v/c/s/q)
        cmd = last_command.upper()
        if last_command == "M" or last_command == "m":
            x, y = read_float(), read_float()
            if x is None or y is None:
                break
            if last_command == "m":
                cur_x, cur_y = cur_x + x, cur_y + y
            else:
                cur_x, cur_y = x, y
            start_x, start_y = cur_x, cur_y
            if current:
                polygons.append(current)
            current = [norm(cur_x, cur_y)]
        elif last_command == "L" or last_command == "l":
            x, y = read_float(), read_float()
            if x is None or y is None:
                break
            if last_command == "l":
                cur_x, cur_y = cur_x + x, cur_y + y
            else:
                cur_x, cur_y = x, y
            current.append(norm(cur_x, cur_y))
        elif last_command == "H" or last_command == "h":
            x = read_float()
            if x is None:
                break
            cur_x = cur_x + x if last_command == "h" else x
            current.append(norm(cur_x, cur_y))
        elif last_command == "V" or last_command == "v":
            y = read_float()
            if y is None:
                break
            cur_y = cur_y + y if last_command == "v" else y
            current.append(norm(cur_x, cur_y))
        elif cmd == "C":
            vals = [read_float() for _ in range(6)]
            if any(v is None for v in vals):
                break
            if last_command == "c":
                cp1x, cp1y = cur_x + vals[0], cur_y + vals[1]
                cp2x, cp2y = cur_x + vals[2], cur_y + vals[3]
                x, y = cur_x + vals[4], cur_y + vals[5]
            else:
                cp1x, cp1y, cp2x, cp2y, x, y = vals
            last_cp_x, last_cp_y = cp2x, cp2y
            px, py = cur_x, cur_y
            for k in range(1, n_steps + 1):
                t = k / n_steps
                u = 1 - t
                bx = u * u * u * px + 3 * u * u * t * cp1x + 3 * u * t * t * cp2x + t * t * t * x
                by = u * u * u * py + 3 * u * u * t * cp1y + 3 * u * t * t * cp2y + t * t * t * y
                current.append(norm(bx, by))
            cur_x, cur_y = x, y
        elif cmd == "S":
            cp2x, cp2y, x, y = read_float(), read_float(), read_float(), read_float()
            if cp2x is None or cp2y is None or x is None or y is None:
                break
            cp1x = 2 * cur_x - last_cp_x
            cp1y = 2 * cur_y - last_cp_y
            if last_command == "s":
                cp2x, cp2y, x, y = cur_x + cp2x, cur_y + cp2y, cur_x + x, cur_y + y
            last_cp_x, last_cp_y = cp2x, cp2y
            px, py = cur_x, cur_y
            for k in range(1, n_steps + 1):
                t = k / n_steps
                u = 1 - t
                bx = u * u * u * px + 3 * u * u * t * cp1x + 3 * u * t * t * cp2x + t * t * t * x
                by = u * u * u * py + 3 * u * u * t * cp1y + 3 * u * t * t * cp2y + t * t * t * y
                current.append(norm(bx, by))
            cur_x, cur_y = x, y
        elif cmd == "Q":
            cpx, cpy, x, y = read_float(), read_float(), read_float(), read_float()
            if cpx is None or cpy is None or x is None or y is None:
                break
            if last_command == "q":
                cpx, cpy, x, y = cur_x + cpx, cur_y + cpy, cur_x + x, cur_y + y
            last_cp_x, last_cp_y = cpx, cpy
            px, py = cur_x, cur_y
            for k in range(1, n_steps + 1):
                t = k / n_steps
                u = 1 - t
                bx = u * u * px + 2 * u * t * cpx + t * t * x
                by = u * u * py + 2 * u * t * cpy + t * t * y
                current.append(norm(bx, by))
            cur_x, cur_y = x, y
        else:
            i += 1
    if current:
        polygons.append(current)
    return polygons


def _render_path_pillow(
    path_data: str, width: int, height: int, color_rgb: tuple,
    viewbox: tuple[float, float, float, float] | None = None,
) -> Image.Image | None:
    """Render SVG path to PIL Image using path parsing + polygon draw (no cairosvg)."""
    if Image is None or ImageDraw is None:
        return None
    polygons = _flatten_path_to_polygons(path_data, viewbox)
    if not polygons:
        return None
    img = Image.new("RGB", (width, height), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    for poly in polygons:
        if len(poly) < 2:
            continue
        # Scale 0-1 to pixel coords
        pts = [(int(p[0] * width), int(p[1] * height)) for p in poly]
        draw.polygon(pts, fill=color_rgb, outline=(80, 80, 80))
    return img


def _render_path_cairo(
    path_data: str, width: int, height: int, color_rgb: tuple,
    viewbox: tuple[float, float, float, float] | None = None,
) -> Image.Image | None:
    """Render SVG path with cairosvg if available."""
    if cairosvg is None or Image is None:
        return None
    d_escaped = path_data.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    r, g, b = color_rgb
    hex_color = f"#{r:02x}{g:02x}{b:02x}"
    if viewbox is not None:
        vbx, vby, vbw, vbh = viewbox
        vb = f"{vbx} {vby} {vbw} {vbh}"
    else:
        vb = "0 0 1 1"
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{width}" height="{height}">
  <path d="{d_escaped}" fill="{hex_color}" stroke="#505050" stroke-width="0.01"/>
</svg>'''
    try:
        png_bytes = cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=width, output_height=height)
        return Image.open(BytesIO(png_bytes)).convert("RGB")
    except Exception:
        return None


def draw_thumbnail(layout: dict, out_path: Path, size: int = 300) -> None:
    """Draw a 300x300 thumbnail. Uses path_data for organic slots; falls back to rect for grid."""
    if Image is None or ImageDraw is None:
        return
    img = Image.new("RGB", (size, size), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    slots = layout.get("slots", [])
    viewbox = layout.get("__viewbox")  # None for JSON (stylish); set for SVG-derived
    # Stylish from JSON: path_data is in full canvas 0-1 → draw paths on full image
    is_full_canvas_paths = viewbox is None

    for i, slot in enumerate(slots):
        nr = slot.get("n_rect", [0, 0, 1, 1])
        if len(nr) < 4:
            continue
        x = int(nr[0] * size)
        y = int(nr[1] * size)
        w = max(1, int(nr[2] * size))
        h = max(1, int(nr[3] * size))
        color = THUMB_COLORS[i % len(THUMB_COLORS)]

        path_data = slot.get("path_data")
        if path_data:
            if is_full_canvas_paths:
                # path_data in 0-1 full canvas: draw directly on full (size×size) image
                polygons = _flatten_path_to_polygons(path_data, viewbox=(0.0, 0.0, 1.0, 1.0))
                for poly in polygons:
                    if len(poly) < 2:
                        continue
                    pts = [(int(p[0] * size), int(p[1] * size)) for p in poly]
                    draw.polygon(pts, fill=color, outline=(80, 80, 80))
                continue
            # SVG-derived: path in viewBox coords → normalize to slot rect, render to slot image, paste
            slot_vb = None
            if viewbox and len(nr) >= 4:
                vbx, vby, vbw, vbh = viewbox[0], viewbox[1], viewbox[2], viewbox[3]
                if vbw > 0 and vbh > 0:
                    slot_vb = (vbx + nr[0] * vbw, vby + nr[1] * vbh, nr[2] * vbw, nr[3] * vbh)
            slot_img = _render_path_pillow(path_data, w, h, color, viewbox=slot_vb or viewbox)
            if slot_img is None and cairosvg is not None:
                slot_img = _render_path_cairo(path_data, w, h, color, viewbox=slot_vb or viewbox)
            if slot_img is not None:
                img.paste(slot_img, (x, y))
                continue
        # Grid slot or fallback: draw rectangle
        draw.rectangle([x, y, x + w, y + h], fill=color, outline=(80, 80, 80), width=1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=85)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate enhanced_manifest.json and thumbnails")
    parser.add_argument("--base-url", required=True, help="Raw GitHub base URL (no trailing slash)")
    parser.add_argument("--repo-root", default=".", help="Repo root directory")
    parser.add_argument("--json-path", default="classic_and_stylish_layouts.json", help="Path to classic+stylish JSON")
    parser.add_argument("--svg-dir", default="collages", help="Folder containing .svg files")
    parser.add_argument("--output", default="enhanced_manifest.json", help="Output manifest path")
    parser.add_argument("--thumbnails-dir", default="thumbnails", help="Output folder for thumbnails")
    parser.add_argument("--svg-id-prefix", default="svg_", help="Prefix for SVG-derived layout ids")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    json_path = repo_root / args.json_path if not os.path.isabs(args.json_path) else Path(args.json_path)
    svg_dir = repo_root / args.svg_dir
    output_path = repo_root / args.output
    thumb_dir = repo_root / args.thumbnails_dir
    base_url = args.base_url.strip().rstrip("/")

    if not json_path.exists():
        print(f"Error: JSON not found: {json_path}", file=sys.stderr)
        return 1

    # 1) Load classic + stylish
    layouts = load_classic_stylish_layouts(json_path, base_url)
    print(f"Loaded {len(layouts)} layouts from {json_path.name}")

    # 2) Parse SVGs
    svg_layouts = parse_svg_folder(svg_dir, base_url, id_prefix=args.svg_id_prefix)
    print(f"Parsed {len(svg_layouts)} layouts from {svg_dir}")
    layouts.extend(svg_layouts)

    # 3) Thumbnails
    thumb_dir.mkdir(parents=True, exist_ok=True)
    for layout in layouts:
        lid = layout["id"]
        draw_thumbnail(layout, thumb_dir / f"{lid}.jpg")
    print(f"Wrote {len(layouts)} thumbnails to {thumb_dir}")

    # 4) Write manifest (strip internal keys like __viewbox)
    def strip_internal_keys(layout: dict) -> dict:
        return {k: v for k, v in layout.items() if not k.startswith("__")}
    layouts_clean = [strip_internal_keys(l) for l in layouts]
    manifest = {"version": "2.0", "layouts": layouts_clean}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"Wrote {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
