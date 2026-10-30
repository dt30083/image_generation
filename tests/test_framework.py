import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dspm.model import FORMATS, OPTIONAL, SENSITIVITY, TECHNIQUES, Plan, allocate, content, profile, CARDS
from dspm.cli import generate_one, verify, size_arg, main
from dspm.evaluate import evaluate


class PlanningTests(unittest.TestCase):
    def test_default_counts(self):
        plan=Plan(100000,FORMATS,SENSITIVITY,'LOW',42)
        self.assertEqual(plan.formats,{k:v*1000 for k,v in FORMATS.items()})
        self.assertEqual(plan.sensitivity,{k:v*1000 for k,v in SENSITIVITY.items()})
        self.assertEqual(sum(x[3] for x in plan.blocks),100000)
        self.assertEqual(list(plan.jobs())[:3],list(plan.jobs())[:3])

    def test_deterministic_coverage_and_bounded_plan(self):
        plan=Plan(1000000,FORMATS,SENSITIVITY,'HIGH',123)
        self.assertLess(len(plan.blocks),1000)
        for fmt in FORMATS:
            for tech in TECHNIQUES[fmt]:
                for bucket in SENSITIVITY:
                    self.assertTrue(any((f,c,t)==(fmt,bucket,tech) for f,c,t,n in plan.blocks))

    def test_reject_insufficient_coverage(self):
        with self.assertRaises(ValueError): Plan(10,FORMATS,SENSITIVITY,'LOW',1)

    def test_allocations_and_sizes(self):
        self.assertEqual(sum(allocate(101,FORMATS).values()),101)
        self.assertEqual(size_arg('1MB:1GB'),(1000000,1000000000))
        for invalid in ('-1:5','1GB:2GB','nope','10:1'):
            with self.assertRaises(ValueError): size_arg(invalid)
        for invalid in (dict(a=-1),dict(a=float('nan')),dict(a=0)):
            with self.assertRaises(ValueError): allocate(100,invalid)

    def test_profiles_and_test_pans(self):
        from datetime import date
        p=profile(42,4)
        self.assertEqual(p,profile(42,1013))
        self.assertGreaterEqual(date.fromisoformat(p['hired']).year-date.fromisoformat(p['dob']).year,18)
        for network,pan in CARDS:
            digits=list(map(int,pan)); transformed=[]
            for i,d in enumerate(reversed(digits)):
                d=d*2 if i%2 else d; transformed.append(d-9 if d>9 else d)
            self.assertEqual(sum(transformed)%10,0,network)

    def test_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'corpus'
            with patch('builtins.print'):
                main(['--dry-run','--output',str(output)])
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_manifest_inside_target_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                main(['--dry-run','--output',tmp,'--manifest',str(Path(tmp)/'truth.jsonl')])
            self.assertEqual(ctx.exception.code,2)



