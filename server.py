#!/usr/bin/env python3
"""
Cosmic Pets — Full Pipeline Server
Handles: background removal (BiRefNet) + background generation (FLUX 2 Pro) + compositing (Pillow)

Run:   python3 server.py
Open:  http://localhost:8080
"""

import base64, glob, io, json, os, pathlib, tempfile, time, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from PIL import Image, ImageFilter
from rembg import remove as rembg_remove, new_session as rembg_new_session

# ── CONFIG ──────────────────────────────────────────────────────────────────
API_KEY      = os.environ.get("REPLICATE_API_TOKEN", "")
PORT         = int(os.environ.get("PORT", 8080))
REFS_FOLDER    = pathlib.Path(__file__).parent / "references"   # put your 8 portraits here
GALLERY_FOLDER = pathlib.Path(__file__).parent / "gallery"       # auto-saved last portraits
GALLERY_MAX    = 6                                                # keep last N portraits
OUTPUT_W, OUTPUT_H = 1024, 1440

# ── REMBG — pre-load once at startup, limit to one concurrent inference ──────
# u2netp is ~30MB vs u2net's 176MB — much lighter, still great for pets
print("  🔄 Pre-loading rembg model (u2netp)…")
REMBG_SESSION = rembg_new_session('u2netp')
REMBG_LOCK    = threading.Semaphore(1)   # only one rembg inference at a time
print("  ✅ rembg model ready")

# ── VIBE PROMPTS ─────────────────────────────────────────────────────────────
BASE_PROMPT = """Surreal photographic collage artwork, portrait orientation.
Composed of real photographic cutouts layered together, alien planet with visible surface texture,
ancient architecture, natural organic forms —
composited against a deep cosmic background of nebulae and star fields.
No animals, no pets, no cats, no dogs.
Elements layered in front of and behind a soft luminous central void, leaving space for a subject.
Rich saturated colours with intentional colour grading. Multiple ethereal light sources, glows, and halos.
Deep blacks with vivid colour accents.
Every element looks like a real photograph, space photography, cutout and placed by hand with multiply and screen blending modes.
No illustration, no painting, no cartoon, no flat AI smoothness.
No temples or religious symbols or religious buildings.
Photographic grain and texture throughout.
Otherworldly yet grounded entirely in the natural and cosmic world. Milky Way starry night.
Elements layered at multiple depths, some botanical forms overlapping in the foreground,
cosmic elements receding and floating into the background. No single focal plane.
A lush crescent garland of botanical flowers and leaves arranged at the lower base of the central void,
wrapping around the bottom of the glowing circle like a floral nest — flowers cascading and overlapping
in front of the lower portion of the void, dense and richly layered.
The central void is empty, ready for a subject to be placed in post-production.
A soft warm halo at the centre, golden light, not overexposed, gentle ethereal radiance."""

VIBE_PROMPTS = {
    "midnight": (
        "Deep navy and black palette. Cosmic starry deep space. "
        "A glowing cosmic portal at the centre — a radiant circular event horizon like a black hole gateway, "
        "luminous silver-white ring of light surrounding a pitch black void opening to another dimension. "
        "Exactly one single silver full moon only — no other moons, no second moon, no partial moons anywhere in the image. "
        "One ringed planet visible in the background. "
        "Black flowers and red botanicals. Mysterious low light. Silver and indigo accents. "
        "No ruins, no architecture, no mermaids, no vehicles, no rockets, no technology, no people."
    ),
    "celestial": (
        "Warm gold and sky blue palette. Ancient ringed planet. A silver warm moon. "
        "Warm nebula clouds. Regal and timeless atmosphere. Old architecture ruins. "
        "Gold leaf tones. Birds of paradise flying in the distance. "
        "No mermaids, no vehicles, no rockets, no technology, no people."
    ),
    "garden": (
        "Soft pinks and mauves. Blue skies with white soft fluffy clouds. "
        "Ancient ringed planets. Cherry blossom petals scattered. "
        "Butterflies and hummingbirds. Dreamy pastel cosmic light. "
        "Soft pink moons in the background. One peacock flying in the distance. "
        "No ruins, no architecture, no mermaids, no vehicles, no rockets, no technology, no people."
    ),
    "electric": (
        "Neon cyan and violet palette. Atmospheric energy. High contrast, vivid oversaturated cosmic light. "
        "Crystals and quartz. Ancient ringed colour saturated moons and planets. "
        "A small rocket ship taking off in the distance. "
        "No ruins, no architecture, no mermaids, no people."
    ),
    "ocean": (
        "Deep teal and midnight blue. Bioluminescent jellyfish. "
        "Coral forms. Tropical fish. "
        "Underwater and cosmic depth combined. "
        "A whale floating gently in the distance. "
        "No mermaids, no ruins, no architecture, no vehicles, no rockets, no technology, no people."
    ),
    "abstract": (
        "Bold overlapping geometric forms, maximalist layered collage, high contrast vivid multicolour, graphic pop energy. "
        "Stripes and geometric patterned circles. "
        "No people, no human figures, no faces, no bodies, no hands, no arms, no ruins, no architecture, no mermaids, no vehicles, no rockets."
    ),
}

