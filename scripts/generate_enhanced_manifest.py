#!/usr/bin/env python3
from __future__ import annotations

"""
Generate enhanced_manifest.json (version 2.0) by merging:
  1. classic_and_stylish_layouts.json (classic_layouts only; stylish removed)
  2. All .svg files in a folder (parsed with svgelements; circles/ellipses from raw SVG like app)

Drag handles: <line> elements with id starting with DRAG_H_ or DRAG_V_ are detected and emitted
as dividers (not as photo slots). Position: DRAG_H_ → y1/viewBoxHeight; DRAG_V_ → x1/viewBoxWidth.
Each divider has an "affects" list of slot ids whose rect edge matches the divider position.

Store manifests (stickers, frames, filters, …): a category folder whose name ends with ``_PR`` or ``_F``
sets the default premium flag for every asset in that folder when the file stem has no ``_PR``/``_F``.
Per-file suffixes always override the folder default.

Usage:
  python generate_enhanced_manifest.py --base-url "https://raw.githubusercontent.com/OWNER/REPO/main"
"""

import argparse
import copy
import difflib
import json
import math
import os
import random
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    from svgelements import (
        SVG,
        Path as SVGPath,
        Rect,
        Polygon,
        Circle,
        Ellipse,
        Matrix,
        Move,
        Line,
        Curve,
        Close,
    )
except ImportError:
    SVG = SVGPath = Rect = Polygon = Circle = Ellipse = None
    Matrix = Move = Line = Curve = Close = None

try:
    import cairosvg
except ImportError:
    cairosvg = None

try:
    from fontTools.ttLib import TTFont
except ImportError:
    TTFont = None


# -----------------------------------------------------------------------------
# Classic layouts from JSON (stylish removed; use SVG-derived layouts for organic)
# -----------------------------------------------------------------------------

def load_classic_layouts(json_path: Path, base_url: str) -> list:
    """Load classic_and_stylish_layouts.json classic_layouts only; add type + thumbnailURL."""
    base_url = base_url.rstrip("/")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    layouts = []
    for layout in data.get("classic_layouts", []):
        layout = dict(layout)
        slot_list = layout.get("slots", [])
        has_path_data = any(s.get("path_data") for s in slot_list)
        layout["type"] = "organic" if has_path_data else "grid"
        layout["thumbnailURL"] = None
        layout.pop("slot_count", None)
        slots_out = []
        for i, slot in enumerate(slot_list):
            s = {"id": slot.get("id", f"slot_{i}"), "n_rect": slot["n_rect"]}
            if slot.get("path_data"):
                s["path_data"] = slot["path_data"]
            slots_out.append(s)
        layout["slots"] = slots_out
        layout.setdefault("dividers", [])
        layouts.append(layout)
    return layouts


# -----------------------------------------------------------------------------
# SVG parsing (svgelements – simple bbox + path_data)
# -----------------------------------------------------------------------------

def _ellipse_points(cx: float, cy: float, rx: float, ry: float, steps: int = 32) -> list[tuple[float, float]]:
    """Points along ellipse (for path_data or transform)."""
    if rx <= 0 or ry <= 0:
        return []
    return [
        (cx + rx * math.cos(2 * math.pi * i / steps), cy + ry * math.sin(2 * math.pi * i / steps))
        for i in range(steps + 1)
    ]


def _ellipse_path_d(cx: float, cy: float, rx: float, ry: float, steps: int = 32) -> str:
    """SVG path d for ellipse (polygon approximation) for thumbnails."""
    pts = _ellipse_points(cx, cy, rx, ry, steps)
    return "M " + " ".join(f"{x} {y}" for x, y in pts) + " Z" if pts else ""


def _apply_matrix_to_points(
    points: list[tuple[float, float]], matrix,
) -> list[tuple[float, float]]:
    """Apply SVG transform matrix (a,b,c,d,e,f) to each point: x'=a*x+c*y+e, y'=b*x+d*y+f."""
    a = getattr(matrix, "a", 1.0)
    b = getattr(matrix, "b", 0.0)
    c = getattr(matrix, "c", 0.0)
    d = getattr(matrix, "d", 1.0)
    e = getattr(matrix, "e", 0.0)
    f = getattr(matrix, "f", 0.0)
    return [(a * x + c * y + e, b * x + d * y + f) for x, y in points]


def _get_viewbox_size(svg) -> tuple[float, float]:
    """Return (width, height) from SVG viewbox or default 500,500."""
    try:
        vb = getattr(svg, "viewbox", None)
        if vb is not None:
            w = getattr(vb, "width", None)
            h = getattr(vb, "height", None)
            if w is not None and h is not None and float(w) > 0 and float(h) > 0:
                return float(w), float(h)
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


def _path_d_for_element(e) -> str | None:
    """Return path_data string for organic elements (Path, Polygon, Circle, Ellipse), else None."""
    if SVGPath is not None and isinstance(e, SVGPath):
        try:
            d = e.d()
            return d if d else None
        except Exception:
            return None
    if Polygon is not None and isinstance(e, Polygon):
        try:
            pts = getattr(e, "points", None)
            if pts is None:
                return None
            # points can be list of (x,y) or Points object
            if callable(pts):
                pts = pts()
            if hasattr(pts, "__iter__") and not isinstance(pts, str):
                flat = []
                for p in pts:
                    if hasattr(p, "__len__") and len(p) >= 2:
                        flat.append((float(p[0]), float(p[1])))
                    else:
                        break
                if len(flat) >= 3:
                    return "M " + " L ".join(f"{x} {y}" for x, y in flat) + " Z"
            return None
        except Exception:
            return None
    def _path_from_ellipse_points(e, cx: float, cy: float, rx: float, ry: float):
        pts = _ellipse_points(cx, cy, rx, ry)
        if not pts:
            return None
        # Apply transform if present (e.g. transform="matrix(...)" on ellipse/circle)
        transform = getattr(e, "transform", None)
        apply_transform = getattr(e, "apply", True)
        if transform is not None and apply_transform:
            try:
                if not transform.is_identity():
                    pts = _apply_matrix_to_points(pts, transform)
            except Exception:
                pass
        return "M " + " L ".join(f"{x} {y}" for x, y in pts) + " Z"

    if Circle is not None and isinstance(e, Circle):
        cx, cy = float(getattr(e, "cx", 0) or 0), float(getattr(e, "cy", 0) or 0)
        r = float(getattr(e, "r", 0) or 0)
        return _path_from_ellipse_points(e, cx, cy, r, r) if r > 0 else None
    if Ellipse is not None and isinstance(e, Ellipse):
        cx, cy = float(getattr(e, "cx", 0) or 0), float(getattr(e, "cy", 0) or 0)
        rx, ry = float(getattr(e, "rx", 0) or 0), float(getattr(e, "ry", 0) or 0)
        return _path_from_ellipse_points(e, cx, cy, rx, ry) if rx > 0 and ry > 0 else None
    return None


def _is_drag_handle_element(e) -> bool:
    """Return True if element is a drag handle (id starts with DRAG_H_ or DRAG_V_)."""
    eid = getattr(e, "id", None)
    if eid is None:
        return False
    eid = str(eid).strip()
    return eid.startswith("DRAG_H_") or eid.startswith("DRAG_V_")


def _compute_dividers_from_handles(
    handles: list[dict],
    slots: list[dict],
    vbw: float,
    vbh: float,
) -> list[dict]:
    """
    Build dividers array: for each handle, set position (0-1), affects (slot ids), and
    segment_start/segment_end (0-1) so the Swift app knows exactly where to draw and hit-test the handle.
    - DRAG_H_: position = y1/viewBoxHeight; segment = X range (min/max of affected slots' n_rect).
    - DRAG_V_: position = x1/viewBoxWidth; segment = Y range (min/max of affected slots' n_rect).
    """
    dividers = []
    for h in handles:
        if h["type"] == "horizontal":
            position = round(h["y1"] / vbh, 4) if vbh > 0 else 0.0
        else:
            position = round(h["x1"] / vbw, 4) if vbw > 0 else 0.0
        affects = []
        for slot in slots:
            nr = slot.get("n_rect", [0, 0, 1, 1])
            if len(nr) < 4:
                continue
            n_x, n_y, n_w, n_h = nr[0], nr[1], nr[2], nr[3]
            min_x, max_x = n_x, n_x + n_w
            min_y, max_y = n_y, n_y + n_h
            if h["type"] == "horizontal":
                if abs(min_y - position) <= _DIVIDER_EPS or abs(max_y - position) <= _DIVIDER_EPS:
                    affects.append(slot["id"])
            else:
                if abs(min_x - position) <= _DIVIDER_EPS or abs(max_x - position) <= _DIVIDER_EPS:
                    affects.append(slot["id"])

        # Segment bounds: extent of affected slots along the axis perpendicular to the divider.
        segment_start, segment_end = 0.0, 1.0
        if affects:
            affected_slots = [s for s in slots if s["id"] in affects]
            if affected_slots:
                if h["type"] == "horizontal":
                    # Segment = X range (left–right) of affected slots
                    min_xs = [s["n_rect"][0] for s in affected_slots if len(s.get("n_rect", [])) >= 4]
                    max_xs = [s["n_rect"][0] + s["n_rect"][2] for s in affected_slots if len(s.get("n_rect", [])) >= 4]
                    if min_xs and max_xs:
                        segment_start = round(min(min_xs), 4)
                        segment_end = round(max(max_xs), 4)
                else:
                    # Segment = Y range (top–bottom) of affected slots
                    min_ys = [s["n_rect"][1] for s in affected_slots if len(s.get("n_rect", [])) >= 4]
                    max_ys = [s["n_rect"][1] + s["n_rect"][3] for s in affected_slots if len(s.get("n_rect", [])) >= 4]
                    if min_ys and max_ys:
                        segment_start = round(min(min_ys), 4)
                        segment_end = round(max(max_ys), 4)

        dividers.append({
            "id": h["id"],
            "type": h["type"],
            "position": position,
            "affects": affects,
            "segment_start": segment_start,
            "segment_end": segment_end,
        })
    return dividers


# Tolerance for "same boundary" when inferring dividers from slot edges (normalized 0-1).
_BOUNDARY_EPS = 1e-5


def _generate_dividers_from_slot_boundaries(layout: dict) -> list[dict]:
    """
    For a regular grid layout: compare n_rect boundaries of all slots and generate a divider
    wherever two slots share a boundary. Only considers slots with n_rect [x, y, w, h].
    Returns list of divider dicts (id, type, position, affects, segment_start, segment_end).
    """
    slots = layout.get("slots", [])
    if len(slots) < 2:
        return []

    def nr(s: dict) -> tuple[float, float, float, float] | None:
        r = s.get("n_rect", [])
        if len(r) < 4:
            return None
        return float(r[0]), float(r[1]), float(r[2]), float(r[3])

    # Collect unique vertical boundaries (x positions where two slots share a vertical edge).
    v_positions: set[float] = set()
    for i, a in enumerate(slots):
        ra = nr(a)
        if ra is None:
            continue
        ax1, ay1, aw, ah = ra
        ax2, ay2 = ax1 + aw, ay1 + ah
        for b in slots[i + 1 :]:
            rb = nr(b)
            if rb is None:
                continue
            bx1, by1, bw, bh = rb
            bx2, by2 = bx1 + bw, by1 + bh
            # Y ranges must overlap for slots to be adjacent along a vertical line
            y_overlap = not (ay2 <= by1 + _BOUNDARY_EPS or by2 <= ay1 + _BOUNDARY_EPS)
            if not y_overlap:
                continue
            if abs(ax2 - bx1) <= _BOUNDARY_EPS:
                v_positions.add(round((ax2 + bx1) / 2, 6))
            elif abs(bx2 - ax1) <= _BOUNDARY_EPS:
                v_positions.add(round((bx2 + ax1) / 2, 6))

    # Collect unique horizontal boundaries (y positions where two slots share a horizontal edge).
    h_positions: set[float] = set()
    for i, a in enumerate(slots):
        ra = nr(a)
        if ra is None:
            continue
        ax1, ay1, aw, ah = ra
        ax2, ay2 = ax1 + aw, ay1 + ah
        for b in slots[i + 1 :]:
            rb = nr(b)
            if rb is None:
                continue
            bx1, by1, bw, bh = rb
            bx2, by2 = bx1 + bw, by1 + bh
            x_overlap = not (ax2 <= bx1 + _BOUNDARY_EPS or bx2 <= ax1 + _BOUNDARY_EPS)
            if not x_overlap:
                continue
            if abs(ay2 - by1) <= _BOUNDARY_EPS:
                h_positions.add(round((ay2 + by1) / 2, 6))
            elif abs(by2 - ay1) <= _BOUNDARY_EPS:
                h_positions.add(round((by2 + ay1) / 2, 6))

    dividers: list[dict] = []
    idx = 0
    for pos in sorted(v_positions):
        affects = []
        seg_min_x, seg_max_x = 1.0, 0.0
        for s in slots:
            r = nr(s)
            if r is None:
                continue
            x1, y1, w, h = r
            x2, y2 = x1 + w, y1 + h
            if abs(x1 - pos) <= _BOUNDARY_EPS or abs(x2 - pos) <= _BOUNDARY_EPS:
                affects.append(s["id"])
                seg_min_x = min(seg_min_x, y1)
                seg_max_x = max(seg_max_x, y2)
        if affects:
            dividers.append({
                "id": f"DRAG_V_{idx}",
                "type": "vertical",
                "position": round(pos, 4),
                "affects": affects,
                "segment_start": round(seg_min_x, 4),
                "segment_end": round(seg_max_x, 4),
            })
            idx += 1

    for pos in sorted(h_positions):
        affects = []
        seg_min_y, seg_max_y = 1.0, 0.0
        for s in slots:
            r = nr(s)
            if r is None:
                continue
            x1, y1, w, h = r
            x2, y2 = x1 + w, y1 + h
            if abs(y1 - pos) <= _BOUNDARY_EPS or abs(y2 - pos) <= _BOUNDARY_EPS:
                affects.append(s["id"])
                seg_min_y = min(seg_min_y, x1)
                seg_max_y = max(seg_max_y, x2)
        if affects:
            dividers.append({
                "id": f"DRAG_H_{idx}",
                "type": "horizontal",
                "position": round(pos, 4),
                "affects": affects,
                "segment_start": round(seg_min_y, 4),
                "segment_end": round(seg_max_y, 4),
            })
            idx += 1

    return dividers


def _ensure_grid_dividers(layouts: list[dict]) -> None:
    """For each regular grid layout with empty dividers, auto-generate dividers from slot boundaries."""
    for layout in layouts:
        if layout.get("type") != "grid":
            continue
        divs = layout.get("dividers") or []
        if divs:
            continue
        generated = _generate_dividers_from_slot_boundaries(layout)
        if generated:
            layout["dividers"] = generated


def parse_svg_file(
    svg_path: Path,
    base_url: str,
    id_prefix: str = "svg_",
    folder_premium_default: bool | None = None,
) -> dict | None:
    """Parse one SVG with svgelements: bbox → n_rect, path_data for organic shapes; drag handles → dividers."""
    if SVG is None:
        print("Warning: svgelements not installed; run pip install svgelements", file=sys.stderr)
        return None
    base_url = base_url.rstrip("/")
    stem = svg_path.stem
    layout_id = f"{id_prefix}{stem}" if id_prefix else stem
    try:
        svg = SVG.parse(str(svg_path))
    except Exception as e:
        print(f"Error parsing {svg_path}: {e}", file=sys.stderr)
        return None

    vbw, vbh = _get_viewbox_size(svg)
    if vbw <= 0 or vbh <= 0:
        vbw, vbh = 500.0, 500.0

    # Path, Rect, Polygon from svgelements; Circle/Ellipse from raw SVG. Exclude drag-handle <line> elements.
    element_types = (SVGPath, Rect, Polygon, Ellipse)
    elements = [
        e for e in svg.elements()
        if isinstance(e, element_types) and not _is_drag_handle_element(e)
    ]
    raw_circles = _parse_raw_svg_circles(svg_path)
    if not elements and not raw_circles:
        print(f"  No slots in {svg_path}", file=sys.stderr)
        return None

    is_organic = any(isinstance(e, (SVGPath, Polygon, Ellipse)) for e in elements) or bool(raw_circles)
    slots = []
    slot_index = 0
    for e in elements:
        try:
            bbox = e.bbox()
            if bbox is None:
                continue
            x1, y1 = float(bbox[0]), float(bbox[1])
            x2, y2 = float(bbox[2]), float(bbox[3])
            n_x = round(x1 / vbw, 4)
            n_y = round(y1 / vbh, 4)
            n_w = round((x2 - x1) / vbw, 4)
            n_h = round((y2 - y1) / vbh, 4)
            slot = {"id": f"slot_{slot_index}", "n_rect": [n_x, n_y, n_w, n_h]}
            path_d = _path_d_for_element(e)
            if path_d:
                slot["path_data"] = path_d
            slots.append(slot)
            slot_index += 1
        except Exception as ex:
            print(f"  Skip element in {svg_path}: {ex}", file=sys.stderr)

    for cx, cy, r in raw_circles:
        x1, y1 = cx - r, cy - r
        n_x = round(x1 / vbw, 4)
        n_y = round(y1 / vbh, 4)
        n_w = round(2 * r / vbw, 4)
        n_h = round(2 * r / vbh, 4)
        path_d = "M " + " L ".join(f"{x} {y}" for x, y in _ellipse_points(cx, cy, r, r)) + " Z"
        slots.append({
            "id": f"slot_{slot_index}",
            "n_rect": [n_x, n_y, n_w, n_h],
            "path_data": path_d,
        })
        slot_index += 1

    if not slots:
        return None

    # Drag handles from <line> elements (DRAG_H_*, DRAG_V_*) → dividers with position and affects
    raw_handles = _parse_raw_svg_drag_handles(svg_path)
    dividers = _compute_dividers_from_handles(raw_handles, slots, vbw, vbh)

    # Category and premium from SVG file name:
    # - If name contains "_CL" → category = "Classic"
    # - If name contains "_SL" (or anything else) → category = "Stylish"
    # - If name contains "_PR" → isPremium = True
    # - If name contains "_F"  → isPremium = False
    # - If neither suffix present → keep current default behavior (premium for SVG-derived layouts)
    stem_upper = stem.upper()
    if "_CL" in stem_upper:
        category = "Classic"
    else:
        # Default / "_SL" / no marker → Stylish collages
        category = "Stylish"

    if "_PR" in stem_upper:
        is_premium = True
    elif "_F" in stem_upper:
        is_premium = False
    else:
        is_premium = (
            folder_premium_default if folder_premium_default is not None else True
        )

    display_stem = _clean_stem_premium_suffix(stem)
    name = display_stem.replace("_", " ").replace("-", " ").title()

    result = {
        "id": layout_id,
        "name": name,
        "category": category,
        # SVG-derived layouts use filename suffixes to determine premium; existing JSON layouts keep their own isPremium.
        "isPremium": is_premium,
        "type": "organic" if is_organic else "grid",
        "thumbnailURL": None,
        "slots": slots,
        "dividers": dividers,
    }
    result["__viewbox"] = (0, 0, vbw, vbh)
    return result


