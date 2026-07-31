#!/usr/bin/env python3
"""Arveno slide toolkit — runs in the Higgsfield sandbox.

Lives in this public repo so the sandbox can fetch it instead of having it
pasted into every call:

    curl -sO https://raw.githubusercontent.com/charlie1906clark-del/arveno-content/main/tools/arveno_slides.py

Commands
--------
  build   <manifest.json>                     whole carousel: fetch, gate, letter, sheet
  upload  <manifest.json>                     PUT finished slides to presigned URLs
  letter  <art.png> <out.webp> "LINE ONE|LINE TWO" [badge]
  card    <out.webp> "HEADING" "rule one|rule two|..."
  brand   <img> [img ...]                     palette gate — PASS/FAIL, numeric
  sheet   <out.png>  <img> [img ...]          contact sheet for inspection
  probe   <img> [img ...]                     per-image stats, no transfer

`build` and `upload` exist because a day of five carousels was costing roughly
forty tool round-trips — a curl and a composite and a check per slide, each one
a chance to lose the sandbox between calls (it is discarded seconds after a
command returns). Both take a manifest and do the whole batch in one command,
so a day costs four calls instead of forty. Art generation stays outside: it
goes through the Higgsfield MCP, which the sandbox cannot reach.

`brand` is the gate that never existed. The 2026-07-29 journal entry says a
style guide is not implemented until a rendered artefact has been diffed
against it, and the diff nobody could do by eye is trivial by number: quantize
the frame and check how much of it lands on the brand palette. It caught a
character sheet that was 70% cream and one terracotta — no ink, no orange, no
amber — which a thumbnail glance would have waved through as "flat and warm".
Numbers also survive the transfer described below, and thumbnails often don't.

`sheet` is the eyes-on step, for the things a palette cannot see: figure count,
anatomy, stray text. Both Higgsfield CDNs are blocked from the build sandbox,
so a slide reaches a reviewer as base64 hand-carried out of tool output. That
transfer is lossy above roughly 3 KB, and lossier still for JPEG, whose base64
carries long runs of one repeated character. So `sheet` emits a colour-quantized
PNG, shrinks it until it fits the budget, and prints an md5 so a bad copy is
detectable instead of silently trusted. Check `brand` first — it is free.
"""
import hashlib
import re
import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

INK = (35, 26, 62)
CREAM = (255, 246, 234)
AMBER = (255, 194, 75)

FONT_PATH = "/usr/share/fonts/truetype/higgsfield/Metropolis-ExtraBold.ttf"

BAND = 0.30  # scrim height as a fraction of the frame
FADE = 0.07  # gradient tail below the scrim, same units
SIDE = 0.08  # horizontal margin
SHEET_BUDGET = 2800  # bytes; above this the base64 transfer starts losing data

# The Bright Riso-Flat palette. A slide that lands little of its area on these
# is off-brand however pleasant it looks on its own.
BRAND = {
    "orange": (0xFF, 0x7A, 0x2F),
    "amber": (0xFF, 0xC2, 0x4B),
    "coral": (0xFF, 0x4D, 0x6D),
    "ink": (0x23, 0x1A, 0x3E),
    "cream": (0xFF, 0xF6, 0xEA),
}
BRAND_TOLERANCE = 60  # euclidean distance in RGB that still counts as on-palette
BRAND_MIN_COVERAGE = 0.70  # of the frame
BRAND_MIN_INK_ORANGE = 0.25  # ink + orange + coral together: the contrast engine

