# Enterprise DSPM evaluation corpus

Generate controlled fictional enterprise documents, keep ground truth outside the
scanner target, and compare scanner results against explicit expectations. The
original image generator remains available with its original CLI and defaults.
No production systems are accessed. No customer records or functioning secrets
are used. Generated identities are composed fictional profiles, not anonymized
records from real people.

## Setup

Python 3.10+ is required. Dependencies write real file formats and independently
reopen their contents for validation.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Optional OCR validation requires the `tesseract` executable on PATH. Fonts and
library versions influence bytes; repeatability requires the same environment.
A font file can be supplied with `--font`, including a licensed handwriting font.
The default renderer searches locally installed fonts. No fonts are downloaded.

## Plan before generating

```bash
python enterprise_generator.py --dry-run
python enterprise_generator.py --dry-run --count 1000000 --complexity HIGH --workers 4
```

Dry-run creates no files. It reports exact counts, planned format/category/placement
coverage, storage estimates, upper bounds, manifest allowances, temporary-space
requirements, and current filesystem utilization when the mount can be verified.
A missing mount appears as `filesystem.verified: false` in a dry-run report; real
generation fails closed. Estimates no longer require capacity for the entire
requested corpus up front: generation prioritizes the requested count and proceeds
until completion or a live storage constraint.

## Dynamic NFS capacity management

Enterprise generation requires a present NFS/NFS4 mount by default. Linux uses
`/proc/self/mountinfo`; macOS uses the system mount table. `--nfs-mount` optionally
names the expected mount point. Otherwise, the enclosing NFS mount, its source,
mount identity, and filesystem device are pinned before any dataset writes.
A missing or replaced mount stops the run; the generator never creates a local
replacement for an absent share. Output files use descriptor-relative operations
and reject symlink directories. Already-open descriptors remain attached to the
original filesystem if the namespace changes between a check and a write.

```bash
# Standard generation; the existing script also exposes the enterprise CLI
python synthetic_sensitive_images.py --count 100000 --output /mnt/sentra_data \
  --reserve-percent 20

# Custom reserve; specify the actual enclosing NFS mount if desired
python synthetic_sensitive_images.py --count 100000 --output /mnt/sentra_data \
  --reserve-percent 15 --nfs-mount /mnt

# Optional dataset cap (decimal GB); no absolute dataset cap is enabled by default
python synthetic_sensitive_images.py --count 100000 --output /mnt/sentra_data \
  --reserve-percent 20 --max-size-gb 50

# Monitor at file and byte boundaries, and wait for capacity to return
python enterprise_generator.py --output /mnt/sentra_data --workers 4 \
  --capacity-check-interval 100 --capacity-check-bytes 8388608 \
  --wait-for-space --capacity-poll-seconds 5

# Explicit local development mode; never selected automatically by enterprise CLI
python enterprise_generator.py --count 1000 --output ./corpus \
  --allow-local-output --workers 2
```

The default `--reserve-percent 20` reserves **20% of total filesystem capacity**.
Available generation budget is `max(0, current available bytes - reserved bytes)`.
For a 100 GB share with 80 GB available, the initial budget is 60 GB. Each fresh
snapshot recalculates the percentage reserve, so both capacity expansion and
consumption/freeing by other NFS clients change the permitted budget automatically.
A conservative byte ledger subtracts our own writes even while an NFS server
returns cached free-space statistics; `conservative_available_bytes` exposes this
admission value alongside the actual reported `available_bytes`.
Available space is that reported to the current process, rather than assuming all
unused blocks are accessible to this user.

A shared coordinator serializes write-ahead reservations for every worker.
Reservations account for concurrent writes, active partial files, native image
encoders, canonical Office-package copies, new directories, and report headroom.
Checks occur before raw writes, split at or below `--capacity-check-bytes` (default
8 MiB), and final capacity snapshots are recorded at least every
`--capacity-check-interval` completed files (default 100). Current writes also
check more frequently than these limits. Large documents cannot bypass checks
with a single library write, and serializers cannot bypass the guard using a raw
file descriptor. Format-specific document size limits remain distinct from the
filesystem reserve and optional dataset cap; quality is never reduced to fit
capacity.

`--max-size-gb` limits final dataset bytes plus logical growth of in-progress
primary files. Duplicate temporary packages and manifests are excluded from the
logical dataset cap but consume filesystem capacity and reservations. The more
restrictive remaining budget always wins. Completed files are never deleted to
make room. Only this run's own unpublished temporary files are cleaned up after
errors or storage stops. Existing output directories and reports are protected
against accidental overwrite.