# Regex to find <circle ...> and extract cx, cy, r (matches any attribute order; optional quotes)
_CIRCLE_TAG_RE = re.compile(
    r"<circle\s[^>]*?>",
    re.IGNORECASE | re.DOTALL,
)
_ATTR_RE = re.compile(
    r"\b(cx|cy|r)\s*=\s*['\"]?([^\"'\s>]+)['\"]?",
    re.IGNORECASE,
)

# Regex to find <line ...> for drag handles (id starts with DRAG_H_ or DRAG_V_)
_LINE_TAG_RE = re.compile(
    r"<line\s[^>]*?>",
    re.IGNORECASE | re.DOTALL,
)
_LINE_ATTR_RE = re.compile(
    r"\b(id|x1|y1|x2|y2)\s*=\s*['\"]?([^\"'\s>]+)['\"]?",
    re.IGNORECASE,
)

# Tolerance for matching divider position to slot edge (normalized 0-1).
# 0.01 allows SVG coordinates off by a tiny fraction of a pixel to still match.
_DIVIDER_EPS = 0.01


def _parse_raw_svg_drag_handles(svg_path: Path) -> list[dict]:
    """
    Parse raw SVG for <line> elements with id starting with DRAG_H_ or DRAG_V_.
    Returns list of {"id": str, "type": "horizontal"|"vertical", "x1": float, "y1": float}.
    """
    handles = []
    try:
        text = svg_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return handles
    for tag_match in _LINE_TAG_RE.finditer(text):
        tag = tag_match.group(0)
        attrs = {}
        for m in _LINE_ATTR_RE.finditer(tag):
            attrs[m.group(1).lower()] = m.group(2).strip()
        elem_id = attrs.get("id", "")
        if not elem_id:
            continue
        if elem_id.startswith("DRAG_H_"):
            try:
                x1 = float(attrs.get("x1", 0))
                y1 = float(attrs.get("y1", 0))
                handles.append({"id": elem_id, "type": "horizontal", "x1": x1, "y1": y1})
            except (ValueError, TypeError):
                pass
        elif elem_id.startswith("DRAG_V_"):
            try:
                x1 = float(attrs.get("x1", 0))
                y1 = float(attrs.get("y1", 0))
                handles.append({"id": elem_id, "type": "vertical", "x1": x1, "y1": y1})
            except (ValueError, TypeError):
                pass
    return handles


def _parse_raw_svg_circles(svg_path: Path) -> list[tuple[float, float, float]]:
    """Parse raw SVG file for <circle cx cy r> (like app's CircleXMLParser). Returns list of (cx, cy, r)."""
    circles = []
    try:
        text = svg_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return circles
    for tag_match in _CIRCLE_TAG_RE.finditer(text):
        tag = tag_match.group(0)
        attrs = {}
        for m in _ATTR_RE.finditer(tag):
            attrs[m.group(1).lower()] = m.group(2)
        cx = float(attrs.get("cx", 0))
        cy = float(attrs.get("cy", 0))
        r = float(attrs.get("r", 0))
        if r > 0:
            circles.append((cx, cy, r))
    return circles


def parse_svg_folder(svg_dir: Path, base_url: str, id_prefix: str = "svg_") -> list:
    """Parse all .svg files in svg_dir and return list of layout dicts."""
    layouts = []
    if not svg_dir.is_dir():
        print(f"SVG dir not found: {svg_dir}", file=sys.stderr)
        return layouts
    _, folder_fd = _folder_display_base_and_premium_default(svg_dir.name)
    for path in sorted(svg_dir.glob("*.svg")):
        layout = parse_svg_file(
            path, base_url, id_prefix=id_prefix, folder_premium_default=folder_fd
        )
        if layout:
            layouts.append(layout)
    return layouts


# -----------------------------------------------------------------------------
# Thumbnails (Pillow: rects + path parsing for organic shapes)
# -----------------------------------------------------------------------------

# Distinct colors per slot index (for grid preview)
# Toolbar / placed-shape default fill — PhotoCollage/Colors.xcassets/appTint.colorset
SHAPE_TOOLBAR_PREVIEW_RGB = (255, 91, 138)

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
    n_steps = 48  # smoother Bezier curves for stylish paths
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
            last_command = "L"  # SVG: after M, next coords are implicit L
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
    """Render SVG path to PIL Image (RGBA, transparent bg) for PNG thumbnails."""
    if Image is None or ImageDraw is None:
        return None
    polygons = _flatten_path_to_polygons(path_data, viewbox)
    if not polygons:
        return None
    img = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    fill_rgba = (*color_rgb, 255)
    for poly in polygons:
        if len(poly) < 2:
            continue
        pts = [(int(p[0] * width), int(p[1] * height)) for p in poly]
        draw.polygon(pts, fill=fill_rgba, outline=(80, 80, 80, 255))
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


def _path_segments_to_polygons(scaled_path) -> list[list[tuple[float, float]]]:
    """
    Convert svgelements Path segments to list of polygons (list of (x,y) points).
    Move = start new subpath; Line = add end point; Curve = sample Bezier; Close = close to first.
    """
    polygons = []
    current = []
    n_curve_steps = 12  # sample Bezier curves for smooth outline

    for segment in scaled_path:
        if isinstance(segment, Move):
            if current:
                polygons.append(current)
            end = segment.end
            current = [(end.x, end.y)] if end is not None else []
        elif isinstance(segment, Line):
            end = segment.end
            if end is not None:
                current.append((end.x, end.y))
        elif isinstance(segment, Curve):
            # Sample points along the Bezier (t=0 is segment.start, t=1 is segment.end)
            for k in range(1, n_curve_steps + 1):
                t = k / n_curve_steps
                try:
                    p = segment.point(t)
                    if p is not None:
                        current.append((p.x, p.y))
                except Exception:
                    pass
        elif isinstance(segment, Close):
            if current:
                current.append(current[0])
                polygons.append(current)
            current = []

    if current:
        polygons.append(current)
    return polygons


def _draw_stylish_thumbnail(img: Image.Image, draw, layout: dict, size: int) -> None:
    """
    Stylish thumbnail using path_data from JSON (same SVG path method as elsewhere).
    path_data is 0-1 normalized; we parse with _flatten_path_to_polygons (SVGLayoutParser-style)
    and scale to pixels. Slots without path_data use n_rect as a rectangle.
    """
    slots = layout.get("slots", [])
    viewbox_01 = (0.0, 0.0, 1.0, 1.0)
    for i, slot in enumerate(slots):
        path_data = slot.get("path_data")
        if not path_data:
            nr = slot.get("n_rect", [0, 0, 1, 1])
            if len(nr) >= 4:
                x = round(nr[0] * (size - 1))
                y = round(nr[1] * (size - 1))
                w = max(1, round(nr[2] * (size - 1)))
                h = max(1, round(nr[3] * (size - 1)))
                color = (*THUMB_COLORS[i % len(THUMB_COLORS)], 255)
                draw.rectangle([x, y, x + w, y + h], fill=color, outline=(80, 80, 80, 255), width=1)
            continue

        color = (*THUMB_COLORS[i % len(THUMB_COLORS)], 255)
        polygons = _flatten_path_to_polygons(path_data, viewbox=viewbox_01)
        for poly in polygons:
            if len(poly) < 2:
                continue
            pts = [(round(p[0] * (size - 1)), round(p[1] * (size - 1))) for p in poly]
            draw.polygon(pts, fill=color, outline=(80, 80, 80, 255))


def draw_thumbnail(layout: dict, out_path: Path, size: int = 300) -> None:
    """Draw a 300x300 PNG thumbnail. Mirrors app: MasterCollage (layout) → one image; each slot = one MasterView (shape from path_data or n_rect). Stylish = 0-1 path_data; SVG-derived = slot viewbox + paste; grid = rect."""
    if Image is None or ImageDraw is None:
        return
    img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    slots = layout.get("slots", [])
    viewbox = layout.get("__viewbox")  # None = stylish (JSON), set = SVG-derived

    if viewbox is None:
        # Stylish: path_data from JSON (same SVG path method as SVG-derived)
        _draw_stylish_thumbnail(img, draw, layout, size)
    else:
        # SVG-derived or grid: per-slot rect or path with slot viewbox
        for i, slot in enumerate(slots):
            nr = slot.get("n_rect", [0, 0, 1, 1])
            if len(nr) < 4:
                continue
            vbx, vby, vbw, vbh = viewbox[0], viewbox[1], viewbox[2], viewbox[3]
            x = int(nr[0] * size)
            y = int(nr[1] * size)
            w = max(1, int(nr[2] * size))
            h = max(1, int(nr[3] * size))
            color = (*THUMB_COLORS[i % len(THUMB_COLORS)], 255)
            path_data = slot.get("path_data")
            if path_data and vbw > 0 and vbh > 0:
                slot_vb = (vbx + nr[0] * vbw, vby + nr[1] * vbh, nr[2] * vbw, nr[3] * vbh)
                slot_img = _render_path_pillow(path_data, w, h, color[:3], viewbox=slot_vb)
                if slot_img is None and cairosvg is not None:
                    slot_img = _render_path_cairo(path_data, w, h, color[:3], viewbox=slot_vb)
                if slot_img is not None:
                    if slot_img.mode != "RGBA":
                        slot_img = slot_img.convert("RGBA")
                    img.paste(slot_img, (x, y), slot_img)
                else:
                    draw.rectangle([x, y, x + w, y + h], fill=color, outline=(80, 80, 80, 255), width=1)
            else:
                draw.rectangle([x, y, x + w, y + h], fill=color, outline=(80, 80, 80, 255), width=1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")


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
    parser.add_argument("--thumbnails-dir", default="thumbnails", help="Output folder for PNG thumbnails (only with --generate-thumbnails)")
    parser.add_argument(
        "--generate-thumbnails",
        action="store_true",
        default=False,
        help="Also render PNG thumbnails/ folder (off by default; app uses CollageThumbnailView).",
    )
    parser.add_argument("--svg-id-prefix", default="svg_", help="Prefix for SVG-derived layout ids")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    json_path = repo_root / args.json_path if not os.path.isabs(args.json_path) else Path(args.json_path)
    svg_dir = repo_root / args.svg_dir
    output_path = repo_root / args.output
    thumb_dir = repo_root / args.thumbnails_dir
    base_url = args.base_url.strip().rstrip("/")

    # Load existing enhanced_manifest (if any) so we can auto-bump the version only when layouts change.
    old_manifest = None
    old_version: str | None = None
    old_layouts: list | None = None
    if output_path.exists():
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                old_manifest = json.load(f)
            old_version = str(old_manifest.get("version", "") or "")
            old_layouts = old_manifest.get("layouts")
        except Exception:
            old_manifest = None
            old_version = None
            old_layouts = None

    if not json_path.exists() and not os.path.isabs(args.json_path):
        fallback_json = repo_root / "PhotoCollageMaker/PhotoCollage/Collage/classic_and_stylish_layouts.json"
        if fallback_json.exists():
            json_path = fallback_json
    if not svg_dir.is_dir() and not os.path.isabs(args.svg_dir):
        fallback_svg = repo_root / "PhotoCollageMaker/PhotoCollage/Collage/CollageLayouts"
        if fallback_svg.is_dir():
            svg_dir = fallback_svg

    if not json_path.exists():
        print(f"Error: JSON not found: {json_path}", file=sys.stderr)
        return 1

    # 1) Load classic + stylish
    layouts = load_classic_layouts(json_path, base_url)
    print(f"Loaded {len(layouts)} layouts from {json_path.name}")

    # 2) Parse SVGs (new layouts will be marked premium by parse_svg_file)
    svg_layouts = parse_svg_folder(svg_dir, base_url, id_prefix=args.svg_id_prefix)
    print(f"Parsed {len(svg_layouts)} layouts from {svg_dir}")
    layouts.extend(svg_layouts)

    # 2b) For regular grid layouts with empty dividers, auto-generate dividers from slot boundaries
    _ensure_grid_dividers(layouts)
    print("Auto-generated dividers for grid layouts that had none.")

    # 3) Optional PNG thumbnails (app previews layouts via slot geometry, not CDN PNGs)
    if args.generate_thumbnails:
        thumb_dir.mkdir(parents=True, exist_ok=True)
        for layout in layouts:
            lid = layout["id"]
            draw_thumbnail(layout, thumb_dir / f"{lid}.png")
        print(f"Wrote {len(layouts)} thumbnails to {thumb_dir}")
    else:
        print("Skipped PNG thumbnail generation (use --generate-thumbnails to enable)")

    # 4) Write manifest (strip internal keys like __viewbox)
    def strip_internal_keys(layout: dict) -> dict:
        return {k: v for k, v in layout.items() if not k.startswith("__")}
    layouts_clean = [strip_internal_keys(l) for l in layouts]
    # Compare using id-sorted copies only — do **not** write sorted layouts, or enhanced_manifest.json
    # order changes every run and breaks collage serial / stable ordering from JSON + SVG scan.
    layouts_norm = _canonicalize_layouts_list(layouts_clean)

    # Determine version: auto-bump minor when layouts differ from existing manifest (JSON or SVG changes).
    def bump_version_string(v: str | None) -> str:
        try:
            if v and "." in v:
                major_s, minor_s = v.split(".", 1)
                major = int(major_s)
                minor = int(minor_s)
            elif v:
                major = int(v)
                minor = 0
            else:
                major, minor = 2, 0
        except Exception:
            major, minor = 2, 0
        minor += 1
        if minor > 9:
            major += 1
            minor = 0
        return f"{major}.{minor}"

    old_layouts_norm = _canonicalize_layouts_list(old_layouts) if old_layouts is not None else None
    layouts_changed = old_layouts_norm is None or old_layouts_norm != layouts_norm
    if layouts_changed:
        new_version = bump_version_string(old_version)
    else:
        # No layout change: keep previous version if present, else default starting point.
        new_version = old_version or "2.1"

    manifest = {"version": new_version, "layouts": layouts_clean}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"Wrote {output_path}")

    return 0


# =============================================================================
# Filter manifest generation
# =============================================================================
#
# Generates Filters/filter_manifest.json by scanning the Filters/ folder for
# sub-folders (= filter categories) and LUT assets: PNG (HALD-style) and/or
# Adobe `.cube` text LUTs.
#
# Naming convention (same _PR / _F suffix as layouts):
#   FilterName_PR.png / .cube  →  isPremium = True
#   FilterName_F.png / .cube   →  isPremium = False
#   FilterName.png / .cube     →  isPremium = True (default, same as SVG layouts)
#
# Category **folder** trailing ``_PR`` / ``_F`` applies the same default to every LUT in that folder
# when the file stem has no ``_PR``/``_F``. Per-file suffixes override the folder default.
#
# Auto-download to editor toolbars (iOS store / quiet sync):
#   Add `_a` after optional `_PR` / `_F`, e.g. `Vintage_PR_a.cube`, `Vintage_a.png`.
#   The app downloads only manifest entries on sync unless the stem matches this rule (_a suffix);
#   `_a` assets are pulled in the background so they appear in filter pickers without tapping GET.
#   Omit `_a` for store-catalog-only items until the user downloads the pack.
#
# If both `FilterName.png` and `FilterName.cube` exist (same base after _PR/_F strip),
# one manifest entry is emitted and the `.cube` asset is preferred (`lutFileName` ends
# with `.cube`). The app downloads `Filters/{category}/{base}.cube` per FilterManifestLoader.
#
# The filter name is derived from the file stem with the suffix removed and
# underscores/hyphens replaced with spaces and title-cased.
#
# Built-in "Basic" iOS CIFilter category is always prepended (hardcoded).
# The manifest is rewritten whenever filters or their premium status change.
# Version is bumped (minor) on any change.
#
# Usage (standalone):
#   python generate_enhanced_manifest.py --generate-filter-manifest \
#       --filters-dir Filters \
#       --filter-output Filters/filter_manifest.json \
#       --base-url "https://raw.githubusercontent.com/OWNER/REPO/main"


# Built-in iOS CIFilters for the "Basic" category.
_BASIC_CI_FILTERS = [
    ("ci_vivid",    "Vivid",    "CIPhotoEffectVivid",    False),
    ("ci_noir",     "Noir",     "CIPhotoEffectNoir",     False),
    ("ci_chrome",   "Chrome",   "CIPhotoEffectChrome",   False),
    ("ci_fade",     "Fade",     "CIPhotoEffectFade",     False),
    ("ci_instant",  "Instant",  "CIPhotoEffectInstant",  False),
    ("ci_process",  "Process",  "CIPhotoEffectProcess",  False),
    ("ci_tonal",    "Tonal",    "CIPhotoEffectTonal",    False),
    ("ci_transfer", "Transfer", "CIPhotoEffectTransfer", False),
]


def _logical_lut_base(stem: str) -> str:
    """File stem with _PR/_F removed — used to pair `Name.png` + `Name.cube` in one filter."""
    s = stem
    for sfx in ("_PR", "_F"):
        if s.upper().endswith(sfx):
            return s[: -len(sfx)]
    return s


def _clean_stem_premium_suffix(stem: str) -> str:
    """Stem without trailing _PR / _F (for published lutFileName / URLs)."""
    clean = stem
    for sfx in ("_PR", "_F"):
        if clean.upper().endswith(sfx):
            clean = clean[: -len(sfx)]
            break
    return clean


def _stem_without_auto_toolbar_marker(stem: str) -> str:
    """
    Remove only the trailing `_a` / `_A` auto-toolbar marker (iOS convention). Caller then strips
    `_PR` / `_F` for premium flags and titles. Does not change published `fileName` stems.
    """
    s = stem
    if s.upper().endswith("_A"):
        s = s[:-2]
    return s


def _collect_lut_paths(cat_dir: Path) -> list[Path]:
    """All LUT PNG / Adobe cube files in a category folder (case-insensitive extension)."""
    out: list[Path] = []
    for ext in ("png", "PNG", "cube", "CUBE"):
        out.extend(cat_dir.glob(f"*.{ext}"))
    return sorted(out, key=lambda p: (p.stem.lower(), p.suffix.lower()))