# UK ASA red flags, ported verbatim from render.mjs. That guard was the only
# code-enforced compliance check in the system, and it lives inside a renderer
# that now draws ONE slide per carousel — so moving narrative slides here
# silently dropped six sevenths of every carousel out of its scope. Every
# headline composited by `letter` is checked against these before it is drawn.
ASA_PATTERNS = [
    r"\b(lose|drop|shed)\s+\d+\s*(kg|lbs?|pounds?|stone)",
    r"\bin\s+\d+\s+(days?|weeks?|months?)\b.*\b(body|fat|weight|abs|shredded|transform)",
    r"\b(body|fat|weight|abs)\b.*\bin\s+\d+\s+(days?|weeks?|months?)\b",
    r"\bsummer body\b",
    r"\bbikini body\b",
    r"\bguaranteed?\b",
    r"\bmelt (fat|belly)",
    r"\bspot.?reduc",
]


def asa_check(text, where):
    """Refuse copy that trips an obvious UK ASA red flag. Coarse and deliberately
    biased toward blocking — a false positive costs one reword."""
    hits = [p for p in ASA_PATTERNS if re.search(p, text, re.I)]
    if hits:
        raise SystemExit(
            f"ASA red flag in {where}: {text!r} matches {hits}. "
            "Reword — no timeframed or guaranteed body change."
        )


def font(size):
    return ImageFont.truetype(FONT_PATH, int(size))


def fit(draw, lines, box_w, box_h, gap=1.14):
    """Largest size at which every line fits the box."""
    for size in range(220, 20, -2):
        f = font(size)
        line_h = draw.textbbox((0, 0), "HXQ", font=f)[3]
        widest = max(draw.textbbox((0, 0), ln, font=f)[2] for ln in lines)
        if widest <= box_w and len(lines) * line_h * gap <= box_h:
            return f, line_h
    return font(20), 20


def scrim(w, band_h, fade_h):
    """Solid ink band with a soft gradient tail, so it reads as light rather
    than as a rectangle pasted over the art."""
    col = Image.new("L", (1, band_h + fade_h))
    px = col.load()
    for y in range(band_h + fade_h):
        px[0, y] = 244 if y < band_h else int(244 * (1 - (y - band_h) / fade_h) ** 1.6)
    layer = Image.new("RGBA", (w, band_h + fade_h), INK + (0,))
    layer.putalpha(col.resize((w, band_h + fade_h)))
    return layer


def handle(draw, w, h):
    f = font(w * 0.026)
    draw.text((int(w * SIDE), h - int(h * 0.055)), "@ARVENO.FITNESS", font=f, fill=CREAM)


def save(im, out_path):
    # TikTok's photo API takes WebP/JPEG only and validates on the URL
    # extension, never the bytes — so the suffix here is load-bearing.
    im.convert("RGB").save(out_path, "WEBP", quality=92)
    print(f"wrote {out_path} {im.size}")


