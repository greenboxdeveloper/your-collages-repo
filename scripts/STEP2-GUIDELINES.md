# Step 2: Python Script – Guidelines

Use this with the **collages repo** (the GitHub repo that holds SVGs and `classic_and_stylish_layouts.json`). Step 1 is done (JSON is in the repo). This step adds the script and (optionally) runs it locally before wiring the GitHub Action.

---

## 1. Where to put the files (in the collages repo)

- **`scripts/generate_enhanced_manifest.py`** – main script (copy from this project’s `scripts/` folder).
- **`scripts/requirements.txt`** – Python dependencies.

Repo root should already contain:

- **`classic_and_stylish_layouts.json`** (from Step 1).
- **`collages/`** (or **`svgs/`**) – folder with `.svg` files.

The script will **create** (do not commit these by hand):

- **`enhanced_manifest.json`** at repo root.
- **`thumbnails/`** at repo root, with one `.jpg` per layout.

---

## 2. Install dependencies (local run)

From the **collages repo** root:

```bash
pip install -r scripts/requirements.txt
```

Or:

```bash
pip install svgelements Pillow
```

- **svgelements** – parse SVG (viewbox, Path, Rect, Polygon, Circle, Ellipse) and get normalized rects + path data.
- **Pillow** – draw thumbnail images (slot rects).

---

## 3. Run the script (local)

From the **collages repo** root, set the base URL to your repo’s raw URL (replace `OWNER` and `REPO` and branch if needed):

```bash
python scripts/generate_enhanced_manifest.py --base-url "https://raw.githubusercontent.com/OWNER/REPO/main"
```

Example:

```bash
python scripts/generate_enhanced_manifest.py --base-url "https://raw.githubusercontent.com/greenboxdeveloper/your-collages-repo/main"
```

Optional arguments (defaults are for repo root):

| Argument | Default | Description |
|----------|---------|-------------|
| `--base-url` | (required) | Raw GitHub base URL (no trailing slash). |
| `--repo-root` | `.` | Repo root (where JSON and output paths are relative to). |
| `--json-path` | `classic_and_stylish_layouts.json` | Path to classic + stylish JSON (relative to repo root or absolute). |
| `--svg-dir` | `collages` | Folder containing `.svg` files (relative to repo root). Use `svgs` if your folder is named that. |
| `--output` | `enhanced_manifest.json` | Output manifest path (relative to repo root). |
| `--thumbnails-dir` | `thumbnails` | Output folder for thumbnail images. |

Example with custom paths:

```bash
python scripts/generate_enhanced_manifest.py \
  --base-url "https://raw.githubusercontent.com/greenboxdeveloper/your-collages-repo/main" \
  --svg-dir svgs \
  --output enhanced_manifest.json
```

---

## 4. What the script does

1. **Load** `classic_and_stylish_layouts.json`  
   - Reads `classic_layouts` and `stylish_layouts`.  
   - For each layout: adds `type` (`"grid"` if no slot has `path_data`, else `"organic"`) and `thumbnailURL` (`{base_url}/thumbnails/{id}.jpg`).  
   - Ensures each slot has `id` and `n_rect`; keeps `path_data` when present.

2. **Parse SVGs** from `collages/` (or `svgs/`).  
   - For each `.svg`: uses **svgelements** to get viewbox and elements (Path, Rect, Polygon, Circle, Ellipse).  
   - For each element: normalized rect `[n_x, n_y, n_w, n_h]` and optional `path_data` (e.g. from Path).  
   - One layout per file: `id` = `svg_{filename_stem}` (to avoid clashing with classic/stylish ids), `name` = humanized filename, `category` = `"Stylish"`, `type` = `"organic"` or `"grid"`.  
   - Appends to the same `layouts` list.

3. **Thumbnails**  
   - Creates `thumbnails/` if needed.  
   - For **every** layout (classic, stylish, SVG): draws a 300×300 image with one colored rectangle per slot (from `n_rect`). Saves as `thumbnails/{id}.jpg`.

4. **Write** `enhanced_manifest.json` with `version: "2.0"` and `layouts: [ ... ]`.

---

## 5. Checking the result

- Open **`enhanced_manifest.json`**: `version` should be `"2.0"`, and `layouts` should list classic, stylish, and SVG-derived entries, each with `thumbnailURL` and `slots`.
- Open **`thumbnails/`**: one `.jpg` per layout id (e.g. `classic_1.jpg`, `stylish_Collage2_1.jpg`, `svg_myfile.jpg`).
- Leave **`manifest.json`** and **`premium-config.json`** unchanged; the script does not touch them.

---

## 6. Next (Step 3)

After the script runs successfully locally, add the **GitHub Action** (`.github/workflows/generate-manifest.yml`) so that on every push (to the right paths), the script runs and commits `enhanced_manifest.json` and `thumbnails/`.