Without `--wait-for-space`, insufficient space stops gracefully with exit code 3.
The console and run report include files completed/remaining, dataset size,
filesystem total/used/available bytes, reserved bytes, in-progress reservations,
and the stop reason. With explicit waiting, the same running process pauses
writes and polls until sufficient capacity returns. Mount loss and a reached
absolute dataset cap stop immediately even in waiting mode. This is in-process
resume, not restart/recovery of a previously exited run.

Reports on the same NFS filesystem share its capacity guard. An explicitly
separate report filesystem gets its own pinned guard and percentage reserve. If
a failed NFS connection prevents writing reports, the final summary is printed
to the console with the reporting error, separate counts of published and
manifest-recorded files, and the last known snapshot marked
`current: false`; reports are never redirected elsewhere. A fresh mounted-share
run is required after a mount replacement.

Filesystem statistics are observations, not a server-wide quota or lock on other
NFS clients. The generator coordinates its own workers and stops on observed
external consumption; it cannot prevent another client from independently crossing
the reserve between checks. NFS quotas and mount timeout/retry policy remain
server/host responsibilities. A hard-mounted NFS syscall may block until the
kernel returns; the generator cannot impose a reliable Python timeout on it.
Automated tests simulate mount loss, expansion, external usage, and concurrent
reservations. A real NFS server integration test is still required in deployment.

## Default corpus

The default is **100,000 files**, output to **`/mnt/sentra_data`**, LOW complexity,
one worker, seed 42, and the `standard` size profile. Directory creation requires
normal filesystem permission and a verified NFS mount. For local development use
`--output ./corpus --allow-local-output`. No automatic mounting or privilege changes occur.

| Format | Share | Files |
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

BMP, GIF, HTML, EML, MSG, and legacy WebP are optional. JPEG/JPG and TIF/TIFF
aliases are accepted. MSG files are Unicode MS-OXMSG messages in actual CFB
containers with subject, body, and message-class properties; they are not renamed
EML files. Outlook rendering/interoperability has not been certified.

| Sensitivity bucket | Share |
|---|---:|
| PII only | 20% |
| PCI only | 10% |
| PHI only | 10% |
| Financial only | 10% |
| Mixed sensitive | 20% |
| Non-sensitive | 20% |
| Ambiguous negative control | 10% |

Mixed documents include all four categories. “Only” refers to the intended test
classification: a patient name is naturally also identifying, so production
classifiers may legitimately apply overlapping taxonomies. Review your category
mapping and policy before interpreting discrepancies as scanner defects.
Negative controls include invalid identifier structures and contextual equipment
codes. Their expected classification is empty; retain the ambiguous bucket as a
separate report slice because vendors may reasonably use different policies.

## Generation examples

```bash
# Default enterprise distribution and external ground truth
python enterprise_generator.py --workers 4

# Smaller pilot; 1,000 preserves default LOW coverage
python enterprise_generator.py --count 1000 --output ./corpus --allow-local-output --workers 2

# Complex placements, OCR conditions, nested archives, and sparse documents
python enterprise_generator.py --count 10000 --output ./high_corpus --allow-local-output \
  --complexity HIGH --manifest ./ground_truth/high.jsonl --workers 4

# Optional formats, explicit equal weights
python enterprise_generator.py --count 1000 --output ./optional_corpus --allow-local-output \
  --formats bmp=1,gif=1,html=1,eml=1,msg=1 --size-profile standard

# Configurable distributions; unspecified formats are excluded
python enterprise_generator.py --count 10000 --output ./custom_corpus --allow-local-output \
  --formats pdf=50,docx=25,xlsx=25 \
  --sensitivity-profile @examples/sensitivity-profile.json --seed 123

# Stream meaningful operational records into large text documents
python enterprise_generator.py --count 10 --output ./large_text --allow-local-output \
  --formats txt --size-profile 900MB:1GB --workers 1

# High-volume sensitive records (also supports PDF/DOCX/XLSX and text formats)
python enterprise_generator.py --count 10 --output ./record_volume --allow-local-output \
  --formats csv --records-per-file 1000 --size-profile 0:5MB

# Matched synthetic-label experiment: same seeds, different explicit notices
python enterprise_generator.py --count 1000 --output ./labeled --allow-local-output \
  --synthetic-label explicit
```

