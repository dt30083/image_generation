"""Deterministic planning, fictional profiles, and explicit classification policy."""
from __future__ import annotations
import hashlib
import math
import random
from collections import Counter
from datetime import date

FORMATS = dict(pdf=25, docx=15, xlsx=15, csv=10, txt=5, json=5, xml=5,
               jpg=7, png=5, tiff=3, zip=3, pptx=2)
OPTIONAL = ('bmp', 'gif', 'html', 'eml', 'msg', 'webp')
SENSITIVITY = dict(pii=20, pci=10, phi=10, financial=10, mixed=20, none=20, ambiguous=10)
CATEGORIES = ('pii', 'pci', 'phi', 'financial')
# Processor-published fixed test numbers; never synthesize arbitrary Luhn-valid PANs.
# https://developer.paypal.com/braintree/docs/reference/general/testing/python#credit-card-numbers
CARDS = (('Visa', '4111111111111111'), ('Mastercard', '5555555555554444'),
         ('American Express', '378282246310005'), ('Discover', '6011111111111117'))
ICD = (('E11.9', 'Type 2 diabetes mellitus without complications'),
       ('I10', 'Essential primary hypertension'), ('J45.909', 'Unspecified asthma uncomplicated'))
# Counts describe labeled field occurrences, not legal/regulatory conclusions.
SIZE_PROFILES = {'small': (0, 100_000), 'standard': (0, 5_000_000),
                 'medium': (100_000, 1_000_000), 'large': (1_000_000, 10_000_000),
                 'stress': (10_000_000, 1_000_000_000)}
TECHNIQUES = {
    'pdf': ('body', 'header', 'footer', 'table', 'image', 'metadata', 'split'),
    'docx': ('body', 'header', 'footer', 'table', 'image', 'metadata', 'split', 'embedded'),
    'xlsx': ('body', 'table', 'hidden', 'image', 'metadata', 'split'),
    'pptx': ('body', 'table', 'image', 'metadata', 'split'),
    'zip': ('archive', 'nested_archive'),
    'jpg': ('image', 'metadata'), 'png': ('image', 'metadata'),
    'tiff': ('image', 'metadata'), 'gif': ('image', 'metadata'),
    'webp': ('image', 'metadata'), 'bmp': ('image',),
    'html': ('body', 'table', 'split'), 'eml': ('body', 'attachment'),
    'msg': ('body',), 'csv': ('table',), 'txt': ('body', 'split'),
    'json': ('body',), 'xml': ('body',),
}

def derive(seed, *parts):
    return int.from_bytes(hashlib.sha256(':'.join(map(str, (seed, *parts))).encode()).digest()[:8], 'big')

def allocate(count, weights):
    total = sum(weights.values())
    if total <= 0 or any(v <= 0 or not math.isfinite(v) for v in weights.values()):
        raise ValueError('Profile weights must be finite and positive')
    exact = {k: count*v/total for k,v in weights.items()}
    result = {k: int(v) for k,v in exact.items()}
    for k in sorted(weights, key=lambda k: (-(exact[k]-result[k]), k))[:count-sum(result.values())]:
        result[k] += 1
    return result

def techniques(fmt, complexity):
    choices = TECHNIQUES[fmt]
    if complexity == 'LOW':
        return choices[:1]
    if complexity == 'MEDIUM':
        return tuple(x for x in choices if x not in ('hidden', 'nested_archive', 'split', 'embedded'))
    return choices

class Plan:
    """O(formats * scenarios) memory; guarantees the requested coverage without random gaps."""
    def __init__(self, count, formats, sensitivity, complexity, seed):
        self.count, self.seed, self.complexity = count, seed, complexity
        self.formats = allocate(count, formats)
        self.sensitivity = allocate(count, sensitivity)
        remaining = self.sensitivity.copy()
        self.blocks = []
        # Each enabled technique and sensitivity bucket gets a deterministic fixture.
        for fmt, n in self.formats.items():
            required = len(techniques(fmt, complexity)) * len(sensitivity)
            if n < required:
                raise ValueError(f'{fmt} needs at least {required} files for selected coverage; allocated {n}')
            for technique in techniques(fmt, complexity):
                for category in sensitivity:
                    if remaining[category] < 1:
                        raise ValueError(f'Insufficient {category} allocation for required format/technique coverage')
                    self.blocks.append((fmt, category, technique, 1))
                    remaining[category] -= 1
            slots = n-required
            # Fill later so rare buckets cannot be consumed before coverage is reserved.
            self.blocks.append((fmt, None, None, slots))
        final = []
        for fmt, category, technique, n in self.blocks:
            if category is not None:
                final.append((fmt, category, technique, n))
            else:
                for cat in sensitivity:
                    take = min(n, remaining[cat])
                    if take:
                        final.append((fmt, cat, techniques(fmt, complexity)[0], take))
                        remaining[cat] -= take
                        n -= take
                if n:
                    raise ValueError('Allocation failed')
        self.blocks = final
        assert sum(x[3] for x in self.blocks) == count

    def jobs(self):
        index = 0
        for fmt, category, technique, count in self.blocks:
            for _ in range(count):
                yield dict(index=index, format=fmt, bucket=category, technique=technique,
                           seed=derive(self.seed, index), complexity=self.complexity)
                index += 1

    def coverage(self):
        counts=Counter()
        for f,c,t,n in self.blocks: counts[f,c,t]+=n
        return [dict(format=f,sensitivity=c,technique=t,files=n) for (f,c,t),n in sorted(counts.items())]

