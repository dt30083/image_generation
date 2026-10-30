"""Format writers and independent structural/content readers."""
from __future__ import annotations
from contextlib import contextmanager
import csv
import html
import io
import json
import math
import random
import struct
import textwrap
import zipfile
from datetime import datetime
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
from pathlib import Path
from xml.etree import ElementTree as ET
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from synthetic_sensitive_images import get_font, read_embedded_metadata

RASTER = {'jpg','png','tiff','bmp','gif','webp'}
# Upper limits protect library DOMs and raster allocations. Text/ZIP writers stream to 1 GB.
LIMITS = {k: 10_000_000 for k in ('pdf','docx','xlsx','pptx','eml','msg')}
LIMITS.update({k: 2_000_000 for k in RASTER})

@contextmanager
def open_zip(path,mode='r',compression=zipfile.ZIP_STORED):
    # Also accepts BytesIO for nested archives.
    if hasattr(path,'open'):
        with path.open('rb' if mode=='r' else 'w+b') as stream, zipfile.ZipFile(stream,mode,compression) as archive:
            yield archive
    else:
        with zipfile.ZipFile(path,mode,compression) as archive: yield archive


def fixed_info(name):
    info=zipfile.ZipInfo(name,(2026,1,1,0,0,0)); info.compress_type=zipfile.ZIP_DEFLATED
    info.external_attr=0o600 << 16
    return info

def canonicalize(path,fmt):
    if fmt not in ('docx','xlsx','pptx'): return
    replacement=path.with_name(path.name+'.canonical')
    try:
        with open_zip(path) as source, open_zip(replacement,'w') as target:
            for name in sorted(source.namelist()):
                with source.open(name) as inp, target.open(fixed_info(name),'w') as out:
                    for block in iter(lambda:inp.read(65536),b''): out.write(block)
        replacement.replace(path)
    finally: replacement.unlink(missing_ok=True)

def operational_rows(seed, n):
    rng = random.Random(seed)
    teams = ('Facilities', 'Procurement', 'Logistics', 'Engineering', 'Operations')
    tasks = ('equipment inspection', 'inventory reconciliation', 'delivery review', 'capacity planning')
    for i in range(n):
        yield (f'Work order {i:09d}; department {rng.choice(teams)}; '
               f'activity {rng.choice(tasks)}; units {rng.randrange(1,10000)}; '
               f'budget USD {rng.randrange(100,100000)}.{rng.randrange(100):02d}; '
               f'status completed; audit reference {rng.getrandbits(64):016x}')

def all_lines(data, seed, n, include_sensitive=True):
    if include_sensitive:
        yield from data['lines']
    yield from operational_rows(seed, n)

