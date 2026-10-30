"""Capacity reservations and descriptor-relative output on a pinned filesystem.

Reservations coordinate this run's workers. Other NFS clients are observed through
fresh filesystem statistics; they cannot be locked by a client-side generator.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import io
import math
from multiprocessing.managers import BaseManager
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time


class StorageStopped(OSError):
    """Space cannot be reserved without violating the configured policy."""


class MountUnavailable(OSError):
    """The pinned filesystem is absent, replaced, or inaccessible."""


@dataclass(frozen=True)
class Mount:
    point: str
    source: str
    filesystem: str
    identifier: str
    device: int


def unescape_mount(value):
    return re.sub(r'\\([0-7]{3})',lambda match:chr(int(match[1],8)),value)


def mount_table():
    if sys.platform.startswith('linux'):
        result=[]
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            before,after=line.split(' - ',1); fields=before.split(); tail=after.split()
            major,minor=map(int,fields[2].split(':'))
            result.append(Mount(unescape_mount(fields[4]),unescape_mount(tail[1]),tail[0],
                                fields[0],os.makedev(major,minor)))
        return result
    if sys.platform=='darwin':
        result=[]
        output=subprocess.run(['/sbin/mount'],check=True,capture_output=True,text=True).stdout
        for line in output.splitlines():
            match=re.match(r'(.+) on (.+) \(([^,)]+)[,)]',line)
            if match:
                source,point,kind=match.groups()
                result.append(Mount(point,source,kind,source+':'+point,os.stat(point).st_dev))
        return result
    raise MountUnavailable('NFS mount verification supports Linux and macOS only')


def nearest_existing(path):
    path=Path(path)
    while not path.exists(): path=path.parent
    return path


def detect_mount(path,expected=None,allow_local=False):
    path=Path(path).resolve()
    candidates=[entry for entry in mount_table()
                if path==Path(entry.point) or Path(entry.point) in path.parents]
    if not candidates: raise MountUnavailable(f'No mount found for {path}')
    # Last stacked entry wins for equally long mount paths.
    mount=max(enumerate(candidates),key=lambda pair:(len(Path(pair[1].point).parts),pair[0]))[1]
    if allow_local and mount.filesystem not in ('nfs','nfs4'):
        anchor=Path(expected).resolve() if expected else nearest_existing(path.parent).resolve()
        if path!=anchor and anchor not in path.parents: raise MountUnavailable('Local test output escaped its pinned directory')
        device=os.stat(nearest_existing(path)).st_dev
        if os.stat(anchor).st_dev!=device: raise MountUnavailable('Local test filesystem changed')
        return Mount(str(anchor),'explicit-local-test','local',str(device),device)
    if expected and Path(mount.point)!=Path(expected).resolve():
        raise MountUnavailable(f'Expected mount {expected}; output resolves to {mount.point}')
    if not allow_local and mount.filesystem not in ('nfs','nfs4'):
        raise MountUnavailable(f'{path} is on {mount.filesystem}, not NFS; mount the share or explicitly use --allow-local-output for tests')
    if os.stat(nearest_existing(path)).st_dev!=mount.device:
        raise MountUnavailable('Mount table and output filesystem device disagree')
    return mount


class StorageBudget:
    """One instance shared by all workers through a manager server.

    A reservation covers a write before it happens. It stays outstanding until the
    unbuffered write completes. Final data, active partials, duplicate temporary
    files, and report headroom are accounted separately.
    """
    def __init__(self,output,reserve_percent=20,max_bytes=None,expected_mount=None,
                 allow_local=False,check_interval=100,check_bytes=8*1024*1024,
                 wait_for_space=False,poll_seconds=5,identity=None):
        if not math.isfinite(reserve_percent) or not 0<=reserve_percent<100:
            raise ValueError('Reserve percent must be finite and in [0,100)')
        if max_bytes is not None and max_bytes<=0: raise ValueError('Dataset size cap must be positive')
        if check_interval<1 or check_bytes<1 or not math.isfinite(poll_seconds) or poll_seconds<=0:
            raise ValueError('Capacity intervals and byte threshold must be positive')
        self.output=str(Path(output).resolve()); self.expected_mount=expected_mount
        self.allow_local=allow_local; self.reserve_percent=reserve_percent; self.max_bytes=max_bytes
        self.check_interval=check_interval; self.check_bytes=check_bytes
        self.wait_for_space=wait_for_space; self.poll_seconds=poll_seconds
        self.identity=Mount(**identity) if identity else detect_mount(self.output,expected_mount,allow_local)
        self.lock=threading.RLock(); self.pending={}; self.sizes={}; self.committed=0
        self.sequence=0; self.completed_files=0; self.headroom=0; self.cancelled=None
        self.last={}; self.peak_pending=0; self.refreshes=0
        self.available_estimate=None; self.previous_reported_free=None
        self.refresh()

    def mount(self): return asdict(self.identity)

    def verify_mount(self):
        if self.identity.filesystem=='local':
            if (os.stat(self.identity.point).st_dev!=self.identity.device
                or os.stat(nearest_existing(self.output)).st_dev!=self.identity.device):
                raise MountUnavailable('Pinned local test filesystem changed')
            return asdict(self.identity)
        current=detect_mount(self.output,self.identity.point if self.identity.filesystem=='local' else self.expected_mount,self.allow_local)
        if current!=self.identity:
            raise MountUnavailable('Output mount changed or disappeared; refusing local fallback or replacement filesystem')
        return asdict(current)

    def refresh(self):
        with self.lock:
            try:
                self.verify_mount()
                usage=shutil.disk_usage(nearest_existing(self.output))
                stats=os.statvfs(nearest_existing(self.output))
            except OSError as error:
                raise MountUnavailable(f'Cannot query pinned filesystem: {error}') from error
            if self.available_estimate is None:
                self.available_estimate=usage.free
            else:
                # Never spend the same cached free-space observation twice.
                released=max(0,usage.free-self.previous_reported_free)
                self.available_estimate=min(usage.free,self.available_estimate+released)
            self.previous_reported_free=usage.free
            reserved=math.ceil(usage.total*self.reserve_percent/100)
            self.refreshes+=1
            self.last=dict(total_bytes=usage.total,used_bytes=usage.used,available_bytes=usage.free,
                           reserved_bytes=reserved,generation_budget_bytes=max(0,usage.free-reserved),
                           block_size=stats.f_frsize or stats.f_bsize,mount=asdict(self.identity))
            return self._snapshot()

    def _dataset(self): return self.committed+sum(size for size,kind in self.sizes.values() if kind=='dataset')

    def _snapshot(self):
        self.last['conservative_available_bytes']=max(0,self.available_estimate or 0)
        self.last['generation_budget_bytes']=max(0,(self.available_estimate or 0)-self.last.get('reserved_bytes',0))
        pending=sum(physical for physical,_,_ in self.pending.values())
        capacity_left=max(0,self.last.get('generation_budget_bytes',0)-pending-self.headroom)
        cap_left=None if self.max_bytes is None else max(0,self.max_bytes-self._dataset()-sum(v[1] for v in self.pending.values() if v[2]=='dataset'))
        return self.last|dict(dataset_bytes=self._dataset(),committed_dataset_bytes=self.committed,completed_files=self.completed_files,
                              in_progress_reserved_bytes=pending,report_headroom_bytes=self.headroom,
                              remaining_generation_budget_bytes=capacity_left if cap_left is None else min(capacity_left,cap_left),
                              remaining_dataset_cap_bytes=cap_left,
                              max_dataset_bytes=self.max_bytes,capacity_queries=self.refreshes,
                              peak_in_progress_reserved_bytes=self.peak_pending)

    def snapshot(self,refresh=True):
        with self.lock: return self.refresh() if refresh else self._snapshot()

    def set_headroom(self,size):
        with self.lock: self.headroom=size

    def reserve(self,key,physical_growth,logical_growth,kind):
        """Atomic check and reservation; no sleeping while holding the shared lock."""
        notified=False
        while True:
            with self.lock:
                if self.cancelled and kind!='metadata': raise StorageStopped(self.cancelled)
                state=self.refresh()  # every raw write is stronger than the requested interval
                pending_physical=sum(value[0] for value in self.pending.values())
                pending_dataset=sum(value[1] for value in self.pending.values() if value[2]=='dataset')
                if self.max_bytes is not None and kind=='dataset' and self._dataset()+pending_dataset+logical_growth>self.max_bytes:
                    raise StorageStopped('Optional maximum dataset size reached')
                headroom=0 if kind=='metadata' else self.headroom
                if state['conservative_available_bytes']-state['reserved_bytes']-pending_physical-headroom>=physical_growth:
                    self.sequence+=1; lease=self.sequence
                    self.pending[lease]=(physical_growth,logical_growth,kind)
                    self.peak_pending=max(self.peak_pending,pending_physical+physical_growth)
                    return lease
                reason='Insufficient available capacity above the percentage reserve (including in-progress writes and temporary/report space)'
                if not self.wait_for_space: raise StorageStopped(reason)
                if not notified:
                    print(reason+'; waiting for capacity (--wait-for-space).',file=sys.stderr,flush=True)
                    notified=True
            time.sleep(self.poll_seconds)

    def _blocks(self,size):
        block=self.last['block_size']
        return math.ceil(size/block)*block

    def settle(self,lease,key,size,kind):
        with self.lock:
            self.pending.pop(lease,None)
            old=self.sizes.get(key,(0,kind))[0]
            self.available_estimate-=self._blocks(size)-self._blocks(old)
            self.sizes[key]=(size,kind)

    def resize(self,key,size,kind):
        with self.lock:
            old=self.sizes.get(key,(size,kind))[0]
            if size<old: self.available_estimate+=self._blocks(old)-self._blocks(size)
            self.sizes[key]=(size,kind)

    def forget(self,key):
        with self.lock:
            old=self.sizes.pop(key,None)
            if old: self.available_estimate+=self._blocks(old[0])

    def replaced(self,source,target,size,kind):
        with self.lock:
            # The canonical bytes remain allocated under their new name; only
            # the overwritten original's blocks have been released.
            self.sizes.pop(source,None)
            old=self.sizes.get(target,(0,kind))[0]
            self.available_estimate+=self._blocks(old)
            self.sizes[target]=(size,kind)

    def finish(self,prefix,final_size):
        with self.lock:
            for key in list(self.sizes):
                if key.startswith(prefix): self.sizes.pop(key)
            self.committed+=final_size; self.completed_files+=1


    def abort(self,reason):
        with self.lock: self.cancelled=reason


class BudgetManager(BaseManager): pass
BudgetManager.register('StorageBudget',StorageBudget)


class GuardedRaw(io.RawIOBase):
    """Unbuffered, seekable file with write-ahead shared reservations."""
    def __init__(self,fd,store,key,kind,mode):
        super().__init__(); self.file=io.FileIO(fd,mode,closefd=True)
        self.store,self.key,self.kind=store,key,kind
    def readable(self): return self.file.readable()
    def writable(self): return self.file.writable()
    def seekable(self): return True
    def fileno(self):
        # Pillow/native codecs must use write(), not bypass reservations via an fd.
        raise io.UnsupportedOperation('Use the guarded write interface')
    def tell(self): return self.file.tell()
    def seek(self,*args): return self.file.seek(*args)
    def readinto(self,buffer): return self.file.readinto(buffer)
    def write(self,data):
        data=memoryview(data); written=0
        while written<len(data):
            chunk=data[written:written+min(self.store.check_bytes,1024*1024)]
            old=os.fstat(self.file.fileno()).st_size
            end=max(old,self.tell()+len(chunk)); block=self.store.block_size
            physical=(math.ceil(end/block)-math.ceil(old/block))*block
            lease=self.store.budget.reserve(self.key,physical,end-old,self.kind)
            try:
                count=self.file.write(chunk)
                if not count: raise OSError('Short filesystem write')
                written+=count
            finally:
                self.store.budget.settle(lease,self.key,os.fstat(self.file.fileno()).st_size,self.kind)
        return written
    def truncate(self,size=None):
        self.store.verify(); size=self.tell() if size is None else size
        old=os.fstat(self.file.fileno()).st_size
        if size>old: raise ValueError('Sparse file expansion is not supported')
        value=self.file.truncate(size); self.store.budget.resize(self.key,size,self.kind); return value
    def flush(self):
        if not self.file.closed: self.file.flush()
    def close(self):
        if not self.closed:
            try: super().close()
            finally: self.file.close()


class OutputStore:
    """Open directories once; subsequent file operations use dir_fd + O_NOFOLLOW.

    Descriptors remain attached to the original filesystem even if the mount path
    disappears between a check and an open. No absolute output-path opens occur.
    """
    def __init__(self,root,budget,token,check_bytes=8*1024*1024,metadata=False,create=False):
        self.root=Path(root).resolve(); self.budget=budget; self.token=str(token)+'/'
        self.check_bytes=check_bytes; self.metadata=metadata; self.fd=None
        identity=Mount(**budget.verify_mount()); self.device=identity.device
        # A metadata directory may be outside the dataset root, but must be on
        # this store's pinned filesystem and mount.
        current=detect_mount(self.root,identity.point,allow_local=identity.filesystem not in ('nfs','nfs4'))
        if current!=identity: raise MountUnavailable('Store directory is on a different filesystem')
        mount_fd=os.open(identity.point,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            if os.fstat(mount_fd).st_dev!=self.device: raise MountUnavailable('Mount changed while opening directory')
            relative=self.root.relative_to(identity.point)
            fd=os.dup(mount_fd)
            try:
                for part in relative.parts:
                    if create: self.mkdir(fd,part)
                    next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                    os.close(fd); fd=next_fd
                    if os.fstat(fd).st_dev!=self.device: raise MountUnavailable('Output crossed into another filesystem')
                self.fd=fd
            except BaseException:
                os.close(fd); raise
        finally: os.close(mount_fd)
        self.block_size=budget.snapshot(False)['block_size']

    def mkdir(self,parent,name):
        try:
            os.stat(name,dir_fd=parent,follow_symlinks=False)
            return
        except FileNotFoundError: pass
        block=self.budget.snapshot(False)['block_size']
        key=self.token+'directory/'+str(os.fstat(parent).st_ino)+'/'+name
        kind='metadata' if self.metadata else 'temporary'
        lease=self.budget.reserve(key,block,0,kind)
        created=False
        try:
            try: os.mkdir(name,dir_fd=parent); created=True
            except FileExistsError: pass
        finally: self.budget.settle(lease,key,block if created else 0,kind)

    def verify(self):
        self.budget.verify_mount()
        if os.fstat(self.fd).st_dev!=self.device: raise MountUnavailable('Pinned directory device changed')

    @contextmanager
    def parent(self,relative,create=False):
        parts=Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(x in ('..','.') for x in parts):
            raise ValueError('Expected a safe relative output path')
        self.verify(); fd=os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create: self.mkdir(fd,part)
                next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                os.close(fd); fd=next_fd
                if os.fstat(fd).st_dev!=self.device: raise MountUnavailable('Output subdirectory crossed filesystems')
            yield fd,parts[-1]
        finally: os.close(fd)

    def path(self,relative): return GuardedPath(self,Path(relative))
    def key(self,relative): return self.token+str(relative)
    def kind(self,relative):
        if self.metadata: return 'metadata'
        return 'temporary' if str(relative).endswith('.canonical') else 'dataset'
    def close(self):
        if self.fd is not None: os.close(self.fd); self.fd=None
    def publish(self,source,target):
        size=source.stat().st_size
        with self.parent(source.relative) as (sfd,sname),self.parent(target.relative) as (tfd,tname):
            self.verify()
            os.link(sname,tname,src_dir_fd=sfd,dst_dir_fd=tfd,follow_symlinks=False)
            os.unlink(sname,dir_fd=sfd)
        self.budget.finish(self.token,size)


class GuardedPath:
    def __init__(self,store,relative): self.store,self.relative=store,relative
    @property
    def name(self): return self.relative.name
    @property
    def suffix(self): return self.relative.suffix
    def __str__(self): return str(self.store.root/self.relative)
    def with_name(self,name): return self.store.path(self.relative.with_name(name))
    def with_suffix(self,suffix): return self.store.path(self.relative.with_suffix(suffix))
    def stat(self):
        with self.store.parent(self.relative) as (fd,name): return os.stat(name,dir_fd=fd,follow_symlinks=False)
    def exists(self):
        try: self.stat(); return True
        except FileNotFoundError: return False
    def open(self,mode='r',encoding=None,newline=None):
        writing=any(x in mode for x in 'wax+')
        flags=os.O_RDWR if '+' in mode else os.O_WRONLY if writing else os.O_RDONLY
        if 'w' in mode: flags|=os.O_CREAT  # truncate only after a verified descriptor open
        if 'x' in mode: flags|=os.O_CREAT|os.O_EXCL
        if 'a' in mode: flags|=os.O_CREAT
        with self.store.parent(self.relative,create=writing) as (parent,name):
            self.store.verify()
            fd=os.open(name,flags|os.O_NOFOLLOW,0o600,dir_fd=parent)
        try:
            if os.fstat(fd).st_dev!=self.store.device: raise MountUnavailable('File device mismatch')
            if 'w' in mode: os.ftruncate(fd,0)
            if 'a' in mode: os.lseek(fd,0,os.SEEK_END)
            kind=self.store.kind(self.relative); key=self.store.key(self.relative)
            raw=GuardedRaw(fd,self.store,key,kind,'r+' if '+' in mode else 'w' if writing else 'r')
            fd=None
            if writing: self.store.budget.resize(key,os.fstat(raw.file.fileno()).st_size,kind)
            buffered=io.BufferedRandom(raw,65536) if '+' in mode else io.BufferedWriter(raw,65536) if writing else io.BufferedReader(raw,65536)
            return buffered if 'b' in mode else io.TextIOWrapper(buffered,encoding=encoding or 'utf-8',newline=newline)
        finally:
            if fd is not None: os.close(fd)
    def unlink(self,missing_ok=False):
        try:
            with self.store.parent(self.relative) as (fd,name): os.unlink(name,dir_fd=fd)
        except FileNotFoundError:
            if not missing_ok: raise
        self.store.budget.forget(self.store.key(self.relative))
    def replace(self,target):
        size=self.stat().st_size
        try: old=target.stat().st_size
        except FileNotFoundError: old=0
        key=self.store.key(target.relative); kind=self.store.kind(target.relative)
        lease=self.store.budget.reserve(key,0,max(0,size-old),kind)
        try:
            with self.store.parent(self.relative) as (sfd,sname),self.store.parent(target.relative) as (tfd,tname):
                self.store.verify(); os.replace(sname,tname,src_dir_fd=sfd,dst_dir_fd=tfd)
            self.store.budget.replaced(self.store.key(self.relative),key,size,kind)
        finally:
            self.store.budget.settle(lease,key,target.stat().st_size if target.exists() else 0,kind)
    def read_bytes(self):
        with self.open('rb') as source: return source.read()
    def write_bytes(self,value):
        with self.open('wb') as out: return out.write(value)
    def write_text(self,value,encoding='utf-8'):
        with self.open('w',encoding=encoding) as out: return out.write(value)