# ── HELPERS ──────────────────────────────────────────────────────────────────
def replicate_get_latest_version(owner, model):
    """Fetch the latest version ID for a Replicate model."""
    url = f"https://api.replicate.com/v1/models/{owner}/{model}"
    req = urllib.request.Request(url, headers={"Authorization": f"Token {API_KEY}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return data["latest_version"]["id"]

def replicate_post(endpoint, payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        endpoint, data=data,
        headers={"Authorization": f"Token {API_KEY}", "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def replicate_poll(url, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        req = urllib.request.Request(url, headers={"Authorization": f"Token {API_KEY}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            pred = json.loads(r.read())
        status = pred.get("status")
        if status == "succeeded":
            return pred
        if status == "failed":
            raise RuntimeError(f"Replicate prediction failed: {pred.get('error')}")
        time.sleep(2)
    raise TimeoutError("Replicate prediction timed out")

def download_bytes(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()

def img_to_b64(path_or_bytes, fmt="PNG"):
    if isinstance(path_or_bytes, (str, pathlib.Path)):
        with open(path_or_bytes, "rb") as f:
            raw = f.read()
    else:
        raw = path_or_bytes
    b64 = base64.b64encode(raw).decode()
    mime = "jpeg" if fmt.lower() in ("jpg", "jpeg") else "png"
    return f"data:image/{mime};base64,{b64}"

def load_reference_images():
    """Load portrait reference images from /references folder as base64 data URIs."""
    refs = []
    if REFS_FOLDER.exists():
        for p in sorted(REFS_FOLDER.glob("*"))[:8]:
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                fmt = "jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "png"
                refs.append(img_to_b64(p, fmt))
    return refs

def composite_images(pet_b64: str, bg_url: str) -> str:
    """
    Composite the pet (transparent PNG) onto the cosmic background.
    Returns base64-encoded final PNG.
    """
    # Load background — cover-crop to fill canvas (preserves aspect ratio, no stretching)
    bg_bytes = download_bytes(bg_url)
    bg = Image.open(io.BytesIO(bg_bytes)).convert("RGBA")
    print(f"  🖼️  FLUX output size: {bg.width}×{bg.height}  ratio={bg.width/bg.height:.3f}")
    bg_ratio = bg.width / bg.height
    target_ratio = OUTPUT_W / OUTPUT_H
    if bg_ratio > target_ratio:
        # wider than target — fit height, crop width
        new_h = OUTPUT_H
        new_w = int(bg.width * OUTPUT_H / bg.height)
    else:
        # taller than target — fit width, crop height
        new_w = OUTPUT_W
        new_h = int(bg.height * OUTPUT_W / bg.width)
    bg = bg.resize((new_w, new_h), Image.LANCZOS)
    x = (new_w - OUTPUT_W) // 2
    y = (new_h - OUTPUT_H) // 2
    bg = bg.crop((x, y, x + OUTPUT_W, y + OUTPUT_H))

    # Load pet — apply EXIF orientation explicitly before anything else
    from PIL import ImageOps as _ImageOps
    pet_bytes = base64.b64decode(pet_b64.split(",")[1] if "," in pet_b64 else pet_b64)
    pet_raw = Image.open(io.BytesIO(pet_bytes))
    print(f"  📐 Pet PNG from rembg: {pet_raw.width}×{pet_raw.height}, mode={pet_raw.mode}")
    pet_raw = _ImageOps.exif_transpose(pet_raw)
    print(f"  📐 After exif_transpose: {pet_raw.width}×{pet_raw.height}")
    pet = pet_raw.convert("RGBA")

    # Detect if image came from the crop tool (square aspect ratio ≈ 1:1)
    # If so, skip bbox crop and portrait normalisation — trust the user's positioning.
    # If not (direct upload), bbox crop to remove rembg's transparent padding.
    input_ratio = pet.width / pet.height if pet.height > 0 else 1
    from_crop_tool = 0.88 <= input_ratio <= 1.14   # square ± ~13%

    if from_crop_tool:
        print(f"  📐 Square crop detected (ratio={input_ratio:.3f}) — preserving user positioning, skipping bbox crop")
    else:
        bbox = pet.getbbox()
        print(f"  📐 Bounding box: {bbox}")
        if bbox:
            pet = pet.crop(bbox)
        print(f"  📐 After bbox crop: {pet.width}×{pet.height}  ratio={pet.width/pet.height:.3f}")

        # Portrait normalisation — only for direct uploads (wide/landscape pets)
        if pet.width > pet.height * 0.85:
            target_w = int(pet.height * 0.75)
            if target_w < pet.width:
                x_offset = (pet.width - target_w) // 2
                pet = pet.crop((x_offset, 0, x_offset + target_w, pet.height))
                print(f"  📐 After portrait crop: {pet.width}×{pet.height}  ratio={pet.width/pet.height:.3f}")

    # Scale pet — max 68% of width, max 65% of height, aspect ratio strictly preserved
    scale = min((OUTPUT_W * 0.68) / pet.width, (OUTPUT_H * 0.65) / pet.height)
    pw, ph = int(pet.width * scale), int(pet.height * scale)
    print(f"  📐 After scale (×{scale:.3f}): {pw}×{ph}  ratio={pw/ph:.3f}")
    pet = pet.resize((pw, ph), Image.LANCZOS)

    # ── Soft circular vignette mask ──────────────────────────────────────────
    # Fades the pet's edges into a soft oval so it sits naturally inside the
    # glowing halo rather than looking like a hard cutout pasted on top.
    # The oval is centred slightly above vertical-centre to favour the face/head.
    from PIL import ImageDraw as _MaskDraw, ImageChops as _Chops
    oval_mask = Image.new("L", (pw, ph), 0)
    cx, cy = pw // 2, int(ph * 0.42)          # head-biased vertical centre
    rx = int(pw * 0.46)                        # horizontal radius ~92% of width
    ry = int(ph * 0.46)                        # vertical radius ~92% of height
    _MaskDraw.Draw(oval_mask).ellipse(
        [cx - rx, cy - ry, cx + rx, cy + ry], fill=255
    )
    # Very slight soften on the edge — just enough to avoid a pixel-hard cut
    oval_mask = oval_mask.filter(ImageFilter.GaussianBlur(radius=3))
    # Multiply with rembg alpha so we keep the existing clean cutout shape
    existing_alpha = pet.split()[3]
    blended_alpha = _Chops.multiply(existing_alpha, oval_mask)
    r, g, b, _ = pet.split()
    pet = Image.merge("RGBA", (r, g, b, blended_alpha))
    print(f"  🔵 Oval mask applied (rx={rx}, ry={ry}, cy={cy})")

    # Position: lock the garland anchor at canvas 76% (y≈1094 on 1440px —
    # firmly below the white-halo zone which sits around y=700–1000),
    # then work backwards so the oval-mask bottom (ph*0.88) lands exactly there.
    # This makes the cat always appear nestled right into the flowers
    # regardless of how tall/short the pet image is.
    garland_center_y = int(OUTPUT_H * 0.76)
    px = (OUTPUT_W - pw) // 2
    py = max(0, garland_center_y - int(ph * 0.88))

    # ── Edge bloom — subtle coloured light radiating from the pet's silhouette ──
    # This replaces the "heaven spotlight" look with a natural cosmic aura.
    # We blur the pet's alpha outward and tint it with a warm amber colour,
    # at low opacity so it feels like the cosmic background is lighting the pet.
    alpha = pet.split()[3]
    bloom_alpha = alpha.filter(ImageFilter.GaussianBlur(radius=22))
    # Tint: warm amber that works across all vibes without reading as "heaven"
    bloom = Image.new("RGBA", (pw, ph), (255, 200, 120, 0))
    bloom.putalpha(bloom_alpha)
    bloom_layer = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    bloom_layer.paste(bloom, (px, py), bloom_alpha)
    # ─────────────────────────────────────────────────────────────────────────

    # Composite: background → edge bloom (soft) → pet (clean cutout)
    result = bg.copy()
    result = Image.alpha_composite(result, bloom_layer)

    pet_layer = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    pet_layer.paste(pet, (px, py), alpha)
    result = Image.alpha_composite(result, pet_layer)

    # ── Foreground strip — bottom 30% of the FLUX background composited ON TOP of the pet ──
    # This makes botanicals/flowers/coral at the bottom of the FLUX image appear
    # to cross in front of the pet's lower body, creating natural depth layering.
    # A gradient mask fades the strip from transparent (top) to semi-opaque (bottom)
    # so the transition is seamless and never looks pasted on.
    strip_h = int(OUTPUT_H * 0.30)                    # bottom 30% of canvas
    fg_strip = bg.crop((0, OUTPUT_H - strip_h, OUTPUT_W, OUTPUT_H))
    # Build a vertical gradient mask: 0 (fully transparent) at top → ~160 (semi-opaque) at bottom
    from PIL import ImageDraw as _FgDraw
    grad_mask = Image.new("L", (OUTPUT_W, strip_h), 0)
    draw_grad = _FgDraw.Draw(grad_mask)
    for row in range(strip_h):
        opacity = int((row / strip_h) ** 2.0 * 160)   # quadratic ease-in, max ~63% opacity at bottom
        draw_grad.line([(0, row), (OUTPUT_W, row)], fill=opacity)
    fg_strip_rgba = fg_strip.convert("RGBA")
    fg_strip_rgba.putalpha(grad_mask)
    fg_layer = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    fg_layer.paste(fg_strip_rgba, (0, OUTPUT_H - strip_h))
    result = Image.alpha_composite(result, fg_layer)

    # ── Floral garland arch ──────────────────────────────────────────────────
    # garland_center_y was set above when calculating pet position.
    # src = dst: sample bg at the same y we paste — actual flowers, no halo.
    garland_h = int(OUTPUT_H * 0.38)
    g_top = max(0, garland_center_y - int(garland_h * 0.20))
    g_bot = min(OUTPUT_H, g_top + garland_h)
    g_h = g_bot - g_top

    if g_h > 20:
        g_strip = bg.crop((0, g_top, OUTPUT_W, g_bot)).convert("RGBA")
        from PIL import ImageDraw as _ArchDraw
        arch_mask = Image.new("L", (OUTPUT_W, g_h), 0)
        acx = OUTPUT_W // 2
        arx = int(OUTPUT_W * 0.48)
        ary = int(g_h * 0.80)
        _ArchDraw.Draw(arch_mask).ellipse(
            [acx - arx, g_h - ary, acx + arx, g_h + ary], fill=252
        )
        arch_mask = arch_mask.filter(ImageFilter.GaussianBlur(radius=int(g_h * 0.08)))
        g_strip.putalpha(arch_mask)
        garland_layer = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
        garland_layer.paste(g_strip, (0, g_top))
        result = Image.alpha_composite(result, garland_layer)
        print(f"  🌸 Garland at oval bottom y={garland_center_y}, strip y={g_top}–{g_bot}")
    # ─────────────────────────────────────────────────────────────────────────

    # Watermark — logo image
    logo_path = pathlib.Path(__file__).parent / "logo" / "cosmic-pets-logo@2x.png"
    watermark = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        wm_w = 200
        wm_h = int(logo.height * wm_w / logo.width)
        logo = logo.resize((wm_w, wm_h), Image.LANCZOS)
        # Apply 90% opacity
        r, g, b, a = logo.split()
        a = a.point(lambda x: int(x * 0.90))
        logo = Image.merge("RGBA", (r, g, b, a))
        margin = 16
        lx = OUTPUT_W - wm_w - margin
        ly = OUTPUT_H - wm_h - margin
        # Subtle dark backing behind logo so it reads on any background
        from PIL import ImageDraw as _ImageDraw
        pad = 10
        pill = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
        _ImageDraw.Draw(pill).rectangle(
            [lx - pad, ly - pad, lx + wm_w + pad, ly + wm_h + pad],
            fill=(0, 0, 0, 100)
        )
        watermark = Image.alpha_composite(watermark, pill)
        watermark.paste(logo, (lx, ly), logo)
    else:
        # Fallback: text watermark
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(watermark)
        txt = "✦ COSMIC PETS"
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
        except Exception:
            font = ImageFont.load_default()
        tbbox = draw.textbbox((0, 0), txt, font=font)
        tw, th = tbbox[2] - tbbox[0], tbbox[3] - tbbox[1]
        margin = 18
        wx = OUTPUT_W - tw - margin * 2
        wy = OUTPUT_H - th - margin * 2
        draw.rectangle([wx - 8, wy - 6, wx + tw + 8, wy + th + 6], fill=(0, 0, 0, 100))
        draw.text((wx, wy), txt, font=font, fill=(255, 255, 255, 200))
    result = Image.alpha_composite(result, watermark)

    # Encode and return
    buf = io.BytesIO()
    result.convert("RGB").save(buf, format="JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ── HTTP HANDLER ─────────────────────────────────────────────────────────────
class CosmicHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "?"
        print(f"  [{status}] {args[0]}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path == "/api/gallery":
            self._gallery()
        elif self.path == "/" or self.path == "":
            self.path = "/cosmic-pets-prototype.html"
            super().do_GET()
        else:
            super().do_GET()

    def _gallery(self):
        """Return last GALLERY_MAX portraits as base64 images, newest first."""
        images = []
        if GALLERY_FOLDER.exists():
            for p in sorted(GALLERY_FOLDER.glob("portrait_*.jpg"), reverse=True)[:GALLERY_MAX]:
                b64 = "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()
                images.append(b64)
        self._json_response({"images": images})

    def do_POST(self):
        routes = {
            "/api/remove-bg":   self._remove_bg,
            "/api/generate-bg": self._generate_bg,
            "/api/composite":   self._composite,
        }
        handler = routes.get(self.path)
        if handler:
            handler()
        else:
            self.send_error(404)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, status=500):
        print(f"  ❌ {msg}")
        self._json_response({"error": msg}, status)

    # ── /api/remove-bg ──
    def _remove_bg(self):
        try:
            body = self._read_json()
            image_b64 = body.get("image")  # base64 data URI
            if not image_b64:
                return self._error("Missing image", 400)

            print("  🐾 Removing background locally via rembg…")
            t0 = time.time()

            # Decode image
            from PIL import ImageOps
            raw = base64.b64decode(image_b64.split(",")[1] if "," in image_b64 else image_b64)

            # Fix EXIF rotation (iPhone photos are often stored sideways with rotation metadata)
            # and resize to a sensible working size before rembg
            img_in = Image.open(io.BytesIO(raw))
            img_in = ImageOps.exif_transpose(img_in)   # apply EXIF orientation
            img_in = img_in.convert("RGB")

            # Resize to max 1200px on the long side — faster rembg, no memory issues
            max_dim = 1200
            if max(img_in.width, img_in.height) > max_dim:
                r = max_dim / max(img_in.width, img_in.height)
                img_in = img_in.resize(
                    (int(img_in.width * r), int(img_in.height * r)), Image.LANCZOS
                )

            buf_in = io.BytesIO()
            img_in.save(buf_in, format="PNG")
            raw = buf_in.getvalue()

            # Run rembg locally — semaphore prevents concurrent calls that would OOM
            with REMBG_LOCK:
                png_bytes = rembg_remove(raw, session=REMBG_SESSION)

            t = time.time() - t0
            result_b64 = "data:image/png;base64," + base64.b64encode(png_bytes).decode()
            print(f"  ✅ Background removed in {t:.1f}s")

            # Quality check — measure how much of the image is detected pet
            quality_warning = None
            try:
                check = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
                pet_bbox = check.getbbox()
                if pet_bbox is None:
                    quality_warning = "We couldn't detect a pet in this photo. Try a clearer, well-lit photo with your pet filling most of the frame."
                    print("  ⚠️  Quality: no pet detected (empty mask)")
                else:
                    bbox_area = (pet_bbox[2] - pet_bbox[0]) * (pet_bbox[3] - pet_bbox[1])
                    coverage = bbox_area / (check.width * check.height)
                    print(f"  📊 Pet coverage: {coverage:.1%} of frame")
                    if coverage < 0.06:
                        quality_warning = "We had trouble detecting your pet clearly — dark fur against a dark background is tricky! For best results, try a well-lit photo where your pet stands out from the background."
                    elif coverage < 0.15:
                        quality_warning = "Your pet looks a little small in this photo — moving closer or cropping in will give a more detailed portrait."
            except Exception as qe:
                print(f"  ⚠️  Quality check failed: {qe}")

            self._json_response({"pet_png": result_b64, "time": t, "warning": quality_warning})

        except Exception as e:
            self._error(str(e))

    # ── /api/generate-bg ──
    def _generate_bg(self):
        try:
            body   = self._read_json()
            vibe   = body.get("vibe", "celestial")
            prompt = BASE_PROMPT + "\n" + VIBE_PROMPTS.get(vibe, VIBE_PROMPTS["celestial"])

            refs = load_reference_images()
            print(f"  🌌 Generating '{vibe}' background via FLUX 2 Pro ({len(refs)} reference images)…")

            payload = {
                "input": {
                    "prompt":           prompt,
                    "aspect_ratio":     "3:4",   # portrait — Klein 4B uses this instead of width/height
                    "output_format":    "png",
                    "safety_tolerance": 2,
                }
            }
            if refs:
                payload["input"]["input_images"] = refs

            pred = replicate_post(
                "https://api.replicate.com/v1/models/black-forest-labs/flux-2-klein-4b/predictions",
                payload
            )
            pred = replicate_poll(pred["urls"]["get"], timeout=240)
            output_url = pred["output"]
            if isinstance(output_url, list):
                output_url = output_url[0]

            t = pred.get("metrics", {}).get("predict_time", 0)
            print(f"  ✅ Background generated in {t:.1f}s → {output_url}")
            self._json_response({"bg_url": str(output_url), "time": t})

        except Exception as e:
            self._error(str(e))

    # ── /api/composite ──
    def _composite(self):
        try:
            body    = self._read_json()
            pet_b64 = body.get("pet_png")
            bg_url  = body.get("bg_url")
            if not pet_b64 or not bg_url:
                return self._error("Missing pet_png or bg_url", 400)

            print("  🎨 Compositing…")
            result_b64 = composite_images(pet_b64, bg_url)
            print("  ✅ Composite complete")

            # Save to gallery (keep last GALLERY_MAX portraits)
            try:
                GALLERY_FOLDER.mkdir(exist_ok=True)
                ts = int(time.time() * 1000)
                img_data = base64.b64decode(result_b64.split(",")[1])
                (GALLERY_FOLDER / f"portrait_{ts}.jpg").write_bytes(img_data)
                # Prune oldest beyond GALLERY_MAX
                saved = sorted(GALLERY_FOLDER.glob("portrait_*.jpg"))
                for old in saved[:-GALLERY_MAX]:
                    old.unlink()
            except Exception as ge:
                print(f"  ⚠️  Gallery save failed: {ge}")

            self._json_response({"result": result_b64})

        except Exception as e:
            self._error(str(e))


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.chdir(pathlib.Path(__file__).parent)

    print(f"""
  ╔══════════════════════════════════════════╗
  ║   ✦  Cosmic Pets Pipeline Server  ✦     ║
  ╠══════════════════════════════════════════╣
  ║  http://localhost:{PORT}                    ║
  ║                                          ║
  ║  Endpoints:                              ║
  ║   POST /api/remove-bg    rembg (local)   ║
  ║   POST /api/generate-bg  FLUX 2 Klein 4B ║
  ║   POST /api/composite    Pillow          ║
  ║                                          ║
  ║  Reference images: ./references/         ║
  ║  ({len(load_reference_images())} image(s) loaded)                  ║
  ╚══════════════════════════════════════════╝
  Press Ctrl+C to stop.
""")
    ThreadingHTTPServer(("0.0.0.0", PORT), CosmicHandler).serve_forever()