class WriterTests(unittest.TestCase):
    def test_every_format_bucket_and_embedding(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=dict(output=tmp,seed=42,label='neutral',sizes=(0,20_000_000),ocr_validate=False)
            index=0
            for fmt,techniques in TECHNIQUES.items():
                for technique in techniques:
                    # Positive, negative, ambiguous, and mixed coverage for every technique.
                    for bucket in ('pii','pci','phi','financial','mixed','none','ambiguous'):
                        job=dict(index=index,format=fmt,bucket=bucket,technique=technique,seed=42+index,complexity='HIGH')
                        with self.subTest(fmt=fmt,technique=technique,bucket=bucket):
                            item=generate_one((job,config))
                            self.assertEqual(item['file_size'],(Path(tmp)/item['path']).stat().st_size)
                            self.assertEqual(item['sensitive_data_types'],list(('pii','pci','phi','financial')) if bucket=='mixed' else [bucket] if bucket not in ('none','ambiguous') else [])
                        index+=1

    def test_repeatable_hashes_and_no_clobber(self):
        for fmt in TECHNIQUES:
            with self.subTest(fmt=fmt),tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b:
                job=dict(index=0,format=fmt,bucket='mixed',technique=TECHNIQUES[fmt][0],seed=7,complexity='LOW')
                config=dict(output=a,seed=42,label='neutral',sizes=(0,5_000_000),ocr_validate=False)
                first=generate_one((job,config)); second=generate_one((job,dict(config,output=b)))
                self.assertEqual(first['sha256'],second['sha256'])
                with self.assertRaises(FileExistsError): generate_one((job,config))

    def test_actual_content_tampering_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            job=dict(index=0,format='txt',bucket='pii',technique='body',seed=7,complexity='LOW')
            config=dict(output=tmp,seed=42,label='neutral',sizes=(0,100000),ocr_validate=False)
            item=generate_one((job,config)); path=Path(tmp)/item['path']; path.write_text('Nothing here')
            with self.assertRaises(ValueError): verify(path,job,content(job,42))

    def test_meaningful_size_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            job=dict(index=1,format='json',bucket='pii',technique='body',seed=7,complexity='LOW')
            item=generate_one((job,dict(output=tmp,seed=42,label='neutral',sizes=(100000,1000000),ocr_validate=False)))
            self.assertGreaterEqual(item['file_size'],100000)
            self.assertIn('Work order',(Path(tmp)/item['path']).read_text())


class IntegrationTests(unittest.TestCase):
    def test_worker_independent_corpus_and_revalidation(self):
        import subprocess
        import sys
        from validate_corpus import validate
        with tempfile.TemporaryDirectory() as tmp:
            first=Path(tmp)/'one'; second=Path(tmp)/'two'
            for output,workers in ((first,'1'),(second,'2')):
                subprocess.run([sys.executable,'enterprise_generator.py','--count','10','--formats','txt',
                                '--output',str(output),'--workers',workers,'--allow-local-output'],check=True,capture_output=True,text=True)
                self.assertEqual(validate(output,output.with_suffix('.manifest.jsonl')),10)
            self.assertEqual(first.with_suffix('.manifest.jsonl').read_bytes(),second.with_suffix('.manifest.jsonl').read_bytes())
            (first/'orphan.txt').write_text('extra record')
            with self.assertRaises(ValueError): validate(first,first.with_suffix('.manifest.jsonl'))

    def test_high_volume_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            job=dict(index=1,format='csv',bucket='pii',technique='table',seed=7,complexity='LOW')
            item=generate_one((job,dict(output=tmp,seed=42,label='neutral',sizes=(0,1_000_000),ocr_validate=False,records_per_file=1000)))
            self.assertEqual(item['expected_entity_counts']['pii'],4000)
            self.assertEqual(len(item['identity_references']),1000)


class EvaluationTests(unittest.TestCase):
    def test_known_outcomes_and_unscanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest=Path(tmp)/'manifest.jsonl'; result=Path(tmp)/'result.csv'
            truth=[]
            for name,cats in [('a',['pii']),('b',['pci']),('c',[]),('d',['phi'])]:
                truth.append(dict(path=name+'.txt',expected_classifications=cats,file_format='txt',scenario_id='body',sensitivity_bucket='none',ocr_condition=None))
            manifest.write_text(''.join(json.dumps(x)+'\n' for x in truth))
            result.write_text('path,categories,status,synthetic\na.txt,pii,scanned,true\nb.txt,,scanned,false\nc.txt,pci,scanned,\n')
            report=evaluate(manifest,result)
            self.assertEqual(report['status_counts'],dict(scanned=3,unscanned=1))
            m=report['metrics']['overall']
            self.assertEqual((m['tp'],m['fp'],m['tn'],m['fn']),(1,1,9,1))
            self.assertEqual(m['precision'],.5); self.assertEqual(m['recall'],.5)
            self.assertEqual(report['synthetic_recognition'],dict(recognized=1,not_recognized=1,unknown=1))
            self.assertEqual(evaluate(manifest,result,complete=True)['metrics']['overall']['fn'],2)

    def test_unknown_classes_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest=Path(tmp)/'m.jsonl'; results=Path(tmp)/'r.jsonl'
            manifest.write_text(json.dumps(dict(path='a.txt'))+'\n')
            results.write_text(json.dumps(dict(path='a.txt',categories=['Unknown classifier']))+'\n')
            with self.assertRaises(ValueError): evaluate(manifest,results)

if __name__=='__main__': unittest.main()