`--count` means total outer files, including each ZIP once. Archive members do not
inflate corpus counts. The manifest also lists archive member paths and expected
categories/counts; nested containers carry aggregate expectations. The primary
scorer evaluates outer files. Normalize member-level findings to outer paths before
importing them, or analyze member ground truth separately.

CLI: `--count`, `--output` (`--out` alias), `--formats`, `--complexity`,
`--size-profile`, `--sensitivity-profile`, `--seed`, `--workers`, `--manifest`,
`--dry-run`, `--synthetic-label`, `--ocr-validate`, `--font`, and
`--records-per-file`, plus the storage options documented above. `--help` describes the options. Profile weights accept
comma-separated names/weights or `@path/to/profile.json`; omitted numeric weights
use the format's default weight, or 1 for optional formats. Largest-remainder
allocation preserves totals for counts that are not multiples of 100.

## Complexity and guaranteed coverage

LOW uses the simplest native placement for each format. MEDIUM introduces tables,
headers/footers, metadata, images, and attachments where available. HIGH additionally
introduces split values, very-hidden spreadsheet sheets, embedded DOCX packages,
nested ZIPs, deep paths, sparse data, and selected 1,000-record body/table documents.
Ordinary MEDIUM documents have up to three records and HIGH up to eight; placement
constraints keep headers and metadata compact. `--records-per-file` explicitly
sets 1..10,000 sensitive records for LOW text/document formats. Large neutral
operational histories increase file sizes without increasing sensitive counts.

The deterministic planner reserves every selected format × sensitivity bucket ×
supported technique before filling the remaining allocations. It does not rely
on random sampling for this coverage. Small requests that cannot meet coverage
fail clearly. Profiles intentionally excluding a bucket do not claim coverage of
that bucket. See `dspm/model.py:TECHNIQUES` for the format capability matrix;
unsupported placements are never advertised as generated coverage.

HIGH image placements cycle through clean, reduced-resolution, rotation,
compression, font-size, noise, low-contrast, serif, monospace, and multi-column
conditions. TIFF uses lossless compression. PNG/GIF/TIFF/JPEG/WebP metadata are
written using native mechanisms; BMP has no metadata-only scenario. OCR raster
content is reproduced and compared after reopening the actual files, including
images embedded in PDF and Office documents. This proves pixel integrity, not OCR
success or human readability in every layout. Use `--ocr-validate` to require
Tesseract recovery and visually review samples for your chosen fonts/conditions.
Font fallbacks may make serif/monospace cases equivalent on some hosts.

## File size and scale boundaries

| Profile | Allowed actual file size |
|---|---|
| small | below or equal to 100 KB |
| standard (default) | below or equal to 5 MB |
| medium | 100 KB to 1 MB |
| large | 1 MB to 10 MB |
| stress | 10 MB to 1 GB |
| custom | `MIN:MAX`, decimal KB/MB/GB, maximum 1 GB |

The standard profile favors small files; its 5 MB ceiling accommodates readable
uncompressed optional BMPs. Not every format can satisfy every size band: raster
writers use a fixed readable document layout and do not grow via padding. A
requested raster minimum that the layout cannot reach fails clearly. Current
bounded library writers reject minima at or above 10 MB for Office/PDF/email and
2 MB for raster formats. MSG additionally has a CFB FAT capacity limit. **The 1 GB
range is implemented for streaming TXT/CSV/JSON/XML/HTML and ZIP content, not every
format.** A full 1 GB output has not been benchmarked. No meaningless bytes are
appended to meet a size target; standard container alignment remains valid.

The corpus planner, task queue, manifest writer, hashes, and plain-text generators
use bounded memory independent of file count. Office/PDF libraries retain per-file
structures; select workers with their per-file memory requirements in mind.
Validation reads bounded text chunks, uses streaming multi-pattern matching, and
uses SQLite for result joins. Office package parsing is bounded by the supported
per-file size, not by total corpus size. Million-file planning is tested;
million-file generation and throughput are not yet benchmarked.

## Ground truth and validation

The default manifest is `/mnt/sentra_data.manifest.jsonl`, outside the scanner
root. Two companion reports hold planned/actual coverage and run status. An
explicit manifest under the target (including through a symlink) is rejected.
The new framework refuses nonempty output directories and existing manifests;
use a fresh location for every run. Failed runs keep completed files and their
manifest records, with `status: failed`. Abrupt process termination can leave
partial files: do not scan a run unless its run report says `complete`.

