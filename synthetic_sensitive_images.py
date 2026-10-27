#!/usr/bin/env python3
"""Generate synthetic sensitive-data images for OCR and metadata-aware DSPM/DLP tests.

Examples:
    pip install Pillow
    python synthetic_sensitive_images.py --out ./test_images --count 3
    python synthetic_sensitive_images.py --formats jpg,gif,bmp --categories pii,pci
    python synthetic_sensitive_images.py --mode metadata --formats jpg,png,gif,tiff,webp

All generated content is fictitious. No personal data or working credentials are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

FORMATS = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "png": "PNG",
    "gif": "GIF",
    "bmp": "BMP",
    "tif": "TIFF",
    "tiff": "TIFF",
    "webp": "WEBP",
}
CATEGORIES = ("pii", "pci", "phi", "financial", "credentials")
FIRST = ("Morgan", "Taylor", "Jordan", "Avery", "Casey", "Alex", "Jamie", "Riley")
LAST = ("Example", "Sample", "Testwell", "Mockley", "Demo")
PCI_TEST_CARDS = (
    ("Visa (published test number)", "4111111111111111"),
    ("Mastercard (published test number)", "5555555555554444"),
    ("Amex (published test number)", "378282246310005"),
)


def generate_record(category: str, rng: random.Random, index: int) -> dict[str, str]:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    person = f"{first} {last}"
    marker = f"TEST-{category.upper()}-{index:05d}"
    if category == "pii":
        return {
            "Record type": "PERSONALLY IDENTIFIABLE INFORMATION (PII)",
            "Name": person,
            "SSN (invalid synthetic)": f"900-{rng.randint(10, 99):02d}-{rng.randint(1000, 9999)}",
            "DOB": f"{rng.randint(1971, 2001)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            "Email": f"{first.lower()}.{last.lower()}{index}@example.com",
            "Phone (reserved range)": f"202-555-01{rng.randint(0, 99):02d}",
            "Street address": f"{rng.randint(100, 999)} Example Lane, Testville, TX 00000",
            "Record ID": marker,
        }
    if category == "pci":
        network, card_number = PCI_TEST_CARDS[(index - 1) % len(PCI_TEST_CARDS)]
        return {
            "Record type": "PAYMENT CARD DATA (PCI TEST FIXTURE)",
            "Cardholder": person,
            "Payment network": network,
            "Card number": card_number,
            "Expiration": "12/2030",
            "Security code (dummy)": "999",
            "Merchant": "Example Test Store",
            "Transaction ID": marker,
        }
    if category == "phi":
        return {
            "Record type": "PROTECTED HEALTH INFORMATION (PHI TEST)",
            "Patient name": person,
            "Patient DOB": f"{rng.randint(1971, 2001)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            "Medical record number": f"FAKE-MRN-{index:07d}",
            "Diagnosis": "Type 2 diabetes mellitus (synthetic case)",
            "ICD-10-CM": "E11.9",
            "Clinical note": "Fictional outpatient assessment; no patient exists.",
            "Encounter ID": marker,
        }
    if category == "financial":
        return {
            "Record type": "FINANCIAL ACCOUNT INFORMATION (TEST)",
            "Account holder": person,
            "Account number (fake)": f"999{index:09d}",
            "Routing number (invalid)": "000000000",
            "Account type": "Checking",
            "Statement balance": f"${rng.randint(1000, 9999):,}.{rng.randint(0, 99):02d}",
            "Tax identifier (invalid)": f"900-{rng.randint(10,99)}-{rng.randint(1000,9999)}",
            "Statement ID": marker,
        }
    if category == "credentials":
        return {
            "Record type": "SECRET-LIKE STRINGS (ALL NONFUNCTIONAL)",
            "Environment": "FAKE_TEST_ONLY",
            "AWS key-shaped fixture": "AKIA" + "TESTONLY" * 2,
            "Stripe-style fixture": f"sk_test_{index:06d}" + "FAKEKEYNOTVALID",
            "Bearer token fixture": f"test_token_{index:08d}_NOT_VALID",
            "Password fixture": f"ExampleOnly!{index:04d}",
            "Owner": person,
            "Secret ID": marker,
        }
    raise ValueError(f"Unknown category: {category}")


def get_font(size: int, custom: str | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [custom] if custom else []
    candidates += [
        "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def make_image(record: dict[str, str], category: str, index: int,
               mode: str, font_path: str | None) -> Image.Image:
    """Visible records support OCR; metadata-only images intentionally omit record text."""
    img = Image.new("RGB", (1500, 900), (248, 249, 251))
    draw = ImageDraw.Draw(img)
    headline = get_font(47, font_path)
    body = get_font(32, font_path)
    small = get_font(25, font_path)
    draw.rectangle((0, 0, 1500, 125), fill=(24, 53, 82))
    draw.text((64, 31), "SYNTHETIC SECURITY TEST DOCUMENT", font=headline, fill="white")
    draw.text((65, 155), f"CLASSIFICATION: {category.upper()}  |  RECORD: {index:04d}",
              font=small, fill=(78, 83, 95))
    if mode != "metadata":
        y = 220
        for label, value in record.items():
            text = f"{label}:  {value}"
            # Keep large, crisp characters for realistic OCR validation.
            draw.text((70, y), text, font=body, fill=(12, 24, 38))
            y += 72
        if y > 850:
            raise ValueError(f"Too many fields to render ({category})")
    else:
        draw.text((70, 300), "Image metadata test fixture", font=headline, fill=(12, 24, 38))
        draw.text((70, 390), "Sensitive-looking fields stored in file metadata only.",
                  font=body, fill=(12, 24, 38))
    draw.line((64, 810, 1435, 810), fill=(180, 186, 194), width=2)
    draw.text((65, 830), "FICTIONAL TEST DATA ONLY - DO NOT USE AS REAL IDENTITIES OR CREDENTIALS",
              font=small, fill=(115, 35, 35))
    return img


def metadata_payload(record: dict[str, str]) -> str:
    return "SYNTHETIC_TEST_DATA\n" + "\n".join(f"{k}: {v}" for k, v in record.items())


def save_image(img: Image.Image, path: Path, payload: str,
               include_metadata: bool) -> bool:
    """Save a standards-compliant raster image. Return True if metadata was embedded."""
    extension = path.suffix.lower().lstrip(".")
    file_format = FORMATS[extension]
    args: dict = {}
    embedded = False

    if include_metadata:
        if file_format == "PNG":
            info = PngImagePlugin.PngInfo()
            info.add_text("Description", payload)
            info.add_text("TestDataNotice", "SYNTHETIC ONLY")
            args["pnginfo"] = info
            embedded = True
        elif file_format in {"JPEG", "WEBP"}:
            exif = Image.Exif()
            exif[0x010E] = payload  # EXIF ImageDescription
            exif[0x0131] = "Synthetic Security Image Generator"
            args["exif"] = exif.tobytes()
            embedded = True
        elif file_format == "GIF":
            args["comment"] = payload.encode("utf-8")
            embedded = True
        elif file_format == "TIFF":
            args["tiffinfo"] = {270: payload, 305: "Synthetic Security Image Generator"}
            embedded = True
        # BMP has no portable, consistently supported text metadata facility.

    if file_format == "JPEG":
        args.update(quality=95, subsampling=0)
    if file_format == "WEBP":
        args.update(quality=95, method=4)
    img.save(path, format=file_format, **args)
    return embedded


def read_embedded_metadata(path: Path) -> str | None:
    with Image.open(path) as im:
        if im.format == "PNG":
            return im.info.get("Description")
        if im.format == "GIF":
            comment = im.info.get("comment", b"")
            return comment.decode("utf-8") if isinstance(comment, bytes) else comment
        if im.format == "TIFF":
            return im.tag_v2.get(270)
        if im.format in {"JPEG", "WEBP"}:
            return im.getexif().get(0x010E)
        return None


def comma_list(raw: str) -> list[str]:
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("synthetic_images"))
    parser.add_argument("--count", type=int, default=2,
                        help="Number of records per category per file format (default: 2)")
    parser.add_argument("--formats", default="jpg,png,gif,bmp,tiff,webp")
    parser.add_argument("--categories", default=",".join(CATEGORIES))
    parser.add_argument("--mode", choices=("visible", "metadata", "both"), default="both",
                        help="Where to put sensitive-looking text (default: both)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--font", default=None, help="Optional path to a TrueType font")
    args = parser.parse_args()

    formats = comma_list(args.formats)
    categories = comma_list(args.categories)
    if args.count < 1:
        parser.error("--count must be at least 1")
    if not formats or any(fmt not in FORMATS for fmt in formats):
        parser.error("--formats must be selected from: " + ", ".join(FORMATS))
    if len(formats) != len(set(formats)):
        parser.error("--formats contains duplicates")
    if not categories or any(cat not in CATEGORIES for cat in categories):
        parser.error("--categories must be selected from: " + ", ".join(CATEGORIES))
    if len(categories) != len(set(categories)):
        parser.error("--categories contains duplicates")
    if args.mode == "metadata" and "bmp" in formats:
        parser.error("BMP does not reliably support text metadata. Remove bmp or use --mode both/visible")

    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    manifest = args.out / "manifest.jsonl"
    total = 0
    with manifest.open("w", encoding="utf-8") as output:
        for category in categories:
            for index in range(1, args.count + 1):
                record = generate_record(category, rng, index)
                payload = metadata_payload(record)
                img = make_image(record, category, index, args.mode, args.font)
                for ext in formats:
                    path = args.out / f"{category}_{index:04d}.{ext}"
                    embedded = save_image(img, path, payload, args.mode != "visible")
                    # Validate file is readable and any claimed metadata survived re-opening.
                    found = read_embedded_metadata(path)
                    if embedded and found != payload:
                        raise RuntimeError(f"Embedded metadata not preserved: {path}")
                    item = {
                        "filename": path.name,
                        "category": category,
                        "synthetic": True,
                        "visible_text": args.mode != "metadata",
                        "embedded_metadata": embedded,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "expected_fields": record,
                    }
                    output.write(json.dumps(item, ensure_ascii=False) + "\n")
                    total += 1
    print(f"Generated {total} image files in {args.out.resolve()}")
    print(f"Ground-truth manifest: {manifest.resolve()}")
    if "bmp" in formats and args.mode == "both":
        print("Note: BMP fixtures contain visible text but no portable text metadata.")


if __name__ == "__main__":
    main()
