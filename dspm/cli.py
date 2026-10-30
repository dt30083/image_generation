"""Enterprise generation CLI. Planning does not create files."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from .model import FORMATS, OPTIONAL, SENSITIVITY, SIZE_PROFILES, TECHNIQUES, techniques, Plan, content
from .writers import RASTER, LIMITS, write, extract, canonicalize, open_zip
from .storage import StorageBudget, BudgetManager, OutputStore, StorageStopped, MountUnavailable


def profile_arg(raw, defaults, allowed=None):
    if raw=='default': return defaults.copy()
    if raw.startswith('@'):
        value=json.loads(Path(raw[1:]).read_text())
        if not isinstance(value,dict): raise ValueError('Profile must be a JSON object of weights')
        result={str(k):float(v) for k,v in value.items()}
    else:
        result={}
        for item in raw.split(','):
            key,sep,weight=item.strip().partition('=')
            if key=='jpeg': key='jpg'
            if key=='tif': key='tiff'
            if key in result: raise ValueError(f'Duplicate profile key: {key}')
            result[key]=float(weight) if sep else defaults.get(key,1)
    if not result or any(k not in (allowed or defaults) for k in result):
        raise ValueError('Unknown or empty profile: '+raw)
    return result

def size_arg(raw):
    if raw in SIZE_PROFILES: return SIZE_PROFILES[raw]
    def size(value):
        value=value.strip().upper()
        for suffix,mult in [('GB',10**9),('MB',10**6),('KB',1000),('B',1)]:
            if value.endswith(suffix): return int(float(value[:-len(suffix)])*mult)
        return int(value)
    try:
        low,high=map(size,raw.split(':'))
    except (ValueError,TypeError): raise ValueError('Size profile must be a preset or MIN:MAX, e.g. 100KB:1MB')
    if not 0<=low<high<=1_000_000_000: raise ValueError('Size bounds must satisfy 0 <= MIN < MAX <= 1GB')
    return low,high

def normalize(value):
    return ''.join(value.split()).lower()

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def verify(path,job,data,ocr=False):
    expected=Counter(normalize(line) for line in data['lines'])
    # Full text counts cannot be recovered from image pixels without OCR.
    pixel=job['technique']=='image'
    from .matching import Matcher
    matcher=Matcher(expected)
    for chunk in extract(path,job['format']): matcher.feed(normalize(chunk))
    found=matcher.counts
    if pixel:
        # Validate the stored raster against a reproducible reference, including embedded images.
        from PIL import Image
        import io
        import zipfile
        from .writers import image_blob, image_bytes
        if job['format'] in RASTER:
            # Lossy codecs are checked against an independently encoded/reopened reference.
            from .writers import save_raster
            reference=image_bytes(data,job['seed'],job['complexity'],job.get('ocr','clean'),job.get('font'))
            with tempfile.TemporaryDirectory() as tmp:
                ref=Path(tmp)/('reference.'+job['format']); save_raster(reference,ref,'',False)
                reference.close()
                with Image.open(ref) as a, path.open('rb') as source, Image.open(source) as b:
                    if a.size!=b.size or a.convert('RGB').tobytes()!=b.convert('RGB').tobytes():
                        raise ValueError('Raster content differs from planned content')
        elif job['format']=='pdf':
            from pypdf import PdfReader
            with path.open('rb') as source:
                images=[im.image for page in PdfReader(source).pages for im in page.images]
            reference=Image.open(io.BytesIO(image_blob(data,job))).convert('RGB')
            if not any(im.size==reference.size and im.convert('RGB').tobytes()==reference.tobytes() for im in images):
                raise ValueError('Embedded PDF image mismatch')
        else:
            prefix={'docx':'word/media/','xlsx':'xl/media/','pptx':'ppt/media/'}[job['format']]
            with open_zip(path) as z:
                expected_blob=image_blob(data,job)
                if not any(z.read(n)==expected_blob for n in z.namelist() if n.startswith(prefix)):
                    raise ValueError('Embedded package image mismatch')
        if ocr:
            if not shutil.which('tesseract'): raise RuntimeError('--ocr-validate requires tesseract on PATH')
            import subprocess
            from .writers import image_blob
            with tempfile.TemporaryDirectory() as tmp:
                raster=Path(tmp)/'check.png'
                if job['format'] in RASTER:
                    with path.open('rb') as source, Image.open(source) as actual: actual.convert('RGB').save(raster,format='PNG')
                else: raster.write_bytes(image_blob(data,job))
                text=normalize(subprocess.run(['tesseract',str(raster),'stdout'],check=True,capture_output=True,text=True).stdout)
                if not all(text.count(k)>=v for k,v in expected.items()): raise ValueError('OCR could not recover all expected content')
        return 'pixel_match+ocr' if ocr else 'pixel_match; OCR not checked'
    for line,count in expected.items():
        if found[line]!=count:
            raise ValueError(f'Content count mismatch: {line!r}: expected {count}, recovered {found[line]}')
    return 'parser+content_counts'

def generate_one(task):
    job,config=task
    budget=config.get('budget')
    # Direct library callers explicitly get local mode; the public CLI defaults to NFS.
    if budget is None: budget=StorageBudget(config['output'],allow_local=True)
    store=OutputStore(config['output'],budget,job['index'],config.get('check_bytes',8*1024*1024),create=True)
    try: return _generate_one(job,config,store)
    except OSError as error:
        import errno
        if error.errno in (errno.ENOSPC,errno.EDQUOT): raise StorageStopped(f'Filesystem refused a write: {error}') from error
        if error.errno in (errno.ESTALE,errno.EIO,errno.ENOTCONN,errno.ETIMEDOUT): raise MountUnavailable(f'NFS/filesystem I/O failed: {error}') from error
        raise
    finally: store.close()

def _generate_one(job,config,store):
    job['records_per_file']=config.get('records_per_file')
    data=content(job,config['seed'],config['label'])
    job['ocr']=('clean','resolution','rotation','compression','font_size','noise','low_contrast','serif','monospace','columns')[job['index']%10] if job['complexity']=='HIGH' else 'clean'
    job['font']=config.get('font')
    low,high=config['sizes']
    initial={'LOW':5,'MEDIUM':40,'HIGH':250}[job['complexity']]
    rows=max(initial,low//220)
    goal=low+(min(high, int(low*1.1))-low)//2 if low else 0
    # Deep paths are intentional and relative IDs remain stable across output locations.
    relative=Path('division')/f'{job["index"]//10000:04d}'
    if job['complexity']=='HIGH': relative=relative/'region'/'department'/'year2026'/'quarter4'/'records'
    relative=relative/f'{job["index"]:09d}.{job["format"]}'
    target=store.path(relative)
    if target.exists(): raise FileExistsError(f'Refusing to overwrite {target}')
    temp=target.with_name('.partial-'+target.name)
    try:
        for attempt in range(10):
            write(temp,job,data,rows)
            canonicalize(temp,job['format'])
            actual=temp.stat().st_size
            if actual>high:
                if rows<=initial or job['format'] in RASTER:
                    raise ValueError(f'{job["format"]} needs {actual} bytes; exceeds size profile maximum {high}')
                rows=max(initial,int(rows*goal/actual))
                continue
            if actual>=low: break
            if job['format'] in RASTER: raise ValueError('Requested raster size cannot be reached without changing readable layout; choose a smaller minimum')
            rows=max(rows+1,int(rows*goal/max(actual,1)))
        else: raise ValueError('Could not reach size minimum with meaningful content')
        validation=verify(temp,job,data,config['ocr_validate'])
        sha=digest(temp)
        # Atomic no-clobber publication. Keep interrupted runs reviewable.
        members=archive_members(temp,data) if job['format']=='zip' else []
        font_sha=digest(Path(job['font'])) if job['font'] else None
        store.publish(temp,target)
    except BaseException:
        try: temp.unlink(missing_ok=True)
        except OSError: pass  # a lost NFS mount must not cause cleanup on a local path
        raise
    cats=data['categories']
    return dict(schema_version=1,path=relative.as_posix(),file_format=job['format'],file_size=actual,
                document_template=data['title'],sensitive_data_types=cats,expected_classifications=cats,
                expected_entity_counts=data['counts'],entity_count_policy='labeled field occurrences',
                identity_references=data['profiles'],scenario_id=f'{job["bucket"]}/{job["technique"]}/{data["variant"]}',
                sensitivity_bucket=job['bucket'],complexity=job['complexity'],embedding_technique=job['technique'],
                sha256=sha,generation_seed=job['seed'],master_seed=config['seed'],
                synthetic=True,synthetic_label=config['label'],pci_variant=data['variant'] if 'pci' in cats else None,
                expected_fields=[dict(category=c,label=k,value=v) for c,k,v in data['fields']],
                ocr_condition=job['ocr'] if job['technique']=='image' else None,
                validation=validation,records_per_file=job['records_per_file'],font_path=job['font'],font_sha256=font_sha,archive_members=members)

def archive_members(path,data):
    import zipfile,io
    result=[]
    def walk(source,prefix=''):
        with open_zip(source) as z:
            for info in z.infolist():
                result.append(dict(path=prefix+info.filename,size=info.file_size,expected_classifications=data['categories'] if not info.filename.startswith('operations/') else [],expected_entity_counts=data['counts'] if not info.filename.startswith('operations/') else {}))
                if info.filename.endswith('.zip'): walk(io.BytesIO(z.read(info)),prefix+info.filename+'!/')
    walk(path)
    return result

def run_generation(args,plan,report,storage_options,manifest,coverage_path,run_path,sizes,manifest_per_file):
    """Keep completed files and write an explicit partial-run summary on space loss."""
    completed=Counter(); start=time.monotonic(); report['status']='running'
    report['completed_files']=0; report['dataset_size_bytes']=0
    output_store=metadata_store=None; reports_started=False
    with BudgetManager() as manager:
        budget=manager.StorageBudget(**storage_options)
        report['filesystem']=budget.snapshot()
        try:
            output_store=OutputStore(args.output,budget,'parent',args.capacity_check_bytes,create=True)
            if os.listdir(output_store.fd): raise ValueError('Output directory must be empty; refusing to mix corpora')
            identity=budget.mount()
            # Reports on this same mounted filesystem share the worker budget.
            same=manifest.parent==Path(identity['point']) or Path(identity['point']) in manifest.parent.parents
            metadata_budget=budget if same else StorageBudget(str(manifest.parent),reserve_percent=args.reserve_percent,allow_local=True)
            metadata_store=OutputStore(manifest.parent,metadata_budget,'reports',args.capacity_check_bytes,metadata=True,create=True)
            truth=metadata_store.path(manifest.name); coverage=metadata_store.path(coverage_path.name); run=metadata_store.path(run_path.name)
            for path in (truth,coverage,run):
                if path.exists(): raise ValueError(f'Refusing to overwrite {path}')
            # Leave space for manifests and the final stop report before admitting data writes.
            headroom=args.workers*2*max(manifest_per_file,16*256*(args.records_per_file or (1000 if args.complexity=='HIGH' else 8)))+len(json.dumps(report))*4
            budget.set_headroom(headroom if same else 0)
            run.write_text(json.dumps(report,indent=2)); reports_started=True
            config=dict(output=str(args.output.resolve()),seed=args.seed,label=args.synthetic_label,sizes=sizes,
                        ocr_validate=args.ocr_validate,font=str(args.font.resolve()) if args.font else None,
                        records_per_file=args.records_per_file,budget=budget,check_bytes=args.capacity_check_bytes)
            with truth.open('x',encoding='utf-8') as stream:
                tasks=((job,config) for job in plan.jobs())
                with ProcessPoolExecutor(max_workers=args.workers) as pool:
                    from itertools import islice
                    while batch:=list(islice(tasks,args.workers*2)):
                        error=None; completed_items=[]
                        futures=[pool.submit(generate_one,task) for task in batch]
                        try:
                            for future in as_completed(futures):
                                try: completed_items.append(future.result())
                                except Exception as problem:
                                    error=error or problem
                                    budget.abort(str(problem))
                        except BaseException:
                            budget.abort('Generation interrupted'); raise
                        for item in sorted(completed_items,key=lambda x:x['path']):
                            stream.write(json.dumps(item,ensure_ascii=False)+'\n'); stream.flush()
                            completed[(item['file_format'],item['sensitivity_bucket'],item['embedding_technique'])]+=1
                            report['completed_files']+=1; report['dataset_size_bytes']+=item['file_size']
                            if report['completed_files']%args.capacity_check_interval==0:
                                report['filesystem']=budget.snapshot()
                                print(f'Generated {report["completed_files"]}/{args.count}',flush=True)
                        if error: raise error
            report['status']='complete'
        except (StorageStopped,MountUnavailable) as error:
            report['status']='stopped_mount' if isinstance(error,MountUnavailable) else 'stopped_storage'
            report['stop_reason']=str(error)
        except BaseException as error:
            report['status']='failed'; report['stop_reason']=str(error)
            raise
        finally:
            report['elapsed_seconds']=round(time.monotonic()-start,3)
            try: report['filesystem']=budget.snapshot()
            except OSError as error: report['filesystem']=budget.snapshot(False)|dict(current=False,error=str(error))
            report['manifest_recorded_files']=report['completed_files']
            report['completed_files']=report['filesystem']['completed_files']
            report['dataset_size_bytes']=report['filesystem']['committed_dataset_bytes']
            report['files_remaining']=args.count-report['completed_files']
            report['actual_coverage']=[dict(format=f,sensitivity=c,technique=t,files=n) for (f,c,t),n in sorted(completed.items())]
            try:
                if metadata_store is not None:
                    # Do not overwrite pre-existing reports if initialization was rejected.
                    if reports_started:
                        coverage.write_text(json.dumps(dict(planned=plan.coverage(),actual=report['actual_coverage']),indent=2))
                        run.write_text(json.dumps(report,indent=2))
            except OSError as error:
                report['report_write_error']=str(error)
                if report['status']=='complete': report['status']='stopped_mount' if isinstance(error,MountUnavailable) else 'stopped_storage'
            finally:
                if metadata_store: metadata_store.close()
                if output_store: output_store.close()
            print(json.dumps({key:report.get(key) for key in ('status','completed_files','manifest_recorded_files','files_remaining','dataset_size_bytes','filesystem','stop_reason','report_write_error')},indent=2),flush=True)
    return report['status']


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count',type=int,default=100000)
    parser.add_argument('--output','--out',type=Path,default=Path('/mnt/sentra_data'))
    parser.add_argument('--formats',default='default',help='Comma-separated formats or format=weight; @JSON file supported')
    parser.add_argument('--complexity',type=str.upper,choices=('LOW','MEDIUM','HIGH'),default='LOW')
    parser.add_argument('--size-profile',default='standard',help='small, standard, medium, large, stress, or MIN:MAX')
    parser.add_argument('--sensitivity-profile',default='default',help='Comma-separated category=weight or @JSON')
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--workers',type=int,default=1)
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--synthetic-label',choices=('neutral','explicit'),default='neutral')
    parser.add_argument('--records-per-file',type=int,help='Override sensitive record count (1..10000); text/table body placements only')
    parser.add_argument('--font',type=Path,help='TrueType/OpenType font; supply a handwriting font for handwriting-style cases')
    parser.add_argument('--ocr-validate',action='store_true',help='Require local Tesseract to recover image content')
    parser.add_argument('--reserve-percent',type=float,default=20)
    parser.add_argument('--max-size-gb',type=float,help='Optional decimal GB cap on dataset files; disabled by default')
    parser.add_argument('--capacity-check-interval',type=int,default=100)
    parser.add_argument('--capacity-check-bytes',type=int,default=8*1024*1024,help='Maximum bytes between checks (writes also check at file boundaries)')
    parser.add_argument('--nfs-mount',type=Path,help='Expected NFS mount point; otherwise pin the detected NFS mount')
    parser.add_argument('--allow-local-output',action='store_true',help='Explicitly allow a pinned local filesystem for development/tests')
    parser.add_argument('--wait-for-space',action='store_true',help='Wait and resume this run when percentage-reserve capacity returns; never wait on mount loss or dataset cap')
    parser.add_argument('--capacity-poll-seconds',type=float,default=5)
    args=parser.parse_args(argv)
    try:
        if args.count<1 or args.workers<1 or args.workers>64: raise ValueError('Count must be positive; workers must be 1..64')
        if args.font:
            from PIL import ImageFont
            ImageFont.truetype(str(args.font.resolve()),26)
        formats=profile_arg(args.formats,FORMATS,set(FORMATS)|set(OPTIONAL))
        sensitivity=profile_arg(args.sensitivity_profile,SENSITIVITY)
        sizes=size_arg(args.size_profile)
        if args.ocr_validate and not shutil.which('tesseract'): raise ValueError('--ocr-validate requires tesseract')
        for fmt in formats:
            if sizes[0]>=LIMITS.get(fmt,1_000_000_001):
                raise ValueError(f'{fmt} minimum must be below {LIMITS[fmt]:,} bytes for bounded-memory writer; select streaming text/archive formats for larger sizes')
        if args.records_per_file is not None and not 1<=args.records_per_file<=10000: raise ValueError('Records per file must be 1..10000')
        if args.records_per_file and (args.complexity!='LOW' or any(f in RASTER or f in ('msg','eml','zip') for f in formats)):
            raise ValueError('--records-per-file requires LOW complexity and PDF/DOCX/XLSX/PPTX or plain text formats')
        plan=Plan(args.count,formats,sensitivity,args.complexity,args.seed)
        output=args.output.resolve()
        manifest=(args.manifest or output.parent/(output.name+'.manifest.jsonl')).resolve()
        if manifest==output or output in manifest.parents: raise ValueError('Manifest must be outside scanner target directory')
        coverage_path=manifest.with_suffix('.coverage.json')
        run_path=manifest.with_suffix('.run.json')
        import math
        if args.max_size_gb is not None and (not math.isfinite(args.max_size_gb) or args.max_size_gb<=0):
            raise ValueError('--max-size-gb must be finite and positive')
        if not math.isfinite(args.reserve_percent) or not 0<=args.reserve_percent<100:
            raise ValueError('--reserve-percent must be in [0,100)')
        if args.capacity_check_interval<1 or args.capacity_check_bytes<1 or not math.isfinite(args.capacity_poll_seconds) or args.capacity_poll_seconds<=0:
            raise ValueError('Capacity intervals and thresholds must be positive')
        if args.nfs_mount and args.allow_local_output: raise ValueError('--nfs-mount cannot be combined with --allow-local-output')
        storage_options=dict(output=str(output),reserve_percent=args.reserve_percent,
                             max_bytes=int(args.max_size_gb*1_000_000_000) if args.max_size_gb is not None else None,
                             expected_mount=str(args.nfs_mount.resolve()) if args.nfs_mount else None,
                             allow_local=args.allow_local_output,check_interval=args.capacity_check_interval,
                             check_bytes=args.capacity_check_bytes,wait_for_space=args.wait_for_space,
                             poll_seconds=args.capacity_poll_seconds)
        estimated_max=args.count*sizes[1]
        typical=dict(pdf=5000,docx=45000,xlsx=10000,csv=5000,txt=5000,json=6000,xml=7000,jpg=220000,png=180000,tiff=180000,zip=5000,pptx=40000,bmp=4500000,gif=150000,html=7000,eml=7000,msg=10000,webp=150000)
        multiplier={'LOW':1,'MEDIUM':4,'HIGH':12}[args.complexity]
        estimated=sum(n*min(sizes[1],max(int(sizes[0]*1.8),typical[f]*multiplier)) for f,n in plan.formats.items())
        manifest_per_file=max(12000,args.records_per_file*16*180 if args.records_per_file else 100000 if args.complexity=='HIGH' else 12000)
        manifest_estimate=args.count*manifest_per_file
        report=dict(files=args.count,formats=plan.formats,sensitivity=plan.sensitivity,
                    complexity=args.complexity,size_bounds_bytes=sizes,
                    storage_bytes=dict(minimum=args.count*sizes[0],estimated=estimated,upper_bound=estimated_max,
                                       temporary_upper_bound=args.workers*sizes[1]*2,
                                       manifest_estimate=manifest_estimate),
                    output=str(output),manifest=str(manifest),coverage=plan.coverage(),
                    capabilities={f:dict(supported=list(TECHNIQUES[f]),selected=list(techniques(f,args.complexity))) for f in formats},
                    note='Storage estimates use format-specific planning allowances; upper bound assumes every file reaches the maximum. Unsupported combinations are rejected; sizes are validated per file.')
        report['storage_policy']=storage_options
        if args.dry_run:
            try: report['filesystem']=StorageBudget(**storage_options).snapshot()
            except OSError as error: report['filesystem']=dict(verified=False,error=str(error))
            print(json.dumps(report,indent=2)); return
        status=run_generation(args,plan,report,storage_options,manifest,coverage_path,run_path,sizes,manifest_per_file)
        if status!='complete': parser.exit(3)
    except Exception as error:
        parser.exit(2,f'Error: {error}\n')

if __name__=='__main__': main()