def image_bytes(data, seed, complexity, stress='clean',font_path=None):
    rng = random.Random(seed)
    width = 1500
    lines = []
    for line in data['lines']:
        lines.extend(textwrap.wrap(line, width=78) or [''])
    height = max(650,160+(math.ceil(len(lines)/2)*130 if stress=='columns' else len(lines)*43))
    if height > 20000:
        raise ValueError('Image exceeds readable layout limit')
    img = Image.new('RGB',(width,height),(250,249,246))
    draw = ImageDraw.Draw(img)
    font = get_font(26 if stress!='font_size' else 21,font_path)
    if stress=='serif' and not font_path: font=get_font(26,'DejaVuSerif.ttf')
    if stress=='monospace' and not font_path: font=get_font(24,'DejaVuSansMono.ttf')
    draw.text((60,30), data['title'], font=get_font(34,font_path), fill=(20,40,60))
    for i,line in enumerate(lines):
        fill = (110,110,110) if stress=='low_contrast' else (20,25,30)

        if stress=='columns':
            col=i%2; row=i//2
            # Two narrower columns retain complete text through wrapping.
            for j,part in enumerate(textwrap.wrap(line,38)):
                draw.text((60+col*740,115+row*130+j*32),part,font=get_font(21,font_path),fill=fill)
        else: draw.text((60,115+i*43),line,font=font,fill=fill)
    if stress=='rotation': img=img.rotate(2, expand=True, fillcolor='white')
    elif stress=='resolution':
        img=img.resize((width//2,height//2)).resize((width,height))
    elif stress=='noise':
        # Mild scanner noise; preserve contrast and legibility.
        for _ in range(width*height//200):
            x,y=rng.randrange(width),rng.randrange(height)
            img.putpixel((x,y),(180,180,180))
    elif stress=='compression':
        buf=io.BytesIO(); img.save(buf,format='JPEG',quality=40); buf.seek(0)
        img=Image.open(buf).convert('RGB')
    return img

def save_raster(img,path,payload,metadata):
    from PIL import PngImagePlugin
    formats={'jpg':'JPEG','png':'PNG','tiff':'TIFF','gif':'GIF','bmp':'BMP','webp':'WEBP'}
    fmt=formats[path.suffix.lower()[1:]]; options={}
    if metadata:
        if fmt=='PNG':
            info=PngImagePlugin.PngInfo(); info.add_text('Description',payload); options['pnginfo']=info
        elif fmt in ('JPEG','WEBP'):
            exif=Image.Exif(); exif[270]=payload; options['exif']=exif.tobytes()
        elif fmt=='TIFF': options['tiffinfo']={270:payload}
        elif fmt=='GIF': options['comment']=payload.encode()
    if fmt=='TIFF': options['compression']='tiff_deflate'
    if fmt=='JPEG': options.update(quality=80,subsampling=0)
    if fmt=='PNG': options['optimize']=True
    if fmt=='WEBP': options.update(quality=90,method=4)
    with path.open('wb') as out: img.save(out,format=fmt,**options)
    if fmt=='TIFF':
        # libtiff's memory-stream encoder can leave an uninitialized alignment
        # byte between strip data and the IFD. Canonicalize only that unused byte.
        with path.open('rb') as source,Image.open(source) as check:
            end=max(offset+length for offset,length in zip(check.tag_v2[273],check.tag_v2[279]))
            source.seek(0); header=source.read(8)
            ifd=int.from_bytes(header[4:8],'little' if header[:2]==b'II' else 'big')
        if ifd-end==1:
            with path.open('r+b') as out: out.seek(end); out.write(b'\0')

def image_blob(data, job):
    image=image_bytes(data,job['seed'],job['complexity'],job.get('ocr','clean'),job.get('font'))
    buf=io.BytesIO(); image.save(buf,format='PNG'); image.close()
    return buf.getvalue()

def write_msg(path, subject, body):
    """Minimal Unicode MS-OXMSG message in a real CFB v3 container.

    Uses mini streams for small properties and regular streams for larger bodies.
    Property tags: PR_SUBJECT_W, PR_BODY_W, PR_MESSAGE_CLASS_W.
    """
    values = [('0037001F',subject),('1000001F',body),('001A001F','IPM.Note')]
    props = bytearray(32)
    streams=[]
    for tag,value in values:
        b=(value+'\0').encode('utf-16le')
        props += struct.pack('<IIII',int(tag,16),6,len(b),0)
        streams.append(('__substg1.0_'+tag,b))
    streams.insert(0,('__properties_version1.0',bytes(props)))
    FREE, END, FAT = 0xffffffff,0xfffffffe,0xfffffffd
    sectors=[]; fat=[]; mini=bytearray(); minifat=[]; entries=[]
    def regular(payload):
        start=len(sectors); n=math.ceil(len(payload)/512)
        for i in range(n):
            sectors.append(payload[i*512:(i+1)*512].ljust(512,b'\0'))
            fat.append(start+i+1 if i<n-1 else END)
        return start
    for name,payload in streams:
        if len(payload)<4096:
            start=len(minifat); n=math.ceil(len(payload)/64)
            for i in range(n):
                mini += payload[i*64:(i+1)*64].ljust(64,b'\0')
                minifat.append(start+i+1 if i<n-1 else END)
        else: start=regular(payload)
        entries.append((name,start,len(payload)))
    mini_start=regular(bytes(mini)) if mini else END
    mf_start=regular(struct.pack('<'+'I'*len(minifat),*minifat)) if minifat else END
    mf_count=math.ceil(len(minifat)*4/512)
    # CFB directory siblings sorted by name length then uppercase lexical order.
    order=sorted(range(1,len(entries)+1),key=lambda i:(len(entries[i-1][0]),entries[i-1][0].upper()))
    links={}; levels={}
    def tree(ids,depth=0):
        if not ids: return FREE
        mid=len(ids)//2; root=ids[mid]
        levels[root]=depth
        links[root]=(tree(ids[:mid],depth+1),tree(ids[mid+1:],depth+1))
        return root
    root_child=tree(order)
    def directory(name,kind,start,size,left=FREE,right=FREE,child=FREE,color=1):
        b=(name+'\0').encode('utf-16le')
        e=bytearray(128); e[:len(b)]=b
        struct.pack_into('<HBBIII',e,64,len(b),kind,color,left,right,child)
        struct.pack_into('<IQ',e,116,start,size)
        return bytes(e)
    dirs=directory('Root Entry',5,mini_start,len(mini),child=root_child)
    for i,(name,start,size) in enumerate(entries,1):
        left,right=links[i]; dirs+=directory(name,2,start,size,left,right,color=0 if levels[i]==max(levels.values()) and levels[i]>0 else 1)
    dir_start=regular(dirs)
    # One FAT covers 128 sectors. DIFAT header supports up to 109 FAT sectors.
    nf=1
    while math.ceil((len(sectors)+nf)/128)>nf: nf+=1
    if nf>109: raise ValueError('MSG exceeds supported CFB size')
    fat_ids=list(range(len(sectors),len(sectors)+nf)); fat.extend([FAT]*nf)
    fat.extend([FREE]*(nf*128-len(fat)))
    header=bytearray(512); header[:8]=bytes.fromhex('D0CF11E0A1B11AE1')
    struct.pack_into('<HHHH',header,24,0x003e,3,0xfffe,9)
    struct.pack_into('<H',header,32,6)
    struct.pack_into('<IIIIIIIII',header,40,0,nf,dir_start,0,4096,mf_start,mf_count,END,0)
    struct.pack_into('<109I',header,76,*(fat_ids+[FREE]*(109-nf)))
    with path.open('wb') as out:
        out.write(header)
        for sector in sectors: out.write(sector)
        out.write(struct.pack('<'+'I'*len(fat),*fat))

def write(path,job,data,rows):
    if job['format']=='pdf':
        with path.open('wb') as stream: return _write(path,job,data,rows,stream)
    return _write(path,job,data,rows)

def _write(path, job, data, rows, pdf_stream=None):
    fmt,tech=job['format'],job['technique']
    seed=job['seed']; title=data['title']
    sensitive='\n'.join(data['lines'])
    include=tech not in ('metadata','image','header','footer','hidden','embedded','attachment')
    lines=lambda: all_lines(data,seed,rows,include)
    if fmt in RASTER:
        img=image_bytes(data,seed,job['complexity'],job.get('ocr','clean'),job.get('font'))
        try:
            if tech=='metadata':
                img.close(); img=image_bytes(dict(title='Operations overview',lines=['Facility review completed.']),seed,'LOW')
            save_raster(img,path,sensitive,tech=='metadata')
        finally: img.close()
    elif fmt=='pdf':
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.lib.utils import ImageReader
        c=Canvas(pdf_stream,pagesize=(612,792),invariant=1,pageCompression=1)
        c.setTitle(title)
        if tech=='metadata': c.setSubject(sensitive)
        if tech=='image': c.drawImage(ImageReader(io.BytesIO(image_blob(data,job))),30,250,width=550,height=350,preserveAspectRatio=True)
        y=735
        c.setFont('Helvetica',10); c.drawString(35,765,title)
        if tech=='header':
            for line in data['lines']:
                for part in textwrap.wrap(line,95): c.drawString(35,y,part); y-=13
        if tech=='footer':
            wrapped=[part for line in data['lines'] for part in textwrap.wrap(line,95)]
            for offset in range(0,len(wrapped),8):
                for i,line in enumerate(wrapped[offset:offset+8]):
                    c.drawString(35,120-i*12,line)
                c.showPage(); c.setFont('Helvetica',10)
        for line in lines():
            if tech=='split': line='\n'.join(textwrap.wrap(line,12))
            for part in line.splitlines():
                for text in textwrap.wrap(part,95):
                    if y<45: c.showPage(); c.setFont('Helvetica',10); y=750

                    if tech=='table': c.rect(32,y-3,548,16)
                    c.drawString(35,y,text); y-=18 if tech=='table' else 13
        c.save()
    elif fmt=='docx':
        from docx import Document
        from docx.shared import Inches
        doc=Document(); doc.add_heading(title,0)
        doc.core_properties.created=doc.core_properties.modified=datetime(2026,1,1)
        if tech=='metadata': doc.core_properties._element.get_or_add_subject().text=sensitive
        elif tech in ('header','footer'):
            target=getattr(doc.sections[0],tech)
            for line in data['lines']: target.add_paragraph(line)
        elif tech=='image': doc.add_picture(io.BytesIO(image_blob(data,job)),width=Inches(6))
        elif tech=='embedded': doc.add_paragraph('Supporting record attached as an embedded package.')
        if tech=='table':
            table=doc.add_table(rows=0,cols=1)
            for line in lines(): table.add_row().cells[0].text=line
        else:
            for line in lines():
                p=doc.add_paragraph()
                if tech=='split':
                    for piece in textwrap.wrap(line,20): p.add_run(piece).add_break()
                else: p.add_run(line)
        with path.open('w+b') as out: doc.save(out)
        if tech=='embedded':
            # Add actual OPC relationship + OLE package reference, not an orphan ZIP entry.
            with open_zip(path) as z: parts={n:z.read(n) for n in z.namelist()}
            rel='http://schemas.openxmlformats.org/package/2006/relationships'
            r=ET.fromstring(parts['word/_rels/document.xml.rels'])
            ET.SubElement(r,'{'+rel+'}Relationship',Id='rIdSensitivePackage',
                          Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/package',
                          Target='embeddings/record.docx')
            parts['word/_rels/document.xml.rels']=ET.tostring(r)
            ns='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
            root=ET.fromstring(parts['word/document.xml']); body=root.find('{'+ns+'}body')
            p=ET.SubElement(body,'{'+ns+'}p'); run=ET.SubElement(p,'{'+ns+'}r')
            obj=ET.SubElement(run,'{'+ns+'}object')
            ET.SubElement(obj,'{urn:schemas-microsoft-com:vml}shape',id='_x0000_i1025',style='width:120pt;height:30pt')
            ET.SubElement(obj,'{urn:schemas-microsoft-com:office:office}OLEObject',
                          Type='Embed',ProgID='Word.Document.12',ShapeID='_x0000_i1025',
                          attrib={'{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id':'rIdSensitivePackage'})
            parts['word/document.xml']=ET.tostring(root)
            ct=ET.fromstring(parts['[Content_Types].xml'])
            ET.SubElement(ct,'{http://schemas.openxmlformats.org/package/2006/content-types}Override',PartName='/word/embeddings/record.docx',ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
            parts['[Content_Types].xml']=ET.tostring(ct)
            embedded=Document()
            for line in data['lines']: embedded.add_paragraph(line)
            embedded.core_properties.created=embedded.core_properties.modified=datetime(2026,1,1)
            embedded_buf=io.BytesIO(); embedded.save(embedded_buf)
            # Canonicalize nested package timestamps too.
            canonical_buf=io.BytesIO()
            with zipfile.ZipFile(embedded_buf) as source,zipfile.ZipFile(canonical_buf,'w') as target:
                for name in sorted(source.namelist()): target.writestr(fixed_info(name),source.read(name))
            parts['word/embeddings/record.docx']=canonical_buf.getvalue()
            with open_zip(path,'w',zipfile.ZIP_DEFLATED) as z:
                for name,value in parts.items(): z.writestr(fixed_info(name),value)
    elif fmt=='xlsx':
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as XLImage
        wb=Workbook(write_only=tech!='image'); ws=wb.create_sheet('Operations') if wb.write_only else wb.active
        ws.append([title]); wb.properties.created=wb.properties.modified=datetime(2026,1,1)
        if tech=='metadata': wb.properties.description=sensitive
        if tech=='hidden':
            hidden=wb.create_sheet('Benefits'); hidden.sheet_state='veryHidden'
            for line in data['lines']: hidden.append([line])
        if tech=='image':
            buf=io.BytesIO(image_blob(data,job)); ws.add_image(XLImage(buf),'C3')
        for i,line in enumerate(lines()):
            if i and i%1_000_000==0: ws=wb.create_sheet(f'Operations{i//1_000_000}')
            ws.append([line[i:i+8] for i in range(0,len(line),8)] if tech=='split' else [line])
        with path.open('w+b') as out: wb.save(out)
        wb.close()
    elif fmt=='pptx':
        from pptx import Presentation
        from pptx.util import Inches, Pt
        prs=Presentation(); prs.core_properties.created=prs.core_properties.modified=datetime(2026,1,1)
        if tech=='metadata': prs.core_properties._element.get_or_add_subject().text=sensitive
        slide=None
        if tech=='image':
            slide=prs.slides.add_slide(prs.slide_layouts[6])
            blob=image_blob(data,job)
            with Image.open(io.BytesIO(blob)) as im: ratio=im.width/im.height
            w=min(9,5*ratio); h=w/ratio
            slide.shapes.add_picture(io.BytesIO(blob),Inches(.3),Inches(.5),width=Inches(w),height=Inches(h))
        for i,line in enumerate(lines()):
            if i%10==0:
                slide=prs.slides.add_slide(prs.slide_layouts[6])
                box=slide.shapes.add_textbox(Inches(.4),Inches(.3),Inches(9),Inches(6.5)); tf=box.text_frame; tf.word_wrap=True
                tf.paragraphs[0].text=title
                if tech=='table':
                    table=slide.shapes.add_table(10,1,Inches(.4),Inches(1),Inches(9),Inches(5)).table
            if tech=='table':
                cell=table.cell(i%10,0); cell.text=line
                for paragraph in cell.text_frame.paragraphs: paragraph.font.size=Pt(10)
                continue
            p=tf.add_paragraph(); p.text=line.replace(': ',':\n',1) if tech=='split' else line
            p.font.size=Pt(12)
        with path.open('w+b') as out: prs.save(out)
    elif fmt in ('zip',):
        with open_zip(path,'w',zipfile.ZIP_DEFLATED) as z:
            if tech=='nested_archive':
                buf=io.BytesIO()
                with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as inner:
                    inner.writestr(fixed_info('benefits/record.txt'),sensitive)
                z.writestr(fixed_info('departments/archive.zip'),buf.getvalue())
            else: z.writestr(fixed_info('departments/record.txt'),sensitive)
            with z.open('operations/report.txt','w') as out:
                for line in operational_rows(seed,rows): out.write((line+'\n').encode())
    elif fmt=='msg': write_msg(path,title,sensitive+'\n'+'\n'.join(operational_rows(seed,rows)))
    elif fmt=='eml':
        msg=EmailMessage(); msg['From']='operations@example.com'; msg['To']='review@example.com'
        msg['Subject']=title; msg['Date']='Thu, 29 Oct 2026 09:00:00 -0500'
        msg['Message-ID']=f'<{seed}@example.com>'
        msg.set_content('\n'.join(lines()))
        if tech=='attachment': msg.add_attachment(sensitive.encode(),maintype='text',subtype='plain',filename='record.txt')
        if msg.is_multipart(): msg.set_boundary(f'boundary-{seed}')
        path.write_bytes(msg.as_bytes(policy=policy.SMTP))
    else:
        with path.open('w',encoding='utf-8',newline='') as out:
            if fmt=='json':
                out.write('{"title":'+json.dumps(title)+',"records":[')
                for i,line in enumerate(lines()): out.write((',' if i else '')+json.dumps(line))
                out.write(']}')
            elif fmt=='xml':
                out.write('<?xml version="1.0" encoding="utf-8"?><report><title>'+html.escape(title)+'</title>')
                for line in lines(): out.write('<record>'+html.escape(line)+'</record>')
                out.write('</report>')
            elif fmt=='html':
                out.write('<!doctype html><html><head><meta charset="utf-8"><title>'+html.escape(title)+'</title></head><body><h1>'+html.escape(title)+'</h1>')
                for line in lines():
                    if tech=='table': out.write('<table><tr><td>'+html.escape(line)+'</td></tr></table>')
                    elif tech=='split': out.write('<p>'+''.join('<span>'+html.escape(p)+'</span>' for p in textwrap.wrap(line,20))+'</p>')
                    else: out.write('<p>'+html.escape(line)+'</p>')
                out.write('</body></html>')
            elif fmt=='csv':
                writer=csv.writer(out); writer.writerow(['Record'])
                for line in lines(): writer.writerow([line])
            else:
                out.write(title+'\n')
                for line in lines(): out.write((line.replace(': ',':\n',1) if tech=='split' else line)+'\n')


def extract(path, fmt):
    """Yield text chunks; parsers are separate from rendering code."""
    if fmt in RASTER:
        with path.open('rb') as source, Image.open(source) as im: im.verify()
        with path.open('rb') as source: value=read_embedded_metadata(source)
        if value: yield value
    elif fmt=='pdf':
        from pypdf import PdfReader
        with path.open('rb') as source:
            reader=PdfReader(source)
            yield str(reader.metadata)
            for page in reader.pages: yield page.extract_text() or ''
    elif fmt in ('docx','xlsx','pptx'):
        # Validate with native parsers, then inspect package text and relationships.
        if fmt=='docx':
            from docx import Document
            with path.open('rb') as source: Document(source)
        elif fmt=='xlsx':
            from openpyxl import load_workbook
            with path.open('rb') as source:
                wb=load_workbook(source,read_only=True); wb.close()
        else:
            from pptx import Presentation
            with path.open('rb') as source: Presentation(source)
        with open_zip(path) as z:
            if z.testzip(): raise ValueError('Corrupt package')
            for name in z.namelist():
                if name.endswith('.xml'):
                    root=ET.fromstring(z.read(name)); yield ' '.join(root.itertext())
                elif name.endswith('.txt'): yield z.read(name).decode()
                elif name.startswith('word/embeddings/') and name.endswith('.docx'):
                    with zipfile.ZipFile(io.BytesIO(z.read(name))) as embedded:
                        yield ' '.join(ET.fromstring(embedded.read('word/document.xml')).itertext())
    elif fmt=='zip':
        def walk(source):
            with open_zip(source) as z:
                if z.testzip(): raise ValueError('Corrupt archive')
                for name in z.namelist():
                    if name.endswith('.zip'): yield from walk(io.BytesIO(z.read(name)))
                    else:
                        with z.open(name) as stream:
                            for line in stream: yield line.decode()
        yield from walk(path)
    elif fmt=='msg':
        import olefile
        with path.open('rb') as source, olefile.OleFileIO(source) as ole:
            for name in ole.listdir():
                if name[-1].endswith('001F'):
                    yield ole.openstream(name).read().decode('utf-16le').rstrip('\0')
    elif fmt=='eml':
        message=BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        if message.defects: raise ValueError(f'Malformed EML: {message.defects}')
        for part in message.walk():
            if part.get_content_type()=='text/plain': yield part.get_payload(decode=True).decode()
    elif fmt=='xml':
        with path.open('rb') as source:
            for event,element in ET.iterparse(source,events=('end',)):
                if element.text: yield element.text
                element.clear()
    elif fmt=='json':
        # Line-oriented tokens are emitted by the streaming writer. Validate JSON
        # using the incremental decoder without loading a 1 GB records array.
        yield from json_strings(path)
    elif fmt=='csv':
        with path.open(newline='') as f:
            for row in csv.reader(f): yield ' '.join(row)
    elif fmt=='html':
        from html.parser import HTMLParser
        class Reader(HTMLParser):
            def handle_data(self,value): self.chunks.append(value)
        parser=Reader(); parser.chunks=[]
        with path.open() as f:
            for block in iter(lambda:f.read(65536),''):
                parser.feed(block); yield from parser.chunks; parser.chunks.clear()
        parser.close()
    else:
        with path.open() as f: yield from f

def json_strings(path):
    # This schema has only a title string and an array of strings.
    decoder=json.JSONDecoder(); buffer=''; eof=False
    with path.open() as f:
        def take_literal(literal):
            nonlocal buffer
            fill(len(literal)); buffer=buffer.lstrip()
            if not buffer.startswith(literal): raise ValueError('Invalid generated JSON')
            buffer=buffer[len(literal):]
        def fill(n=1):
            nonlocal buffer,eof
            while len(buffer.lstrip())<n and not eof:
                block=f.read(65536); eof=not block; buffer+=block
        def string():
            nonlocal buffer,eof
            while True:
                fill(); buffer=buffer.lstrip()
                try: value,end=decoder.raw_decode(buffer); buffer=buffer[end:]; break
                except json.JSONDecodeError:
                    if eof: raise
                    block=f.read(65536); eof=not block; buffer+=block
            if not isinstance(value,str): raise ValueError('Expected JSON string')
            return value
        take_literal('{"title":'); yield string(); take_literal(',"records":[')
        first=True
        while True:
            fill(); buffer=buffer.lstrip()
            if buffer.startswith(']'): break
            if not first: take_literal(',')
            yield string(); first=False
        take_literal(']}'); fill()
        if buffer.strip(): raise ValueError('Trailing JSON content')
