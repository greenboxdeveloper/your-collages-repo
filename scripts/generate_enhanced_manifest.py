#!/usr/bin/env python3
from __future__ import annotations

"""
Generate enhanced_manifest.json (version 2.0) by merging:
  1. classic_and_stylish_layouts.json (classic + stylish from app export)
  2. All .svg files in a folder (parsed with svgelements)

Generates PNG thumbnails (one per layout) in thumbnails/.

Stylish layouts match app collage generation (CollageProtocol.swift, MasterView.swift):
  - One layout = one MasterCollage; each slot = one MasterView.
  - Slot shape comes from path_data (→ UIBezierPath → svgMaskPath) or n_rect (rectangle).
  - Thumbnails use the same path_data parsing as SVGLayoutParser.createBezierPath so shapes match.

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
            layout["thumbnailURL"] = f"{base_url}/thumbnails/{layout['id']}.png"
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


def parse_svg_file(svg_path: Path, base_url: str, id_prefix: str = "svg_") -> dict | None:
    """Parse one SVG with svgelements: bbox → n_rect, path_data for organic shapes."""
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

    element_types = (SVGPath, Rect, Polygon, Circle, Ellipse)
    elements = [e for e in svg.elements() if isinstance(e, element_types)]
    if not elements:
        print(f"  No slots in {svg_path}", file=sys.stderr)
        return None

    is_organic = any(isinstance(e, (SVGPath, Polygon, Circle, Ellipse)) for e in elements)
    slots = []
    for i, e in enumerate(elements):
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
            slot = {"id": f"slot_{i}", "n_rect": [n_x, n_y, n_w, n_h]}
            path_d = _path_d_for_element(e)
            if path_d:
                slot["path_data"] = path_d
            slots.append(slot)
        except Exception as ex:
            print(f"  Skip element {i} in {svg_path}: {ex}", file=sys.stderr)

    if not slots:
        return None
    result = {
        "id": layout_id,
        "name": name,
        "category": "Stylish",
        "isPremium": False,
        "type": "organic" if is_organic else "grid",
        "thumbnailURL": f"{base_url}/thumbnails/{layout_id}.png",
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


# -----------------------------------------------------------------------------
# PathMaker-style paths (mirror PathMaker.swift for stylish collages)
# Layout id → list of PathMaker viewIdentifier values (one per slot). Same as
# Special/Collage*.swift: view1.viewIdentifier = X, view2.viewIdentifier = Y, ...
# -----------------------------------------------------------------------------
STYLISH_LAYOUT_TO_PATHMAKER_IDS: dict[str, list[int]] = {
    "stylish_Collage2_1": [21, 212],
    "stylish_Collage2_2": [221, 222],
    "stylish_Collage2_3": [231, 232],
    "stylish_Collage2_4": [241, 242],
    "stylish_Collage2_5": [251, 252],
    "stylish_Collage2_6": [261, 262],
    "stylish_Collage2_7": [271, 272],
    "stylish_Collage2_8": [281, 282],
    "stylish_Collage2_9": [291, 292],
    "stylish_Collage3_1": [311, 312, 313],
    "stylish_Collage3_2": [321, 322, 323],
    "stylish_Collage3_3": [331, 332, 333],
    "stylish_Collage3_4": [341, 342, 343],
    "stylish_Collage3_5": [351, 352, 353],
    "stylish_Collage3_6": [361, 362, 363],
    "stylish_Collage3_7": [371, 372, 373],
    "stylish_Collage3_8": [381, 382, 383],
    "stylish_Collage3_9": [391, 392, 393],
    "stylish_Collage3_10": [3101, 3102, 3103],
    "stylish_Collage3_11": [3111, 3112, 3113],
    "stylish_Collage3_12": [3121, 3122, 3123],
    "stylish_Collage3_13": [3131, 3132, 3133],
    "stylish_Collage4_1": [411, 412, 413, 414],
    "stylish_Collage4_2": [421, 422, 423, 424],
    "stylish_Collage4_3": [431, 432, 433, 434],
    "stylish_Collage4_4": [441, 442, 443, 444],
}


def _arc_tangent_points(
    p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float],
    radius: float, steps: int = 12,
) -> list[tuple[float, float]]:
    """
    Arc tangent to line p0-p1 and p1-p2 with given radius (mirror CGPath.addArc(tangent1End:tangent2End:radius:)).
    Returns points along the arc (excluding p1); empty if radius <= 0 or degenerate.
    When p0==p1 (first segment after move), arc goes from p1 to tangent on p1-p2 (quarter circle).
    """
    if radius <= 0 or steps < 2:
        return []
    ax, ay = p0[0] - p1[0], p0[1] - p1[1]
    bx, by = p2[0] - p1[0], p2[1] - p1[1]
    la = math.sqrt(ax * ax + ay * ay)
    lb = math.sqrt(bx * bx + by * by)
    if lb < 1e-10:
        return []
    bx, by = bx / lb, by / lb
    # Degenerate: previous segment has zero length (e.g. first arc after move_to(p1))
    if la < 1e-10:
        # Arc from p1 to tangent point on p1-p2; center is at distance r from line p1-p2
        nx, ny = -by, bx
        cx = p1[0] + nx * radius
        cy = p1[1] + ny * radius
        t2x = p1[0] + bx * radius
        t2y = p1[1] + by * radius
        a1 = math.atan2(p1[1] - cy, p1[0] - cx)
        a2 = math.atan2(t2y - cy, t2x - cx)
        da = a2 - a1
        while da > math.pi:
            da -= 2 * math.pi
        while da < -math.pi:
            da += 2 * math.pi
        return [
            (cx + radius * math.cos(a1 + (k / steps) * da), cy + radius * math.sin(a1 + (k / steps) * da))
            for k in range(1, steps)
        ]
    ax, ay = ax / la, ay / la
    nax, nay = -ay, ax
    nbx, nby = -by, bx
    bix = nax + nbx
    biy = nay + nby
    bi_len = math.sqrt(bix * bix + biy * biy)
    if bi_len < 1e-10:
        return []
    bix, biy = bix / bi_len, biy / bi_len
    cos_angle = max(-1.0, min(1.0, -ax * bx - ay * by))
    half_angle = math.acos(cos_angle) * 0.5
    sin_half = math.sin(half_angle)
    if sin_half < 1e-10:
        return []
    dist_center = radius / sin_half
    cx = p1[0] + bix * dist_center
    cy = p1[1] + biy * dist_center
    tan_dist = radius / math.tan(half_angle) if half_angle >= 1e-6 else 0.0
    t1x = p1[0] - ax * tan_dist
    t1y = p1[1] - ay * tan_dist
    t2x = p1[0] + bx * tan_dist
    t2y = p1[1] + by * tan_dist
    a1 = math.atan2(t1y - cy, t1x - cx)
    a2 = math.atan2(t2y - cy, t2x - cx)
    da = a2 - a1
    while da > math.pi:
        da -= 2 * math.pi
    while da < -math.pi:
        da += 2 * math.pi
    return [
        (cx + radius * math.cos(a1 + (k / steps) * da), cy + radius * math.sin(a1 + (k / steps) * da))
        for k in range(1, steps)
    ]


def _path_maker_path(
    identifier: int,
    frame_w: float, frame_h: float,
    main_w: float, main_h: float,
    space: float, radius: float,
) -> list[tuple[float, float]]:
    """
    Build path polygon for one slot (PathMaker.getPath(by:frame:...) in frame coords).
    frame_origin is (0,0); frame size is (frame_w, frame_h). main = full canvas (main_w, main_h).
    Returns list of (x,y) points forming a closed polygon.
    """
    ox, oy = 0.0, 0.0
    pts: list[tuple[float, float]] = []

    def move_to(x: float, y: float) -> None:
        pts.append((x, y))

    def add_arc(p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float]) -> None:
        for q in _arc_tangent_points(p0, p1, p2, radius):
            pts.append(q)

    # Angles and space-derived offsets (mirror PathMaker)
    if identifier == 231:  # for_231_Left
        Q = math.atan(main_w / 3 / main_h) if main_h > 0 else 0
        x = space * math.tan(Q)
        x1 = space / 2 / math.cos(Q) if math.cos(Q) > 1e-10 else 0
        left_top = (ox + space, oy + space)
        right_top = ((frame_w / 2) - (x1 - x), oy + space)
        right_bottom = (frame_w - x1 - x, frame_h - space)
        left_bottom = (ox + space, frame_h - space)
        mid_left = ((left_top[0] + left_bottom[0]) / 2, (left_top[1] + left_bottom[1]) / 2)
        move_to(*mid_left)
        add_arc(mid_left, left_top, right_top)
        add_arc(left_top, right_top, right_bottom)
        add_arc(right_top, right_bottom, left_bottom)
        add_arc(right_bottom, left_bottom, mid_left)
    elif identifier == 232:  # for_232_Right
        one_piece = frame_w / 2
        Q = math.atan(main_w / 3 / main_h) if main_h > 0 else 0
        x = space * math.tan(Q)
        x1 = space / 2 / math.cos(Q) if math.cos(Q) > 1e-10 else 0
        left_top = (ox + x1 + x, oy + space)
        right_top = (frame_w - space, oy + space)
        right_bottom = (frame_w - space, frame_h - space)
        left_bottom = (ox + one_piece + (x1 - x), frame_h - space)
        left_mid = ((left_top[0] + left_bottom[0]) / 2, (left_top[1] + left_bottom[1]) / 2)
        move_to(*left_mid)
        add_arc(left_mid, left_top, right_top)
        add_arc(left_top, right_top, right_bottom)
        add_arc(right_top, right_bottom, left_bottom)
        add_arc(right_bottom, left_bottom, left_mid)
    elif identifier == 221:  # for_221_Left
        Q = math.atan(main_w / 3 / main_h) if main_h > 0 else 0
        x = space * math.tan(Q)
        x1 = space / 2 / math.cos(Q) if math.cos(Q) > 1e-10 else 0
        left_top = (ox + space, oy + space)
        right_top = (frame_w - x - x1, oy + space)
        right_bottom = (frame_w / 3 - (x1 - x), frame_h - space)
        left_bottom = (ox + space, frame_h - space)
        mid_left = ((left_top[0] + left_bottom[0]) / 2, (left_top[1] + left_bottom[1]) / 2)
        move_to(*mid_left)
        add_arc(mid_left, left_top, right_top)
        add_arc(left_top, right_top, right_bottom)
        add_arc(right_top, right_bottom, left_bottom)
        add_arc(right_bottom, left_bottom, mid_left)
    elif identifier == 222:  # for_222_Right
        Q = math.atan(main_w / 3 / main_h) if main_h > 0 else 0
        x = space * math.tan(Q)
        x1 = space / 2 / math.cos(Q) if math.cos(Q) > 1e-10 else 0
        one_piece = frame_w / 3
        left_top = (ox + (one_piece * 2) + (x1 - x), oy + space)
        right_top = (frame_w - space, oy + space)
        right_bottom = (frame_w - space, frame_h - space)
        left_bottom = (ox + x1 + x, frame_h - space)
        mid_left = ((left_top[0] + left_bottom[0]) / 2, (left_top[1] + left_bottom[1]) / 2)
        move_to(*mid_left)
        add_arc(mid_left, left_top, right_top)
        add_arc(left_top, right_top, right_bottom)
        add_arc(right_top, right_bottom, left_bottom)
        add_arc(right_bottom, left_bottom, mid_left)
    elif identifier == 21:  # for_211_Top
        Q = math.atan(main_h / 3 / main_w) if main_w > 0 else 0
        x = space * math.tan(Q)
        x1 = space / 2 / math.cos(Q) if math.cos(Q) > 1e-10 else 0
        left_top = (ox + space, oy + space)
        right_top = (frame_w - space, oy + space)
        right_bottom = (frame_w - space, frame_h / 3 - (x1 - x))
        left_bottom = (ox + space, frame_h - x - x1)
        mid = ((left_top[0] + left_bottom[0]) / 2, (left_top[1] + left_bottom[1]) / 2)
        move_to(*mid)
        add_arc(mid, left_top, right_top)
        add_arc(left_top, right_top, right_bottom)
        add_arc(right_top, right_bottom, left_bottom)
        add_arc(right_bottom, left_bottom, mid)
    elif identifier == 212:  # for_212_Bottom
        one_piece = frame_h / 3
        Q = math.atan(main_h / 3 / main_w) if main_w > 0 else 0
        x = space * math.tan(Q)
        x1 = space / 2 / math.cos(Q) if math.cos(Q) > 1e-10 else 0
        left_top = (ox + space, frame_h - one_piece + (x1 - x))
        right_top = (frame_w - space, oy + x + x1)
        right_bottom = (frame_w - space, frame_h - space)
        left_bottom = (ox + space, frame_h - space)
        mid = ((left_top[0] + left_bottom[0]) / 2, (left_top[1] + left_bottom[1]) / 2)
        move_to(*mid)
        add_arc(mid, left_top, right_top)
        add_arc(left_top, right_top, right_bottom)
        add_arc(right_top, right_bottom, left_bottom)
        add_arc(right_bottom, left_bottom, mid)
    elif identifier == 271:  # for_271_Left
        x = space / math.sin(math.pi / 4)
        p1 = (frame_w / 2, space)
        p2 = (frame_w - space - x / 2, space)
        p3 = (space, frame_h - space - x / 2)
        p4 = (space, space)
        move_to(p1[0], p1[1])
        add_arc(p1, p1, p2)
        add_arc(p2, p2, p3)
        add_arc(p3, p3, p4)
        add_arc(p4, p4, p1)
    elif identifier == 272:  # for_272_Right
        x = space / math.sin(math.pi / 4)
        p1 = (frame_w - space, frame_h / 2)
        p2 = (frame_w - space, frame_h - space)
        p3 = (space + x / 2, frame_h - space)
        p4 = (frame_w - space, oy + space + x / 2)
        move_to(p1[0], p1[1])
        add_arc(p1, p1, p2)
        add_arc(p2, p2, p3)
        add_arc(p3, p3, p4)
        add_arc(p4, p4, p1)
    else:
        # Not implemented: fall back to rectangle from frame
        pts.extend([
            (ox + space, oy + space),
            (frame_w - space, oy + space),
            (frame_w - space, frame_h - space),
            (ox + space, frame_h - space),
        ])
    if pts and (pts[0][0] != pts[-1][0] or pts[0][1] != pts[-1][1]):
        pts.append(pts[0])
    return pts


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
    Stylish thumbnail: one slot = one shape, matching app collage generation.

    When the layout id is in STYLISH_LAYOUT_TO_PATHMAKER_IDS we use PathMaker-style paths
    (mirror PathMaker.swift): each slot is drawn with _path_maker_path(identifier, frame, mainSize, space, radius)
    so thumbnails match what MasterView displays via PathMaker.getPath(by:viewIdentifier, frame:...).

    Otherwise we use path_data from JSON (SVGLayoutParser.createBezierPath style): parse with
    _flatten_path_to_polygons and scale 0-1 to pixels.
    """
    slots = layout.get("slots", [])
    layout_id = layout.get("id", "")
    path_maker_ids = STYLISH_LAYOUT_TO_PATHMAKER_IDS.get(layout_id) if layout_id else None

    # PathMaker constants (match app: small gap and corner radius in points; scale to thumbnail)
    space_pt = 4.0
    radius_pt = 6.0
    space = max(1.0, space_pt * size / 300.0)
    radius = max(0.5, radius_pt * size / 300.0)

    if path_maker_ids is not None and len(path_maker_ids) == len(slots):
        # Draw using PathMaker-style paths (same as app)
        main_w = main_h = float(size)
        for i, slot in enumerate(slots):
            nr = slot.get("n_rect", [0, 0, 1, 1])
            if len(nr) < 4:
                continue
            # Slot frame in thumbnail: origin (nx, ny), size (nw, nh) in pixels
            nx = nr[0] * size
            ny = nr[1] * size
            nw = max(1.0, nr[2] * size)
            nh = max(1.0, nr[3] * size)
            poly = _path_maker_path(
                path_maker_ids[i], nw, nh, main_w, main_h, space, radius
            )
            if len(poly) < 2:
                continue
            # Path is in slot-local coords (0,0..nw,nh); translate to image
            pts = [(round(p[0] + nx), round(p[1] + ny)) for p in poly]
            color = (*THUMB_COLORS[i % len(THUMB_COLORS)], 255)
            draw.polygon(pts, fill=color, outline=(80, 80, 80, 255))
        return

    # Fallback: path_data (0-1) parsed like SVGLayoutParser.createBezierPath
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
        # Stylish: PathMaker-style paths when id mapped, else path_data
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
        draw_thumbnail(layout, thumb_dir / f"{lid}.png")
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