def _choose_lut_path_per_logical_base(paths: list[Path]) -> list[Path]:
    """
    One path per logical base name: if both PNG and `.cube` exist for the same base
    (after _PR/_F strip), prefer `.cube`.
    """
    groups: dict[str, list[Path]] = {}
    for p in paths:
        base = _logical_lut_base(p.stem)
        groups.setdefault(base, []).append(p)

    chosen: list[Path] = []
    for base in sorted(groups.keys(), key=str.lower):
        g = groups[base]
        cubes = [p for p in g if p.suffix.lower() == ".cube"]
        pngs = [p for p in g if p.suffix.lower() == ".png"]
        if cubes:
            pick = sorted(cubes, key=lambda p: p.name.lower())[0]
            chosen.append(pick)
            for q in pngs:
                if q != pick:
                    print(
                        f"[filter-manifest] Note: using {pick.name} over {q.name} (same filter base “{base}”)",
                        file=sys.stderr,
                    )
        elif pngs:
            chosen.append(sorted(pngs, key=lambda p: p.name.lower())[0])
        else:
            chosen.extend(sorted(g, key=lambda p: p.name.lower()))
    return sorted(chosen, key=lambda p: (p.stem.lower(), p.suffix.lower()))


def _filter_stem_to_name_and_premium(
    stem: str, folder_premium_default: bool | None = None
) -> tuple[str, bool]:
    """
    Extract display name and isPremium from a LUT file stem.
    Strips optional trailing `_a` (auto-toolbar), then _PR / _F (case-insensitive), then title-cases.
    Default (no premium suffix) → folder_premium_default if set, else isPremium = True.
    """
    s = _stem_without_auto_toolbar_marker(stem)
    stem_up = s.upper()
    if stem_up.endswith("_PR"):
        clean = s[: -len("_PR")]
        is_premium = True
    elif stem_up.endswith("_F"):
        clean = s[: -len("_F")]
        is_premium = False
    else:
        clean = s
        if folder_premium_default is not None:
            is_premium = folder_premium_default
        else:
            is_premium = True  # default: premium (matches SVG layout behaviour)

    name = clean.replace("_", " ").replace("-", " ").strip().title()
    return name, is_premium


def _filter_id_from_category_and_stem(category_id: str, stem: str) -> str:
    """Build a stable, URL-safe filter id like 'film__amber_vibe'."""
    clean = stem.upper()
    for suffix in ("_PR", "_F"):
        if clean.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", stem).strip("_").lower()
    return f"{category_id}__{safe}"


def _bump_filter_version(v: str | None) -> str:
    """Bump minor version: 1.0 → 1.1, 1.9 → 2.0, etc."""
    try:
        if v and "." in v:
            major_s, minor_s = v.split(".", 1)
            major, minor = int(major_s), int(minor_s)
        elif v:
            major, minor = int(v), 0
        else:
            major, minor = 1, 0
    except Exception:
        major, minor = 1, 0
    minor += 1
    if minor > 9:
        major += 1
        minor = 0
    return f"{major}.{minor}"


def generate_filter_manifest(
    filters_dir: Path,
    output_path: Path,
    base_url: str,
) -> int:
    """
    Scan `filters_dir` for sub-folders (categories) and LUT assets (PNG and/or Adobe `.cube`).
    Generate / update `output_path` (filter_manifest.json).

    Returns 0 on success, 1 on error.
    """
    base_url = base_url.rstrip("/")

    if not filters_dir.is_dir():
        print(f"[filter-manifest] Filters dir not found: {filters_dir}", file=sys.stderr)
        # Create an empty-category manifest so the file always exists.
        filters_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Load existing manifest for version-bump comparison
    # ------------------------------------------------------------------
    old_manifest: dict | None = None
    old_version: str | None = None
    if output_path.exists():
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                old_manifest = json.load(f)
            old_version = str(old_manifest.get("version", "") or "")
        except Exception:
            old_manifest = None
            old_version = None

    # ------------------------------------------------------------------
    # 1) Built-in "Basic" category (iOS CIFilters, no LUT files needed)
    # ------------------------------------------------------------------
    basic_filters = [
        {
            "id": fid,
            "name": fname,
            "isPremium": is_pr,
            "source": "ciFilter",
            "ciFilterName": ci_name,
            "lutFileName": None,
        }
        for fid, fname, ci_name, is_pr in _BASIC_CI_FILTERS
    ]
    categories: list[dict] = [
        {"id": "basic", "name": "Basic", "filters": basic_filters}
    ]

    # ------------------------------------------------------------------
    # 2) Scan sub-folders (one per OTA category)
    # ------------------------------------------------------------------
    for cat_dir in sorted(filters_dir.iterdir()):
        if not cat_dir.is_dir():
            continue  # skip files at root level (e.g. filter_manifest.json itself)
        # Reserved folders under Filters/ that are not categories.
        if cat_dir.name in ("StockImage", "StorePreviews"):
            continue

        cat_id = re.sub(r"[^a-zA-Z0-9]+", "_", cat_dir.name).strip("_").lower()
        folder_base, folder_fd = _folder_display_base_and_premium_default(cat_dir.name)
        cat_name = _title_from_stem(folder_base)

        filters_in_cat: list[dict] = []
        lut_paths = _choose_lut_path_per_logical_base(_collect_lut_paths(cat_dir))
        for lut_path in lut_paths:
            stem = lut_path.stem
            display_name, is_premium = _filter_stem_to_name_and_premium(
                stem, folder_premium_default=folder_fd
            )
            filter_id = _filter_id_from_category_and_stem(cat_id, stem)
            clean_stem = _clean_stem_premium_suffix(stem)
            is_cube = lut_path.suffix.lower() == ".cube"
            # PNG: lutFileName without extension → app resolves Filters/{cat}/{name}.png
            # Cube: lutFileName must end with .cube → app resolves Filters/{cat}/{name}.cube
            lut_file_name = f"{clean_stem}.cube" if is_cube else clean_stem

            filters_in_cat.append({
                "id": filter_id,
                "name": display_name,
                "isPremium": is_premium,
                "source": "ota",
                "ciFilterName": None,
                "lutFileName": lut_file_name,
            })

        cat_block = {
            "id": cat_id,
            "name": cat_name,
            "remoteFolderName": cat_dir.name,
            "filters": filters_in_cat,
        }

        if filters_in_cat:
            categories.append(cat_block)

    # ------------------------------------------------------------------
    # 3) Version bump: only when categories / filters actually changed
    # ------------------------------------------------------------------
    new_categories_clean = _canonicalize_categories_payload(categories)

    old_categories = old_manifest.get("categories") if old_manifest else None
    if isinstance(old_categories, list):
        old_categories = _canonicalize_categories_payload(old_categories)
    changed = old_categories is None or old_categories != new_categories_clean

    if changed:
        new_version = _bump_filter_version(old_version)
    else:
        new_version = old_version or "1.1"

    manifest = {"version": new_version, "categories": new_categories_clean}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    total_filters = sum(len(c["filters"]) for c in new_categories_clean)
    print(
        f"[filter-manifest] v{new_version} — {len(new_categories_clean)} categories, "
        f"{total_filters} filters → {output_path}"
        + (" (bumped)" if changed else " (unchanged)")
    )
    return 0


# -----------------------------------------------------------------------------
# Filter store previews (offline LUT application; eye-blink images)
# -----------------------------------------------------------------------------

def _is_stock_image(path: Path) -> bool:
    return path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")


def _list_stock_images(stock_dir: Path) -> list[Path]:
    if not stock_dir.is_dir():
        raise FileNotFoundError(f"[filter-previews] Stock dir not found: {stock_dir}")
    imgs = [p for p in stock_dir.rglob("*") if p.is_file() and _is_stock_image(p)]
    if not imgs:
        raise RuntimeError(f"[filter-previews] No stock images found under: {stock_dir}")
    return sorted(imgs, key=lambda p: p.as_posix().lower())


def _cap_long_edge_pil(img, max_edge: int):
    w, h = img.size
    if max(w, h) <= max_edge:
        return img
    if w >= h:
        new_w = max_edge
        new_h = int(round(h * (max_edge / w)))
    else:
        new_h = max_edge
        new_w = int(round(w * (max_edge / h)))
    return img.resize((new_w, new_h), resample=Image.LANCZOS)


def _infer_lut_layout_rgb(lut_img) -> tuple[str, int, int]:
    """
    Returns (kind, n, grid):
      - kind='strip' : size (N*N, N)
      - kind='square_tiles' : size (G*N, G*N) where N=G*G
    """
    w, h = lut_img.size
    if h > 0 and w == h * h:
        return ("strip", h, h)
    if w == h:
        for grid in range(2, 65):
            if w % grid != 0:
                continue
            tile = w // grid
            n = grid * grid
            if tile == n:
                return ("square_tiles", n, grid)
    raise ValueError(f"Unsupported LUT layout: {w}x{h}px")


def _lut_from_png(path: Path):
    if Image is None or np is None:
        raise RuntimeError("[filter-previews] Missing dependencies: pillow and numpy are required.")
    lut_img = Image.open(path).convert("RGB")
    kind, n, grid = _infer_lut_layout_rgb(lut_img)
    arr = np.asarray(lut_img, dtype=np.float32) / 255.0
    lut = np.zeros((n, n, n, 3), dtype=np.float32)

    if kind == "strip":
        for b in range(n):
            x0 = b * n
            lut[:, :, b, :] = arr[:, x0 : x0 + n, :]
        lut = np.transpose(lut, (1, 0, 2, 3))
        return lut

    tile = n
    for b in range(n):
        tx = b % grid
        ty = b // grid
        x0 = tx * tile
        y0 = ty * tile
        tile_pixels = arr[y0 : y0 + tile, x0 : x0 + tile, :]
        lut[:, :, b, :] = tile_pixels
    lut = np.transpose(lut, (1, 0, 2, 3))
    return lut