def profile(seed, index):
    # Stable references recur across document types. No actual person is sourced.
    ref = index % 1009
    rng = random.Random(derive(seed, 'identity', ref))
    first, last = rng.choice((('Amara', 'Okafor'), ('Mei', 'Tan'), ('Ravi', 'Shah'),
                              ('Sofia', 'Alvarez'), ('Morgan', 'Bennett'), ('Lina', 'Haddad')))
    dob = date(rng.randint(1960, 1999), rng.randint(1,12), rng.randint(1,28))
    return dict(id=f'person-{ref:06d}', name=f'{first} {last}', dob=dob.isoformat(),
                age=2026-dob.year-((10,29)<(dob.month,dob.day)),
                hired=f'{max(dob.year+22, 2010)}-06-01',
                address=f'{100+ref} Cedar Avenue, Austin, TX 78701',
                email=f'person{ref}@example.com', ssn=f'900-{10+ref%90:02d}-{1000+ref:04d}',
                mrn=f'MR{ref:09d}', member=f'INS{ref:09d}', account=f'AC{ref:012d}')

def content(job, master_seed, label='neutral'):
    p = profile(master_seed, job['index'])
    cats = list(CATEGORIES) if job['bucket']=='mixed' else ([job['bucket']] if job['bucket'] in CATEGORIES else [])
    fields = []
    copies = {'LOW':1, 'MEDIUM':3, 'HIGH':8}[job['complexity']]
    # Sparse HIGH cases use one sensitive record amid substantial operational content.
    if job['index'] % 2 == 0 or job['technique'] in ('header','footer','metadata'):
        copies = 1
    variant = ('plain','spaces','hyphens','prose','masked','truncated')[job['index']%6]
    if job.get('records_per_file'):
        copies=job['records_per_file']
    elif job['complexity']=='HIGH' and job['index']%17==1 and job['technique'] in ('body','table') and job['format'] in ('pdf','docx','xlsx','csv','txt','json','xml'):
        copies=1000
    references=set()
    for number in range(copies):
        p=profile(master_seed,job['index']+number)
        if cats: references.add(p['id'])
        for cat in cats:
            if cat=='pii':
                fields += [(cat, 'Employee name', p['name']), (cat,'Date of birth',p['dob']),
                           (cat,'Tax identifier',p['ssn']), (cat,'Mailing address',p['address'])]
            elif cat=='pci':
                network, pan = CARDS[(job['index']+number)%4]
                if variant=='spaces': pan=' '.join(pan[i:i+4] for i in range(0,len(pan),4))
                if variant=='hyphens': pan='-'.join(pan[i:i+4] for i in range(0,len(pan),4))
                if variant=='prose': pan=f'Payment was processed against card {pan} for the quarterly service.'
                if variant=='masked': pan='*'*(len(pan)-4)+pan[-4:]
                if variant=='truncated': pan=pan[-4:]
                fields += [(cat, f'{network} payment reference', pan)]
            elif cat=='phi':
                code, diagnosis = ICD[(job['index']+number)%len(ICD)]
                fields += [(cat,'Patient name',p['name']), (cat,'Patient birth date',p['dob']),
                           (cat,'Medical record',p['mrn']), (cat,'Member identifier',p['member']),
                           (cat,'Diagnosis',f'{code} {diagnosis}')]
            else:
                fields += [(cat,'Account holder',p['name']), (cat,'Account reference',p['account']),
                           (cat,'Opening balance','USD 1250.00'), (cat,'Credits','USD 3250.00'),
                           (cat,'Debits','USD 875.25'), (cat,'Closing balance','USD 3624.75')]
    title = {'pii':'Employee personnel review', 'pci':'Merchant settlement report',
             'phi':'Outpatient encounter summary', 'financial':'Monthly account statement',
             'mixed':'Benefits and reimbursement review', 'none':'Facilities operating report',
             'ambiguous':'Inventory reference catalog'}[job['bucket']]
    if label=='explicit': title += ' SYNTHETIC TEST DATA'
    lines = [f'{key}: {value}' for _,key,value in fields]
    if job['bucket']=='ambiguous':
        lines = ['Warehouse reference: 123-00-6789', 'Product dimension: 4111-2222-3333-4444',
                 'Calendar code: 2026-13-40', 'Assembly code: E11.9; this is an equipment designation.']
    if not lines:
        lines = ['Quarterly maintenance review completed.', 'All scheduled facility inspections passed.']
    return dict(title=title, lines=lines, fields=fields, categories=cats, profile=p['id'],profiles=sorted(references),
                variant=variant, counts=dict(Counter(cat for cat,_,_ in fields)))
