"""No NFS server needed: mutable stat snapshots model other clients and resizing."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from dspm.storage import (StorageBudget,StorageStopped,MountUnavailable,Mount,
                          OutputStore,detect_mount,mount_table,unescape_mount)

GB=1_000_000_000

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.mount=Mount('/tmp','server:/evaluation','nfs','41',os.stat('/tmp').st_dev)
        self.detect=self.stack.enter_context(patch('dspm.storage.detect_mount',return_value=self.mount))
        self.usage=SimpleNamespace(total=100*GB,used=20*GB,free=80*GB)
        self.disk=self.stack.enter_context(patch('dspm.storage.shutil.disk_usage',side_effect=lambda path:self.usage))
        self.stack.enter_context(patch('dspm.storage.os.statvfs',return_value=SimpleNamespace(f_frsize=4096,f_bsize=4096)))
        self.budget=StorageBudget('/tmp/output')

    def test_example_budget_and_default_no_dataset_cap(self):
        state=self.budget.snapshot()
        self.assertEqual(state['reserved_bytes'],20*GB)
        self.assertEqual(state['generation_budget_bytes'],60*GB)
        self.assertIsNone(state['max_dataset_bytes'])

    def test_capacity_increases_and_other_clients_consume_or_free(self):
        self.usage.free=21*GB; self.usage.used=79*GB
        with self.assertRaises(StorageStopped): self.budget.reserve('large',2*GB,2*GB,'dataset')
        self.usage.total=200*GB; self.usage.free=121*GB
        lease=self.budget.reserve('large',70*GB,70*GB,'dataset')
        self.assertEqual(self.budget.snapshot()['reserved_bytes'],40*GB)
        self.budget.settle(lease,'large',70*GB,'dataset')
        self.usage.free=41*GB
        with self.assertRaises(StorageStopped): self.budget.reserve('next',2*GB,2*GB,'dataset')
        self.usage.free=80*GB
        self.budget.reserve('next',2*GB,2*GB,'dataset')

    def test_cached_nfs_free_space_is_not_spent_twice(self):
        self.usage.free=22*GB; self.budget.refresh()
        lease=self.budget.reserve('a',2*GB,2*GB,'dataset')
        self.budget.settle(lease,'a',2*GB,'dataset')
        # Server statistics still report 22 GB; the coordinator knows 2 GB was written.
        with self.assertRaises(StorageStopped): self.budget.reserve('b',1,1,'dataset')
        self.budget.forget('a')
        self.budget.reserve('b',GB,GB,'dataset')

    def test_atomic_worker_reservations(self):
        barrier=threading.Barrier(2)
        def worker(key):
            barrier.wait()
            try: return self.budget.reserve(key,40*GB,40*GB,'dataset')
            except StorageStopped: return None
        with ThreadPoolExecutor(2) as pool: leases=list(pool.map(worker,('one','two')))
        self.assertEqual(sum(x is not None for x in leases),1)
        self.assertEqual(self.budget.snapshot()['in_progress_reserved_bytes'],40*GB)

    def test_dataset_cap_includes_partials_but_not_canonical_duplicates(self):
        budget=StorageBudget('/tmp/output',max_bytes=20)
        lease=budget.reserve('a',15,15,'dataset'); budget.settle(lease,'a',15,'dataset')
        with self.assertRaises(StorageStopped): budget.reserve('b',6,6,'dataset')
        temporary=budget.reserve('a.canonical',15,15,'temporary')
        budget.settle(temporary,'a.canonical',15,'temporary')
        self.assertEqual(budget.snapshot()['dataset_bytes'],15)
        budget.resize('a',5,'dataset'); budget.reserve('b',10,10,'dataset')

    def test_temporary_reservations_and_report_headroom_protect_reserve(self):
        self.budget.set_headroom(10*GB)
        self.budget.reserve('canonical',40*GB,40*GB,'temporary')
        with self.assertRaises(StorageStopped): self.budget.reserve('data',11*GB,11*GB,'dataset')
        self.budget.reserve('report',5*GB,5*GB,'metadata')

    def test_refresh_for_each_write_even_below_byte_and_file_intervals(self):
        before=self.disk.call_count
        lease=self.budget.reserve('a',1,1,'dataset'); self.budget.settle(lease,'a',1,'dataset')
        self.usage.free=19*GB
        with self.assertRaises(StorageStopped): self.budget.reserve('a',1,1,'dataset')
        self.assertGreaterEqual(self.disk.call_count-before,2)

    def test_explicit_wait_resumes_after_capacity_growth(self):
        self.usage.free=10*GB
        budget=StorageBudget('/tmp/output',wait_for_space=True,poll_seconds=.01)
        with ThreadPoolExecutor(1) as pool:
            result=pool.submit(budget.reserve,'a',GB,GB,'dataset')
            time.sleep(.03); self.assertFalse(result.done())
            self.usage.total=200*GB; self.usage.free=100*GB
            self.assertIsInstance(result.result(timeout=2),int)

    def test_wait_never_resumes_on_replaced_mount_and_cap_does_not_wait(self):
        budget=StorageBudget('/tmp/output',wait_for_space=True,max_bytes=1,poll_seconds=.01)
        with self.assertRaises(StorageStopped): budget.reserve('a',2,2,'dataset')
        self.detect.return_value=Mount('/tmp','other:/share','nfs','99',self.mount.device)
        with self.assertRaises(MountUnavailable): budget.reserve('a',1,1,'dataset')

    def test_stat_errors_fail_closed(self):
        self.disk.side_effect=OSError('NFS unavailable')
        with self.assertRaises(MountUnavailable): self.budget.reserve('a',1,1,'dataset')

    def test_metadata_remains_writable_after_worker_stop(self):
        self.budget.abort('Dataset cap')
        with self.assertRaises(StorageStopped): self.budget.reserve('a',1,1,'dataset')
        self.budget.reserve('report',1,1,'metadata')


class MountAndWriteTests(unittest.TestCase):
    def test_missing_nfs_never_creates_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'must-not-exist'
            with self.assertRaises(MountUnavailable),patch('dspm.storage.mount_table',return_value=[Mount('/','local','ext4','1',os.stat(tmp).st_dev)]):
                StorageBudget(str(output))
            self.assertFalse(output.exists())

    def test_linux_mountinfo_parser_and_escaped_paths(self):
        text='41 1 0:55 / /mnt/test\\040share rw - nfs4 server:/data rw\n'
        with patch('dspm.storage.sys.platform','linux'),patch('pathlib.Path.read_text',return_value=text):
            mount=mount_table()[0]
            self.assertEqual(mount.point,'/mnt/test share'); self.assertEqual(mount.filesystem,'nfs4')
        self.assertEqual(unescape_mount('/a\\134b'),'/a\\b')

    def test_wrong_mount_rejected(self):
        device=os.stat('/tmp').st_dev
        with patch('dspm.storage.mount_table',return_value=[Mount('/','local','ext4','1',device),Mount('/tmp','server:/a','nfs','2',device)]):
            with self.assertRaises(MountUnavailable): detect_mount('/tmp/corpus','/other')

    def test_chunked_large_write_checks_and_mount_loss_leave_local_target_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'output'; budget=StorageBudget(str(root),allow_local=True,check_bytes=1024)
            store=OutputStore(root,budget,'job',1024,create=True); self.addCleanup(store.close)
            path=store.path('large.txt'); before=budget.refreshes
            with path.open('wb') as stream: stream.write(b'x'*10000)
            self.assertGreaterEqual(budget.refreshes-before,10)
            self.assertEqual(path.stat().st_size,10000)
            # Rename simulates a disappearing namespace while the descriptor stays pinned.
            root.rename(Path(tmp)/'old-mount'); root.mkdir()
            # The namespace is rejected before another write when the mount verifier reports loss.
            with patch.object(budget,'verify_mount',side_effect=MountUnavailable('unmounted')):
                with self.assertRaises(MountUnavailable): store.path('fallback.txt').write_bytes(b'never')
            self.assertFalse((root/'fallback.txt').exists())
            # Even if unmount races just after verification, openat remains
            # attached to the old directory instead of the replacement path.
            with patch.object(budget,'verify_mount',return_value=budget.mount()):
                store.path('pinned.txt').write_bytes(b'original filesystem')
            self.assertFalse((root/'pinned.txt').exists())
            self.assertEqual((Path(tmp)/'old-mount'/'pinned.txt').read_bytes(),b'original filesystem')
            self.assertEqual((Path(tmp)/'old-mount'/'large.txt').stat().st_size,10000)

    def test_native_image_encoder_cannot_bypass_the_budget(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            budget=StorageBudget(tmp,allow_local=True,max_bytes=1000)
            store=OutputStore(tmp,budget,'image'); self.addCleanup(store.close)
            path=store.path('image.jpg')
            with self.assertRaises(StorageStopped):
                with path.open('wb') as out, Image.new('RGB',(2000,2000),'white') as image:
                    image.save(out,format='JPEG')
            self.assertLessEqual(path.stat().st_size,1000)

    def test_symlink_subdirectory_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp,tempfile.TemporaryDirectory() as outside:
            budget=StorageBudget(tmp,allow_local=True); store=OutputStore(tmp,budget,'job')
            self.addCleanup(store.close); (Path(tmp)/'escape').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(OSError): store.path('escape/forbidden.txt').write_bytes(b'never')
            self.assertFalse((Path(outside)/'forbidden.txt').exists())


class StorageIntegrationTests(unittest.TestCase):
    def test_parallel_cap_stops_with_valid_completed_manifest_and_reports(self):
        from validate_corpus import validate
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'corpus'
            command=[sys.executable,'synthetic_sensitive_images.py','--count','30','--formats','txt','--output',str(output),
                     '--allow-local-output','--max-size-gb','0.000005','--workers','2','--capacity-check-bytes','512']
            result=subprocess.run(command,capture_output=True,text=True,timeout=60)
            self.assertEqual(result.returncode,3,result.stderr+result.stdout)
            report=json.loads(output.with_suffix('.manifest.run.json').read_text())
            self.assertEqual(report['status'],'stopped_storage')
            self.assertGreater(report['completed_files'],0); self.assertLess(report['completed_files'],30)
            self.assertEqual(report['files_remaining'],30-report['completed_files'])
            self.assertLessEqual(report['dataset_size_bytes'],5000)
            self.assertEqual(validate(output,output.with_suffix('.manifest.jsonl')),report['completed_files'])
            self.assertIn('maximum dataset size',report['stop_reason'])
            self.assertFalse(list(output.rglob('.partial-*')))
            # A new run cannot clear or overwrite completed files.
            files={p:p.read_bytes() for p in output.rglob('*.txt')}
            result=subprocess.run(command,capture_output=True,text=True,timeout=60)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(files,{p:p.read_bytes() for p in files})

    def test_nfs_default_rejects_local_cli_without_creating_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'not-nfs'
            result=subprocess.run([sys.executable,'enterprise_generator.py','--output',str(output),'--count','10','--formats','txt'],capture_output=True,text=True,timeout=30)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('not NFS',result.stderr); self.assertFalse(output.exists())

if __name__=='__main__': unittest.main()
