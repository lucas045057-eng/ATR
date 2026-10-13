import hashlib
import io
import os
import tarfile
import time
import zipfile
from pathlib import Path

import pytest
from app.business import media_worker
from app.business.media_worker import process_media
from app.business.static import inspect_file
from conftest import wait_job
from PIL import Image


def pdf_bytes(count):
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids=[]
    for index in range(count):
        page=len(objects)+1
        stream=page+1
        kids.append(str(page)+' 0 R')
        content=('BT /F1 12 Tf 30 740 Td (catalog page '+str(index+1)+') Tj ET').encode()
        objects.extend([f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {stream} 0 R >>'.encode(),
                        b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream'])
    objects[1]=f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {count} >>'.encode()
    raw=b'%PDF-1.4\n'
    offsets=[0]
    for number,obj in enumerate(objects,1):
        offsets.append(len(raw))
        raw+=str(number).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    start=len(raw)
    raw+=b'xref\n0 '+str(len(offsets)).encode()+b'\n0000000000 65535 f \n'
    raw+=b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
    return raw+f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()


def source(path,output):
    return {'path':path.name,'content_digest':hashlib.sha256(path.read_bytes()).hexdigest(),
            'derived_dir':str(output),'max_pages':50}


@pytest.mark.parametrize('suffix,format',[('.png','PNG'),('.jpg','JPEG'),('.webp','WEBP')])
def test_full_media_path_does_not_apply_text_read_limit(tmp_path,monkeypatch,suffix,format):
    path=tmp_path/('architecture'+suffix)
    image=Image.frombytes('RGB',(700,700),os.urandom(700*700*3))
    image.save(path,format=format,quality=100)
    assert path.stat().st_size>256*1024
    assert inspect_file(path,path.name,256*1024,defer_media=True)['parse_state']=='pending_media_local'
    monkeypatch.setattr(media_worker,'ocr',lambda image:[])
    result=process_media(path,source(path,tmp_path/'derived'),{},time.monotonic()+120)
    assert result['next_page']==1 and result['pages'][0]['image_size']==[700,700]
    assert result['pages'][0]['source_refs'][0]['content_digest']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert not result['pages'][0]['semantic_relations_verified']


def test_51_page_pdf_continues_without_reprocessing_first_batch(tmp_path,monkeypatch):
    monkeypatch.setattr(media_worker,'ocr',lambda image:[])
    path=tmp_path/'guide.pdf'
    path.write_bytes(pdf_bytes(51))
    metadata=source(path,tmp_path/'derived')
    first=process_media(path,metadata,{},time.monotonic()+120)
    assert first['next_page']==50 and first['omitted_pages']==[51] and first['parse_state']=='partial_media'
    original=[(Path(metadata['derived_dir'])/page['image_path']).stat().st_mtime_ns for page in first['pages']]
    second=process_media(path,metadata,first,time.monotonic()+120)
    assert second['next_page']==51 and second['parse_state']=='local_media_complete'
    assert [page['page_number'] for page in second['pages']]==list(range(1,52))
    assert original==[(Path(metadata['derived_dir'])/page['image_path']).stat().st_mtime_ns for page in second['pages'][:50]]
    assert 'catalog page 51' in second['pages'][-1]['text']


def test_original_replacement_cannot_reuse_old_media_cursor(tmp_path,monkeypatch):
    monkeypatch.setattr(media_worker,'ocr',lambda image:[])
    path=tmp_path/'guide.pdf'
    path.write_bytes(pdf_bytes(1))
    metadata=source(path,tmp_path/'derived')
    first=process_media(path,metadata,{},time.monotonic()+120)
    path.write_bytes(pdf_bytes(2))
    with pytest.raises(ValueError,match='digest'):
        process_media(path,metadata,first,time.monotonic()+120)
    second=process_media(path,source(path,tmp_path/'next'),first,time.monotonic()+120)
    assert second['total_pages']==2 and len(second['pages'])==2


def test_secret_page_is_not_written_for_external_vision(tmp_path,monkeypatch):
    path=tmp_path/'secret.png'
    Image.new('RGB',(100,100),'white').save(path)
    monkeypatch.setattr(media_worker,'ocr',lambda image:[{'text':'api_key=[REDACTED]','confidence':.99,'region':[], 'sensitive':True}])
    output=tmp_path/'derived'
    result=process_media(path,source(path,output),{},time.monotonic()+120)
    assert result['pages'][0]['external_paused'] and result['pages'][0]['image_path'] is None
    assert result['pages'][0]['text']=='[REDACTED_SENSITIVE_PAGE]'
    assert list(output.glob('*.png'))==[]


def test_ocr_unavailable_remains_an_explicit_related_gap(tmp_path,monkeypatch):
    path=tmp_path/'architecture.png'
    Image.new('RGB',(100,100),'white').save(path)
    def unavailable(image):
        raise ImportError
    monkeypatch.setattr(media_worker,'ocr',unavailable)
    result=process_media(path,source(path,tmp_path/'derived'),{},time.monotonic()+120)
    assert result['pages'][0]['ocr_state']=='ocr_unavailable' and result['pages'][0]['external_paused']


def test_pdf_text_layer_does_not_bypass_raster_secret_check(tmp_path,monkeypatch):
    path=tmp_path/'guide.pdf'
    path.write_bytes(pdf_bytes(1))
    monkeypatch.setattr(media_worker,'ocr',lambda image:[{'text':'[REDACTED]','confidence':.99,'region':[], 'sensitive':True}])
    result=process_media(path,source(path,tmp_path/'derived'),{},time.monotonic()+120)
    assert result['pages'][0]['external_paused']
    assert result['pages'][0]['text']=='[REDACTED_SENSITIVE_PAGE]'
    assert not (tmp_path/'derived'/'vision-1.jpg').exists()


@pytest.mark.parametrize('archive_kind',['direct','zip','tar','tar.gz'])
def test_actual_ingestion_pipeline_preserves_media_digest_and_page_sources(client,archive_kind):
    profile=client.post('/api/v1/businesses',json={'name':'Source equivalence '+archive_kind}).json()
    raw=pdf_bytes(2)
    filename='guide.pdf'
    payload=raw
    if archive_kind=='zip':
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as bundle:
            bundle.writestr(filename,raw)
        filename='guide.zip'
        payload=stream.getvalue()
    elif archive_kind.startswith('tar'):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz' if archive_kind=='tar.gz' else 'w') as bundle:
            member=tarfile.TarInfo(filename)
            member.size=len(raw)
            bundle.addfile(member,io.BytesIO(raw))
        filename='guide.'+archive_kind
        payload=stream.getvalue()
    base='/api/v1/businesses/'+profile['business_id']
    accepted=client.post(base+'/artifacts?filename='+filename,content=payload).json()
    assert wait_job(client,accepted['job_id'])['status']=='completed'
    result=client.get(base).json()
    assert len(result['documents'])==2
    assert result['source_references'][0]['pages_processed']==2
    first=result['documents'][0]['source_refs'][0]
    assert first['content_digest']==hashlib.sha256(raw).hexdigest()
    assert first['archive_path']=='guide.pdf' and first['original_artifact_id']==accepted['artifact_id']
    assert first['profile_version']==result['profile_version'] and first['page_number']==1
    removed=client.delete(base+'/artifacts/'+accepted['artifact_id']).json()
    assert removed['documents']==[]
    assert all(f['verification_status']=='stale' for f in removed['facts'] if f['predicate']=='describes')
