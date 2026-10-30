"""Normalize scanner exports and score file/category decisions with bounded memory."""
import argparse
import csv
import json
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
from contextlib import closing
from collections import defaultdict
from .model import CATEGORIES


def rows(path):
    with path.open(encoding='utf-8-sig',newline='') as source:
        if path.suffix.lower()=='.csv': yield from csv.DictReader(source)
        else:
            for line in source:
                if line.strip(): yield json.loads(line)


def normalized_path(raw,root=None):
    value=str(raw).replace('\\','/')
    if root:
        prefix=str(root).replace('\\','/').rstrip('/')+'/'
        if value.startswith(prefix): value=value[len(prefix):]
    path=PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not value or str(path)=='.':
        raise ValueError(f'Expected a corpus-relative path, got {value!r}; use --target-root for absolute exports')
    return str(path)


def metrics(counts):
    tp,fp,tn,fn=(counts.get(k,0) for k in ('tp','fp','tn','fn'))
    precision=tp/(tp+fp) if tp+fp else None
    recall=tp/(tp+fn) if tp+fn else None
    return dict(tp=tp,fp=fp,tn=tn,fn=fn,precision=precision,recall=recall,
                f1=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None)


def evaluate(manifest,results,mapping=None,complete=False,root=None):
    mapping=mapping or {}
    columns={'path':'path','categories':'categories','status':'status','synthetic':'synthetic'}|mapping.get('columns',{})
    aliases=mapping.get('classifications',{c:c for c in CATEGORIES})
    with tempfile.TemporaryDirectory() as tmp, closing(sqlite3.connect(str(Path(tmp)/'evaluation.sqlite'))) as db:
        db.execute('create table truth(path text primary key, data text)')
        db.execute('create table observed(path text primary key, data text)')
        for row in rows(manifest):
            key=normalized_path(row['path'])
            db.execute('insert into truth values (?,?)',(key,json.dumps(row)))
        for row in rows(results):
            path=normalized_path(row[columns['path']],root)
            raw=row.get(columns['categories'],[])
            if isinstance(raw,str):
                raw=json.loads(raw) if raw.strip().startswith('[') else [x.strip() for x in raw.split(';') if x.strip()]
            if not isinstance(raw,list): raise ValueError('Categories must be a JSON array or semicolon-separated string')
            unknown=[x for x in raw if x not in aliases]
            if unknown: raise ValueError(f'Unmapped scanner classifications: {unknown}; supply --mapping')
            status=row.get(columns['status'],'scanned')
            if status not in ('scanned','unsupported','error','unscanned'): raise ValueError(f'Unknown scan status {status}')
            value=row.get(columns['synthetic'])
            if isinstance(value,str):
                if value.lower() not in ('true','false',''): raise ValueError('Synthetic must be true, false, or empty')
                value={'true':True,'false':False,'':None}[value.lower()]
            normalized=dict(categories=sorted(set(aliases[x] for x in raw)),status=status,synthetic=value)
            if any(x not in CATEGORIES for x in normalized['categories']): raise ValueError('Mappings must resolve to canonical sensitivity categories')
            # Duplicate rows must be normalized upstream: silently merging statuses masks export errors.
            db.execute('insert into observed values (?,?)',(path,json.dumps(normalized)))
        db.commit()
        unknown=db.execute('select count(*) from observed left join truth using(path) where truth.path is null').fetchone()[0]
        if unknown: raise ValueError(f'{unknown} scanner paths do not match the manifest')
        groups=defaultdict(lambda:defaultdict(int)); status_counts=defaultdict(int); coverage=defaultdict(lambda:defaultdict(int))
        synthetic=defaultdict(int); entity=defaultdict(int)
        for truth_raw,observed_raw in db.execute('select truth.data, observed.data from truth left join observed using(path)'):
            truth=json.loads(truth_raw)
            observed=json.loads(observed_raw) if observed_raw else dict(categories=[],status='scanned' if complete else 'unscanned',synthetic=None)
            status_counts[observed['status']]+=1
            for dimension in ('format/'+truth['file_format'],'scenario/'+truth['scenario_id'],'bucket/'+truth['sensitivity_bucket']):
                coverage[dimension][observed['status']]+=1
            if observed['status']!='scanned': continue
            expected=set(truth['expected_classifications']); actual=set(observed['categories'])
            dimensions=['overall','format/'+truth['file_format'],'scenario/'+truth['scenario_id'],
                        'bucket/'+truth['sensitivity_bucket']]
            if truth.get('ocr_condition'): dimensions+=['ocr/'+truth['ocr_condition'],'ocr/overall']
            if observed.get('synthetic') is not None: dimensions+=['synthetic_recognition/'+('recognized' if observed['synthetic'] else 'not_recognized')]
            for cat in CATEGORIES:
                outcome=('tp' if cat in actual else 'fn') if cat in expected else ('fp' if cat in actual else 'tn')
                for group in dimensions+['category/'+cat]: groups[group][outcome]+=1
            recognized=observed.get('synthetic')
            synthetic['unknown' if recognized is None else 'recognized' if recognized else 'not_recognized']+=1
        result=dict(unit='file/category decision; micro-averaged across four categories',
                    missing_result_policy='scanned negative' if complete else 'unscanned',
                    status_counts=dict(status_counts),discovery_coverage={k:dict(statuses=dict(v),scanned_fraction=v.get('scanned',0)/sum(v.values())) for k,v in sorted(coverage.items())},synthetic_recognition=dict(synthetic),
                    metrics={k:metrics(v) for k,v in sorted(groups.items())},
                    limitations=['Entity-level accuracy requires scanner entity exports and is not inferred from file classifications.',
                                 'Archive scores apply to outer files. Member paths must be aggregated upstream.',
                                 'All corpus files are synthetic; synthetic recognition specificity cannot be measured.'])
        db.close()
        return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True); p.add_argument('--results',type=Path,required=True)
    p.add_argument('--mapping',type=Path); p.add_argument('--target-root')
    p.add_argument('--complete-export',action='store_true',help='Assert missing files were scanned and had no findings')
    p.add_argument('--output',type=Path)
    a=p.parse_args(argv)
    try:
        result=evaluate(a.manifest,a.results,json.loads(a.mapping.read_text()) if a.mapping else None,a.complete_export,a.target_root)
        payload=json.dumps(result,indent=2)
        if a.output:
            with a.output.open('x') as out: out.write(payload+'\n')
        else: print(payload)
    except (ValueError,KeyError,OSError,sqlite3.IntegrityError) as error: p.exit(2,f'Error: {error}\n')

if __name__=='__main__': main()
