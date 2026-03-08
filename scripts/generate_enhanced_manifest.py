#!/usr/bin/env python3
from __future__ import annotations

"""
Generate enhanced_manifest.json (version 2.0) by merging:
  1. classic_and_stylish_layouts.json (classic + stylish from app export)
  2. All .svg files in a folder (parsed with svgelements)

Also generates thumbnail images (one per layout) in thumbnails/.

Usage:
  python generate_enhanced_manifest.py --base-url "https://raw.githubusercontent.com/OWNER/REPO/main"
"""

import argparse
import json
import os
import re
import sys
from io import BytesIO
from pathlib import Path

# Optional: svgelements for SVG parsing
try:
    from svgelements import SVG, Path as SVGPath, Rect, Polygon, Circle, Ellipse
except ImportError:
    SVG = None

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
# SVG parsing (svgelements)
# -----------------------------------------------------------------------------

def _get_viewbox_size(svg) -> tuple:
    """Return (width, height) from SVG viewbox or default 500,500."""
    try:
        vb = getattr(svg, "viewbox", None)
        if vb is not None:
            w = getattr(vb, "width", None)
            h = getattr(vb, "height", None)
            if w is not None and h is not None and float(w) > 0 and float(h) > 0:
                return float(w), float(h)
            # viewbox can be a string "minX minY width height"
            if isinstance(vb, str):
                parts = vb.strip().split()
                if len(parts) >= 4:
                    return float(parts[2]), float(parts[3])
        if hasattr(svg, "width") and hasattr(svg, "height"):
            w, h = float(svg.width), float(svg.height)
            if w > 0 and h > 0:
                return w, h
    except Exception:
        pass
    return 500.0, 500.0


def parse_svg_file(svg_path: Path, base_url: str, id_prefix: str = "svg_") -> dict | None:
    """Parse one SVG and return a layout dict, or None on error."""
    if SVG is None:
        print("Warning: svgelements not installed; skipping SVG parsing.", file=sys.stderr)
        return None

    base_url = base_url.rstrip("/")
    stem = svg_path.stem
    layout_id = f"{id_prefix}{stem}" if id_prefix else stem
    name = stem.replace("_", " ").replace("-", " ").title()

    try:
        svg = SVG.parse(str(svg_path))
    except Exception as e:
        print(f"Error parsing {svg_path}: {e}", file=sys.stderr)
        return None

    vw, vh = _get_viewbox_size(svg)
    if vw <= 0 or vh <= 0:
        vw, vh = 500.0, 500.0

    # Collect elements that define slots (path, rect, polygon, circle, ellipse)
    element_types = (SVGPath, Rect, Polygon, Circle, Ellipse)
    elements = [e for e in svg.elements() if isinstance(e, element_types)]
    is_organic = any(isinstance(e, (SVGPath, Polygon, Circle, Ellipse)) for e in elements)

    slots = []
    for i, e in enumerate(elements):
        try:
            bbox = e.bbox()
            if bbox is None:
                continue
            # bbox can be (x1,y1,x2,y2) or similar
            x1, y1 = float(bbox[0]), float(bbox[1])
            x2, y2 = float(bbox[2]), float(bbox[3])
            n_x = round(x1 / vw, 4)
            n_y = round(y1 / vh, 4)
            n_w = round((x2 - x1) / vw, 4)
            n_h = round((y2 - y1) / vh, 4)
            slot = {"id": f"slot_{i}", "n_rect": [n_x, n_y, n_w, n_h]}
            if is_organic and hasattr(e, "d") and callable(e.d):
                try:
                    d_val = e.d()
                    if d_val:
                        slot["path_data"] = d_val
                except Exception:
                    pass
            slots.append(slot)
        except Exception as ex:
            print(f"  Skip element {i} in {svg_path}: {ex}", file=sys.stderr)

    if not slots:
        print(f"  No slots extracted from {svg_path}", file=sys.stderr)
        return None

    result = {
        "id": layout_id,
        "name": name,
        "category": "Stylish",
        "isPremium": False,
        "type": "organic" if is_organic else "grid",
        "thumbnailURL": f"{base_url}/thumbnails/{layout_id}.jpg",
        "slots": slots,
    }
    # Store viewbox for thumbnail rendering (path_data is in SVG coords); stripped before writing manifest
    result["__viewbox"] = (0, 0, vw, vh)
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


