# Generate the enterprise test corpus

Run from the repository directory with dependencies installed and the NFS share
mounted. The output directory must be empty. Use the dry-run command at the bottom
first to check the plan and available capacity without generating files.

## Generate 100,000 files

```bash
python synthetic_sensitive_images.py \
  --count 100000 \
  --output /mnt/sentra_data \
  --formats pdf=25,docx=15,xlsx=15,csv=10,txt=5,json=5,xml=5,jpg=7,png=5,tiff=3,zip=3,pptx=2 \
  --complexity MEDIUM \
  --reserve-percent 20 \
  --workers 4 \
  --seed 42
```

| File type | Share | Files |
|---|---:|---:|
| PDF | 25% | 25,000 |
| DOCX | 15% | 15,000 |
| XLSX | 15% | 15,000 |
| CSV | 10% | 10,000 |
| TXT | 5% | 5,000 |
| JSON | 5% | 5,000 |
| XML | 5% | 5,000 |
| JPEG | 7% | 7,000 |
| PNG | 5% | 5,000 |
| TIFF | 3% | 3,000 |
| ZIP | 3% | 3,000 |
| PPTX | 2% | 2,000 |

Ground truth is stored outside the scanner target at
`/mnt/sentra_data.manifest.jsonl`. Generation stops safely when available capacity
cannot accommodate further writes above the 20% filesystem reserve.

## Dry-run

```bash
python synthetic_sensitive_images.py \
  --count 100000 \
  --output /mnt/sentra_data \
  --formats pdf=25,docx=15,xlsx=15,csv=10,txt=5,json=5,xml=5,jpg=7,png=5,tiff=3,zip=3,pptx=2 \
  --complexity MEDIUM \
  --reserve-percent 20 \
  --workers 4 \
  --seed 42 \
  --dry-run
```
