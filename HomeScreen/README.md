# Home screen configuration (`home_config.json`)

This folder holds the **server-driven home layout** for Photo Collage. Upload these files to your OTA CDN (same base URL as other remote content).

## Deployment

| Item | Location |
|------|----------|
| **Remote path** | Firebase Remote Config key `home_config_path` (default: `HomeScreen/home_config.json`) |
| **Bundled fallback** | App ships `PhotoCollage/Resource/home_config.json` if remote fails |
| **On device** | Downloaded copy cached as `Caches/home_config.json` |

**Load order:** OTA JSON → disk cache → bundled fallback.

### Validate before upload

From the project root:

```bash
python3 scripts/validate_home_config.py path/to/home_config.json
```

Exits with an error if JSON is invalid or required rules are broken.

---

## Top-level structure

```json
{
  "version": "1.0",
  "sections": [ ],
  "randomize": { }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `version` | Recommended | Your own version string (e.g. `"1.0"`) |
| `sections` | **Yes** | Ordered list of home blocks (top → bottom) |
| `randomize` | No | Optional “Discover More” row |

---

## Section object

Every entry in `sections` must include:

| Key | Type | Description |
|-----|------|-------------|
| `id` | string | **Unique** section id (no duplicates) |
| `title` | string | Section heading |
| `subtitle` | string | Subheading (`""` if none) |
| `display_size` | string | `"hero"`, `"medium"`, or `"small"` |
| `category` | string | What to show (see tables below) |

### Optional keys

| Key | Type | Description |
|-----|------|-------------|
| `sub_category` | string | Narrows templates, store, or filter items |
| `count` | integer or `"all"` | How many items to show |
| `item` | string | Single item id/name — **only when `count` is `1`** |

### `display_size` (dynamic / catalog rows)

| Value | UI |
|-------|-----|
| `hero` | Full-width promo (stacked if multiple cards) |
| `medium` | Full-width banner height |
| `small` | Fixed-height horizontal scroller; tile **width** follows each preview’s aspect ratio |

Reserved built-in rows (`slideshow`, `classic_layouts`, etc.) use their own UI; `display_size` is mostly informational for those.

---

## Reserved categories (built-in UI)

These use **existing home blocks**. No image URLs in JSON — content comes from the app or other manifests.

| `category` | What appears |
|------------|----------------|
| `slideshow` | Photo slideshow / hero strip |
| `tools_grid` | “Collage Options” row (Classic / Stylish **buttons**, Generate, Template, etc.) |
| `template_store_row` | Template store entry row |
| `popular_layouts` | **Trending** layouts (usage-based; classic + stylish mixed) |
| `made_for_you` | Curated For You (requires photo library access) |
| `continue_projects` | Continue where you left off |
| `classic_layouts` | Horizontal scroll of **Classic** layouts from `enhanced_manifest.json` (`category == "Classic"`) |
| `stylish_layouts` | Horizontal scroll of **Stylish** layouts (`category != "Classic"`) |

### Classic / Stylish layout rows

```json
{
  "id": "classic_layouts",
  "title": "Classic Collage",
  "subtitle": "Grid layouts from the catalog",
  "display_size": "small",
  "category": "classic_layouts",
  "count": 12
}
```

```json
{
  "id": "stylish_layouts",
  "title": "Stylish Collage",
  "subtitle": "Creative shapes and styles",
  "display_size": "small",
  "category": "stylish_layouts",
  "count": 12
}
```

| Note | Detail |
|------|--------|
| Data source | `enhanced_manifest.json` on OTA (not template store) |
| `count` | Max tiles in the carousel; `"all"` allowed; app caps at **28** per row for performance |
| `sub_category` / `item` | **Not supported** — do not use |
| Empty row | Section hidden until manifest layouts are loaded |
| **See All** | Opens full Classic or Stylish layout browser |

**Do not confuse** with template store:

```json
"category": "templates",
"sub_category": "Classic"
```

That refers to the **template manifest** category named Classic, not enhanced layout collages.

---

## Dynamic categories (manifest-driven)

For any `category` not in the reserved list, the app builds cards from existing loaders. Matching is **case-insensitive**: manifest **id** → **display name** → **OTA folder name**.

**No URLs in JSON** — thumbnails and taps come from template / store / filter manifests.

### Templates

```json
{
  "id": "featured_templates",
  "title": "Featured Templates",
  "subtitle": "Holiday picks",
  "display_size": "small",
  "category": "templates",
  "sub_category": "Holiday 3",
  "count": 8
}
```

| Field | Meaning |
|-------|---------|
| `category` | `"templates"` or a template category name from the template manifest |
| `sub_category` | Template category folder / name (e.g. `"Classic"`, `"Holiday 3"`) |
| `count` | Number of templates, or `"all"` |
| `item` | With `"count": 1`, one template id / title / recipe / preview name |

**Tap:** opens collage editor with that template.

### Store sections

`category` must be one of:

`filters` · `frames` · `stickers` · `backgrounds` · `shapes` · `fonts`

#### Category row (one tile per store category)

```json
{
  "id": "sticker_packs",
  "title": "Sticker Packs",
  "subtitle": "",
  "display_size": "small",
  "category": "stickers",
  "sub_category": "Cute",
  "count": 8
}
```

#### Filter **items** row (before/after previews)

Use `category: "filters"` and `sub_category` = filter **category** id or name (e.g. `"film"`, `"Color Boost"`):

```json
{
  "id": "film_filters",
  "title": "Film Looks",
  "subtitle": "",
  "display_size": "small",
  "category": "filters",
  "sub_category": "film",
  "count": 10
}
```

**Tap:** opens In-App Store at the matching section / category / item.

### Single hero item

```json
{
  "id": "hero_pick",
  "title": "Editor's Pick",
  "subtitle": "Try this layout",
  "display_size": "hero",
  "category": "templates",
  "sub_category": "Classic",
  "count": 1,
  "item": "template_id_from_manifest"
}
```

---

## `randomize` — Discover More

Shows OTA templates and store items **not already featured** in `sections`, shuffled on launch.

```json
"randomize": {
  "enabled": true,
  "title": "Discover More",
  "subtitle": "",
  "display_size": "small",
  "template_count": 6,
  "store_count": 6
}
```

| Field | Description |
|-------|-------------|
| `enabled` | `true` to show the row |
| `title` / `subtitle` | Header text |
| `display_size` | Usually `"small"` |
| `template_count` | Max template cards (integer or `"all"`) |
| `store_count` | Max store cards (integer or `"all"`) |

Combined limit: `template_count + store_count` from one shuffled pool. Only **remote (OTA)** catalog items are eligible.

---

## Full example

```json
{
  "version": "1.0",
  "sections": [
    {
      "id": "slideshow",
      "title": "Welcome",
      "subtitle": "",
      "display_size": "hero",
      "category": "slideshow"
    },
    {
      "id": "tools",
      "title": "Your Collage",
      "subtitle": "Pick a style and start",
      "display_size": "medium",
      "category": "tools_grid"
    },
    {
      "id": "template_store",
      "title": "Template Store",
      "subtitle": "Browse curated templates",
      "display_size": "medium",
      "category": "template_store_row"
    },
    {
      "id": "trending",
      "title": "Trending Layouts",
      "subtitle": "",
      "display_size": "small",
      "category": "popular_layouts"
    },
    {
      "id": "continue",
      "title": "Continue Projects",
      "subtitle": "Pick up where you left off",
      "display_size": "small",
      "category": "continue_projects"
    },
    {
      "id": "curated",
      "title": "Curated For You",
      "subtitle": "",
      "display_size": "small",
      "category": "made_for_you"
    },
    {
      "id": "classic_layouts",
      "title": "Classic Collage",
      "subtitle": "Grid layouts from the catalog",
      "display_size": "small",
      "category": "classic_layouts",
      "count": 12
    },
    {
      "id": "stylish_layouts",
      "title": "Stylish Collage",
      "subtitle": "Creative shapes and styles",
      "display_size": "small",
      "category": "stylish_layouts",
      "count": 12
    },
    {
      "id": "featured_templates",
      "title": "Featured Templates",
      "subtitle": "",
      "display_size": "small",
      "category": "templates",
      "sub_category": "Classic",
      "count": 8
    },
    {
      "id": "film_filters",
      "title": "Film",
      "subtitle": "",
      "display_size": "small",
      "category": "filters",
      "sub_category": "film",
      "count": 10
    }
  ],
  "randomize": {
    "enabled": true,
    "title": "Discover More",
    "subtitle": "",
    "display_size": "small",
    "template_count": 6,
    "store_count": 6
  }
}
```

---

## Quick reference — `category` values

| `category` | Source |
|------------|--------|
| `slideshow` | Built-in |
| `tools_grid` | Built-in |
| `template_store_row` | Built-in |
| `popular_layouts` | Built-in (trending) |
| `made_for_you` | Built-in |
| `continue_projects` | Built-in |
| `classic_layouts` | `enhanced_manifest.json` (Classic) |
| `stylish_layouts` | `enhanced_manifest.json` (non-Classic) |
| `templates` | Template manifest |
| `filters` / `stickers` / `frames` / `backgrounds` / `shapes` / `fonts` | Store manifests |

---

## Rules and common mistakes

1. **Unique `id`** per section.
2. **`item` only with `count: 1`.**
3. **`count`** must be a positive integer or `"all"`.
4. **No URLs** in home JSON.
5. **Category names must match manifests** — typos result in empty sections (hidden on home).
6. **Section order** = array order in `sections`.
7. **Empty dynamic sections are hidden** — no header if zero cards resolve.

| Mistake | Fix |
|---------|-----|
| Using `"Backgrounds"` as `category` for templates | Use `"templates"` + correct `sub_category` |
| Expecting `classic_layouts` from template JSON | Use `category: "classic_layouts"` for enhanced layouts |
| `sub_category` on `classic_layouts` / `stylish_layouts` | Remove — not used |
| Confusing `tools_grid` with layout carousels | `tools_grid` = buttons only; use `classic_layouts` / `stylish_layouts` for thumbnails |

---

## Files in this folder (CDN)

| File | Purpose |
|------|---------|
| `home_config.json` | Live home layout (upload this) |
| `README.md` | This authoring guide (optional on CDN; for your team) |

After changing Remote Config or OTA JSON, users get updates on next app launch / config refresh (cached copy used offline).