def _apply_lut_trilinear(img, lut):
    if Image is None or np is None:
        raise RuntimeError("[filter-previews] Missing dependencies: pillow and numpy are required.")
    n = lut.shape[0]
    im = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0

    r = im[..., 0] * (n - 1)
    g = im[..., 1] * (n - 1)
    b = im[..., 2] * (n - 1)

    r0 = np.floor(r).astype(np.int32)
    g0 = np.floor(g).astype(np.int32)
    b0 = np.floor(b).astype(np.int32)
    r1 = np.clip(r0 + 1, 0, n - 1)
    g1 = np.clip(g0 + 1, 0, n - 1)
    b1 = np.clip(b0 + 1, 0, n - 1)

    dr = (r - r0)[..., None]
    dg = (g - g0)[..., None]
    db = (b - b0)[..., None]

    c000 = lut[r0, g0, b0]
    c100 = lut[r1, g0, b0]
    c010 = lut[r0, g1, b0]
    c110 = lut[r1, g1, b0]
    c001 = lut[r0, g0, b1]
    c101 = lut[r1, g0, b1]
    c011 = lut[r0, g1, b1]
    c111 = lut[r1, g1, b1]

    c00 = c000 * (1 - dr) + c100 * dr
    c10 = c010 * (1 - dr) + c110 * dr
    c01 = c001 * (1 - dr) + c101 * dr
    c11 = c011 * (1 - dr) + c111 * dr
    c0 = c00 * (1 - dg) + c10 * dg
    c1 = c01 * (1 - dg) + c11 * dg
    out = c0 * (1 - db) + c1 * db

    out8 = np.clip(out * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(out8, mode="RGB")


def _preview_urls(base_url: str, category_folder: str, filter_id: str) -> tuple[str, str]:
    base_url = base_url.rstrip("/")
    # Previews live inside each category folder for easy management.
    rel = f"Filters/{category_folder}/StorePreviews/{filter_id}"
    rel = rel.replace("\\", "/")
    return (
        f"{base_url}/{quote(rel, safe='/')}/original.jpg",
        f"{base_url}/{quote(rel, safe='/')}/filtered.jpg",
    )


def generate_filter_previews_and_attach_to_manifest(
    repo_root: Path,
    filter_manifest_path: Path,
    filters_dir: Path,
    stock_dir: Path,
    previews_root: Path,
    base_url: str,
    max_edge: int = 1080,
) -> int:
    """
    Generate one preview pair per OTA filter and inject preview URLs into filter_manifest.json.

    Output images:
      StorePreviews/Filters/<CategoryFolder>/<FilterId>/original.jpg
      StorePreviews/Filters/<CategoryFolder>/<FilterId>/filtered.jpg
    """
    if Image is None or np is None:
        print("[filter-previews] Missing pillow/numpy; install dependencies.", file=sys.stderr)
        return 1

    try:
        stock_images = _list_stock_images(stock_dir)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    if not filter_manifest_path.exists():
        print(f"[filter-previews] filter_manifest.json not found: {filter_manifest_path}", file=sys.stderr)
        return 1

    try:
        with open(filter_manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        print(f"[filter-previews] Failed to read manifest: {e}", file=sys.stderr)
        return 1

    categories = manifest.get("categories") or []
    wrote = 0
    updated = 0

    for cat in categories:
        cat_display = str(cat.get("name") or "").strip()
        cat_id = str(cat.get("id") or "").strip()
        if not cat_display or not cat_id:
            continue

        # On-disk / GitHub folder under Filters/ (see ``remoteFolderName`` in filter_manifest.json).
        category_folder = str(cat.get("remoteFolderName") or "").strip() or cat_display

        for item in (cat.get("filters") or []):
            if (item.get("source") or "") != "ota":
                continue
            filter_id = str(item.get("id") or "").strip()
            lut_file_name = str(item.get("lutFileName") or "").strip()
            if not filter_id or not lut_file_name:
                continue

            # Deterministic random stock selection per filter id.
            rng = random.Random(filter_id)
            stock_path = rng.choice(stock_images)

            # LUT PNG path in repo: Filters/<CategoryFolder>/<LUTStem>.png
            lut_stem = Path(lut_file_name).stem
            lut_png_path = filters_dir / category_folder / f"{lut_stem}.png"
            if not lut_png_path.exists():
                print(f"[filter-previews] LUT png missing: {lut_png_path}", file=sys.stderr)
                continue

            try:
                lut = _lut_from_png(lut_png_path)
                original = _cap_long_edge_pil(Image.open(stock_path).convert("RGB"), max_edge)
                filtered = _apply_lut_trilinear(original, lut)
            except Exception as e:
                print(f"[filter-previews] Failed for {category_folder}/{filter_id}: {e}", file=sys.stderr)
                continue

            # Store previews inside Filters/<CategoryFolder>/StorePreviews/<FilterId>/...
            out_dir = filters_dir / category_folder / "StorePreviews" / filter_id
            out_dir.mkdir(parents=True, exist_ok=True)
            original_path = out_dir / "original.jpg"
            filtered_path = out_dir / "filtered.jpg"
            original.save(original_path, format="JPEG", quality=88, optimize=True, progressive=True)
            filtered.save(filtered_path, format="JPEG", quality=88, optimize=True, progressive=True)
            wrote += 2

            o_url, f_url = _preview_urls(base_url, category_folder, filter_id)
            if item.get("previewOriginalUrl") != o_url or item.get("previewFilteredUrl") != f_url:
                item["previewOriginalUrl"] = o_url
                item["previewFilteredUrl"] = f_url
                updated += 1

    try:
        cats = manifest.get("categories")
        if isinstance(cats, list):
            manifest["categories"] = _canonicalize_categories_payload(cats)
        with open(filter_manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[filter-previews] Failed to write manifest: {e}", file=sys.stderr)
        return 1

    print(f"[filter-previews] Wrote {wrote} images; updated {updated} filter rows → {filter_manifest_path}")
    return 0


def _slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()


def _title_from_stem(stem: str) -> str:
    return stem.replace("_", " ").replace("-", " ").strip().title()


def _folder_display_base_and_premium_default(folder_name: str) -> tuple[str, bool | None]:
    """
    Trailing ``_PR`` / ``_F`` on a **category folder** name sets the default ``isPremium`` for every
    asset in that folder when the file stem omits its own ``_PR``/``_F``. Individual file suffixes
    always win. Returns ``(base_name_without_suffix, premium_default_or_None)``.
    """
    base = folder_name
    up = base.upper()
    if up.endswith("_PR"):
        return base[: -len("_PR")], True
    if up.endswith("_F"):
        return base[: -len("_F")], False
    return folder_name, None


def _store_stem_to_name_and_premium(
    stem: str,
    default_premium: bool = False,
    folder_premium_default: bool | None = None,
) -> tuple[str, bool]:
    s = _stem_without_auto_toolbar_marker(stem)
    stem_up = s.upper()
    if stem_up.endswith("_PR"):
        clean = s[: -len("_PR")]
        is_premium = True
    elif stem_up.endswith("_F"):
        clean = s[: -len("_F")]
        is_premium = False
    else:
        clean = s
        if folder_premium_default is not None:
            is_premium = folder_premium_default
        else:
            is_premium = default_premium
    return _title_from_stem(clean), is_premium


def _load_json_if_exists(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


_STORE_CATEGORY_ITEM_KEYS = (
    "stickers",
    "frames",
    "backgrounds",
    "filters",
    "shapes",
    "fonts",
)


def _canonicalize_categories_payload(categories: list) -> list:
    """Sort categories and nested store lists by ``id``.

    Manifests are always **rebuilt from the repo** (scan); they do not merge with previous JSON.
    Normalizing order makes deletes reliable in diffs and makes version bumps depend on content,
    not iteration order.
    """
    cats = copy.deepcopy(categories)
    for cat in cats:
        for key in _STORE_CATEGORY_ITEM_KEYS:
            arr = cat.get(key)
            if isinstance(arr, list):
                arr.sort(key=lambda x: str((x or {}).get("id") or ""))
    cats.sort(key=lambda c: str(c.get("id") or ""))
    return cats


def _canonicalize_layouts_list(layouts: list) -> list:
    """Sort layout dicts by ``id`` for **version-bump comparison only**.

    ``enhanced_manifest.json`` must keep the build order (classic JSON + SVG scan); do not assign
    this result to the written ``layouts`` array.
    """
    L = copy.deepcopy(layouts)
    L.sort(key=lambda x: str(x.get("id") or ""))
    return L


def _canonicalize_templates_index(templates: list) -> list:
    """Sort template index entries by ``id`` for stable diffs and version bumps."""
    T = copy.deepcopy(templates)
    T.sort(key=lambda x: str((x or {}).get("id") or ""))
    return T


def _write_versioned_manifest(output_path: Path, payload_key: str, payload_value) -> str:
    old_manifest = _load_json_if_exists(output_path)
    old_version = str(old_manifest.get("version", "") or "") if old_manifest else None

    if payload_key == "categories" and isinstance(payload_value, list):
        payload_value = _canonicalize_categories_payload(payload_value)

    old_payload = old_manifest.get(payload_key) if old_manifest else None
    if payload_key == "categories" and isinstance(old_payload, list):
        old_payload = _canonicalize_categories_payload(old_payload)
    elif payload_key == "templates" and isinstance(old_payload, list):
        old_payload = _canonicalize_templates_index(old_payload)

    if payload_key == "templates" and isinstance(payload_value, list):
        payload_value = _canonicalize_templates_index(payload_value)

    changed = old_payload is None or old_payload != payload_value
    new_version = _bump_filter_version(old_version) if changed else (old_version or "1.1")
    manifest = {"version": new_version, payload_key: payload_value}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return new_version


def _scan_category_pngs(
    root_dir: Path, *, exact_folder_name_for_category: bool = False
) -> list[tuple[str, str, list[Path], Path]]:
    """Scan one-level category folders containing PNGs.

    If ``exact_folder_name_for_category`` is True, ``name`` in the manifest is the
    on-disk folder name (same as filter manifest). The iOS app builds OTA URLs as
    ``.../Backgrounds/<category.name>/<file>.png``, so this must match GitHub case.
    Otherwise ``name`` is title-cased for display (frames/stickers).

    Returns ``(cat_id, cat_name, pngs, cat_dir)`` so sticker manifests can emit
    ``remoteFolderName`` / banner URLs from the real folder path.
    """
    categories: list[tuple[str, str, list[Path], Path]] = []
    if not root_dir.is_dir():
        root_dir.mkdir(parents=True, exist_ok=True)
        return categories
    for cat_dir in sorted(root_dir.iterdir()):
        if not cat_dir.is_dir():
            continue
        pngs = sorted(cat_dir.glob("*.png"), key=lambda p: p.name.lower())
        if not pngs:
            continue
        folder_base, _ = _folder_display_base_and_premium_default(cat_dir.name)
        cat_id = _slugify(cat_dir.name)
        cat_name = (
            cat_dir.name
            if exact_folder_name_for_category
            else _title_from_stem(folder_base)
        )
        categories.append((cat_id, cat_name, pngs, cat_dir))
    return categories


def generate_frame_store_manifest(
    frames_dir: Path,
    output_path: Path,
    base_url: str | None = None,
    *,
    generate_preview_webp: bool = True,
    preview_max_edge: int = 128,
    preview_workers: int = 0,
) -> int:
    """Emit ``frame_manifest.json`` with optional ``bannerImageUrl`` / ``promoHeaderUrl`` / ``remoteFolderName``
    (same reserved assets pattern as stickers: ``banner.png``, ``promo_header.png`` are not frame items).

    When ``generate_preview_webp`` is true, writes ``{clean_stem}_preview.webp`` beside each frame PNG
    under ``<frames_dir>/<CategoryFolder>/``. With ``--base-url``, every OTA row gets ``previewWebpUrl``
    pointing at that path on CDN (even if you skip local WebP generation).
    """
    preview_max_edge = max(32, int(preview_max_edge))
    frames_dir = frames_dir.resolve()
    scanned = _scan_category_pngs(frames_dir)
    frame_png_count = sum(
        len([p for p in pngs if not _is_reserved_sticker_pack_asset_png(p)])
        for _, _, pngs, _ in scanned
    )
    if frame_png_count == 0:
        print(
            f"[frame-manifest] WARNING: no frame PNGs under {frames_dir} "
            f"(expected e.g. {frames_dir}/MyPack/foo.png)",
            file=sys.stderr,
        )
    elif generate_preview_webp and Image is None:
        print(
            "[frame-manifest] WARNING: Pillow not installed — skipping WebP files "
            "(pip install -r scripts/requirements.txt). Manifest previewWebpUrl still emitted if --base-url set.",
            file=sys.stderr,
        )

    categories = []
    preview_jobs: list[tuple[str, str, int]] = []
    for cat_id, cat_name, pngs, cat_dir in scanned:
        _, folder_fd = _folder_display_base_and_premium_default(cat_dir.name)
        frames = []
        bu = base_url.rstrip("/") if base_url else None
        for p in pngs:
            if _is_reserved_sticker_pack_asset_png(p):
                continue
            display_name, is_premium = _store_stem_to_name_and_premium(
                p.stem, default_premium=False, folder_premium_default=folder_fd
            )
            clean_stem = _clean_stem_premium_suffix(p.stem)
            item_id = f"{cat_id}__{_slugify(clean_stem)}"
            preview_webp_name = _ota_preview_webp_basename(clean_stem)
            preview_webp_path = p.parent / preview_webp_name
            if generate_preview_webp and Image is not None:
                regen = (
                    not preview_webp_path.is_file()
                    or p.stat().st_mtime > preview_webp_path.stat().st_mtime
                )
                if regen:
                    preview_jobs.append((str(p.resolve()), str(preview_webp_path.resolve()), preview_max_edge))
            row: dict = {
                "id": item_id,
                "name": display_name,
                "isPremium": is_premium,
                "source": "ota",
                "fileName": clean_stem,
            }
            if bu:
                row["previewWebpUrl"] = _ota_asset_public_url(bu, "Frames", cat_dir.name, preview_webp_name)
            frames.append(row)
        cat_entry: dict = {
            "id": cat_id,
            "name": cat_name,
            "icon": "square.on.square",
            "remoteFolderName": cat_dir.name,
            "frames": frames,
            "bannerImageUrl": None,
            "promoHeaderUrl": None,
        }
        if base_url:
            bu = base_url.rstrip("/")
            for fname in ("banner.png", "banner.jpg", "Banner.png"):
                banner_path = cat_dir / fname
                if banner_path.is_file():
                    cat_entry["bannerImageUrl"] = _ota_asset_public_url(bu, "Frames", cat_dir.name, fname)
                    break
            for fname in ("promo_header.png", "promo_header.jpg", "promo.jpg"):
                promo_path = cat_dir / fname
                if promo_path.is_file():
                    cat_entry["promoHeaderUrl"] = _ota_asset_public_url(bu, "Frames", cat_dir.name, fname)
                    break
        categories.append(cat_entry)

    preview_written = _run_ota_preview_webp_jobs(
        preview_jobs,
        preview_workers,
        log_prefix="frame-manifest",
        assets_root=frames_dir,
    )

    version = _write_versioned_manifest(output_path, "categories", categories)
    extra = ""
    if generate_preview_webp:
        extra = f", {preview_written}/{len(preview_jobs)} preview WebPs written"
    elif base_url:
        extra = ", previewWebpUrl URLs only (use --skip-frame-preview-webp or CI to generate files)"
    print(f"[frame-manifest] v{version} — {len(categories)} categories, {frame_png_count} frames{extra} → {output_path}")
    return 0


def _is_reserved_sticker_pack_asset_png(path: Path) -> bool:
    """Category-folder PNGs used only for store UI (not sticker items)."""
    return path.name.lower() in {"banner.png", "promo_header.png", "promo.png"}


def _ota_preview_webp_basename(clean_stem: str) -> str:
    """Lightweight toolbar preview beside ``{clean_stem}.png`` in the same category folder."""
    return f"{clean_stem}_preview.webp"


def _sticker_preview_webp_basename(clean_stem: str) -> str:
    """Alias for stickers; same naming as frames."""
    return _ota_preview_webp_basename(clean_stem)


def _ota_asset_public_url(base_url: str, *path_parts: str) -> str:
    """Join CDN/GitHub base + path segments with every segment percent-encoded (spaces, ``@``, etc.)."""
    bu = base_url.rstrip("/")
    encoded = "/".join(quote(str(p).strip("/"), safe="") for p in path_parts if str(p).strip("/"))
    return f"{bu}/{encoded}"


def _generate_ota_preview_webp(png_path: Path, webp_out: Path, max_edge: int) -> bool:
    """Write a small RGBA WebP preview next to the source PNG. Returns True on success."""
    if Image is None:
        return False
    try:
        with Image.open(png_path) as src:
            img = src.convert("RGBA")
        img = _cap_long_edge_pil(img, max_edge)
        webp_out.parent.mkdir(parents=True, exist_ok=True)
        # method=4 is much faster than 6; fine for small toolbar thumbs.
        img.save(webp_out, format="WEBP", quality=80, method=4)
        return True
    except Exception as e:
        print(f"[ota-preview] WebP failed for {png_path}: {e}", file=sys.stderr)
        return False


def _ota_preview_webp_worker(job: tuple[str, str, int]) -> bool:
    """Process-pool worker: (png_path, webp_out, max_edge) as strings."""
    png_s, webp_s, max_edge = job
    return _generate_ota_preview_webp(Path(png_s), Path(webp_s), max_edge)


def _sticker_preview_webp_worker(job: tuple[str, str, int]) -> bool:
    return _ota_preview_webp_worker(job)


def _run_ota_preview_webp_jobs(
    preview_jobs: list[tuple[str, str, int]],
    preview_workers: int,
    *,
    log_prefix: str,
    assets_root: Path,
    worker_fn=None,
) -> int:
    """Generate preview WebPs in parallel. Returns count written."""
    if worker_fn is None:
        worker_fn = _ota_preview_webp_worker
    if not preview_jobs:
        return 0
    workers = preview_workers if preview_workers > 0 else min(8, max(1, (os.cpu_count() or 4)))
    print(
        f"[{log_prefix}] writing {len(preview_jobs)} preview WebP(s) under {assets_root} "
        f"({workers} workers)…",
        flush=True,
    )
    preview_written = 0
    if workers <= 1:
        for job in preview_jobs:
            if worker_fn(job):
                preview_written += 1
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker_fn, job) for job in preview_jobs]
            for fut in as_completed(futures):
                if fut.result():
                    preview_written += 1
    sample = Path(preview_jobs[0][1]) if preview_jobs else None
    if sample is not None:
        print(f"[{log_prefix}] example preview path: {sample}", flush=True)
    return preview_written


def generate_sticker_store_manifest(
    stickers_dir: Path,
    output_path: Path,
    base_url: str | None = None,
    *,
    generate_preview_webp: bool = True,
    preview_max_edge: int = 128,
    preview_workers: int = 0,
) -> int:
    """Emit sticker_store_manifest.json with optional ``bannerImageUrl`` / ``promoHeaderUrl`` when
    ``banner.png`` / ``promo_header.png`` (etc.) exist under each category folder.

    Sticker PNG stems use ``_PR`` / ``_F`` for premium/free (same as filters/frames). Optional ``_a``
    marks auto-toolbar inclusion on iOS.

    When ``generate_preview_webp`` is true, writes ``{clean_stem}_preview.webp`` beside each sticker PNG
    under ``<stickers_dir>/<CategoryFolder>/``. With ``--base-url``, every OTA row gets ``previewWebpUrl``
    pointing at that path on CDN (even if you skip local WebP generation).
    """
    preview_max_edge = max(32, int(preview_max_edge))
    stickers_dir = stickers_dir.resolve()
    scanned = _scan_category_pngs(stickers_dir)
    sticker_png_count = sum(
        len([p for p in pngs if not _is_reserved_sticker_pack_asset_png(p)])
        for _, _, pngs, _ in scanned
    )
    if sticker_png_count == 0:
        print(
            f"[sticker-manifest] WARNING: no sticker PNGs under {stickers_dir} "
            f"(expected e.g. {stickers_dir}/MyPack/foo.png)",
            file=sys.stderr,
        )
    elif generate_preview_webp and Image is None:
        print(
            "[sticker-manifest] WARNING: Pillow not installed — skipping WebP files "
            "(pip install -r scripts/requirements.txt). Manifest previewWebpUrl still emitted if --base-url set.",
            file=sys.stderr,
        )

    categories = []
    preview_jobs: list[tuple[str, str, int]] = []
    for cat_id, cat_name, pngs, cat_dir in scanned:
        _, folder_fd = _folder_display_base_and_premium_default(cat_dir.name)
        stickers = []
        bu = base_url.rstrip("/") if base_url else None
        for p in pngs:
            if _is_reserved_sticker_pack_asset_png(p):
                continue
            display_name, is_premium = _store_stem_to_name_and_premium(
                p.stem, default_premium=False, folder_premium_default=folder_fd
            )
            clean_stem = _clean_stem_premium_suffix(p.stem)
            item_id = f"{cat_id}__{_slugify(clean_stem)}"
            preview_webp_name = _sticker_preview_webp_basename(clean_stem)
            preview_webp_path = p.parent / preview_webp_name
            if generate_preview_webp and Image is not None:
                regen = (
                    not preview_webp_path.is_file()
                    or p.stat().st_mtime > preview_webp_path.stat().st_mtime
                )
                if regen:
                    preview_jobs.append((str(p.resolve()), str(preview_webp_path.resolve()), preview_max_edge))
            row: dict = {
                "id": item_id,
                "name": display_name,
                "isPremium": is_premium,
                "source": "ota",
                "fileName": clean_stem,
            }
            # Always publish the CDN path when base_url is set (WebPs may be deployed without git commit).
            if bu:
                row["previewWebpUrl"] = _ota_asset_public_url(bu, "Stickers", cat_dir.name, preview_webp_name)
            stickers.append(row)
        # Optional store art: explicit nulls so editors can paste GitHub raw URLs later without reshaping JSON.
        cat_entry: dict = {
            "id": cat_id,
            "name": cat_name,
            "count": len(stickers),
            "remoteFolderName": cat_dir.name,
            "stickers": stickers,
            "bannerImageUrl": None,
            "promoHeaderUrl": None,
        }
        if base_url:
            bu = base_url.rstrip("/")
            for fname in ("banner.png", "banner.jpg", "Banner.png"):
                banner_path = cat_dir / fname
                if banner_path.is_file():
                    cat_entry["bannerImageUrl"] = _ota_asset_public_url(bu, "Stickers", cat_dir.name, fname)
                    break
            for fname in ("promo_header.png", "promo_header.jpg", "promo.jpg"):
                promo_path = cat_dir / fname
                if promo_path.is_file():
                    cat_entry["promoHeaderUrl"] = _ota_asset_public_url(bu, "Stickers", cat_dir.name, fname)
                    break
        categories.append(cat_entry)

    preview_written = _run_ota_preview_webp_jobs(
        preview_jobs,
        preview_workers,
        log_prefix="sticker-manifest",
        assets_root=stickers_dir,
    )

    version = _write_versioned_manifest(output_path, "categories", categories)
    extra = ""
    if generate_preview_webp:
        extra = f", {preview_written}/{len(preview_jobs)} preview WebPs written"
    elif base_url:
        extra = ", previewWebpUrl URLs only (use --skip-sticker-preview-webp or CI to generate files)"
    print(f"[sticker-manifest] v{version} — {len(categories)} categories, {sticker_png_count} stickers{extra} → {output_path}")
    return 0


# Raster / common photo formats for Backgrounds store (iOS loads via UIImage).
_BACKGROUND_IMAGE_EXTS = frozenset({
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".heic",
    ".gif",
    ".tiff",
    ".tif",
    ".bmp",
    ".avif",
})


def _scan_category_background_images(root_dir: Path) -> list[tuple[str, str, str, bool | None, list[Path]]]:
    """Like ``_scan_category_pngs`` but includes multiple image types.

    Returns ``(cat_id, display_name, remote_folder_name, folder_premium_default, images)``.
    ``remote_folder_name`` is the real GitHub folder segment; ``display_name`` omits trailing ``_PR``/``_F``.

    ``fileName`` in the manifest is the full on-disk name (e.g. ``sunset.jpg``) so GitHub URLs and
    the app cache extension match. Item ids include the extension to avoid stem collisions
    (e.g. ``foo.png`` vs ``foo.jpg``).
    """
    categories: list[tuple[str, str, str, bool | None, list[Path]]] = []
    if not root_dir.is_dir():
        root_dir.mkdir(parents=True, exist_ok=True)
        return categories
    for cat_dir in sorted(root_dir.iterdir()):
        if not cat_dir.is_dir():
            continue
        images = sorted(
            [p for p in cat_dir.iterdir() if p.is_file() and p.suffix.lower() in _BACKGROUND_IMAGE_EXTS],
            key=lambda p: p.name.lower(),
        )
        if not images:
            continue
        cat_id = _slugify(cat_dir.name)
        folder_base, folder_fd = _folder_display_base_and_premium_default(cat_dir.name)
        cat_name = _title_from_stem(folder_base)
        categories.append((cat_id, cat_name, cat_dir.name, folder_fd, images))
    return categories


def generate_background_store_manifest(backgrounds_dir: Path, output_path: Path) -> int:
    """Writes ``background_store_manifest.json`` matching ``BackgroundStoreManifest`` / ``BackgroundStoreItem`` in the app."""
    categories = []
    for cat_id, cat_name, remote_folder, folder_fd, image_paths in _scan_category_background_images(
        backgrounds_dir
    ):
        backgrounds = []
        for p in image_paths:
            display_name, is_premium = _store_stem_to_name_and_premium(
                p.stem, default_premium=False, folder_premium_default=folder_fd
            )
            clean_stem = _clean_stem_premium_suffix(p.stem)
            ext = p.suffix.lower().lstrip(".") or "png"
            item_id = f"{cat_id}__{_slugify(clean_stem)}_{ext}"
            # fileName = full remote filename (with extension); iOS builds URL .../<category>/<fileName>
            backgrounds.append({
                "id": item_id,
                "name": display_name,
                "isPremium": is_premium,
                "kind": "image",
                "source": "ota",
                "colorHex": None,
                "fileName": p.name,
            })
        categories.append(
            {
                "id": cat_id,
                "name": cat_name,
                "remoteFolderName": remote_folder,
                "backgrounds": backgrounds,
            }
        )
    version = _write_versioned_manifest(output_path, "categories", categories)
    print(f"[background-manifest] v{version} — {len(categories)} categories → {output_path}")
    return 0


def _extract_postscript_name(font_path: Path) -> str:
    stem = font_path.stem.replace(" ", "")
    if TTFont is None:
        return stem
    try:
        font = TTFont(str(font_path))
        names = font["name"].names
        for rec in names:
            if rec.nameID != 6:
                continue
            text = rec.toUnicode().strip()
            if text:
                return text
    except Exception:
        pass
    return stem


# --- Font EULA / license documents (.txt, .pdf, … under Fonts/, often beside .ttf/.otf) -----

_LICENSE_TOKEN_SKIP: frozenset[str] = frozenset(
    {
        "",
        "1001fonts",
        "dafont",
        "fontspace",
        "fontsquirrel",
        "font",
        "fonts",
        "eula",
        "end",
        "user",
        "license",
        "licence",
        "licensing",
        "agreement",
        "ofl",
        "sil",
        "open",
        "readme",
        "txt",
        "pdf",
        "the",
        "a",
        "an",
        "for",
        "and",
        "or",
        "free",
        "personal",
        "commercial",
        "use",
        "public",
        "domain",
        "v1",
        "v2",
        "v3",
        "ver",
        "version",
        "regular",
        "bold",
        "italic",
        "light",
        "medium",
        "black",
        "thin",
    }
)


def _license_compact_signature(text: str) -> str:
    """
    Normalize font or license filenames / display names for fuzzy matching:
    lowercase, split on non-alphanumeric, drop vendor/noise tokens and digits-only
    pieces, concatenate remainder (e.g. "1001fonts-brock-script-eula" → "brockscript").
    """
    tokens = re.split(r"[^a-zA-Z0-9]+", text.lower())
    parts: list[str] = []
    for t in tokens:
        if not t or t in _LICENSE_TOKEN_SKIP or t.isdigit():
            continue
        parts.append(t)
    return "".join(parts)


def _license_file_parent_rel_posix(rel_posix: str) -> str:
    """Directory of a license file relative to ``Fonts/`` (``\"\"`` if file is directly under Fonts/)."""
    p = Path(rel_posix).parent
    if p == Path(".") or str(p) in (".", ""):
        return ""
    return p.as_posix().replace("\\", "/")


# Extensions for license / EULA assets co-located with store fonts (case-insensitive).
_LICENSE_FILE_SUFFIXES: frozenset[str] = frozenset({".txt", ".pdf"})


def _collect_license_document_files_under_fonts(fonts_dir: Path) -> list[Path]:
    """Every ``.txt`` / ``.pdf`` (any case) under ``fonts_dir``, skipping hidden path segments."""
    if not fonts_dir.is_dir():
        return []
    out: list[Path] = []
    for p in fonts_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _LICENSE_FILE_SUFFIXES:
            continue
        try:
            rel = p.relative_to(fonts_dir)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        out.append(p)
    return sorted(set(out), key=lambda x: str(x).lower())


def _font_match_compacts_for_entry(
    font_path_stem: str, display_name: str, post_script: str, entry_id: str
) -> list[str]:
    """Several normalized signatures for one catalog font (longest-first for scoring)."""
    id_tail = entry_id.split("__", 1)[-1] if "__" in entry_id else entry_id
    raw_sources = [
        _clean_stem_premium_suffix(font_path_stem),
        display_name,
        post_script,
        id_tail.replace("_", " "),
        id_tail.replace("_", "-"),
    ]
    out: set[str] = set()
    for s in raw_sources:
        c = _license_compact_signature(s)
        if len(c) >= 3:
            out.add(c)
    return sorted(out, key=len, reverse=True)


def _license_match_score(font_compact: str, license_compact: str) -> float:
    if not font_compact or not license_compact:
        return 0.0
    if font_compact == license_compact:
        return 1.0
    shorter, longer = (
        (font_compact, license_compact)
        if len(font_compact) <= len(license_compact)
        else (license_compact, font_compact)
    )
    if shorter in longer:
        return len(shorter) / max(len(longer), 1)
    # Longest common substring (simple O(n*m) for short strings)
    best = 0
    fc, lc = font_compact, license_compact
    for i in range(len(fc)):
        for j in range(len(lc)):
            k = 0
            while i + k < len(fc) and j + k < len(lc) and fc[i + k] == lc[j + k]:
                k += 1
            if k > best:
                best = k
    if best == 0:
        return 0.0
    return best / max(len(fc), len(lc))


def _best_license_for_font(
    font_compacts: list[str],
    license_rows: list[tuple[str, str]],
    *,
    min_score: float = 0.45,
    same_folder_as: str | None = None,
) -> str | None:
    """
    :param font_compacts: normalized font signatures (longer first)
    :param license_rows: list of (relative_posix_path_from_fonts_dir, license_compact_signature)
    :param same_folder_as: font's ``remoteDirectory`` (path under ``Fonts/``); boosts a co-located file
    :return: relative path to chosen license file or None
    """
    folder_norm = (same_folder_as or "").replace("\\", "/").strip("/")
    best_rel: str | None = None
    best_score = -1.0
    for rel_posix, lic_c in license_rows:
        if not lic_c:
            continue
        s = max(_license_match_score(fc, lic_c) for fc in font_compacts) if font_compacts else 0.0
        lic_parent = _license_file_parent_rel_posix(rel_posix).replace("\\", "/").strip("/")
        if folder_norm == lic_parent:
            s = min(1.0, s + 0.22)
        if s < min_score:
            continue
        if s > best_score + 1e-9 or (
            abs(s - best_score) < 1e-9 and (best_rel is None or rel_posix < best_rel)
        ):
            best_score = s
            best_rel = rel_posix
    return best_rel


def _raw_github_fonts_file_url(manifest_base_url: str, *path_under_fonts: str) -> str:
    """e.g. base .../main + English/Foo/eula.pdf → .../main/Fonts/English/Foo/eula.pdf"""
    base = manifest_base_url.rstrip("/")
    encoded = "/".join(quote(seg, safe="") for seg in path_under_fonts)
    return f"{base}/Fonts/{encoded}"


def _attach_font_license_urls(
    categories: list[dict],
    fonts_dir: Path,
    manifest_base_url: str | None,
) -> None:
    """Match license docs (``.txt``, ``.pdf``) under ``Fonts/`` to catalog fonts and set ``licenseUrl``."""
    lic_paths = _collect_license_document_files_under_fonts(fonts_dir)
    if not lic_paths:
        return

    if not (manifest_base_url and str(manifest_base_url).strip()):
        print(
            "[font-catalog] license file(s) (.txt/.pdf) found under "
            f"{fonts_dir.as_posix()}/ but no --base-url; "
            "skipping licenseUrl (use --generate-store-manifests with --base-url).",
            file=sys.stderr,
        )
        return

    base = str(manifest_base_url).strip().rstrip("/")
    try:
        rel_and_sig: list[tuple[str, str]] = []
        for p in lic_paths:
            rel = p.relative_to(fonts_dir).as_posix()
            stem_sig = _license_compact_signature(p.stem)
            rel_and_sig.append((rel, stem_sig))
    except ValueError:
        return

    matched = 0
    for cat in categories:
        for entry in cat.get("fonts") or []:
            if not isinstance(entry, dict):
                continue
            file_name = str(entry.get("fileName") or "")
            font_stem = Path(file_name).stem if file_name else ""
            display = str(entry.get("displayName") or "")
            ps = str(entry.get("postScriptName") or "")
            eid = str(entry.get("id") or "")
            remote_dir = entry.get("remoteDirectory")
            remote_dir_s = str(remote_dir) if remote_dir is not None else ""
            compacts = _font_match_compacts_for_entry(font_stem, display, ps, eid)
            rel_txt = _best_license_for_font(
                compacts, rel_and_sig, same_folder_as=remote_dir_s or None
            )
            if not rel_txt:
                continue
            segs = rel_txt.split("/")
            entry["licenseUrl"] = _raw_github_fonts_file_url(base, *segs)
            matched += 1

    if matched:
        print(
            f"[font-catalog] attached licenseUrl to {matched} font(s) "
            f"({len(lic_paths)} license file(s) under {fonts_dir.as_posix()}/)",
            file=sys.stderr,
        )


def _font_entry_dict(
    cat_id: str,
    font_path: Path,
    *,
    remote_directory: str | None = None,
    folder_premium_default: bool | None = None,
) -> dict:
    display_name, is_premium = _store_stem_to_name_and_premium(
        font_path.stem, default_premium=False, folder_premium_default=folder_premium_default
    )
    clean_stem = _clean_stem_premium_suffix(font_path.stem)
    # Use the on-disk basename so CDN URLs match published files (e.g. Agamtoh_PR.ttf).
    file_name = font_path.name
    entry_id = f"{cat_id}__{_slugify(clean_stem)}"
    d: dict = {
        "id": entry_id,
        "displayName": display_name,
        "postScriptName": _extract_postscript_name(font_path),
        "isPremium": is_premium,
        "source": "ota",
        "fileName": file_name,
    }
    if remote_directory is not None:
        d["remoteDirectory"] = remote_directory.replace("\\", "/")
    return d


def _fonts_collect_under(root: Path) -> list[Path]:
    out: list[Path] = []
    for ext in ("*.ttf", "*.otf", "*.TTF", "*.OTF"):
        out.extend(root.rglob(ext))
    return sorted(set(out), key=lambda p: str(p).lower())


def _fonts_loose_direct(fonts_dir: Path) -> list[Path]:
    loose: list[Path] = []
    for ext in ("*.ttf", "*.otf", "*.TTF", "*.OTF"):
        loose.extend(fonts_dir.glob(ext))
    return sorted(set(loose), key=lambda p: p.name.lower())


# Top-level folder names (case-insensitive) → (stable id, App Store tab title).
_LANGUAGE_FOLDER_ALIASES: dict[str, tuple[str, str]] = {
    "english": ("en", "English"),
    "en": ("en", "English"),
    "default": ("en", "English"),
    "font library": ("en", "English"),
    "spanish": ("es", "Spanish"),
    "es": ("es", "Spanish"),
    "french": ("fr", "French"),
    "fr": ("fr", "French"),
    "german": ("de", "German"),
    "de": ("de", "German"),
    "italian": ("it", "Italian"),
    "it": ("it", "Italian"),
    "portuguese": ("pt", "Portuguese"),
    "pt": ("pt", "Portuguese"),
    "russian": ("ru", "Russian"),
    "ru": ("ru", "Russian"),
    "japanese": ("ja", "Japanese"),
    "ja": ("ja", "Japanese"),
    "korean": ("ko", "Korean"),
    "ko": ("ko", "Korean"),
    "chinese": ("zh", "Chinese"),
    "zh": ("zh", "Chinese"),
    "traditional chinese": ("zh-hant", "Traditional Chinese"),
    "simplified chinese": ("zh-hans", "Simplified Chinese"),
    "hindi": ("hi", "Hindi"),
    "hi": ("hi", "Hindi"),
    "bengali": ("bn", "Bengali"),
    "bangla": ("bn", "Bengali"),
    "arabic": ("ar", "Arabic"),
    "ar": ("ar", "Arabic"),
    "turkish": ("tr", "Turkish"),
    "tr": ("tr", "Turkish"),
    "vietnamese": ("vi", "Vietnamese"),
    "vi": ("vi", "Vietnamese"),
    "thai": ("th", "Thai"),
    "th": ("th", "Thai"),
    "greek": ("el", "Greek"),
    "el": ("el", "Greek"),
    "hebrew": ("he", "Hebrew"),
    "he": ("he", "Hebrew"),
}


def _language_category_for_folder(folder_name: str) -> tuple[str, str]:
    base, _pm = _folder_display_base_and_premium_default(folder_name)
    key = base.strip().lower()
    if key in _LANGUAGE_FOLDER_ALIASES:
        return _LANGUAGE_FOLDER_ALIASES[key]
    slug = _slugify(base)
    return slug, _title_from_stem(base)


def generate_font_catalog_manifest(
    fonts_dir: Path,
    output_path: Path,
    *,
    manifest_base_url: str | None = None,
    licenses_subdir: str = "licenses",
) -> int:
    """
    Emit `font_catalog.json` with **language** categories (not arbitrary style packs):

    - Each immediate subfolder of `Fonts/` names a language (`English`, `ja`, `Japanese`, …).
      All `.ttf`/`.otf` under that tree are listed; each entry includes `remoteDirectory`
      = posix path from `Fonts/` to the file's parent (supports nested folders on GitHub).
    - Fonts placed **loose** in `Fonts/*.ttf` go into **English** (`en`) with `remoteDirectory` \"\"
      (flat `Fonts/{fileName}` URLs).
    - Optional ``**/*.{txt,pdf}`` EULA files **anywhere under** ``Fonts/`` (often beside each ``.ttf``/``.otf``):
      matched to fonts by normalized name; co-located licenses (same folder as the font) get a score boost.
      Emits absolute ``licenseUrl`` when ``manifest_base_url`` is set (GitHub raw root).

    - ``licenses_subdir``: only the **name of a top-level folder under Fonts/** that must **not** become a
      language tab (default ``licenses``), e.g. when you keep a central ``Fonts/licenses/`` tree
      without font files in that folder.

    Extend `_LANGUAGE_FOLDER_ALIASES` when adding a new language folder name.
    """
    if not fonts_dir.is_dir():
        fonts_dir.mkdir(parents=True, exist_ok=True)

    buckets: dict[str, dict] = {}

    def ensure_bucket(cat_id: str, display: str) -> dict:
        if cat_id not in buckets:
            buckets[cat_id] = {"id": cat_id, "name": display, "remoteFolder": None, "fonts": []}
        return buckets[cat_id]

    lic_root_low = licenses_subdir.strip("/").lower()
    for lang_dir in sorted([p for p in fonts_dir.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
        if lang_dir.name.startswith("."):
            continue
        if lang_dir.name.lower() == lic_root_low:
            # e.g. Fonts/licenses/ holds EULA docs only, not a UI language tab.
            continue
        cat_id, cat_display = _language_category_for_folder(lang_dir.name)
        _, folder_fd = _folder_display_base_and_premium_default(lang_dir.name)
        bucket = ensure_bucket(cat_id, cat_display)
        for font_path in _fonts_collect_under(lang_dir):
            rel_parent = font_path.parent.relative_to(fonts_dir)
            remote_dir = str(rel_parent).replace("\\", "/")
            entry = _font_entry_dict(
                cat_id, font_path, remote_directory=remote_dir, folder_premium_default=folder_fd
            )
            bucket["fonts"].append(entry)

    for font_path in _fonts_loose_direct(fonts_dir):
        bucket = ensure_bucket("en", "English")
        entry = _font_entry_dict("en", font_path, remote_directory="")
        bucket["fonts"].append(entry)

    def sort_key(cid: str) -> tuple:
        name = buckets[cid]["name"]
        return (0, "") if cid == "en" else (1, name.lower())

    ordered = sorted(buckets.keys(), key=sort_key)
    categories = [buckets[c] for c in ordered if buckets[c]["fonts"]]

    _attach_font_license_urls(categories, fonts_dir, manifest_base_url)

    version = _write_versioned_manifest(output_path, "categories", categories)
    nfonts = sum(len(c.get("fonts") or []) for c in categories)
    print(f"[font-catalog] v{version} — {len(categories)} language categories, {nfonts} fonts → {output_path}")
    return 0


_SHAPE_VECTOR_EXTS = frozenset({".svg", ".SVG"})
# EPS is not parsed on iOS; convert offline to SVG before adding to the repo.
_SHAPE_SKIP_EXTS = frozenset({".eps", ".EPS", ".ai", ".AI"})


def _is_reserved_shape_pack_asset(path: Path) -> bool:
    return path.name.lower() in {"banner.png", "promo_header.png", "promo.png"}


def _is_shape_preview_webp_asset(path: Path) -> bool:
    """``{stem}_preview.webp`` beside the SVG — not a catalog row."""
    return path.suffix.lower() == ".webp" and path.stem.endswith("_preview")


def _recolor_rgba_silhouette(img, rgb: tuple[int, int, int]):
    """Single fill color with preserved alpha (matches Swift ``.fill(Color.appTint)`` on path ``d``)."""
    img = img.convert("RGBA")
    r, g, b = rgb
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            _, _, _, a = px[x, y]
            if a:
                px[x, y] = (r, g, b, a)
    return img


def _shape_preview_webp_needs_recolor(webp_path: Path, rgb: tuple[int, int, int], tolerance: int = 48) -> bool:
    """True when an existing WebP still uses source SVG colors (e.g. black) instead of toolbar tint."""
    if Image is None or not webp_path.is_file():
        return True
    try:
        with Image.open(webp_path) as src:
            im = src.convert("RGBA")
        tr, tg, tb = rgb
        for pr, pg, pb, a in im.getdata():
            if a < 16:
                continue
            if abs(pr - tr) + abs(pg - tg) + abs(pb - tb) > tolerance:
                return True
        return False
    except Exception:
        return True


def _render_shape_svg_preview_webp(svg_path: Path, webp_out: Path, max_edge: int) -> bool:
    """Rasterize one SVG to a small RGBA WebP for store / toolbar (requires cairosvg + Pillow)."""
    if cairosvg is None or Image is None:
        return False
    try:
        png_bytes = cairosvg.svg2png(url=str(svg_path.resolve()))
        with Image.open(BytesIO(png_bytes)) as src:
            img = _recolor_rgba_silhouette(src, SHAPE_TOOLBAR_PREVIEW_RGB)
        img = _cap_long_edge_pil(img, max_edge)
        w, h = img.size
        side = max(w, h, 1)
        if w != h:
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(img, ((side - w) // 2, (side - h) // 2))
            img = canvas
        webp_out.parent.mkdir(parents=True, exist_ok=True)
        img.save(webp_out, format="WEBP", quality=80, method=4)
        return True
    except Exception as e:
        print(f"[shape-manifest] SVG preview WebP failed for {svg_path}: {e}", file=sys.stderr)
        return False


def _shape_preview_webp_worker(job: tuple[str, str, int]) -> bool:
    svg_s, webp_s, max_edge = job
    return _render_shape_svg_preview_webp(Path(svg_s), Path(webp_s), max_edge)


def generate_shape_store_manifest(
    shapes_dir: Path,
    output_path: Path,
    base_url: str | None = None,
    *,
    generate_preview_webp: bool = True,
    preview_max_edge: int = 128,
    preview_workers: int = 0,
) -> int:
    """Emit ``shape_store_manifest.json`` for SVG shape packs (same layout as stickers: category folders).

    Each category folder contains ``.svg`` files (single ``<path>`` or ``<polygon>`` recommended).
    ``banner.png`` / ``promo_header.png`` are store-only (omitted from ``shapes``). ``.eps`` files are skipped
    with a log line — convert to SVG for GitHub + the app.

    When ``generate_preview_webp`` is true, writes ``{clean_stem}_preview.webp`` beside each SVG (cairosvg).
    With ``--base-url``, every OTA row gets ``previewWebpUrl`` for lightweight CDN thumbnails.
    """
    preview_max_edge = max(32, int(preview_max_edge))
    shapes_dir = shapes_dir.resolve()
    categories = []
    preview_jobs: list[tuple[str, str, int]] = []
    shape_item_count = 0
    if not shapes_dir.is_dir():
        shapes_dir.mkdir(parents=True, exist_ok=True)
    elif generate_preview_webp and (cairosvg is None or Image is None):
        missing = []
        if Image is None:
            missing.append("Pillow (pip install Pillow)")
        if cairosvg is None:
            missing.append(
                "cairosvg (pip install cairosvg plus system libs: libcairo2-dev libpango1.0-dev on Linux)"
            )
        print(
            f"[shape-manifest] WARNING: {'; '.join(missing)} — skipping WebP previews. "
            "previewWebpUrl still emitted if --base-url set.",
            file=sys.stderr,
        )
    for cat_dir in sorted(shapes_dir.iterdir()):
        if not cat_dir.is_dir():
            continue
        files = sorted(
            [p for p in cat_dir.iterdir() if p.is_file() and not p.name.startswith(".")],
            key=lambda p: p.name.lower(),
        )
        vector_files = [
            p for p in files
            if p.suffix in _SHAPE_VECTOR_EXTS and not _is_shape_preview_webp_asset(p)
        ]
        for p in files:
            if p.suffix in _SHAPE_SKIP_EXTS:
                print(f"[shape-manifest] skip (convert to SVG): {p}")
        if not vector_files:
            continue
        cat_id = _slugify(cat_dir.name)
        folder_base, folder_fd = _folder_display_base_and_premium_default(cat_dir.name)
        cat_name = _title_from_stem(folder_base)
        shapes = []
        bu = base_url.rstrip("/") if base_url else None
        for p in vector_files:
            if _is_reserved_shape_pack_asset(p):
                continue
            display_name, is_premium = _store_stem_to_name_and_premium(
                p.stem, default_premium=False, folder_premium_default=folder_fd
            )
            clean_stem = _clean_stem_premium_suffix(p.stem)
            item_id = f"{cat_id}__{_slugify(clean_stem)}"
            preview_webp_name = _ota_preview_webp_basename(clean_stem)
            preview_webp_path = p.parent / preview_webp_name
            if generate_preview_webp and cairosvg is not None and Image is not None:
                regen = (
                    not preview_webp_path.is_file()
                    or p.stat().st_mtime > preview_webp_path.stat().st_mtime
                    or _shape_preview_webp_needs_recolor(preview_webp_path, SHAPE_TOOLBAR_PREVIEW_RGB)
                )
                if regen:
                    preview_jobs.append((str(p.resolve()), str(preview_webp_path.resolve()), preview_max_edge))
            row: dict = {
                "id": item_id,
                "name": display_name,
                "isPremium": is_premium,
                "source": "ota",
                "kindId": _slugify(clean_stem),
                "fileName": p.name,
            }
            if bu:
                row["previewWebpUrl"] = _ota_asset_public_url(bu, "Shapes", cat_dir.name, preview_webp_name)
            shapes.append(row)
            shape_item_count += 1
        if not shapes:
            continue
        cat_entry: dict = {
            "id": cat_id,
            "name": cat_name,
            "remoteFolderName": cat_dir.name,
            "shapes": shapes,
            "bannerImageUrl": None,
            "promoHeaderUrl": None,
        }
        if base_url:
            bu = base_url.rstrip("/")
            for fname in ("banner.png", "banner.jpg", "Banner.png"):
                banner_path = cat_dir / fname
                if banner_path.is_file():
                    cat_entry["bannerImageUrl"] = _ota_asset_public_url(bu, "Shapes", cat_dir.name, fname)
                    break
            for fname in ("promo_header.png", "promo_header.jpg", "promo.jpg"):
                promo_path = cat_dir / fname
                if promo_path.is_file():
                    cat_entry["promoHeaderUrl"] = _ota_asset_public_url(bu, "Shapes", cat_dir.name, fname)
                    break
        categories.append(cat_entry)

    preview_written = _run_ota_preview_webp_jobs(
        preview_jobs,
        preview_workers,
        log_prefix="shape-manifest",
        assets_root=shapes_dir,
        worker_fn=_shape_preview_webp_worker,
    )

    version = _write_versioned_manifest(output_path, "categories", categories)
    extra = ""
    if generate_preview_webp:
        extra = f", {preview_written}/{len(preview_jobs)} preview WebPs written"
    elif base_url:
        extra = ", previewWebpUrl URLs only (use --skip-shape-preview-webp or CI to generate files)"
    print(f"[shape-manifest] v{version} — {len(categories)} categories, {shape_item_count} shapes{extra} → {output_path}")
    return 0


_PNG_TEMPLATE_IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def _load_png_template_sidecar(image_path: Path) -> dict:
    """Optional ``<stem>.cues.json`` next to the template image for color-cue detection."""
    sidecar = image_path.with_name(f"{image_path.stem}.cues.json")
    if not sidecar.is_file():
        return {}
    try:
        with open(sidecar, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def generate_png_template_manifest(
    templates_dir: Path,
    output_path: Path,
) -> int:
    """Emit ``png_template_manifest.json`` from ``PNGTemplates/<Category>/*.{png,jpg,jpeg}``.

    Rules:
    - PNG and JPEG templates are included (JPG defaults to color-cue detection in the app).
    - Folder name under ``PNGTemplates/`` is preserved as ``remoteFolderName`` (GitHub URL segment).
    - Optional sidecar ``<stem>.cues.json``: ``detectionMode``, ``slotCues`` (hex/shape/tolerance).
    - Item ``holeCount`` is informational only (runtime ``SlotRegionAnalyzer`` is authoritative).
    - Version is bumped only when categories/items actually change.
    """
    categories: list[dict] = []
    if not templates_dir.is_dir():
        templates_dir.mkdir(parents=True, exist_ok=True)

    for cat_dir in sorted([p for p in templates_dir.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
        images = sorted(
            [
                p for p in cat_dir.iterdir()
                if p.is_file() and p.suffix.lower() in _PNG_TEMPLATE_IMAGE_EXTS
            ],
            key=lambda p: p.name.lower(),
        )
        if not images:
            continue

        cat_id = _slugify(cat_dir.name)
        folder_base, folder_fd = _folder_display_base_and_premium_default(cat_dir.name)
        cat_name = _title_from_stem(folder_base)
        items: list[dict] = []
        for p in images:
            stem = p.stem
            display_name, is_premium = _store_stem_to_name_and_premium(
                stem, default_premium=False, folder_premium_default=folder_fd
            )
            clean_stem = _clean_stem_premium_suffix(stem)
            item_id = f"{cat_id}__{_slugify(clean_stem)}"
            ext = p.suffix.lower().lstrip(".")
            file_ext = "jpg" if ext == "jpeg" else ext
            sidecar = _load_png_template_sidecar(p)
            entry: dict = {
                "id": item_id,
                "name": display_name,
                "fileName": clean_stem,
                "isPremium": is_premium,
                "holeCount": 0,
                "fileExtension": file_ext,
            }
            detection_mode = sidecar.get("detectionMode") or sidecar.get("detection_mode")
            if isinstance(detection_mode, str) and detection_mode.strip():
                entry["detectionMode"] = detection_mode.strip()
            slot_cues = sidecar.get("slotCues") or sidecar.get("slot_cues")
            if isinstance(slot_cues, list) and slot_cues:
                entry["slotCues"] = slot_cues
            grey_range = sidecar.get("greyRange") or sidecar.get("grey_range")
            if isinstance(grey_range, dict) and grey_range:
                entry["greyRange"] = grey_range
            items.append(entry)

        if not items:
            continue

        categories.append({
            "id": cat_id,
            "name": cat_name,
            "icon": "square.grid.2x2",
            "remoteFolderName": cat_dir.name,
            "items": items,
        })

    version = _write_versioned_manifest(output_path, "categories", categories)
    total_items = sum(len(c.get("items") or []) for c in categories)
    print(f"[png-template-manifest] v{version} — {len(categories)} categories, {total_items} templates → {output_path}")
    return 0


# Legacy flat layout under ``Templates/Recipes`` and ``Templates/Previews`` (still supported in the app;
# this generator only emits the **category-folder** layout: ``Templates/<CategoryFolder>/*.json`` + preview.)
_JSON_TEMPLATE_INDEX_SKIP_DIRS = frozenset({"Recipes", "Previews"})
_JSON_TEMPLATE_PREVIEW_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _find_template_preview_in_category(cat_dir: Path, stem: str) -> Path | None:
    """Return the preview image for ``stem`` in ``cat_dir`` (case-insensitive stem + extension).

    GitHub Actions runs on Linux where ``FathersDay 1.3.JPG`` does not match a literal ``.jpg`` path.
    """
    stem_lower = stem.lower()
    allowed_exts = {ext.lower() for ext in _JSON_TEMPLATE_PREVIEW_EXTS}
    matches: list[Path] = []
    for p in cat_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in allowed_exts:
            continue
        if p.stem.lower() != stem_lower:
            continue
        matches.append(p)
    if not matches:
        return None

    def _preview_rank(path: Path) -> int:
        try:
            return _JSON_TEMPLATE_PREVIEW_EXTS.index(path.suffix.lower())
        except ValueError:
            return len(_JSON_TEMPLATE_PREVIEW_EXTS)

    return min(matches, key=_preview_rank)


def generate_json_template_index(templates_dir: Path, output_path: Path) -> int:
    """Emit ``Templates/templates_index.json`` for iOS ``TemplateStoreCatalog`` / ``TemplateManifestLoader``.

    Scans one level of subfolders under ``templates_dir`` (e.g. repo ``Templates/``). Each subfolder is a
    **category folder** (``category_folder`` in JSON, same as PNG/Frames/Stickers store scans).

    Per folder, every ``*.json`` except ``templates_index.json`` is a recipe. The preview is the first
    existing file with the same stem: ``.png`` → ``.jpg`` → ``.jpeg`` → ``.webp``.

    Skips ``Recipes`` and ``Previews`` (old split layout). Premium follows the same ``_PR`` / ``_F`` stem
    rules as other store manifests.
    """
    entries: list[dict] = []
    if not templates_dir.is_dir():
        templates_dir.mkdir(parents=True, exist_ok=True)

    for cat_dir in sorted([p for p in templates_dir.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
        if cat_dir.name in _JSON_TEMPLATE_INDEX_SKIP_DIRS or cat_dir.name.startswith("."):
            continue

        json_files = [
            p
            for p in cat_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".json" and p.name != "templates_index.json"
        ]
        json_files.sort(key=lambda p: p.name.lower())

        folder_base, folder_fd = _folder_display_base_and_premium_default(cat_dir.name)
        for jf in json_files:
            stem = jf.stem
            preview_path = _find_template_preview_in_category(cat_dir, stem)
            if preview_path is None:
                preview_name = None
            else:
                preview_name = preview_path.name
            if preview_name is None:
                print(
                    f"[template-index] skip (no preview for stem “{stem}”): {jf} — "
                    f"add same-stem {list(_JSON_TEMPLATE_PREVIEW_EXTS)} in {cat_dir.name}/",
                    file=sys.stderr,
                )
                continue

            display_name, is_premium = _store_stem_to_name_and_premium(
                stem, default_premium=True, folder_premium_default=folder_fd
            )
            clean = _clean_stem_premium_suffix(stem)
            entry_id = f"{_slugify(cat_dir.name)}__{_slugify(clean)}"
            category_label = _title_from_stem(folder_base)

            entries.append(
                {
                    "id": entry_id,
                    "title": display_name,
                    "category": category_label,
                    "category_folder": cat_dir.name,
                    "recipe": jf.name,
                    "preview": preview_name,
                    "is_premium": is_premium,
                    "neutral_empty_slots": True,
                }
            )

    version = _write_versioned_manifest(output_path, "templates", entries)
    print(f"[template-index] v{version} — {len(entries)} template(s) → {output_path}")
    return 0


# -----------------------------------------------------------------------------
# Home screen config (home_config.json) — blueprint + manifest catalog
# -----------------------------------------------------------------------------

_HOME_VALID_DISPLAY_SIZES = frozenset({"hero", "medium", "small"})
_HOME_RESERVED_CATEGORIES = frozenset({
    "slideshow",
    "tools_grid",
    "continue_projects",
    "made_for_you",
    "popular_layouts",
    "classic_layouts",
    "stylish_layouts",
})
_HOME_STORE_CATEGORIES_NEEDING_SUB = frozenset({
    "templates",
    "filters",
    "stickers",
    "backgrounds",
    "frames",
    "shapes",
    "fonts",
})
_HOME_FIXED_PREFIX: list[dict] = [
    {
        "id": "slideshow",
        "title": "Welcome",
        "subtitle": "",
        "display_size": "hero",
        "category": "slideshow",
    },
    {
        "id": "tools",
        "title": "Your Collage",
        "subtitle": "Pick a style and start",
        "display_size": "medium",
        "category": "tools_grid",
    },
    {
        "id": "trending_layouts",
        "title": "Trending Layouts",
        "subtitle": "",
        "display_size": "small",
        "category": "popular_layouts",
        "count": 10,
    },
    {
        "id": "continue_projects",
        "title": "Continue Projects",
        "subtitle": "Pick up where you left off",
        "display_size": "small",
        "category": "continue_projects",
        "count": 10,
    },
    {
        "id": "curated_for_you",
        "title": "Curated For You",
        "subtitle": "",
        "display_size": "small",
        "category": "made_for_you",
        "count": 10,
    },
]


def _normalize_catalog_key(value: str) -> str:
    s = (value or "").strip().lower()
    for suffix in ("_pr", "_f"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _home_catalog_entries_from_categories(categories: list | None) -> list[tuple[str, str, str]]:
    """(id, display name, folder key) per store category."""
    out: list[tuple[str, str, str]] = []
    for cat in categories or []:
        if not isinstance(cat, dict):
            continue
        cid = str(cat.get("id") or "").strip()
        name = str(cat.get("name") or cid).strip()
        folder = str(
            cat.get("remoteFolderName")
            or cat.get("remote_folder")
            or cat.get("category_folder")
            or name
        ).strip()
        if cid or name:
            out.append((cid, name, folder or name))
    return out


def _home_template_categories(templates: list | None) -> list[tuple[str, str, str, int]]:
    """(folder, display category, folder, template_count) sorted by folder name."""
    buckets: dict[str, dict] = {}
    for entry in templates or []:
        if not isinstance(entry, dict):
            continue
        folder = str(entry.get("category_folder") or entry.get("category") or "").strip()
        if not folder:
            continue
        label = str(entry.get("category") or folder).strip()
        b = buckets.setdefault(
            folder,
            {"folder": folder, "label": label, "count": 0},
        )
        b["count"] += 1
    rows = [(b["folder"], b["label"], b["folder"], b["count"]) for b in buckets.values()]
    rows.sort(key=lambda r: r[0].lower())
    return rows


def _load_home_catalog_index(
    *,
    filter_manifest_path: Path,
    sticker_manifest_path: Path,
    background_manifest_path: Path,
    font_manifest_path: Path,
    templates_index_path: Path,
    frame_manifest_path: Path | None = None,
) -> dict[str, list[tuple[str, str, str]]]:
    filters_doc = _load_json_if_exists(filter_manifest_path) or {}
    stickers_doc = _load_json_if_exists(sticker_manifest_path) or {}
    backgrounds_doc = _load_json_if_exists(background_manifest_path) or {}
    fonts_doc = _load_json_if_exists(font_manifest_path) or {}
    templates_doc = _load_json_if_exists(templates_index_path) or {}
    frames_doc = _load_json_if_exists(frame_manifest_path) if frame_manifest_path else {}

    return {
        "filters": _home_catalog_entries_from_categories(filters_doc.get("categories")),
        "stickers": _home_catalog_entries_from_categories(stickers_doc.get("categories")),
        "backgrounds": _home_catalog_entries_from_categories(backgrounds_doc.get("categories")),
        "frames": _home_catalog_entries_from_categories(frames_doc.get("categories")),
        "fonts": _home_catalog_entries_from_categories(fonts_doc.get("categories")),
        "templates": [
            (folder, label, folder)
            for folder, label, _, _ in _home_template_categories(templates_doc.get("templates"))
        ],
        "template_rows_meta": _home_template_categories(templates_doc.get("templates")),
        "templates_raw": templates_doc.get("templates") or [],
        "filters_doc": filters_doc,
    }


def _resolve_catalog_match(
    requested: str,
    entries: list[tuple[str, str, str]],
    *,
    fuzzy_cutoff: float = 0.82,
) -> tuple[str, str, str] | None:
    """Return (id, name, folder) for sub_category field (prefer display name)."""
    key = _normalize_catalog_key(requested)
    if not key:
        return None
    for cid, name, folder in entries:
        candidates = {_normalize_catalog_key(cid), _normalize_catalog_key(name), _normalize_catalog_key(folder)}
        if key in candidates:
            return cid, name, folder
    keys = []
    index: list[tuple[str, str, str]] = []
    for cid, name, folder in entries:
        for label in (name, folder, cid):
            nk = _normalize_catalog_key(label)
            if nk:
                keys.append(nk)
                index.append((cid, name, folder))
    if not keys:
        return None
    matches = difflib.get_close_matches(key, keys, n=1, cutoff=fuzzy_cutoff)
    if not matches:
        return None
    hit = matches[0]
    for i, nk in enumerate(keys):
        if nk == hit:
            return index[i]
    return None


def _resolve_sub_category_value(
    requested: str | None,
    entries: list[tuple[str, str, str]],
    *,
    min_fuzzy_ratio: float = 0.72,
) -> str | None:
    if not requested or not str(requested).strip():
        return None
    raw = str(requested).strip()
    match = _resolve_catalog_match(raw, entries)
    if not match:
        return None
    _cid, name, folder = match
    resolved = name or folder
    req_n = _normalize_catalog_key(raw)
    res_n = _normalize_catalog_key(resolved)
    if req_n != res_n:
        ratio = difflib.SequenceMatcher(None, req_n, res_n).ratio()
        if ratio < min_fuzzy_ratio:
            return None
    return resolved


def _section_dedup_key(section: dict) -> tuple[str, str]:
    cat = str(section.get("category") or "").lower()
    sub = str(section.get("sub_category") or "").strip()
    return cat, sub


def _merge_slot_into_section(base: dict, slot: dict) -> dict:
    out = dict(base)
    for key in ("id", "title", "subtitle", "display_size", "category", "sub_category", "count", "item"):
        if key in slot and slot[key] is not None:
            out[key] = slot[key]
    if "subtitle" not in out:
        out["subtitle"] = ""
    return out


def _fixed_prefix_sections(blueprint: dict) -> list[dict]:
    overrides = {str(s.get("id")): s for s in blueprint.get("fixed_prefix") or [] if isinstance(s, dict)}
    sections: list[dict] = []
    for base in _HOME_FIXED_PREFIX:
        merged = _merge_slot_into_section(base, overrides.get(base["id"], {}))
        sections.append(merged)
    return sections


def _make_section(
    *,
    section_id: str,
    title: str,
    subtitle: str,
    display_size: str,
    category: str,
    sub_category: str | None = None,
    count: int | str | None = None,
    item: str | None = None,
) -> dict:
    sec: dict = {
        "id": section_id,
        "title": title,
        "subtitle": subtitle or "",
        "display_size": display_size,
        "category": category.lower() if category not in _HOME_RESERVED_CATEGORIES else category,
    }
    if sub_category is not None and str(sub_category).strip():
        sec["sub_category"] = str(sub_category).strip()
    if count is not None:
        sec["count"] = count
    if item is not None and str(item).strip():
        sec["item"] = str(item).strip()
    return sec


def _ordered_filter_categories(filter_manifest_path: Path, *, exclude: set[str]) -> list[tuple[str, str, str]]:
    doc = _load_json_if_exists(filter_manifest_path) or {}
    rows: list[tuple[str, str, str]] = []
    for cat in doc.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        cid = str(cat.get("id") or "").strip()
        if not cid or cid.lower() in exclude:
            continue
        if not cat.get("filters"):
            continue
        name = str(cat.get("name") or cid)
        folder = str(cat.get("remoteFolderName") or name)
        rows.append((cid, name, folder))
    return rows


def _apply_priority_order(
    rows: list[tuple],
    priority_folders: list[str] | None,
    *,
    folder_index: int = 0,
) -> list[tuple]:
    if not priority_folders:
        return rows
    priority_keys = [_normalize_catalog_key(p) for p in priority_folders]
    ranked: list[tuple[int, int, tuple]] = []
    for i, row in enumerate(rows):
        folder_key = _normalize_catalog_key(str(row[folder_index]))
        rank = len(priority_keys) + 1
        for pi, pk in enumerate(priority_keys):
            if folder_key == pk or folder_key.startswith(pk) or pk.startswith(folder_key):
                rank = pi
                break
        ranked.append((rank, i, row))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in ranked]


# Blueprint v2 — human-authored `store` / `file` / `pack` (no slot / id required).
_BLUEPRINT_STORE_TO_CATEGORY: dict[str, str] = {
    "templates": "templates",
    "template": "templates",
    "filters": "filters",
    "filter": "filters",
    "stickers": "stickers",
    "sticker": "stickers",
    "frames": "frames",
    "frame": "frames",
    "backgrounds": "backgrounds",
    "background": "backgrounds",
    "fonts": "fonts",
    "font": "fonts",
    "layouts": "layouts",
    "layout": "layouts",
}

_HOME_STICKER_DISPLAY_SIZE = "medium"


def _display_size_for_store(store_norm: str, requested: str) -> str:
    """Stickers always use medium (full-width pack banner); other stores use blueprint size."""
    if store_norm == "stickers":
        return _HOME_STICKER_DISPLAY_SIZE
    return requested


_BLUEPRINT_STORE_TO_SLOT: dict[str, str] = {
    "templates": "template_row",
    "filters": "filter_row",
    "stickers": "sticker_row",
    "frames": "frame_row",
    "backgrounds": "background_row",
    "fonts": "fonts_row",
}


def _normalize_blueprint_store(raw: str | None) -> str | None:
    key = _normalize_catalog_key(str(raw or ""))
    return _BLUEPRINT_STORE_TO_CATEGORY.get(key)


def _parse_blueprint_file_ref(raw: str) -> tuple[str, str | None]:
    """Return (basename, optional parent folder from path)."""
    text = str(raw or "").strip().replace("\\", "/")
    for prefix in ("templates/", "template/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    parts = [p for p in text.split("/") if p]
    if len(parts) >= 2:
        return parts[-1], parts[-2]
    if parts:
        return parts[-1], None
    return "", None


def _template_entry_matches_file(entry: dict, basename: str, path_folder: str | None) -> bool:
    bn = basename.lower()
    if not bn:
        return False
    matched_field = False
    for field in ("recipe", "preview", "id", "title"):
        val = str(entry.get(field) or "").strip()
        if not val:
            continue
        leaf = Path(val).name.lower()
        if val.lower() == bn or leaf == bn:
            matched_field = True
            break
    if not matched_field:
        return False
    if path_folder:
        folder = str(entry.get("category_folder") or entry.get("category") or "").strip()
        if folder and _normalize_catalog_key(folder) != _normalize_catalog_key(path_folder):
            return False
    return True


def _find_template_by_file(file_ref: str, templates_raw: list) -> dict | None:
    basename, path_folder = _parse_blueprint_file_ref(file_ref)
    if not basename:
        return None
    for entry in templates_raw or []:
        if isinstance(entry, dict) and _template_entry_matches_file(entry, basename, path_folder):
            return entry
    return None


def _template_file_suggestions(basename: str, templates_raw: list, *, limit: int = 8) -> list[str]:
    bn = _normalize_catalog_key(Path(basename).stem or basename)
    if not bn:
        return []
    hits: list[str] = []
    for entry in templates_raw or []:
        if not isinstance(entry, dict):
            continue
        for field in ("recipe", "preview", "id"):
            val = str(entry.get(field) or "").strip()
            if val and (_normalize_catalog_key(val).find(bn) >= 0 or bn in _normalize_catalog_key(val)):
                hits.append(val)
                break
        if len(hits) >= limit:
            break
    return hits


def _find_filter_by_file(file_ref: str, filters_doc: dict) -> tuple[str, str] | None:
    """Return (category display name, item id) for home_config ``item``."""
    basename, _ = _parse_blueprint_file_ref(file_ref)
    if not basename:
        return None
    bn = _normalize_catalog_key(Path(basename).stem or basename)
    for cat in filters_doc.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        cat_name = str(cat.get("name") or cat.get("id") or "").strip()
        for filt in cat.get("filters") or []:
            if not isinstance(filt, dict):
                continue
            for field in ("id", "name", "lutFileName"):
                val = str(filt.get(field) or "").strip()
                if not val:
                    continue
                if (
                    val.lower() == basename.lower()
                    or _normalize_catalog_key(val) == bn
                    or _normalize_catalog_key(Path(val).name) == bn
                ):
                    item_key = str(filt.get("id") or filt.get("name") or "").strip()
                    return cat_name, item_key
    return None


def _auto_section_id(parts: list[str], used: set[str]) -> str:
    base = _slugify("_".join(p for p in parts if p)) or "section"
    sid = base
    suffix = 2
    while sid in used:
        sid = f"{base}_{suffix}"
        suffix += 1
    used.add(sid)
    return sid


def _try_emit_home_section(sec: dict, emitted: set[tuple[str, str]], sections: list[dict]) -> bool:
    key = _section_dedup_key(sec)
    if key in emitted:
        return False
    cat = key[0]
    if cat in _HOME_STORE_CATEGORIES_NEEDING_SUB and not sec.get("sub_category") and not sec.get("item"):
        print(
            f"[home-config] skip section '{sec.get('id')}': missing sub_category",
            file=sys.stderr,
        )
        return False
    emitted.add(key)
    sections.append(sec)
    return True


def _compile_blueprint_section_v2(
    entry: dict,
    catalog: dict,
    emitted: set[tuple[str, str]],
    *,
    filter_manifest_path: Path,
    used_ids: set[str],
    default_display_size: str = "small",
) -> list[dict]:
    store_norm = _normalize_blueprint_store(entry.get("store") or entry.get("NameOfItem"))
    if not store_norm:
        print(
            f"[home-config] skip section: unknown store '{entry.get('store')}' "
            f"(title='{entry.get('title', '')}')",
            file=sys.stderr,
        )
        return []

    title = str(entry.get("title") or "").strip()
    subtitle = str(entry.get("subtitle") or "")
    display_size = str(entry.get("display_size") or default_display_size)

    if store_norm == "layouts":
        pack = str(entry.get("pack") or "classic").strip().lower()
        category = "stylish_layouts" if pack in ("stylish", "special", "stylish_layouts") else "classic_layouts"
        sec = _make_section(
            section_id=_auto_section_id([category, title or pack], used_ids),
            title=title or ("Stylish Collage" if category == "stylish_layouts" else "Classic Collage"),
            subtitle=subtitle,
            display_size=display_size,
            category=category,
            count=entry.get("count", 12),
        )
        out: list[dict] = []
        if _try_emit_home_section(sec, emitted, out):
            return out
        return []

    if entry.get("all_packs") and store_norm == "templates":
        legacy = {
            "slot": "template_rows",
            "id": _auto_section_id(["templates", "all_packs", title], used_ids),
            "title": entry.get("title") or "{name}",
            "subtitle": subtitle,
            "display_size": display_size,
            "max_rows": int(entry.get("max_rows") or 3),
            "count": entry.get("count", 8),
            "title_template": entry.get("title") or "{name}",
            "subtitle_template": entry.get("subtitle_template") or subtitle,
        }
        return _expand_tail_slots(
            legacy, catalog, emitted, filter_manifest_path=filter_manifest_path
        )

    file_ref = str(entry.get("file") or "").strip()
    if file_ref:
        if store_norm == "templates":
            tpl = _find_template_by_file(file_ref, catalog.get("templates_raw") or [])
            if not tpl:
                hints = _template_file_suggestions(_parse_blueprint_file_ref(file_ref)[0], catalog.get("templates_raw") or [])
                hint_txt = f" (e.g. {', '.join(hints)})" if hints else ""
                print(
                    f"[home-config] ERROR: Templates file not found '{file_ref}'{hint_txt}",
                    file=sys.stderr,
                )
                return []
            basename, _ = _parse_blueprint_file_ref(file_ref)
            sub = str(tpl.get("category") or tpl.get("category_folder") or "").strip() or None
            sec = _make_section(
                section_id=_auto_section_id(["templates", basename, title], used_ids),
                title=title or str(tpl.get("title") or basename),
                subtitle=subtitle,
                display_size=display_size,
                category="templates",
                sub_category=sub,
                count=1,
                item=basename,
            )
            out = []
            if _try_emit_home_section(sec, emitted, out):
                return out
            return []

        if store_norm == "filters":
            found = _find_filter_by_file(file_ref, catalog.get("filters_doc") or {})
            if not found:
                print(
                    f"[home-config] ERROR: Filters file/id not found '{file_ref}'",
                    file=sys.stderr,
                )
                return []
            cat_name, item_key = found
            sec = _make_section(
                section_id=_auto_section_id(["filters", item_key, title], used_ids),
                title=title or item_key,
                subtitle=subtitle,
                display_size=display_size,
                category="filters",
                sub_category=cat_name,
                count=1,
                item=item_key,
            )
            out = []
            if _try_emit_home_section(sec, emitted, out):
                return out
            return []

        print(
            f"[home-config] skip section: single 'file' only supported for Templates/Filters "
            f"(store={store_norm}, file={file_ref})",
            file=sys.stderr,
        )
        return []

    pack = str(entry.get("pack") or entry.get("folder") or "").strip()
    if not pack:
        print(
            f"[home-config] skip section: need 'pack' or 'file' for store '{store_norm}' "
            f"(title='{title}')",
            file=sys.stderr,
        )
        return []

    slot_kind = _BLUEPRINT_STORE_TO_SLOT.get(store_norm)
    if not slot_kind:
        print(
            f"[home-config] skip section: store '{store_norm}' has no row compiler yet "
            f"(use legacy slot format)",
            file=sys.stderr,
        )
        return []

    legacy = {
        "slot": slot_kind,
        "id": _auto_section_id([store_norm, pack, title], used_ids),
        "title": title or pack,
        "subtitle": subtitle,
        "display_size": _display_size_for_store(store_norm, display_size),
        "sub_category": pack,
        "count": 1 if store_norm == "stickers" else entry.get("count", 10),
        "item": entry.get("item"),
    }
    return _expand_tail_slots(
        legacy, catalog, emitted, filter_manifest_path=filter_manifest_path
    )


def _compile_blueprint_section(
    entry: dict,
    catalog: dict,
    emitted: set[tuple[str, str]],
    *,
    filter_manifest_path: Path,
    used_ids: set[str],
    default_display_size: str = "small",
) -> list[dict]:
    if not isinstance(entry, dict):
        return []
    if entry.get("slot"):
        return _expand_tail_slots(
            entry,
            catalog,
            emitted,
            filter_manifest_path=filter_manifest_path,
            default_display_size=default_display_size,
        )
    if entry.get("store") or entry.get("NameOfItem"):
        return _compile_blueprint_section_v2(
            entry,
            catalog,
            emitted,
            filter_manifest_path=filter_manifest_path,
            used_ids=used_ids,
            default_display_size=default_display_size,
        )
    print(
        f"[home-config] skip section: need 'store' or legacy 'slot' (title='{entry.get('title', '')}')",
        file=sys.stderr,
    )
    return []


def _expand_tail_slots(
    slot: dict,
    catalog: dict,
    emitted: set[tuple[str, str]],
    *,
    filter_manifest_path: Path,
    default_display_size: str = "small",
) -> list[dict]:
    kind = str(slot.get("slot") or slot.get("category") or "").strip().lower()
    if not kind:
        return []

    display_size = str(slot.get("display_size") or default_display_size)
    count = slot.get("count", 10)
    max_rows = int(slot.get("max_rows") or 99)
    priority = slot.get("priority_folders") or slot.get("priority")
    sections: list[dict] = []

    def try_emit(sec: dict) -> None:
        key = _section_dedup_key(sec)
        if key in emitted:
            return
        cat = key[0]
        if cat in _HOME_STORE_CATEGORIES_NEEDING_SUB and not sec.get("sub_category"):
            print(f"[home-config] skip section '{sec.get('id')}': missing sub_category", file=sys.stderr)
            return
        emitted.add(key)
        sections.append(sec)

    if kind in _HOME_RESERVED_CATEGORIES or kind in {
        "slideshow",
        "tools_grid",
        "popular_layouts",
        "continue_projects",
        "made_for_you",
        "classic_layouts",
        "stylish_layouts",
    }:
        cat = kind if kind in _HOME_RESERVED_CATEGORIES else {
            "slideshow": "slideshow",
            "tools": "tools_grid",
            "tools_grid": "tools_grid",
            "trending": "popular_layouts",
            "popular_layouts": "popular_layouts",
            "continue": "continue_projects",
            "continue_projects": "continue_projects",
            "curated": "made_for_you",
            "made_for_you": "made_for_you",
            "classic": "classic_layouts",
            "classic_layouts": "classic_layouts",
            "stylish": "stylish_layouts",
            "stylish_layouts": "stylish_layouts",
        }.get(kind, kind)
        sec = _make_section(
            section_id=str(slot.get("id") or cat),
            title=str(slot.get("title") or cat),
            subtitle=str(slot.get("subtitle") or ""),
            display_size=display_size,
            category=cat,
            count=slot.get("count"),
            item=slot.get("item"),
        )
        try_emit(sec)
        return sections

    if kind in ("hero_template", "template_row", "featured_template"):
        entries = catalog.get("templates") or []
        sub_raw = slot.get("sub_category")
        if sub_raw:
            resolved = _resolve_sub_category_value(sub_raw, entries)
            if not resolved:
                print(
                    f"[home-config] skip template_row '{slot.get('id')}': unknown sub_category '{sub_raw}'",
                    file=sys.stderr,
                )
                return sections
        elif entries:
            resolved = entries[0][1] or entries[0][2]
        else:
            return sections
        if sub_raw and resolved != sub_raw:
            print(f"[home-config] corrected template sub_category '{sub_raw}' → '{resolved}'")
        sec = _make_section(
            section_id=str(slot.get("id") or f"templates_{_slugify(resolved)}"),
            title=str(slot.get("title") or resolved),
            subtitle=str(slot.get("subtitle") or ""),
            display_size=str(slot.get("display_size") or "hero"),
            category="templates",
            sub_category=resolved,
            count=slot.get("count", 1),
            item=slot.get("item"),
        )
        try_emit(sec)
        return sections

    if kind == "template_rows":
        rows = list(catalog.get("template_rows_meta") or [])
        rows = _apply_priority_order(rows, priority, folder_index=0)
        for folder, label, _, tpl_count in rows[:max_rows]:
            sec = _make_section(
                section_id=str(slot.get("id_prefix") or "templates") + f"_{_slugify(folder)}",
                title=str(slot.get("title_template") or "{name}").replace("{name}", label),
                subtitle=str(slot.get("subtitle_template") or "").replace("{count}", str(tpl_count)),
                display_size=display_size,
                category="templates",
                sub_category=folder,
                count=count,
            )
            try_emit(sec)
        return sections

    if kind in ("filter_row", "filters_row"):
        entries = catalog.get("filters") or []
        sub_raw = slot.get("sub_category")
        if sub_raw:
            resolved = _resolve_sub_category_value(sub_raw, entries)
            if not resolved:
                print(
                    f"[home-config] skip filter_row '{slot.get('id')}': unknown sub_category '{sub_raw}'",
                    file=sys.stderr,
                )
                return sections
        elif entries:
            resolved = entries[0][1]
        else:
            return sections
        if sub_raw and resolved != sub_raw:
            print(f"[home-config] corrected filter sub_category '{sub_raw}' → '{resolved}'")
        sec = _make_section(
            section_id=str(slot.get("id") or f"filters_{_slugify(resolved)}"),
            title=str(slot.get("title") or resolved),
            subtitle=str(slot.get("subtitle") or ""),
            display_size=display_size,
            category="filters",
            sub_category=resolved,
            count=count,
        )
        try_emit(sec)
        return sections

    if kind == "filter_rows":
        exclude = {_normalize_catalog_key(x) for x in (slot.get("exclude") or ["basic"])}
        rows = _ordered_filter_categories(filter_manifest_path, exclude=exclude)
        rows = _apply_priority_order(rows, priority, folder_index=2)
        for cid, name, folder in rows[:max_rows]:
            sec = _make_section(
                section_id=f"filters_{_slugify(cid)}",
                title=name,
                subtitle=str(slot.get("subtitle") or ""),
                display_size=display_size,
                category="filters",
                sub_category=name,
                count=count,
            )
            try_emit(sec)
        return sections

    if kind in ("sticker_row", "stickers_row"):
        entries = catalog.get("stickers") or []
        sub_raw = slot.get("sub_category")
        resolved = _resolve_sub_category_value(sub_raw, entries) if sub_raw else None
        if not resolved and entries:
            resolved = entries[0][1]
        if not resolved:
            return sections
        sec = _make_section(
            section_id=str(slot.get("id") or f"stickers_{_slugify(resolved)}"),
            title=str(slot.get("title") or resolved),
            subtitle=str(slot.get("subtitle") or ""),
            display_size=_HOME_STICKER_DISPLAY_SIZE,
            category="stickers",
            sub_category=resolved,
            count=1,
        )
        try_emit(sec)
        return sections

    if kind == "sticker_rows":
        rows = list(catalog.get("stickers") or [])
        rows = _apply_priority_order([(e[0], e[1], e[2]) for e in rows], priority, folder_index=2)
        for cid, name, folder in rows[:max_rows]:
            sec = _make_section(
                section_id=f"stickers_{_slugify(cid)}",
                title=name,
                subtitle=str(slot.get("subtitle") or ""),
                display_size=_HOME_STICKER_DISPLAY_SIZE,
                category="stickers",
                sub_category=name,
                count=1,
            )
            try_emit(sec)
        return sections

    if kind in ("background_row", "backgrounds_row"):
        entries = catalog.get("backgrounds") or []
        sub_raw = slot.get("sub_category")
        resolved = _resolve_sub_category_value(sub_raw, entries) if sub_raw else None
        if not resolved and entries:
            resolved = entries[0][1]
        if not resolved:
            return sections
        sec = _make_section(
            section_id=str(slot.get("id") or f"backgrounds_{_slugify(resolved)}"),
            title=str(slot.get("title") or resolved),
            subtitle=str(slot.get("subtitle") or ""),
            display_size=display_size,
            category="backgrounds",
            sub_category=resolved,
            count=count,
        )
        try_emit(sec)
        return sections

    if kind in ("frame_row", "frames_row"):
        entries = catalog.get("frames") or []
        sub_raw = slot.get("sub_category")
        resolved = _resolve_sub_category_value(sub_raw, entries) if sub_raw else None
        if not resolved and entries:
            resolved = entries[0][1]
        if not resolved:
            return sections
        sec = _make_section(
            section_id=str(slot.get("id") or f"frames_{_slugify(resolved)}"),
            title=str(slot.get("title") or resolved),
            subtitle=str(slot.get("subtitle") or ""),
            display_size=display_size,
            category="frames",
            sub_category=resolved,
            count=count,
        )
        try_emit(sec)
        return sections

    if kind == "background_rows":
        rows = list(catalog.get("backgrounds") or [])
        rows = _apply_priority_order([(e[0], e[1], e[2]) for e in rows], priority, folder_index=2)
        for cid, name, folder in rows[:max_rows]:
            sec = _make_section(
                section_id=f"backgrounds_{_slugify(cid)}",
                title=name,
                subtitle=str(slot.get("subtitle") or ""),
                display_size=display_size,
                category="backgrounds",
                sub_category=name,
                count=count,
            )
            try_emit(sec)
        return sections

    if kind in ("fonts_row", "font_row"):
        entries = catalog.get("fonts") or []
        sub_raw = slot.get("sub_category")
        resolved = _resolve_sub_category_value(sub_raw, entries) if sub_raw else None
        if not resolved and entries:
            pick = slot.get("pick") or "first"
            if str(pick).lower() == "first":
                resolved = entries[0][1]
        if not resolved:
            print("[home-config] skip fonts_row: no font category in catalog", file=sys.stderr)
            return sections
        sec = _make_section(
            section_id=str(slot.get("id") or f"fonts_{_slugify(resolved)}"),
            title=str(slot.get("title") or resolved),
            subtitle=str(slot.get("subtitle") or ""),
            display_size=display_size,
            category="fonts",
            sub_category=resolved,
            count=slot.get("count", 10),
            item=slot.get("item"),
        )
        try_emit(sec)
        return sections

    print(f"[home-config] unknown slot '{kind}' — skipped", file=sys.stderr)
    return sections


def _build_home_config_payload(blueprint: dict, catalog: dict, *, filter_manifest_path: Path) -> dict:
    emitted: set[tuple[str, str]] = set()
    sections: list[dict] = []

    for sec in _fixed_prefix_sections(blueprint):
        key = _section_dedup_key(sec)
        emitted.add(key)
        sections.append(sec)

    used_ids: set[str] = {str(sec["id"]) for sec in sections if sec.get("id")}

    for entry in blueprint.get("sections") or []:
        if not isinstance(entry, dict):
            continue
        sections.extend(
            _compile_blueprint_section(
                entry,
                catalog,
                emitted,
                filter_manifest_path=filter_manifest_path,
                used_ids=used_ids,
            )
        )

    randomize = blueprint.get("randomize")
    if randomize is False:
        randomize_block = None
    elif isinstance(randomize, dict):
        randomize_block = copy.deepcopy(randomize)
    else:
        randomize_block = {
            "enabled": True,
            "title": "Discover More",
            "subtitle": "",
            "display_size": "small",
            "max_rows": 6,
            "items_per_row": 10,
        }

    payload: dict = {"sections": sections}
    if randomize_block is not None:
        payload["randomize"] = randomize_block
    return payload


def _write_home_config_files(output_paths: list[Path], payload: dict) -> str:
    """Write home_config.json to all paths; bump version on every generator run."""
    old_version: str | None = None
    for path in output_paths:
        old = _load_json_if_exists(path)
        if old and old.get("version"):
            old_version = str(old["version"])
            break
    new_version = _bump_filter_version(old_version)
    out = {"version": new_version, **payload}
    text = json.dumps(out, indent=2, ensure_ascii=False) + "\n"
    for path in output_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return new_version


def validate_home_config(path: Path) -> int:
    """Validate home_config.json; return 0 on success, 1 on failure."""
    if not path.exists():
        print(f"[home-config] ERROR: file not found: {path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[home-config] ERROR: invalid JSON: {exc}", file=sys.stderr)
        return 1

    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        print("[home-config] ERROR: sections must be a non-empty array", file=sys.stderr)
        return 1

    seen_ids: set[str] = set()
    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            print(f"[home-config] ERROR: sections[{i}] must be an object", file=sys.stderr)
            return 1
        for key in ("id", "title", "subtitle", "display_size", "category"):
            if key not in section:
                print(f"[home-config] ERROR: sections[{i}] missing required key '{key}'", file=sys.stderr)
                return 1
        sec_id = str(section["id"])
        if sec_id in seen_ids:
            print(f"[home-config] ERROR: duplicate section id: {sec_id}", file=sys.stderr)
            return 1
        seen_ids.add(sec_id)
        if section["display_size"] not in _HOME_VALID_DISPLAY_SIZES:
            print(
                f"[home-config] ERROR: sections[{i}].display_size must be one of {sorted(_HOME_VALID_DISPLAY_SIZES)}",
                file=sys.stderr,
            )
            return 1
        cat = str(section["category"]).lower()
        if cat in {"classic_layouts", "stylish_layouts"}:
            if section.get("sub_category"):
                print(f"[home-config] ERROR: sections[{i}].sub_category is not used for {cat}", file=sys.stderr)
                return 1
            if section.get("item"):
                print(f"[home-config] ERROR: sections[{i}].item is not used for {cat}", file=sys.stderr)
                return 1
        if cat in _HOME_STORE_CATEGORIES_NEEDING_SUB:
            sub = str(section.get("sub_category") or "").strip()
            has_item = bool(str(section.get("item") or "").strip())
            count_val = section.get("count")
            is_single_item = count_val == 1 or count_val == "1"
            if not sub and not (has_item and is_single_item):
                print(
                    f"[home-config] ERROR: sections[{i}] category '{cat}' requires non-empty sub_category "
                    f"(or item with count=1)",
                    file=sys.stderr,
                )
                return 1
        if "sub_category" in section and not isinstance(section["sub_category"], str):
            print(f"[home-config] ERROR: sections[{i}].sub_category must be a string", file=sys.stderr)
            return 1
        if "count" in section:
            c = section["count"]
            if not isinstance(c, int) and not (isinstance(c, str) and c.lower() == "all"):
                print("[home-config] ERROR: count must be an integer or 'all'", file=sys.stderr)
                return 1
        if "item" in section and section.get("count") != 1:
            print(f"[home-config] ERROR: sections[{i}].item is only allowed with count=1", file=sys.stderr)
            return 1

    randomize = data.get("randomize")
    if isinstance(randomize, dict):
        if "display_size" in randomize and randomize["display_size"] not in _HOME_VALID_DISPLAY_SIZES:
            print("[home-config] ERROR: randomize.display_size invalid", file=sys.stderr)
            return 1
        for key in ("template_count", "store_count"):
            if key in randomize:
                c = randomize[key]
                if not isinstance(c, int) and not (isinstance(c, str) and c.lower() == "all"):
                    print(f"[home-config] ERROR: randomize.{key} must be int or 'all'", file=sys.stderr)
                    return 1
        for key in ("max_rows", "items_per_row"):
            if key in randomize and not isinstance(randomize[key], int):
                print(f"[home-config] ERROR: randomize.{key} must be an integer", file=sys.stderr)
                return 1

    print(f"[home-config] OK: {path}")
    return 0


def generate_home_config(
    *,
    repo_root: Path,
    blueprint_path: Path,
    output_ota: Path,
    output_bundle: Path,
    filter_manifest_path: Path,
    sticker_manifest_path: Path,
    background_manifest_path: Path,
    font_manifest_path: Path,
    templates_index_path: Path,
) -> int:
    if not blueprint_path.is_file():
        print(f"[home-config] blueprint not found: {blueprint_path}", file=sys.stderr)
        return 1
    try:
        blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[home-config] invalid blueprint JSON: {exc}", file=sys.stderr)
        return 1

    frame_candidates = [
        repo_root / "Frames" / "frame_manifest.json",
        repo_root / "PhotoCollageMaker/PhotoCollage/Resource/frame_manifest.json",
    ]
    frame_manifest_path = next((p for p in frame_candidates if p.is_file()), frame_candidates[0])

    catalog = _load_home_catalog_index(
        filter_manifest_path=filter_manifest_path,
        sticker_manifest_path=sticker_manifest_path,
        background_manifest_path=background_manifest_path,
        font_manifest_path=font_manifest_path,
        templates_index_path=templates_index_path,
        frame_manifest_path=frame_manifest_path,
    )
    payload = _build_home_config_payload(blueprint, catalog, filter_manifest_path=filter_manifest_path)
    outputs = [output_ota.resolve(), output_bundle.resolve()]
    version = _write_home_config_files(outputs, payload)
    print(f"[home-config] v{version} — {len(payload['sections'])} section(s) → {output_ota}")
    for out_path in outputs:
        rc = validate_home_config(out_path)
        if rc != 0:
            return rc
    return 0


def _add_home_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--generate-home-config",
        action="store_true",
        default=False,
        help="Generate HomeScreen/home_config.json from blueprint + store manifests.",
    )
    parser.add_argument(
        "--home-blueprint",
        default="HomeScreen/home_screen_blueprint.json",
        help="Blueprint JSON v2 (store/file/pack) or legacy slots + fixed header prefix.",
    )
    parser.add_argument(
        "--home-output-ota",
        default="HomeScreen/home_config.json",
        help="OTA home_config.json output path.",
    )
    parser.add_argument(
        "--home-output-bundle",
        default="PhotoCollageMaker/PhotoCollage/Resource/home_config.json",
        help="Bundled fallback home_config.json path.",
    )


def _add_filter_manifest_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--generate-filter-manifest",
        action="store_true",
        default=False,
        help="Also generate Filters/filter_manifest.json from Filters/ sub-folders.",
    )
    parser.add_argument(
        "--filters-dir",
        default="Filters",
        help="Folder containing LUT sub-folders (default: Filters).",
    )
    parser.add_argument(
        "--filter-output",
        default="Filters/filter_manifest.json",
        help="Output path for filter_manifest.json (default: Filters/filter_manifest.json).",
    )
    parser.add_argument(
        "--generate-filter-previews",
        action="store_true",
        default=False,
        help="Generate StorePreviews/Filters/<Category>/<FilterId> preview images and inject preview URLs into filter_manifest.json.",
    )
    parser.add_argument(
        "--filter-stock-dir",
        default="Filters/StockImage",
        help="Folder containing stock images used to render previews (default: Filters/StockImage).",
    )
    parser.add_argument(
        "--filter-previews-dir",
        default="(derived)",
        help=(
            "Deprecated: previews are written under Filters/<CategoryFolder>/StorePreviews/<FilterId>/ by default. "
            "This flag is kept for backward compatibility and is ignored."
        ),
    )
    parser.add_argument(
        "--filter-preview-max-edge",
        type=int,
        default=1080,
        help="Max long edge for generated preview JPGs (default: 1080).",
    )


def _add_store_manifest_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--generate-store-manifests",
        action="store_true",
        default=False,
        help="Generate frame/sticker/background/font/shape manifests from repository folders.",
    )
    parser.add_argument("--frames-dir", default="Frames")
    parser.add_argument("--frames-output", default="Frames/frame_manifest.json")
    parser.add_argument(
        "--frame-preview-max-edge",
        type=int,
        default=128,
        help="Max long edge for frame toolbar preview WebPs (default: 128).",
    )
    parser.add_argument(
        "--skip-frame-preview-webp",
        action="store_true",
        help=(
            "Only update frame_manifest.json (fast). Still emits previewWebpUrl when --base-url is set. "
            "Generate WebP files in CI or run without this flag before Cloudflare deploy."
        ),
    )
    parser.add_argument(
        "--frame-preview-workers",
        type=int,
        default=0,
        help="Parallel workers for frame WebP generation (0 = auto, default min(8, CPU count)).",
    )
    parser.add_argument("--stickers-dir", default="Stickers")
    parser.add_argument("--stickers-output", default="Stickers/sticker_store_manifest.json")
    parser.add_argument(
        "--sticker-preview-max-edge",
        type=int,
        default=128,
        help="Max long edge for sticker toolbar preview WebPs (default: 128).",
    )
    parser.add_argument(
        "--skip-sticker-preview-webp",
        action="store_true",
        help=(
            "Only update sticker_store_manifest.json (fast). Still emits previewWebpUrl when --base-url is set. "
            "Generate WebP files in CI or run without this flag before Cloudflare deploy."
        ),
    )
    parser.add_argument(
        "--sticker-preview-workers",
        type=int,
        default=0,
        help="Parallel workers for WebP generation (0 = auto, default min(8, CPU count)).",
    )
    parser.add_argument("--backgrounds-dir", default="Backgrounds")
    parser.add_argument("--backgrounds-output", default="Backgrounds/background_store_manifest.json")
    parser.add_argument("--fonts-dir", default="Fonts")
    parser.add_argument("--fonts-output", default="Fonts/font_catalog.json")
    parser.add_argument(
        "--font-licenses-subdir",
        default="licenses",
        help=(
            "Top-level folder name under Fonts/ to exclude from language categories (default: licenses). "
            "License files (.txt, .pdf) are collected from all of Fonts/, not only this folder."
        ),
    )
    parser.add_argument("--shapes-dir", default="Shapes")
    parser.add_argument("--shapes-output", default="Shapes/shape_store_manifest.json")
    parser.add_argument(
        "--shape-preview-max-edge",
        type=int,
        default=128,
        help="Max long edge for shape toolbar/store preview WebPs rasterized from SVG (default: 128).",
    )
    parser.add_argument(
        "--skip-shape-preview-webp",
        action="store_true",
        help=(
            "Only update shape_store_manifest.json (fast). Still emits previewWebpUrl when --base-url is set. "
            "Generate WebP files in CI or run without this flag before Cloudflare deploy."
        ),
    )
    parser.add_argument(
        "--shape-preview-workers",
        type=int,
        default=0,
        help="Parallel workers for shape WebP generation (0 = auto, default min(8, CPU count)).",
    )
    parser.add_argument(
        "--generate-png-template-manifest",
        action="store_true",
        default=False,
        help="Generate PNGTemplates/png_template_manifest.json from PNGTemplates category folders.",
    )
    parser.add_argument("--png-templates-dir", default="PNGTemplates")
    parser.add_argument("--png-templates-output", default="PNGTemplates/png_template_manifest.json")
    parser.add_argument(
        "--generate-templates-index",
        action="store_true",
        default=False,
        help="Generate templates_index.json by scanning Templates/<CategoryFolder> for *.json + same-stem preview.",
    )
    parser.add_argument(
        "--templates-index-dir",
        default="Templates",
        help="Root folder to scan (default: Templates; each subfolder is a category).",
    )
    parser.add_argument(
        "--templates-index-output",
        default="Templates/templates_index.json",
        help="Output path (default: Templates/templates_index.json).",
    )


# Re-run main with filter manifest support (extended entry point).
def main_with_filter_support() -> int:
    """
    Drop-in replacement for main() that also handles --generate-filter-manifest.
    Called by GitHub Actions via the workflow.
    """
    parser = argparse.ArgumentParser(
        description="Generate enhanced_manifest.json (and optionally filter_manifest.json)"
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--json-path", default="classic_and_stylish_layouts.json")
    parser.add_argument("--svg-dir", default="collages")
    parser.add_argument("--output", default="enhanced_manifest.json")
    parser.add_argument("--thumbnails-dir", default="thumbnails")
    parser.add_argument("--svg-id-prefix", default="svg_")
    _add_filter_manifest_args(parser)
    _add_store_manifest_args(parser)
    _add_home_config_args(parser)
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()

    # --- Run existing enhanced_manifest generation ---
    # Temporarily override sys.argv so the inner main() parses cleanly.
    orig_argv = sys.argv[:]
    sys.argv = [
        sys.argv[0],
        "--base-url", args.base_url,
        "--repo-root", str(repo_root),
        "--json-path", args.json_path,
        "--svg-dir", args.svg_dir,
        "--output", args.output,
        "--thumbnails-dir", args.thumbnails_dir,
        "--svg-id-prefix", args.svg_id_prefix,
    ]
    result = main()
    sys.argv = orig_argv

    if result != 0:
        return result

    # --- Optionally generate filter_manifest ---
    if args.generate_filter_manifest:
        filters_dir   = repo_root / args.filters_dir
        filter_output = repo_root / args.filter_output
        result = generate_filter_manifest(filters_dir, filter_output, args.base_url)
        if result != 0:
            return result

        if args.generate_filter_previews:
            result = generate_filter_previews_and_attach_to_manifest(
                repo_root=repo_root,
                filter_manifest_path=filter_output,
                filters_dir=filters_dir,
                stock_dir=repo_root / args.filter_stock_dir,
                previews_root=repo_root / args.filter_previews_dir,
                base_url=args.base_url,
                max_edge=int(args.filter_preview_max_edge),
            )
            if result != 0:
                return result

    if args.generate_store_manifests:
        result = generate_frame_store_manifest(
            repo_root / args.frames_dir,
            repo_root / args.frames_output,
            base_url=args.base_url,
            generate_preview_webp=not args.skip_frame_preview_webp,
            preview_max_edge=int(args.frame_preview_max_edge),
            preview_workers=int(args.frame_preview_workers),
        )
        if result != 0:
            return result
        result = generate_sticker_store_manifest(
            repo_root / args.stickers_dir,
            repo_root / args.stickers_output,
            base_url=args.base_url,
            generate_preview_webp=not args.skip_sticker_preview_webp,
            preview_max_edge=int(args.sticker_preview_max_edge),
            preview_workers=int(args.sticker_preview_workers),
        )
        if result != 0:
            return result
        result = generate_background_store_manifest(repo_root / args.backgrounds_dir, repo_root / args.backgrounds_output)
        if result != 0:
            return result
        result = generate_font_catalog_manifest(
            repo_root / args.fonts_dir,
            repo_root / args.fonts_output,
            manifest_base_url=args.base_url,
            licenses_subdir=args.font_licenses_subdir,
        )
        if result != 0:
            return result
        result = generate_shape_store_manifest(
            repo_root / args.shapes_dir,
            repo_root / args.shapes_output,
            base_url=args.base_url,
            generate_preview_webp=not args.skip_shape_preview_webp,
            preview_max_edge=int(args.shape_preview_max_edge),
            preview_workers=int(args.shape_preview_workers),
        )
        if result != 0:
            return result

    if args.generate_png_template_manifest:
        result = generate_png_template_manifest(
            repo_root / args.png_templates_dir,
            repo_root / args.png_templates_output,
        )
        if result != 0:
            return result

    if args.generate_templates_index:
        result = generate_json_template_index(
            repo_root / args.templates_index_dir,
            repo_root / args.templates_index_output,
        )
        if result != 0:
            return result

    if args.generate_home_config:
        result = generate_home_config(
            repo_root=repo_root,
            blueprint_path=repo_root / args.home_blueprint,
            output_ota=repo_root / args.home_output_ota,
            output_bundle=repo_root / args.home_output_bundle,
            filter_manifest_path=repo_root / args.filter_output,
            sticker_manifest_path=repo_root / args.stickers_output,
            background_manifest_path=repo_root / args.backgrounds_output,
            font_manifest_path=repo_root / args.fonts_output,
            templates_index_path=repo_root / args.templates_index_output,
        )
        if result != 0:
            return result

    return result


if __name__ == "__main__":
    sys.exit(main_with_filter_support())
