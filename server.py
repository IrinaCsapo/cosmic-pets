#!/usr/bin/env python3
"""
Cosmic Pets — Full Pipeline Server
Handles: background removal (BiRefNet) + background generation (FLUX 2 Pro) + compositing (Pillow)

Run:   python3 server.py
Open:  http://localhost:8080
"""

import base64, glob, hashlib, hmac as hmaclib, io, json, os, pathlib, sqlite3, tempfile, time, threading, urllib.error, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from PIL import Image, ImageFilter, ImageEnhance, ImageStat
from rembg import remove as rembg_remove, new_session as rembg_new_session

# ── CONFIG ──────────────────────────────────────────────────────────────────
API_KEY      = os.environ.get("REPLICATE_API_TOKEN", "")
PORT         = int(os.environ.get("PORT", 8080))
REFS_FOLDER    = pathlib.Path(__file__).parent / "references"   # put your 8 portraits here
GALLERY_FOLDER = pathlib.Path(__file__).parent / "gallery"       # auto-saved last portraits
GALLERY_MAX    = 6                                                # keep last N portraits
OUTPUT_W, OUTPUT_H = 1440, 2016

# ── STRIPE / PAYMENTS ────────────────────────────────────────────────────────
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
TOKEN_SECRET      = os.environ.get("TOKEN_SECRET", "change-me-in-production")

# Map pack size → Stripe Price ID (set these as Railway env vars)
PACK_PRICES = {
    "6":  os.environ.get("STRIPE_PRICE_6",  ""),
    "12": os.environ.get("STRIPE_PRICE_12", ""),
    "20": os.environ.get("STRIPE_PRICE_20", ""),
}
PACK_CREDITS = {"6": 6, "12": 12, "20": 20}

DB_PATH = pathlib.Path(__file__).parent / "payments.db"

def check_nsfw(image_b64):
    """
    Run the uploaded image through Falconsai NSFW detection on Replicate.
    Returns (is_nsfw: bool, label: str, score: float).
    Fails open — if the check itself errors, returns (False, 'unknown', 0).
    """
    if not API_KEY:
        return False, "unknown", 0
    try:
        pred = replicate_post(
            "https://api.replicate.com/v1/models/falconsai/nsfw_image_detection/predictions",
            {"input": {"image": image_b64}}
        )
        pred  = replicate_poll(pred["urls"]["get"], timeout=30)
        output = pred.get("output", {})
        if isinstance(output, dict):
            label = output.get("label", "normal").lower()
            score = float(output.get("score", 0))
        elif isinstance(output, str):
            label = output.lower()
            score = 1.0
        else:
            return False, "unknown", 0
        is_nsfw = label == "nsfw" and score > 0.80
        return is_nsfw, label, score
    except Exception as e:
        print(f"  ⚠️  NSFW check error (allowing through): {e}")
        return False, "unknown", 0

