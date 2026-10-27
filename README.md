# Synthetic Sensitive Image Generator

A Python CLI that generates **image files** containing **fictional sensitive-looking data** for authorized DSPM and DLP evaluation. It creates JPG, PNG, GIF, BMP, TIFF, and WebP images (not disk `.img` files).

## Requirements

- Python 3.10+
- Pillow

## Quick start (VS Code terminal)

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python synthetic_sensitive_images.py --out ./synthetic_images --count 5
```

## Examples

```bash
# Generate 3,000 images (100 records × 5 categories × 6 formats)
python synthetic_sensitive_images.py --out ./dlp_test_images --count 100 --formats jpg,png,gif,bmp,tiff,webp --categories pii,pci,phi,financial,credentials --mode both

# OCR-only testing
python synthetic_sensitive_images.py --out ./synthetic_images --count 10 --mode visible

# Metadata-only testing (BMP excluded)
python synthetic_sensitive_images.py --out ./synthetic_images --count 10 --formats jpg,png,gif,tiff,webp --mode metadata

# Full CLI help
python synthetic_sensitive_images.py --help
```

## Categories

- `pii`: fictional identity data
- `pci`: payment-card test data
- `phi`: fictional healthcare records
- `financial`: synthetic banking records
- `credentials`: nonfunctional credential-like strings

## Modes

- `visible`: render text into pixels to test OCR-based detection.
- `metadata`: embed text in image metadata (where supported).
- `both`: render text and embed metadata (default). BMP has visible text only.

The output directory contains a `manifest.jsonl` with filenames, SHA-256 checksums, categories, and expected fields to support detection-rate analysis. The script uses a deterministic random seed by default; change `--seed` to generate another reproducible dataset.

**Safety:** All records are synthetic. Do not substitute production records or actual secrets. Keep generated datasets out of the repository unless intentionally publishing test fixtures. Generated output directories are ignored by default.

