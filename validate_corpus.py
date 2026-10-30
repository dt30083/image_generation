#!/usr/bin/env python3
"""Recheck a corpus against external ground truth without generating files."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from dspm.cli import digest, verify
from dspm.model import content
from dspm.evaluate import normalized_path


def validate(root,manifest,ocr=False):
    root=root.resolve(); count=0
    with tempfile.TemporaryDirectory() as tmp, closing(sqlite3.connect(str(Path(tmp)/'seen.sqlite'))) as seen:
        seen.execute('create table files(path text primary key)')
        with manifest.open() as source:
            for line in source:
                item=json.loads(line); relative=normalized_path(item['path']); path=(root/relative).resolve()
                if root not in path.parents: raise ValueError('File resolves outside corpus')
                seen.execute('insert into files values (?)',(relative,))
                if path.stat().st_size!=item['file_size'] or digest(path)!=item['sha256']:
                    raise ValueError(f'Size/hash mismatch: {relative}')
                if path.suffix!='.'+item['file_format']: raise ValueError(f'Format/extension mismatch: {relative}')
                job=dict(index=int(path.stem),seed=item['generation_seed'],format=item['file_format'],
                         bucket=item['sensitivity_bucket'],technique=item['embedding_technique'],
                         complexity=item['complexity'],records_per_file=item.get('records_per_file'),
                         ocr=item.get('ocr_condition') or 'clean',font=item.get('font_path'))
                if item.get('font_path') and digest(Path(item['font_path']))!=item.get('font_sha256'):
                    raise ValueError('Font changed since generation')
                data=content(job,item['master_seed'],item['synthetic_label'])
                fields=[dict(category=c,label=k,value=v) for c,k,v in data['fields']]
                if (data['counts']!=item['expected_entity_counts'] or data['categories']!=item['expected_classifications']
                    or fields!=item['expected_fields'] or data['profiles']!=item['identity_references']):
                    raise ValueError(f'Ground truth differs from deterministic content: {relative}')
                verify(path,job,data,ocr); count+=1
        seen.commit()
        for path in root.rglob('*'):
            if path.is_file() and not seen.execute('select 1 from files where path=?',(path.relative_to(root).as_posix(),)).fetchone():
                raise ValueError(f'File missing from manifest: {path.relative_to(root)}')
    return count


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--ocr-validate',action='store_true')
    args=parser.parse_args()
    try: print(f'Validated {validate(args.output,args.manifest,args.ocr_validate)} files')
    except Exception as error: parser.exit(2,f'Error: {error}\n')

if __name__=='__main__': main()
