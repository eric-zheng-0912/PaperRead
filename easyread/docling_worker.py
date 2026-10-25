"""Standalone, standard-library-only entry point for the external Docling env.

JSON request on stdin, native structured JSON in the requested output file.
Never import PaperRead here: its environment deliberately does not own torch.
"""
import os
import sys
import json
import time
import hashlib
import socket
from pathlib import Path

VERSION = 1


def main():
    global request
    request = json.load(sys.stdin)
    runtime = Path(request['runtime']).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ('temp', 'cache', 'torch'):
        (runtime/name).mkdir(exist_ok=True)
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
        HF_HUB_CACHE=request['hub'], DOCLING_DEVICE='cpu', DOCLING_NUM_THREADS='4',
        TEMP=str(runtime/'temp'), TMP=str(runtime/'temp'), XDG_CACHE_HOME=str(runtime/'cache'),
        TORCH_HOME=str(runtime/'torch'), PYTHONDONTWRITEBYTECODE='1', TOKENIZERS_PARALLELISM='false')
    def no_network(*args, **kwargs):
        raise RuntimeError('Offline Docling worker prohibits network access')
    socket.socket.connect = no_network
    socket.create_connection = no_network
    import importlib.metadata
    if sys.version_info < (3, 10):
        raise RuntimeError('Python 3.10 or newer required')
    version = importlib.metadata.version('docling')
    if version.split('.')[0] != '2':
        raise RuntimeError('Unsupported Docling API version; legacy extractor remains available')
    start = time.perf_counter()
    if request.get('probe'): print('probe: importing local inference dependencies', flush=True)
    import huggingface_hub
    original_snapshot = huggingface_hub.snapshot_download
    def local_snapshot(*args, **kwargs):
        kwargs['local_files_only'] = True
        kwargs['force_download'] = False
        return original_snapshot(*args, **kwargs)
    # Enforce local-only lookup, including Docling helpers named download_models.
    huggingface_hub.snapshot_download = local_snapshot
    import torch
    if request.get('probe'): print('probe: torch imported; importing Docling PDF API', flush=True)
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableStructureOptions, TableFormerMode
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    torch.set_num_threads(4)
    if request.get('probe'): print('probe: constructing offline CPU pipeline options', flush=True)
    options = PdfPipelineOptions(do_ocr=False, do_table_structure=True, enable_remote_services=False,
        do_code_enrichment=False, do_formula_enrichment=False, do_picture_classification=False,
        do_picture_description=False, allow_external_plugins=False,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4),
        table_structure_options=TableStructureOptions(mode=TableFormerMode.ACCURATE, do_cell_matching=True))
    converter = DocumentConverter(allowed_formats=[InputFormat.PDF],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    if request.get('probe'):
        Path(request['output']).write_text(json.dumps({'protocol':VERSION,'docling':version,
            'torch':torch.__version__,
            'python_version':list(sys.version_info[:3]),'status':'basic_import','inference':False,
            'reason':'Offline imports and pipeline configuration checked; no model initialization or inference'}),encoding='utf8')
        return
    init = time.perf_counter()
    converter.initialize_pipeline(InputFormat.PDF)
    initialized = time.perf_counter()-init
    pdf = Path(request['pdf'])
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != request['pdf_sha256']:
        raise RuntimeError('Recognition snapshot changed')
    convert = time.perf_counter()
    result = converter.convert(pdf, raises_on_error=True)  # One whole document, once.
    if getattr(result.status,'value',None)!='success':
        raise RuntimeError('Docling did not complete the document; existing extractor retained')
    document = result.document
    def prov(item):
        return [{'page_no': p.page_no,
                 'bbox': p.bbox.to_top_left_origin(document.pages[p.page_no].size.height).model_dump(mode='json')}
                for p in item.prov]
    def refs(values):
        return [{'text': r.resolve(document).text, 'provenance': prov(r.resolve(document))} for r in values]
    tables = [{'id': t.self_ref, 'provenance': prov(t), 'rows': t.data.num_rows, 'columns': t.data.num_cols,
               'cells': [c.model_dump(mode='json') for c in t.data.table_cells],
               'captions': refs(t.captions), 'footnotes': refs(getattr(t, 'footnotes', []))} for t in document.tables]
    metrics = {'initialization_seconds': initialized, 'conversion_seconds': time.perf_counter()-convert,
               'total_seconds': time.perf_counter()-start, 'cpu_threads': 4, 'cpu_only': True}
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Memory(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)]+[(n, ctypes.c_size_t) for n in
                ('peak_rss','rss','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile')]
        kernel = ctypes.WinDLL('kernel32');kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi');psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,ctypes.POINTER(Memory),wintypes.DWORD]
        memory = Memory();memory.cb = ctypes.sizeof(memory)
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(memory),memory.cb):
            metrics.update(peak_rss_bytes=memory.peak_rss, peak_commit_bytes=memory.peak_pagefile)
    output = {'protocol': VERSION, 'pdf': request['pdf_sha256'], 'engine': 'docling-'+version+'-tableformer-v1-accurate',
              'pages': {str(n): {'width': p.size.width, 'height': p.size.height} for n,p in document.pages.items()},
              'tables': tables, 'metrics': metrics}
    Path(request['output']).write_text(json.dumps(output, ensure_ascii=False), encoding='utf8')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Preserve package-vs-import distinction without running inference.
        if isinstance(globals().get('request'), dict) and request.get('probe'):
            import importlib.metadata
            packages = {}
            for name in ('docling','torch','docling-core','docling-ibm-models','transformers','huggingface-hub'):
                try: packages[name] = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError: packages[name] = None
            Path(request['output']).write_text(json.dumps({'status':'incomplete' if packages['docling'] else 'unavailable',
                'docling':packages['docling'],'packages':packages,'python_version':list(sys.version_info[:3]),
                'inference':False,'reason':type(exc).__name__+': '+str(exc)}),encoding='utf8')
            sys.exit(0)
        print(type(exc).__name__+': '+str(exc), file=sys.stderr)
        sys.exit(1)