Each JSONL entry contains path, actual format/size, template, expected categories,
entity counts, fictional profile references, complexity, embedding technique,
scenario, per-file/master seeds, SHA-256, synthetic provenance, PCI presentation,
expected fields, OCR condition, validation method, and archive members. Entity
counts mean **labeled field occurrences**, including repeated names; they do not
mean unique people or legally protected records. Expected fields contain test
values, so keep the manifest out of the scanner target.

```bash
python validate_corpus.py --output ./corpus --manifest ./corpus.manifest.jsonl
# Include optional OCR recovery
python validate_corpus.py --output ./corpus --manifest ./corpus.manifest.jsonl --ocr-validate
```

Generation verifies parseability, expected text occurrences, actual sizes, and
hashes before publication. Raster cases use pixel equality against a regenerated
reference. Revalidation also checks stored hashes and deterministic ground truth.
Identities have stable profile IDs, valid dates, adult employment dates, reserved
900-prefix SSN-like values, and fictional account/member/MRN identifiers. Medical
cases use E11.9, I10, and J45.909 with matching terminology. Financial statements
balance arithmetically. Payment cases use only four fixed processor-published test
PANs, with Luhn checks in the tests: [Braintree testing documentation](https://developer.paypal.com/braintree/docs/reference/general/testing/python#credit-card-numbers).
No live payment credentials are generated. PCI representations include plain,
spaced, hyphenated, prose, masked, truncated, split, and image cases. The expected
policy classifies masked/truncated card context as PCI; adjust your evaluation
policy if the scanner promises only full-PAN detection.

## Import Sentra or another scanner's export

A real Sentra export was not supplied. The importer therefore supports a documented
normalized **CSV or JSONL contract**, plus configurable column/classification
mapping, without inventing an official Sentra export schema or calling its API.
Adapt `examples/sentra-mapping.json` to the actual export's field names and labels.
If the export contains nested JSON, finding-per-row records, or separate archive
members, normalize it first. Duplicate file rows and unknown labels are rejected.

```json
{"path":"division/0000/000000000.pdf","categories":["pii"],"status":"scanned","synthetic":true}
```

Paths are corpus-relative; `--target-root /mnt/sentra_data` also accepts absolute
paths under that prefix. Categories are JSON arrays or semicolon-separated CSV
values. Supported statuses: `scanned`, `unsupported`, `error`, `unscanned`.
Synthetic recognition is a separate optional boolean; missing means unknown.

```bash
python evaluate_scanner.py --manifest ./corpus.manifest.jsonl \
  --results ./sentra-results.csv --mapping examples/sentra-mapping.json \
  --target-root /mnt/sentra_data --output ./evaluation.json
```

Absent export rows default to **unscanned**, not true negatives. Only use
`--complete-export` when you independently know every absent file was scanned
and had no findings. Precision, recall, F1, TP/FP/TN/FN are file/category decisions
across four canonical categories. Reports slice by file format, sensitivity
category, scenario, bucket, OCR condition, and synthetic-recognition result.
Discovery coverage includes excluded/error/unsupported statuses separately.
Undefined metric denominators yield `null`. Entity-level scoring needs actual
entity-level scanner exports and is not inferred. Since every input is synthetic,
synthetic-recognition specificity cannot be measured with this corpus.

## Original image generator

The existing script preserves legacy count semantics, modes, metadata checks,
visible test notices, credentials fixtures, and its manifest location when invoked
with legacy options (`--out`, `--mode`, `--categories`). Passing `--output` or a
new enterprise/storage option dispatches to the enterprise CLI, with total-file
count semantics and NFS checks. `python synthetic_sensitive_images.py --output
/mnt/sentra_data --help` shows enterprise help; plain `--help` shows legacy help.
Unlike the enterprise CLI, its count is records per category per format and its
manifest is inside its output. Keep that legacy directory out of enterprise scans
when external-only ground truth is required.

```bash
python synthetic_sensitive_images.py --out ./synthetic_images --count 5
python synthetic_sensitive_images.py --out ./dlp_test_images --count 100 \
  --formats jpg,png,gif,bmp,tiff,webp \
  --categories pii,pci,phi,financial,credentials --mode both
python synthetic_sensitive_images.py --count 10 --mode visible
python synthetic_sensitive_images.py --count 10 \
  --formats jpg,png,gif,tiff,webp --mode metadata
python synthetic_sensitive_images.py --help
```
