#!/usr/bin/env python3
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
import sys
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

    return {
        "id": layout_id,
        "name": name,
        "category": "Stylish",
        "isPremium": False,
        "type": "organic" if is_organic else "grid",
        "thumbnailURL": f"{base_url}/thumbnails/{layout_id}.jpg",
        "slots": slots,
    }


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
# Thumbnails (Pillow)
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


def draw_thumbnail(layout: dict, out_path: Path, size: int = 300) -> None:
    """Draw a 300x300 thumbnail with one colored rect per slot (n_rect)."""
    if Image is None or ImageDraw is None:
        return
    img = Image.new("RGB", (size, size), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    slots = layout.get("slots", [])
    for i, slot in enumerate(slots):
        nr = slot.get("n_rect", [0, 0, 1, 1])
        if len(nr) < 4:
            continue
        x = nr[0] * size
        y = nr[1] * size
        w = nr[2] * size
        h = nr[3] * size
        color = THUMB_COLORS[i % len(THUMB_COLORS)]
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

    # 4) Write manifest
    manifest = {"version": "2.0", "layouts": layouts}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"Wrote {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