def _tokenize_path(d: str) -> list[tuple[str, list[float]]]:
    """Parse SVG path d string into list of (command, args). Handles M,L,H,V,C,S,Q,T,Z and lower."""
    # Split on command letters (allow minus/plus after letter for relative coords)
    parts = re.split(r"([MmLlHhVvCcSsQqTtZz])", d)
    tokens = []
    i = 1
    while i < len(parts):
        cmd = parts[i].strip()
        i += 1
        if i >= len(parts):
            break
        rest = parts[i].strip()
        i += 1
        if cmd.upper() == "Z":
            tokens.append((cmd.upper(), []))
            continue
        # Parse numbers (allow comma or space separated, and e.g. -0.5 or .5)
        nums = re.findall(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", rest)
        args = [float(n) for n in nums]
        tokens.append((cmd.upper(), args))
    return tokens


def _flatten_path_to_polygons(d: str, viewbox: tuple[float, float, float, float] | None) -> list[list[tuple[float, float]]]:
    """Convert path d to list of polygons (each polygon = list of (x,y) in 0-1)."""
    vbx, vby, vbw, vbh = viewbox if viewbox else (0.0, 0.0, 1.0, 1.0)
    if vbw <= 0 or vbh <= 0:
        vbw, vbh = 1.0, 1.0

    def norm(x: float, y: float) -> tuple[float, float]:
        return ((x - vbx) / vbw, (y - vby) / vbh)

    tokens = _tokenize_path(d)
    polygons: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    cur_x, cur_y = 0.0, 0.0
    start_x, start_y = 0.0, 0.0
    n_steps = 24  # curve segments

    for cmd, args in tokens:
        if cmd == "M":
            for j in range(0, len(args), 2):
                if j + 1 < len(args):
                    cur_x, cur_y = args[j], args[j + 1]
                    if current:
                        polygons.append(current)
                    current = [norm(cur_x, cur_y)]
                    start_x, start_y = cur_x, cur_y
        elif cmd == "L":
            for j in range(0, len(args), 2):
                if j + 1 < len(args):
                    cur_x, cur_y = args[j], args[j + 1]
                    current.append(norm(cur_x, cur_y))
        elif cmd == "H":
            for x in args:
                cur_x = x
                current.append(norm(cur_x, cur_y))
        elif cmd == "V":
            for y in args:
                cur_y = y
                current.append(norm(cur_x, cur_y))
        elif cmd == "C":
            for j in range(0, len(args), 6):
                if j + 5 >= len(args):
                    break
                x1, y1, x2, y2, x, y = args[j], args[j+1], args[j+2], args[j+3], args[j+4], args[j+5]
                px, py = cur_x, cur_y
                for k in range(1, n_steps + 1):
                    t = k / n_steps
                    u = 1 - t
                    bx = u*u*u*px + 3*u*u*t*x1 + 3*u*t*t*x2 + t*t*t*x
                    by = u*u*u*py + 3*u*u*t*y1 + 3*u*t*t*y2 + t*t*t*y
                    current.append(norm(bx, by))
                cur_x, cur_y = x, y
        elif cmd == "Q":
            for j in range(0, len(args), 4):
                if j + 3 >= len(args):
                    break
                x1, y1, x, y = args[j], args[j+1], args[j+2], args[j+3]
                px, py = cur_x, cur_y
                for k in range(1, n_steps + 1):
                    t = k / n_steps
                    u = 1 - t
                    bx = u*u*px + 2*u*t*x1 + t*t*x
                    by = u*u*py + 2*u*t*y1 + t*t*y
                    current.append(norm(bx, by))
                cur_x, cur_y = x, y
        elif cmd == "Z":
            if current:
                current.append(current[0])
                polygons.append(current)
            current = []
            cur_x, cur_y = start_x, start_y
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
    """Draw a 300x300 thumbnail. Uses path_data for organic slots; falls back to rect for grid or if cairosvg unavailable."""
    if Image is None or ImageDraw is None:
        return
    img = Image.new("RGB", (size, size), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    slots = layout.get("slots", [])
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
            viewbox = layout.get("__viewbox")  # SVG-derived layouts have path in file coords
            # Prefer Pillow path parsing (works everywhere); fall back to cairo then rect
            slot_img = _render_path_pillow(path_data, w, h, color, viewbox=viewbox)
            if slot_img is None and cairosvg is not None:
                slot_img = _render_path_cairo(path_data, w, h, color, viewbox=viewbox)
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