def letter(art_path, out_path, headline, badge=None):
    asa_check(headline.replace("|", " "), f"headline for {out_path}")
    im = Image.open(art_path).convert("RGBA")
    w, h = im.size
    band_h, fade_h = int(h * BAND), int(h * FADE)
    im.alpha_composite(scrim(w, band_h, fade_h), (0, 0))

    draw = ImageDraw.Draw(im)
    lines = [ln.strip() for ln in headline.split("|") if ln.strip()]
    pad = int(w * SIDE)
    f, line_h = fit(draw, lines, w - 2 * pad, band_h * 0.74)

    step = int(line_h * 1.14)
    y = (band_h - step * len(lines)) // 2
    for ln in lines:
        tw = draw.textbbox((0, 0), ln, font=f)[2]
        draw.text(((w - tw) // 2, y), ln, font=f, fill=CREAM)
        y += step

    if badge:
        r = int(w * 0.055)
        cx, cy = pad + r, band_h + int(h * 0.05)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=AMBER)
        bf = font(r * 1.2)
        bb = draw.textbbox((0, 0), badge, font=bf)
        draw.text((cx - bb[2] / 2, cy - bb[3] / 2 - r * 0.14), badge, font=bf, fill=INK)

    handle(draw, w, h)
    save(im, out_path)


def card(out_path, heading, rules, size=(1856, 2304)):
    """The save beat: guaranteed-legible typography, no illustration."""
    asa_check(f"{heading} {rules}".replace("|", " "), f"card {out_path}")
    w, h = size
    im = Image.new("RGBA", size, INK + (255,))
    draw = ImageDraw.Draw(im)
    pad = int(w * SIDE)

    draw.text((pad, int(h * 0.10)), heading, font=font(w * 0.105), fill=CREAM)
    draw.rectangle((pad, int(h * 0.215), pad + int(w * 0.16), int(h * 0.222)), fill=AMBER)

    rf, nf = font(w * 0.052), font(w * 0.044)
    y = int(h * 0.29)
    for i, item in enumerate([r.strip() for r in rules.split("|") if r.strip()], 1):
        draw.text((pad, y + int(h * 0.006)), str(i), font=nf, fill=AMBER)
        draw.text((pad + int(w * 0.075), y), item, font=rf, fill=CREAM)
        y += int(h * 0.105)

    # The card is the one slide that closes: every carousel's final beat carries the
    # CTA. Added 2026-07-31 — before this, the only conversion nudge on any slide was
    # the tiny handle, and the caption's "link in bio" that most viewers never open.
    # Copy is deliberately claim-free (ASA): the free first scan is the real offer.
    draw.text((pad, int(h * 0.83)), "FREE FIRST SCAN", font=font(w * 0.055), fill=AMBER)
    draw.text((pad, int(h * 0.895)), "arveno.fitness — no card needed", font=font(w * 0.042), fill=CREAM)
    handle(draw, w, h)
    save(im, out_path)


def brand(*paths):
    """Diff a rendered frame against the palette. Exits non-zero on a fail so a
    batch script stops instead of shipping off-brand art."""
    failed = False
    for p in paths:
        im = Image.open(p).convert("RGB")
        im.thumbnail((240, 240))
        q = im.quantize(colors=12, method=Image.MEDIANCUT)
        pal, counts = q.getpalette(), q.getcolors()
        total = sum(c for c, _ in counts)

        share = dict.fromkeys(BRAND, 0.0)
        off = 0.0
        for count, i in counts:
            rgb = tuple(pal[i * 3 : i * 3 + 3])
            name, dist = min(
                ((n, sum((a - b) ** 2 for a, b in zip(rgb, v)) ** 0.5) for n, v in BRAND.items()),
                key=lambda t: t[1],
            )
            if dist <= BRAND_TOLERANCE:
                share[name] += count / total
            else:
                off += count / total

        covered = 1 - off
        punch = share["ink"] + share["orange"] + share["coral"]
        ok = covered >= BRAND_MIN_COVERAGE and punch >= BRAND_MIN_INK_ORANGE
        failed |= not ok
        breakdown = "  ".join(f"{n}={share[n]:.0%}" for n in BRAND)
        print(f"{'PASS' if ok else 'FAIL'} {p}  on-palette={covered:.0%}  punch={punch:.0%}")
        print(f"       {breakdown}  off-palette={off:.0%}")
        if not ok:
            if covered < BRAND_MIN_COVERAGE:
                print(f"       coverage below {BRAND_MIN_COVERAGE:.0%} — colours drifted off the guide")
            if punch < BRAND_MIN_INK_ORANGE:
                print(f"       ink+orange+coral below {BRAND_MIN_INK_ORANGE:.0%} — washed out, no contrast")
    sys.exit(1 if failed else 0)


def sheet(out_path, *paths):
    """Contact sheet sized to survive a hand-carried base64 transfer."""
    n = len(paths)
    cols = min(n, 3)
    rows = (n + cols - 1) // cols

    for cell_w, colours in ((150, 12), (124, 10), (104, 8), (88, 6), (72, 5)):
        tiles = []
        for p in paths:
            im = Image.open(p).convert("RGB")
            # The riso grain is the whole point of the art and pure noise to a
            # PNG encoder. Blur it out before quantizing or the sheet triples in
            # size for detail nobody is inspecting at this scale.
            tiles.append(
                im.resize((cell_w, int(cell_w * im.height / im.width)), Image.LANCZOS)
                .filter(ImageFilter.SMOOTH_MORE)
            )
        cell_h = max(t.height for t in tiles)
        grid = Image.new("RGB", (cell_w * cols, cell_h * rows), "white")
        for i, t in enumerate(tiles):
            grid.paste(t, ((i % cols) * cell_w, (i // cols) * cell_h))
        # Dithering scatters single pixels everywhere, which is exactly what PNG
        # cannot compress. Flat art does not need it.
        grid.quantize(colors=colours, method=Image.MEDIANCUT, dither=Image.Dither.NONE).save(
            out_path, optimize=True
        )
        size = len(open(out_path, "rb").read())
        if size <= SHEET_BUDGET:
            break

    blob = open(out_path, "rb").read()
    print(f"sheet {grid.size} {len(blob)}B md5={hashlib.md5(blob).hexdigest()}")
    if len(blob) > SHEET_BUDGET:
        print(f"WARNING over budget by {len(blob) - SHEET_BUDGET}B — split the batch")
    print("BEGIN")
    print(subprocess.run(["base64", "-w0", out_path], capture_output=True, text=True).stdout)
    print("END")


def probe(*paths):
    """Cheap sanity read with no transfer: dimensions, and how much of the top
    third is actually empty — a slide whose reserved band is busy will letter
    badly."""
    for p in paths:
        im = Image.open(p).convert("L")
        w, h = im.size
        top = im.crop((0, 0, w, int(h * BAND)))
        px = list(top.getdata())
        mean = sum(px) / len(px)
        var = sum((v - mean) ** 2 for v in px) / len(px)
        print(f"{p} {w}x{h} top-third mean={mean:.0f} stddev={var ** 0.5:.0f}")


def build(manifest_path):
    """One carousel, end to end. Manifest:

        {"slug": "scan-photo-rules",
         "slides": [
           {"art": "https://...png", "headline": "ONE WALL|FLAT LIGHT", "badge": "2"},
           {"card": true, "heading": "SCREENSHOT THIS.", "rules": "a|b|c"}
         ]}

    Off-brand art fails the gate and the slide is skipped rather than lettered,
    so a bad batch surfaces as a short slide list instead of a published post.
    """
    spec = json.load(open(manifest_path))
    slug = spec["slug"]
    os.makedirs(slug, exist_ok=True)

    finished, rejected = [], []
    for i, s in enumerate(spec["slides"], 1):
        out = f"{slug}/slide-{i:02d}.webp"
        if s.get("card"):
            card(out, s["heading"], s["rules"])
            finished.append(out)
            continue

        art = f"{slug}/art-{i:02d}.png"
        subprocess.run(["curl", "-sfL", "-o", art, s["art"]], check=True)
        try:
            brand(art)
        except SystemExit as e:
            if e.code:
                rejected.append((out, s.get("headline", "")))
                continue
        letter(art, out, s["headline"], s.get("badge"))
        finished.append(out)

    print(f"\n{len(finished)}/{len(spec['slides'])} slides built for {slug}")
    for out, head in rejected:
        print(f"REJECTED {out} ({head.replace('|', ' / ')}) — art failed the brand gate")
    if finished:
        sheet(f"{slug}/sheet.png", *finished)


def upload(manifest_path):
    """PUT finished slides to presigned URLs. Manifest is a list of
    {"file", "url", "content_type"} — normally straight from media_upload."""
    for item in json.load(open(manifest_path)):
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "PUT",
             "-H", f"Content-Type: {item.get('content_type', 'image/webp')}",
             "--data-binary", f"@{item['file']}", item["url"]],
            capture_output=True, text=True,
        )
        print(f"{r.stdout} {item['file']}")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    {
        "build": build, "upload": upload, "letter": letter, "card": card,
        "brand": brand, "sheet": sheet, "probe": probe,
    }[cmd](*args)
