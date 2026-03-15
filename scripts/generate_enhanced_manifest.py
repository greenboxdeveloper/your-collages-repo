#!/usr/bin/env python3
from __future__ import annotations

"""
Generate enhanced_manifest.json (version 2.0) by merging:
  1. classic_and_stylish_layouts.json (classic_layouts only; stylish removed)
  2. All .svg files in a folder (parsed with svgelements; circles/ellipses from raw SVG like app)

Drag handles: <line> elements with id starting with DRAG_H_ or DRAG_V_ are detected and emitted
as dividers (not as photo slots). Position: DRAG_H_ → y1/viewBoxHeight; DRAG_V_ → x1/viewBoxWidth.
Each divider has an "affects" list of slot ids whose rect edge matches the divider position.

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

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

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
        layout["thumbnailURL"] = f"{base_url}/thumbnails/{layout['id']}.png"
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


def parse_svg_file(svg_path: Path, base_url: str, id_prefix: str = "svg_") -> dict | None:
    """Parse one SVG with svgelements: bbox → n_rect, path_data for organic shapes; drag handles → dividers."""
    if SVG is None:
        print("Warning: svgelements not installed; run pip install svgelements", file=sys.stderr)
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
        # Default behavior for SVG-derived layouts: premium
        is_premium = True

    result = {
        "id": layout_id,
        "name": name,
        "category": category,
        # SVG-derived layouts use filename suffixes to determine premium; existing JSON layouts keep their own isPremium.
        "isPremium": is_premium,
        "type": "organic" if is_organic else "grid",
        "thumbnailURL": f"{base_url}/thumbnails/{layout_id}.png",
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
    parser.add_argument("--thumbnails-dir", default="thumbnails", help="Output folder for thumbnails")
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

    # 3) Thumbnails
    thumb_dir.mkdir(parents=True, exist_ok=True)
    for layout in layouts:
        lid = layout["id"]
        draw_thumbnail(layout, thumb_dir / f"{lid}.png")
    print(f"Wrote {len(layouts)} thumbnails to {thumb_dir}")

    # 4) Write manifest (strip internal keys like __viewbox)
    def strip_internal_keys(layout: dict) -> dict:
        return {k: v for k, v in layout.items() if not k.startswith("__")}
    layouts_clean = [strip_internal_keys(l) for l in layouts]

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

    layouts_changed = old_layouts is None or old_layouts != layouts_clean
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


if __name__ == "__main__":
    sys.exit(main())
