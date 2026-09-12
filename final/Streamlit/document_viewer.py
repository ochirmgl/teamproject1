"""Render original document pages locally, without uploading to a viewer service."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import streamlit as st
import pypdfium2 as pdfium

OFFICE_TYPES = {'.doc','.docx','.xls','.xlsx','.ppt','.pptx','.odt','.ods','.odp'}


def find_office():
    configured = os.getenv('LIBREOFFICE_PATH')
    choices = [configured, shutil.which('soffice'), shutil.which('libreoffice'),
               r'C:\Program Files\LibreOffice\program\soffice.exe',
               r'C:\Program Files (x86)\LibreOffice\program\soffice.exe']
    return next((str(p) for p in choices if p and Path(p).is_file()), None)


def convert(source, cache_root):
    source = Path(source)
    engine = find_office()
    if not engine:
        raise RuntimeError('Бүрэн хуудас харуулах үйлчилгээ бэлэн болоогүй байна. Серверт LibreOffice суулгах шаардлагатай.')
    signature = hashlib.sha256(source.read_bytes() + str(Path(engine).stat().st_mtime_ns).encode()).hexdigest()
    cache = Path(cache_root)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (signature + '.pdf')
    if target.is_file():
        return target
    with tempfile.TemporaryDirectory(prefix='convert-',dir=cache) as temporary:
        folder = Path(temporary)
        # Use a safe ASCII copy to avoid path/locale problems; preserve original bytes.
        copy = folder / ('source' + source.suffix.lower())
        shutil.copyfile(source, copy)
        result = subprocess.run([engine, '-env:UserInstallation=' + (folder/'profile').as_uri(),
            '--headless','--convert-to','pdf','--outdir',str(folder),str(copy)],
            capture_output=True, timeout=120)
        output = folder / 'source.pdf'
        if result.returncode or not output.is_file():
            raise RuntimeError('Хуудсыг хөрвүүлж чадсангүй. Файл нууц үгтэй эсвэл гэмтсэн эсэхийг шалгана уу.')
        with pdfium.PdfDocument(str(output)) as pdf:
            if len(pdf) == 0:
                raise RuntimeError('Хуудас олдсонгүй.')
        os.replace(output,target)
    return target


def render_pages(path, key, initial_page=1):
    with pdfium.PdfDocument(str(path)) as pdf:
        count = len(pdf)
        if not count:
            st.warning('Хуудас олдсонгүй.')
            return
        page_number = st.number_input('Хуудас',min_value=1,max_value=count,value=max(1,min(initial_page,count)),step=1,key=f'page_{key}_{initial_page}')
        st.caption(f'{page_number} / {count} хуудас')
        zoom = st.select_slider('Томруулах',options=[100,125,150,200],value=125,key=f'zoom_{key}')
        page = pdf[int(page_number)-1]
        bitmap = page.render(scale=zoom/75)
        try:
            st.image(bitmap.to_pil(),use_container_width=True)
        finally:
            bitmap.close()
            page.close()


def render_document(path, root, initial_page=1):
    path = Path(path)
    key = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        render_pages(path,key,initial_page)
    elif suffix in OFFICE_TYPES:
        with st.spinner('Баримтын хуудсыг бэлтгэж байна...'):
            converted = convert(path,Path(root)/'preview_cache')
        render_pages(converted,key,initial_page)
    elif suffix in {'.png','.jpg','.jpeg','.webp'}:
        st.image(str(path),use_container_width=True)
    elif suffix in {'.txt','.csv','.log'}:
        # Plain files have no page layout: show their literal source, not extracted sections.
        st.code(path.read_text(encoding='utf-8-sig'),language=None,wrap_lines=False)
    else:
        st.warning('Энэ файлын дүрслэл хараахан дэмжигдээгүй байна.')
