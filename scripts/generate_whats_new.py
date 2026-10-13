#!/usr/bin/env python3
"""Generate the in-app "What's New" catalog from App Store Connect release notes.

Source:  metadata/version/<version>/<locale>.json  (field "whatsNew", from `asc metadata pull`)
         When metadata/ is absent (content repo CI), WhatsNew/release_notes/<version>.json instead.
         Reading metadata/ also refreshes release_notes/, so only public release notes reach the content repo.
Config:  WhatsNew/config.json  (optional per-version item picks / icon overrides)
Outputs: WhatsNew/whats_new.json                                  -> publish to the OTA content CDN
         PhotoCollageMaker/PhotoCollage/Resource/whats_new.json   -> bundled fallback in the app

Release notes are parsed as:
    <intro paragraph>            -> headline
    SECTION HEADER:              -> skipped
    [* ] Title: description      -> item

Every locale is a translation of the same list, so items line up by position and icons are
picked once from the en-US titles. Usage:
    python3 scripts/generate_whats_new.py            # all versions under metadata/version
    python3 scripts/generate_whats_new.py 3.2.2      # only these versions
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METADATA_DIR = ROOT / "metadata" / "version"
RELEASE_NOTES_DIR = ROOT / "WhatsNew" / "release_notes"
CONFIG_PATH = ROOT / "WhatsNew" / "config.json"
OUTPUTS = [
    ROOT / "WhatsNew" / "whats_new.json",
    ROOT / "PhotoCollageMaker" / "PhotoCollage" / "Resource" / "whats_new.json",
]
REFERENCE_LOCALE = "en-US"
DEFAULT_MAX_ITEMS = 5
MAX_VERSIONS = 5
MAX_TITLE_CHARS = 80

# First matching keyword (lowercased en-US title) wins. iOS 16-safe SF Symbols only.
ICON_KEYWORDS = [
    ("playground", "wand.and.stars"),
    ("cutout", "scissors"),
    ("draw", "pencil.and.outline"),
    ("layout design", "pencil.and.outline"),
    ("engine", "bolt.fill"),
    ("template", "square.grid.2x2"),
    ("store", "square.grid.2x2"),
    ("save", "square.and.arrow.down.on.square"),
    ("project", "square.and.arrow.down.on.square"),
    ("export", "square.and.arrow.up"),
    ("font", "textformat"),
    ("shape", "square.on.circle"),
    ("generat", "sparkles"),
    ("sticker", "face.smiling"),
    ("filter", "camera.filters"),
    ("fix", "wrench.and.screwdriver"),
    ("fast", "hare.fill"),
    ("speed", "hare.fill"),
]
DEFAULT_ICON = "sparkles"

BULLET_RE = re.compile(r"^\s*[*\-•·]\s*")
COLON_RE = re.compile(r"\s*[:：]\s*")


def parse_notes(text):
    """Return (headline, [(title, subtitle), ...]) from plain release-notes text."""
    headline_lines, items, seen_structure = [], [], False
    for raw in text.splitlines():
        line = BULLET_RE.sub("", raw).strip()
        if not line:
            continue
        parts = COLON_RE.split(line, maxsplit=1)
        if len(parts) == 2 and len(parts[0]) <= MAX_TITLE_CHARS:
            title, desc = parts[0].strip(), parts[1].strip()
            seen_structure = True
            if desc:
                items.append((title, desc))
            continue  # empty desc = section header
        if not seen_structure:
            headline_lines.append(line)
    return " ".join(headline_lines), items


def icon_for(title):
    lowered = title.lower()
    for keyword, icon in ICON_KEYWORDS:
        if keyword in lowered:
            return icon
    return DEFAULT_ICON


def load_release_notes(version):
    """{locale: whatsNew text} from metadata/ (also mirrored to release_notes/) or release_notes/."""
    version_dir = METADATA_DIR / version
    if version_dir.is_dir():
        texts = {}
        for path in sorted(version_dir.glob("*.json")):
            text = json.loads(path.read_text(encoding="utf-8")).get("whatsNew") or ""
            if text.strip():
                texts[path.stem] = text
        RELEASE_NOTES_DIR.mkdir(parents=True, exist_ok=True)
        (RELEASE_NOTES_DIR / f"{version}.json").write_text(
            json.dumps(texts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return texts
    path = RELEASE_NOTES_DIR / f"{version}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def available_versions():
    names = set()
    if METADATA_DIR.is_dir():
        names |= {p.name for p in METADATA_DIR.iterdir() if p.is_dir()}
    if RELEASE_NOTES_DIR.is_dir():
        names |= {p.stem for p in RELEASE_NOTES_DIR.glob("*.json")}
    return sorted(names, key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")])


def build_version(version, config):
    notes = {locale: parse_notes(text) for locale, text in load_release_notes(version).items()}
    if REFERENCE_LOCALE not in notes:
        print(f"  {version}: no {REFERENCE_LOCALE} release notes, skipped")
        return None

    ref_items = notes[REFERENCE_LOCALE][1]
    if not ref_items:
        print(f"  {version}: no 'Title: description' lines in {REFERENCE_LOCALE}, skipped")
        return None

    picks = config.get("pick") or list(range(1, min(DEFAULT_MAX_ITEMS, len(ref_items)) + 1))
    icon_overrides = config.get("icons", {})
    icons = [icon_overrides.get(str(i), icon_for(ref_items[i - 1][0])) for i in picks]

    locales = {}
    for locale, (headline, items) in notes.items():
        if len(items) != len(ref_items):
            print(f"  {version}/{locale}: {len(items)} items vs {len(ref_items)} in {REFERENCE_LOCALE}, skipped")
            continue
        locales[locale] = {
            "headline": headline,
            "items": [
                {"id": f"item{i}", "icon": icon, "title": items[i - 1][0], "subtitle": items[i - 1][1]}
                for i, icon in zip(picks, icons)
            ],
        }
    print(f"  {version}: {len(locales)} locales, items {picks}")
    return {"locales": locales}


def main():
    config_all = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    versions = sys.argv[1:] or available_versions()[-MAX_VERSIONS:]

    catalog = {"schema": 1, "versions": {}}
    for version in versions:
        built = build_version(version, config_all.get(version, {}))
        if built:
            catalog["versions"][version] = built

    payload = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    for out in OUTPUTS:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
