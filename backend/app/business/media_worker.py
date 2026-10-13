"""Isolated local PDF/image/OCR processing; no database or model credentials."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import sys
import time
from itertools import chain, islice
from pathlib import Path

from app.tools.redaction import redact_text

VERSION='ab-media-local-2'
_ocr=None
SENSITIVE=re.compile(r'(?i)\b(?:password|passwd|api[\s_-]*key|secret|private[\s_-]*key|authorization|bearer|access[\s_-]*token)\b|密码|密钥|私钥|\beyJ[A-Za-z0-9_-]{6,}\.')


def read_progress(path):
    if path.stat().st_size>64*1024*1024:
        raise ValueError('media_progress_size_limit')
    return json.loads(path.read_text(encoding='utf-8'))


def checkpoint(path,result):
    encoded=json.dumps(result,ensure_ascii=False)
    if len(encoded.encode('utf-8'))>64*1024*1024:
        raise ValueError('media_progress_size_limit')
    temp=path.with_suffix('.tmp')
    with temp.open('w',encoding='utf-8') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp,path)


def resize(image):
    from PIL import Image, ImageOps
    width,height=image.size
    if width<=0 or height<=0 or width*height>40_000_000:
        raise ValueError('decoded_pixel_limit')
    image=ImageOps.exif_transpose(image)
    width,height=image.size
    factor=min(1,2000/max(width,height),math.sqrt(4_000_000/(width*height)))
    if factor<1:
        image=image.resize((max(1,int(width*factor)),max(1,int(height*factor))),Image.Resampling.LANCZOS)
    return image.convert('RGB'),[width,height]


def ocr(image):
    global _ocr
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    if _ocr is None:
        import cv2
        cv2.setNumThreads(1)
        _ocr=RapidOCR(intra_op_num_threads=1,inter_op_num_threads=1)
    values,_=_ocr(np.asarray(image))
    if len(values or [])>500:
        raise ValueError('ocr_layout_capacity_limit')
    regions=[]
    for item in (values or [])[:500]:
        points,text,score=item
        if not isinstance(text,str) or not math.isfinite(float(score)):
            continue
        regions.append({'text':redact_text(text)[:1000],'confidence':float(score),
                        'sensitive':bool(SENSITIVE.search(text)) or redact_text(text)!=text,
                        'region':[[round(float(p[0])/image.width,6),round(float(p[1])/image.height,6)] for p in points]})
    return regions


def vision_frame(image,output,number):
    frame=image.copy()
    frame.thumbnail((1024,1024))
    try:
        for quality in (90,75,55):
            stream=io.BytesIO()
            frame.save(stream,format='JPEG',quality=quality)
            data=stream.getvalue()
            if len(data)<=900*1024:
                filename=f'vision-{number}.jpg'
                (output/filename).write_bytes(data)
                return {'vision_image_path':filename,'vision_image_size':list(frame.size),
                        'vision_image_digest':hashlib.sha256(data).hexdigest()}
        raise ValueError('vision_frame_byte_limit')
    finally:
        frame.close()


def process_media(path: Path, source: dict, cursor: dict, deadline: float) -> dict:
    path=Path(path)
    pdf=path.suffix.lower()=='.pdf'
    if path.stat().st_size>(50 if pdf else 20)*1024*1024:
        raise ValueError('media_file_size_limit')
    if not 1<=int(source.get('max_pages',50))<=50:
        raise ValueError('media_batch_page_limit')
    raw=path.read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=source['content_digest']:
        raise ValueError('media_original_digest_changed')
    if len(raw)>(50 if pdf else 20)*1024*1024:
        raise ValueError('media_file_size_limit')
    output=Path(source['derived_dir'])
    output.mkdir(parents=True,exist_ok=True)
    state_path=output/'progress.json'
    compatible=(cursor.get('parser_version')==VERSION and cursor.get('content_digest')==digest)
    result={**cursor} if compatible else {'parser_version':VERSION,'content_digest':digest,'pages':[],'next_page':0}
    pages=list(result.get('pages',[]))
    if (len(pages)!=result.get('next_page',0)
            or any(page.get('page_number')!=i+1 for i,page in enumerate(pages))):
        raise ValueError('media_cursor_identity_mismatch')
    result['pages']=pages
    from PIL import Image
    Image.MAX_IMAGE_PIXELS=40_000_000
    document=None
    total=1
    try:
        if pdf:
            if not raw.startswith(b'%PDF-'):
                raise ValueError('pdf_signature_mismatch')
            import pypdfium2 as pdfium
            document=pdfium.PdfDocument(raw)
            total=len(document)
        else:
            with Image.open(path) as original:
                expected={'.png':'PNG','.jpg':'JPEG','.jpeg':'JPEG','.webp':'WEBP'}.get(path.suffix.lower())
                if original.format!=expected:
                    raise ValueError('image_signature_mismatch')
                if getattr(original,'n_frames',1)!=1:
                    raise ValueError('animated_image_requires_review')
        result['total_pages']=total
        start=int(result.get('next_page',0))
        retry=[index for index,page in enumerate(pages) if page.get('ocr_state') in {'ocr_failed','ocr_unavailable'}]
        for number in islice(chain(retry,range(start,total)),int(source.get('max_pages',50))):
            if time.monotonic()>=deadline:
                break
            native_text=''
            text_incomplete=False
            page=None
            bitmap=None
            if document is not None:
                page=document[number]
                textpage=page.get_textpage()
                try:
                    characters=textpage.count_chars()
                    text_incomplete=characters>32000
                    native_text=textpage.get_text_range(index=0,count=min(characters,32000))
                finally:
                    textpage.close()
                width,height=page.get_size()
                if not 0<width<100000 or not 0<height<100000:
                    raise ValueError('pdf_page_dimension_limit')
                scale=min(2,2000/max(width,height),math.sqrt(4_000_000/(width*height)))
                bitmap=page.render(scale=scale)
                image=bitmap.to_pil().copy()
                image,original_size=resize(image)
            else:
                with Image.open(path) as original:
                    image,original_size=resize(original)
            regions=[]
            ocr_error_type=None
            # A PDF text layer does not reveal secrets embedded in its raster content.
            try:
                regions=ocr(image)
                ocr_state='completed'
            except ImportError as error:
                ocr_state='ocr_unavailable'
                ocr_error_type=type(error).__name__
            except Exception as error:
                ocr_state='ocr_failed'
                ocr_error_type=type(error).__name__
            text=native_text if native_text.strip() else '\n'.join(row['text'] for row in regions)
            sensitive=bool(SENSITIVE.search(text)) or redact_text(text)!=text or any(region['sensitive'] for region in regions)
            paused=sensitive or text_incomplete or ocr_state in {'ocr_failed','ocr_unavailable'}
            filename=f'page-{number+1}.png'
            frame={}
            if not paused:
                image.save(output/filename,format='PNG',optimize=False)
                frame=vision_frame(image,output,number+1)
            if sensitive:
                text='[REDACTED_SENSITIVE_PAGE]'
                regions=[]
            reference={'schema_version':'source-ref-1','path':source['path'],'archive_path':source['path'],
                       'content_digest':digest,'digest_basis':'original_bytes','page_number':number+1,
                       'source_type':'Documented','region_coordinate_system':'normalized_page','region':[0,0,1,1]}
            record={**frame,'page_number':number+1,'text':redact_text(text)[:4000],
                          'text_truncated':len(text)>4000,'text_source':'pdf_text' if native_text.strip() else 'ocr',
                          'regions':regions,'source_refs':[reference],'ocr_state':ocr_state,
                          'ocr_error_type':ocr_error_type,
                          'image_path':filename if not paused else None,'image_size':list(image.size),
                          'original_size':original_size,'external_paused':paused,'local_text_incomplete':text_incomplete,
                          'vision_state':'pending_vision','semantic_relations_verified':False}
            if number<len(pages):
                pages[number]=record
            else:
                pages.append(record)
            result['next_page']=len(pages)
            result['parse_state']='partial_media' if result['next_page']<total else 'local_media_complete'
            result['omitted_pages']=list(range(result['next_page']+1,min(total+1,result['next_page']+1001)))
            result['omitted_page_count']=total-result['next_page']
            checkpoint(state_path,result)
            image.close()
            if bitmap:
                bitmap.close()
            if page:
                page.close()
        result['parse_state']='partial_media' if result['next_page']<total else 'local_media_complete'
        result['omitted_pages']=list(range(result['next_page']+1,min(total+1,result['next_page']+1001)))
        result['omitted_page_count']=total-result['next_page']
        checkpoint(state_path,result)
        return result
    finally:
        if document is not None:
            document.close()


def main():
    os.environ.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
    from app.business.resources import apply_memory_limit
    apply_memory_limit(2*1024*1024*1024,120)
    request=json.loads(sys.stdin.buffer.read(131073))
    if not isinstance(request,dict) or set(request)!={'path','source','cursor','timeout'}:
        raise ValueError('Invalid fixed media request')
    timeout=float(request['timeout'])
    if not 0<timeout<=120:
        raise ValueError('Invalid media deadline')
    try:
        progress=Path(request['source']['derived_dir'])/'progress.json'
        if not request['cursor'] and progress.is_file():
            request['cursor']=read_progress(progress)
        result=process_media(Path(request['path']),request['source'],request['cursor'],time.monotonic()+timeout)
    except Exception as error:
        result={'parser_version':VERSION,'content_digest':request['source']['content_digest'],
                'parse_state':'media_local_failed','error_type':type(error).__name__,'pages':[],
                'reason':'媒体处理失败，原件已保留；可继续处理或核对损坏与能力状态'}
        if progress.is_file():
            previous=read_progress(progress)
            if previous.get('parser_version')==VERSION and previous.get('content_digest')==request['source']['content_digest']:
                result={**previous,'parse_state':'partial_media','error_type':type(error).__name__,'reason':result['reason']}
                checkpoint(progress,result)
    if result.get('parse_state')!='media_local_failed':
        result={'result_path':str(progress),'parse_state':result['parse_state'],'next_page':result['next_page']}
    sys.stdout.write(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