def _init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id       TEXT PRIMARY KEY,
                credits_remaining INTEGER NOT NULL,
                created_at       INTEGER NOT NULL
            )
        """)

def _db_get_credits(session_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT credits_remaining FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
    return row[0] if row else None

def _db_set_credits(session_id, credits):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, credits_remaining, created_at) VALUES (?,?,?)",
            (session_id, credits, int(time.time()))
        )

def _db_decrement(session_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE sessions SET credits_remaining = credits_remaining - 1 "
            "WHERE session_id=? AND credits_remaining > 0",
            (session_id,)
        )
        row = conn.execute(
            "SELECT credits_remaining FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
    return row[0] if row else 0

def _make_token(session_id):
    sig = hmaclib.new(TOKEN_SECRET.encode(), session_id.encode(), hashlib.sha256).hexdigest()
    return f"{session_id}.{sig}"

def _verify_token(token):
    parts = token.rsplit(".", 1)
    if len(parts) != 2:
        return None
    session_id, sig = parts
    expected = hmaclib.new(TOKEN_SECRET.encode(), session_id.encode(), hashlib.sha256).hexdigest()
    if not hmaclib.compare_digest(sig, expected):
        return None
    return session_id

def _stripe_request(method, endpoint, data=None):
    url = f"https://api.stripe.com/v1/{endpoint}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {STRIPE_SECRET_KEY}")
    if body:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            err_body = json.loads(raw)
            stripe_msg = err_body.get("error", {}).get("message", raw)
        except Exception:
            stripe_msg = raw
        print(f"  ❌ Stripe {method} {endpoint} → HTTP {e.code}: {stripe_msg}")
        raise RuntimeError(f"Stripe error ({e.code}): {stripe_msg}") from None

_init_db()

# ── REMBG — pre-load once at startup, limit to one concurrent inference ──────
# u2netp is ~30MB vs u2net's 176MB — much lighter, still great for pets
print("  🔄 Pre-loading rembg model (u2netp)…")
REMBG_SESSION = rembg_new_session('u2netp')
REMBG_LOCK    = threading.Semaphore(1)   # only one rembg inference at a time
print("  ✅ rembg model ready")

# ── VIBE PROMPTS ─────────────────────────────────────────────────────────────
BASE_PROMPT = """Surreal photographic collage artwork, portrait orientation.
Composed of real photographic cutouts layered together, alien planets with visible surface texture,
natural organic forms — composited against a deep cosmic background of nebulae and star fields.
No animals, no pets, no cats, no dogs.
Elements layered in front of and behind a large circular portal window at the centre.
Rich saturated colours. Every element looks like a real photograph, cutout and placed by hand.
No illustration, no painting, no cartoon.
No temples or religious symbols. No architecture.
Photographic grain throughout. Milky Way starry night.
A lush crescent garland surrounds the base of the portal in a semi-circle, wrapping around the bottom.
The central circle is a cosmic portal window filled with a mineral light moon surface — NOT white, NOT empty.
False-colour geological mapping, craters, richly textured.
A luminous glow ring frames the portal edge."""

VIBE_PROMPTS = {
    "midnight": (
        "The portal moon surface is deep indigo, cold silver-grey, and near-black — "
        "dark mineral mapping with silver crater rims and icy indigo plains. "
        "Deep navy and black palette. Cosmic starry deep space. "
        "A ringed planet in the background. "
        "Ancient Persepolis ruins emerging from darkness — moonlit stone columns, lamassu guardian statues with winged animal bodies, "
        "carved griffin friezes half-swallowed by shadow and indigo light. "
        "A distant black hole with a glowing crimson and deep violet accretion disk. "
        "A murder of crows — some perched on ruins, others in flight against the dark sky. "
        "Gothic ornamental motifs — wrought iron curls, thorn vines, iron candelabras. "
        "Garland of black roses, deep crimson peonies, dried black botanicals, and dark burgundy dahlias. "
        "Black flowers and red botanicals. Mysterious low light. Silver and indigo accents. "
        "No cherry blossoms, no pink flowers, no mermaids, no vehicles, no rockets, no technology, no people."
    ),
    "celestial": (
        "The portal moon surface glows in warm amber gold, burnished copper, and soft sky blue — "
        "golden highlands, luminous amber mineral plains, warm regal tones. "
        "Warm gold and sky blue palette. Ancient ringed planet. A silver warm moon. "
        "Warm nebula clouds. Regal and timeless atmosphere. Gold leaf tones. "
        "Ancient Persepolis ruins — teal-lit stone columns, lamassu winged guardian statues, carved animal friezes glowing in the dark. "
        "Peacocks with iridescent teal and gold tail feathers fully displayed in the scene. "
        "Birds of paradise flowers and birds in flight. "
        "A distant black hole with a luminous golden and amber accretion disk. "
        "Garland of gold and amber tropical flowers, birds of paradise blooms, and lush gold-dusted leaves. "
        "No cherry blossoms, no mermaids, no vehicles, no rockets, no technology, no people."
    ),
    "garden": (
        "The portal moon surface is soft rose pink, blush lavender, and pale peach — "
        "gentle dreamy pastel mineral mapping. "
        "Soft pinks and mauves. Blue skies with white soft fluffy clouds. "
        "Ancient ringed planets. Cherry blossom petals scattered. Dreamy pastel cosmic light. "
        "A soft pink moon in the background. "
        "Butterflies and hummingbirds. "
        "Peacocks with iridescent tail feathers fanned open. "
        "Persepolis ruins with ancient animal carvings — lamassu statues and carved animal friezes. "
        "Lush tropical greenery — giant monstera leaves, banana palms, bird-of-paradise plants, ferns cascading. "
        "No mermaids, no vehicles, no rockets, no technology, no people."
    ),
    "electric": (
        "The portal moon surface blazes in neon cyan, electric violet, acid yellow, and hot magenta — "
        "hyper-saturated false-colour geological mapping, vivid and electric. "
        "Neon cyan and violet palette. Atmospheric energy. High contrast, vivid oversaturated cosmic light. "
        "Crystals and quartz. Ancient ringed colour saturated moons and planets. "
        "A small rocket ship taking off in the distance. "
        "Garland of crystals and flowers — white quartz points, split-open rainbow geodes, amethyst clusters, "
        "tourmaline shards woven with vivid electric-coloured blooms, 70% crystals 30% flowers. "
        "Large freestanding crystal formations and geode slices emerging from the scene edges. "
        "No ruins, no architecture of any kind, no mermaids, no people."
    ),
    "ocean": (
        "The portal moon surface is deep ocean teal, midnight blue, and bioluminescent aquamarine — "
        "like a water world seen from space, dark oceanic depths with glowing teal patches. "
        "Deep teal and midnight blue. One ringed moon in the sky. "
        "Bioluminescent jellyfish. Coral forms. Tropical fish. Underwater and cosmic depth combined. "
        "A whale floating gently in the distance. "
        "Garland made entirely of ocean life — brain coral, staghorn coral, sea fans, "
        "sea anemones, kelp fronds, and vivid sea urchins. No flowers. "
        "Tropical reef fish — clownfish, angelfish, lionfish — swimming freely throughout the scene. "
        "No flowers, no cherry blossoms, no botanical plants, no terrestrial vegetation, "
        "no ruins, no mermaids, no vehicles, no rockets, no technology, no people."
    ),
    "abstract": (
        "Bold overlapping geometric forms, maximalist layered collage, high contrast vivid multicolour, graphic pop energy. "
        "Stripes and geometric patterned circles. "
        "Garland made of bold geometric shapes — coloured circles, triangles, hexagons, graphic forms. "
        "Soft graphic moons in the sky. "
        "No people, no human figures, no ruins, no mermaids, no vehicles, no rockets."
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

def auto_correct_photo(img: Image.Image):
    """
    Lightweight Pillow corrections applied before rembg.
    Returns (corrected_img, list_of_applied_corrections).
    """
    stat = ImageStat.Stat(img)
    avg_brightness = sum(stat.mean[:3]) / 3 / 255   # 0.0–1.0
    corrections = []

    # ── Brightness boost for dark photos ─────────────────────────────────────
    if avg_brightness < 0.32:
        factor = min(0.50 / max(avg_brightness, 0.01), 2.5)
        img = ImageEnhance.Brightness(img).enhance(factor)
        corrections.append(f"brightness×{factor:.2f}")

    # ── Contrast lift for flat / overcast photos ──────────────────────────────
    if avg_brightness > 0.75:
        img = ImageEnhance.Contrast(img).enhance(1.25)
        corrections.append("contrast×1.25")

    # ── Gentle sharpening — always helps rembg edge detection ─────────────────
    img = ImageEnhance.Sharpness(img).enhance(1.6)
    corrections.append("sharpen×1.6")

    return img, corrections


def enhance_with_nano_banana(img: Image.Image, reason: str = "") -> Image.Image:
    """
    Use Google's Gemini 2.5 Flash Image (nano-banana on Replicate) to clean up
    a problematic pet photo before feeding it to rembg.
    Only called when ENABLE_NANO_BANANA=1 is set in the environment AND the photo
    triggers a quality check (too dark, etc.).
    Falls back gracefully — returns original image on any error.
    """
    print(f"  🍌 nano-banana enhancement triggered ({reason})…")
    try:
        # Encode as JPEG for the API call
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        b64 = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

        prompt = (
            "This is a photo of a pet. Please enhance it for use as a professional "
            "portrait: correct underexposure, improve sharpness and detail, reduce noise, "
            "and neutralise any cluttered or distracting background — especially if the "
            "background colour is very similar to the pet's fur or feathers. "
            "Keep the pet's appearance completely natural and photorealistic. "
            "Do not cartoon-ify, stylise, or alter the pet itself in any way."
        )

        payload = {
            "input": {
                "prompt": prompt,
                "image_input": [b64],
                "output_format": "jpg",
            }
        }

        data = json.dumps(payload).encode()
        # Use Prefer: wait so fast responses come back in one round-trip
        req = urllib.request.Request(
            "https://api.replicate.com/v1/models/google/nano-banana/predictions",
            data=data,
            headers={
                "Authorization": f"Token {API_KEY}",
                "Content-Type": "application/json",
                "Prefer": "wait=55",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=65) as r:
            pred = json.loads(r.read())

        # If Prefer: wait returned a completed prediction use it directly,
        # otherwise fall through to polling
        if pred.get("status") != "succeeded":
            poll_url = pred["urls"]["get"]
            pred = replicate_poll(poll_url, timeout=120)

        out_url = pred["output"]
        if isinstance(out_url, list):
            out_url = out_url[0]

        img_bytes = download_bytes(out_url)
        enhanced = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        print(f"  ✅ nano-banana done: {enhanced.width}×{enhanced.height}")
        return enhanced

    except Exception as exc:
        print(f"  ⚠️  nano-banana failed ({exc}) — using original photo")
        return img


def enhance_with_realesrgan(img_b64: str) -> Image.Image:
    """
    Send the image to Replicate's Real-ESRGAN for AI upscaling / restoration.
    Returns a PIL Image. Falls back silently — caller should catch exceptions.
    """
    import urllib.request, json, time, base64, io

    print("  🤖 Running Real-ESRGAN enhancement via Replicate…")
    payload = {
        "version": "f121d640bd286e1fdc67f9799164c1d5be36ff74576ee11c803ae5b665dd46aa",
        "input": {
            "image":        img_b64,
            "scale":        2,
            "face_enhance": False,
        }
    }
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        "https://api.replicate.com/v1/predictions",
        data=data,
        headers={"Authorization": f"Token {API_KEY}", "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        pred = json.loads(r.read())

    # Poll
    poll_url = pred["urls"]["get"]
    deadline = time.time() + 90
    while time.time() < deadline:
        req2 = urllib.request.Request(poll_url, headers={"Authorization": f"Token {API_KEY}"})
        with urllib.request.urlopen(req2, timeout=15) as r:
            pred = json.loads(r.read())
        if pred["status"] == "succeeded":
            out_url = pred["output"]
            if isinstance(out_url, list): out_url = out_url[0]
            print(f"  ✅ Real-ESRGAN done → {out_url}")
            with urllib.request.urlopen(out_url, timeout=30) as r:
                return Image.open(io.BytesIO(r.read())).convert("RGB")
        if pred["status"] == "failed":
            raise RuntimeError(f"Real-ESRGAN failed: {pred.get('error')}")
        time.sleep(2)
    raise TimeoutError("Real-ESRGAN timed out")


def composite_images(pet_b64: str, bg_url: str, vibe: str = "") -> str:
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

    # Scale pet — max 90% of width, max 82% of height, aspect ratio strictly preserved
    scale = min((OUTPUT_W * 0.90) / pet.width, (OUTPUT_H * 0.82) / pet.height)
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
    # Gentle soften — just enough to avoid a pixel-hard cut at the edges
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
    garland_center_y = int(OUTPUT_H * 0.75)
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

    # ── Rembg garland — rembg cutout composited OVER pet ─────────────────────
    # Skipped for vibes where FLUX generates complex scenes at the bottom
    # (e.g. abstract) that rembg would extract as landscape/terrain rather
    # than a clean garland.  Those vibes rely solely on the FLUX foreground.
    # Rembg garland disabled for all vibes — FLUX generates the foreground
    # directly and rembg extraction was causing misfires across all vibes.
    use_rembg_garland = False

    garland_layer = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    if use_rembg_garland:
        garland_src_top = int(OUTPUT_H * 0.65)
        garland_src = bg.crop((0, garland_src_top, OUTPUT_W, OUTPUT_H)).convert("RGB")
        print(f"  🌸 Running rembg on garland source (y={garland_src_top}–{OUTPUT_H})…")
        buf_g = io.BytesIO()
        garland_src.save(buf_g, format="PNG")
        with REMBG_LOCK:
            garland_cut_bytes = rembg_remove(buf_g.getvalue(), session=REMBG_SESSION)
        garland_cut = Image.open(io.BytesIO(garland_cut_bytes)).convert("RGBA")
        # Fade the top 60px to kill any hard seam at the crop boundary
        gc_w, gc_h = garland_cut.size
        fade_px = min(60, gc_h // 4)
        r_ch, g_ch, b_ch, a_ch = garland_cut.split()
        import numpy as np
        a_arr = np.array(a_ch, dtype=np.float32)
        ramp = np.linspace(0.0, 1.0, fade_px)
        a_arr[:fade_px, :] *= ramp[:, np.newaxis]
        a_ch = Image.fromarray(a_arr.clip(0, 255).astype(np.uint8))
        garland_cut = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_ch))
        # Mirror horizontally so rembg layer is complementary to the FLUX bg garland
        garland_cut = garland_cut.transpose(Image.FLIP_LEFT_RIGHT)
        garland_place_y = int(OUTPUT_H * 0.53)
        garland_layer.paste(garland_cut, (0, garland_place_y), garland_cut.split()[3])
        print(f"  ✅ Garland cutout placed at y={garland_place_y} (flipped)")
    else:
        print(f"  ⏭️  Rembg garland skipped for vibe='{vibe}' — using FLUX foreground only")
    # ─────────────────────────────────────────────────────────────────────────

    # Composite: bg → bloom → pet → garland (rembg cutout over pet, if applicable)
    result = bg.copy()
    result = Image.alpha_composite(result, bloom_layer)

    pet_layer = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    pet_layer.paste(pet, (px, py), alpha)
    result = Image.alpha_composite(result, pet_layer)

    result = Image.alpha_composite(result, garland_layer)   # no-op for vibes that skip rembg
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
    result.convert("RGB").save(buf, format="JPEG", quality=97)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ── HTTP HANDLER ─────────────────────────────────────────────────────────────
class CosmicHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "?"
        print(f"  [{status}] {args[0]}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Credits-Token")

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path == "/api/gallery":
            self._gallery()
        elif self.path.startswith("/api/verify-payment"):
            self._verify_payment()
        elif self.path == "/" or self.path == "" or self.path.startswith("/?"):
            self.path = "/cosmic-pets-prototype.html"
            super().do_GET()
        elif self.path == "/reset":
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
            "/api/remove-bg":       self._remove_bg,
            "/api/generate-bg":     self._generate_bg,
            "/api/composite":       self._composite,
            "/api/create-checkout": self._create_checkout,
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

            # ── NSFW check ────────────────────────────────────────────────────
            print("  🔍 Running content safety check…")
            is_nsfw, nsfw_label, nsfw_score = check_nsfw(image_b64)
            if is_nsfw:
                print(f"  🚫 NSFW content detected (label={nsfw_label}, score={nsfw_score:.2f}) — rejecting")
                return self._json_response({
                    "error": "This image can't be used. Please upload a photo of your pet.",
                    "nsfw": True
                }, 400)
            print(f"  ✅ Content check passed (label={nsfw_label}, score={nsfw_score:.2f})")
            # ─────────────────────────────────────────────────────────────────

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

            # ── Step 1: Auto-correct brightness / contrast / sharpness ──────────
            img_in, corrections = auto_correct_photo(img_in)
            if corrections:
                print(f"  🎨 Auto-corrections applied: {', '.join(corrections)}")

            # ── Step 2: nano-banana AI enhancement (optional, gated by env flag) ──
            # Set ENABLE_NANO_BANANA=1 on Railway to activate.
            # Only fires for photos likely to cause rembg trouble:
            #   • Very dark  (avg brightness < 0.28)
            #   • Very blown-out / flat contrast (avg brightness > 0.82)
            # The model cleans up lighting, sharpness, and cluttered backgrounds
            # before rembg sees the image — dramatically improving cutout quality
            # on tricky user photos without touching normal good-quality shots.
            if os.environ.get("ENABLE_NANO_BANANA") == "1" and API_KEY:
                from PIL import ImageStat as _IStat
                _stat = _IStat.Stat(img_in)
                _avg_b = sum(_stat.mean[:3]) / 3 / 255
                if _avg_b < 0.28:
                    img_in = enhance_with_nano_banana(img_in, reason=f"dark photo (brightness={_avg_b:.2f})")
                elif _avg_b > 0.82:
                    img_in = enhance_with_nano_banana(img_in, reason=f"washed-out photo (brightness={_avg_b:.2f})")
            # ──────────────────────────────────────────────────────────────────

            # ── Step 3: Real-ESRGAN (DISABLED) ────────────────────────────────
            # Real-ESRGAN sharpens fur-edge boundaries in ways that confuse rembg,
            # leaving ghost halos around the pet. Keeping code for reference.
            # ──────────────────────────────────────────────────────────────────

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
    def _create_checkout(self):
        try:
            body = self._read_json()
            pack = str(body.get("pack", "12"))
            price_id = PACK_PRICES.get(pack)
            print(f"  💳 Checkout requested: pack={pack} price_id={price_id!r} key_prefix={STRIPE_SECRET_KEY[:8] if STRIPE_SECRET_KEY else 'MISSING'}...")
            if not price_id:
                self._json_response({"error": "Invalid pack or Stripe not configured"}, 400)
                return
            host   = self.headers.get("Host", "localhost")
            scheme = "https" if ("railway.app" in host or "cosmicpets" in host) else "http"
            base_url = f"{scheme}://{host}"
            session = _stripe_request("POST", "checkout/sessions", {
                "mode":                         "payment",
                "line_items[0][price]":         price_id,
                "line_items[0][quantity]":      "1",
                "metadata[pack]":               pack,
                "success_url":                  f"{base_url}/?session_id={{CHECKOUT_SESSION_ID}}",
                "cancel_url":                   f"{base_url}/",
            })
            self._json_response({"url": session["url"]})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _verify_payment(self):
        try:
            qs         = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            session_id = qs.get("session_id", [None])[0]
            if not session_id:
                self._json_response({"error": "Missing session_id"}, 400)
                return
            # Idempotent — if already verified, just return current credits
            existing = _db_get_credits(session_id)
            if existing is not None:
                self._json_response({"credits": existing, "token": _make_token(session_id)})
                return
            stripe_session = _stripe_request("GET", f"checkout/sessions/{session_id}")
            if stripe_session.get("payment_status") != "paid":
                self._json_response({"error": "Payment not completed"}, 402)
                return
            pack    = stripe_session.get("metadata", {}).get("pack", "12")
            credits = PACK_CREDITS.get(str(pack), 12)
            _db_set_credits(session_id, credits)
            self._json_response({"credits": credits, "token": _make_token(session_id)})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _generate_bg(self):
        # ── Token / credit check ──────────────────────────────────────────────
        token_header = self.headers.get("X-Credits-Token", "").strip()
        session_id   = None
        if token_header:
            session_id = _verify_token(token_header)
            if not session_id:
                self._json_response({"error": "Invalid payment token"}, 401)
                return
            credits = _db_get_credits(session_id)
            if credits is None or credits <= 0:
                self._json_response({"error": "No credits remaining"}, 402)
                return
            _db_decrement(session_id)
        # If no token present, free-tier request — server allows it (client enforces 3-try limit)
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

            vibe = body.get("vibe", "")
            print(f"  🎨 Compositing (vibe={vibe})…")
            result_b64 = composite_images(pet_b64, bg_url, vibe=vibe)
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
