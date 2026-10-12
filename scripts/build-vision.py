#!/usr/bin/env python3
"""Build only pinned upstream Torchvision, reusing the original audited Torch wheel."""
import hashlib, io, json, os, platform, posixpath, re, shutil, subprocess, sys, tarfile
import urllib.request, zipfile, importlib.util
from pathlib import Path

def clean_environment(environment=None):
    environment = os.environ if environment is None else environment
    return {k: environment[k] for k in ('PATH','HOME','USERPROFILE','TMPDIR','TMP','TEMP','LANG','LC_ALL') if k in environment}

def verify_bytes(data, spec):
    if ('bytes' in spec and len(data) != spec['bytes']) or len(data) > spec.get('maxBytes', spec.get('bytes', 500000000)) or hashlib.sha256(data).hexdigest() != spec['sha256']:
        raise ValueError('ARTIFACT_HASH_OR_SIZE_MISMATCH')
    return data

def download(spec):
    request = urllib.request.Request(spec['url'], headers={'User-Agent':'exact-oss-vision-wheel-builder'})
    limit = spec.get('maxBytes', spec.get('bytes', 500000000))
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(limit + 1)
    return verify_bytes(data, spec)

def extract_python(data, destination, prefix='python/'):
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
        entries = {entry.name.rstrip('/'):entry for entry in archive.getmembers()}
        def safe(name):
            if not name.startswith(prefix) or '\\' in name or any(part in ('','..','.') for part in name.split('/')):
                raise ValueError('PYTHON_ARCHIVE_PATH_REJECTED')
            return name[len(prefix):]
        def regular(entry, visited):
            if entry.name in visited: raise ValueError('PYTHON_ARCHIVE_LINK_CYCLE')
            if entry.isfile(): return entry
            if entry.issym() or entry.islnk():
                linked = posixpath.normpath(posixpath.join(posixpath.dirname(entry.name),entry.linkname)) if entry.issym() else entry.linkname
                safe(linked)
                if linked not in entries: raise ValueError('PYTHON_ARCHIVE_LINK_MISSING')
                return regular(entries[linked], visited | {entry.name})
            raise ValueError('PYTHON_ARCHIVE_TYPE_REJECTED')
        for entry in entries.values():
            if entry.name.rstrip('/') == prefix.rstrip('/'): continue
            relative = safe(entry.name.rstrip('/')); target = destination / relative
            if entry.isdir(): target.mkdir(parents=True, exist_ok=True); continue
            resolved = regular(entry,set()); target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(resolved)
            if stream is None: raise ValueError('PYTHON_ARCHIVE_DATA_MISSING')
            with stream, target.open('wb') as output: shutil.copyfileobj(stream, output)
            target.chmod(resolved.mode & 0o777)

def digest(file):
    checksum = hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''): checksum.update(block)
    return checksum.hexdigest()

def run(args, *, environment, cwd=None, log=None):
    with subprocess.Popen([str(arg) for arg in args],cwd=cwd,env=environment,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True) as child:
        for line in child.stdout:
            print(line,end='',flush=True)
            if log: log.write(line);log.flush()
        if child.wait(): raise RuntimeError('BUILD_COMMAND_FAILED:'+str(child.returncode))

def collect_notices(source,destination):
    for file in source.rglob('*'):
        if file.is_file() and not file.is_symlink() and '.git' not in file.relative_to(source).parts and any(value in file.name.upper() for value in ['LICENSE','NOTICE','COPYING']):
            target=destination/file.relative_to(source);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,target)

def build_codecs(inputs,work,out,python,environment,log):
    prefix=work/'codec-prefix';prefix.mkdir(parents=True,exist_ok=True);records=[]
    cmake=python.parent/'cmake';nasm=shutil.which('nasm',path=environment['PATH'])
    if not nasm:raise ValueError('ORIGINAL_JPEG_SIMD_ASSEMBLER_REQUIRED')
    assembler={'path':nasm,'sha256':digest(Path(nasm).resolve()),'version':subprocess.check_output([nasm,'-v'],env=environment,text=True).strip(),'scope':'Existing standard runner build tool; not installed on users computers'}
    for spec in sorted(inputs['codecs'],key=lambda item:['libpng','jpeg-turbo','webp'].index(item['name'])):
        src=work/('codec-source-'+spec['name']);src.mkdir(parents=True,exist_ok=True);extract_python(download(spec),src,spec['stripPrefix'])
        collect_notices(src,out/'licenses/codecs'/spec['name'])
        target=work/('codec-build-'+spec['name']);flags=['-DCMAKE_BUILD_TYPE=Release','-DCMAKE_OSX_ARCHITECTURES=x86_64','-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0','-DCMAKE_INSTALL_PREFIX='+str(prefix),'-DCMAKE_PREFIX_PATH='+str(prefix),'-DCMAKE_IGNORE_PREFIX_PATH=/usr/local;/opt/homebrew','-DBUILD_SHARED_LIBS=ON','-DCMAKE_ASM_NASM_COMPILER='+nasm]
        run([cmake,'-S',src,'-B',target,*flags],environment=environment,log=log)
        run([cmake,'--build',target,'--parallel','3'],environment=environment,log=log)
        run([cmake,'--install',target],environment=environment,log=log)
        cache=target/'CMakeCache.txt';records.append({**spec,'sourceArchiveVerified':True,'sourcePrefix':str(src),'installPrefix':str(prefix),'cmakeFlags':flags,'cmakeCacheSha256':digest(cache),'cmakeCacheLines':[line for line in cache.read_text().splitlines() if any(key in line for key in ['CMAKE_OSX_DEPLOYMENT_TARGET','CMAKE_OSX_ARCHITECTURES','JPEG','WEBP','PNG','SIMD'])]})
    (out/'CODEC_SOURCE_BUILD_PROVENANCE.json').write_text(json.dumps({'sources':records,'minimumMacOS':'14.0','architecture':'x86_64','homebrewBottlesUsed':False,'assembler':assembler,'codecRuntimeFeaturesDisabled':False},indent=2)+'\n')
    return prefix

def build(repo):
    if platform.system() != 'Darwin' or platform.machine() != 'x86_64' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('NATIVE_STANDARD_INTEL_RUNNER_REQUIRED')
    inputs=json.loads((repo/'public-build-inputs.json').read_text(encoding='utf-8-sig'))
    if inputs['vision']['commit'] != '1f1a920d685d46dfbe187934b785b7ecb7a182f2' or inputs['vision']['version'] != '0.29.1' or inputs['torch']['sha256'] != '2024626159a2d3be915608baed1c82612de6f8f3e0846de4d07911f42ac563df':
        raise ValueError('EXACT_ORIGINAL_LIBRARY_REQUIRED')
    work=Path(os.environ['RUNNER_TEMP'])/'exact-oss-vision'; work.mkdir(parents=True,exist_ok=True)
    out=repo/'result';out.mkdir(parents=True,exist_ok=True)
    (out/'wheels').mkdir(exist_ok=True);(out/'raw-wheels').mkdir(exist_ok=True);(out/'licenses').mkdir(exist_ok=True)
    environment=clean_environment();environment.update({'PYTHONNOUSERSITE':'1','PIP_DISABLE_PIP_VERSION_CHECK':'1','PIP_CONFIG_FILE':'/dev/null','MACOSX_DEPLOYMENT_TARGET':'14.0','MAX_JOBS':'3','CMAKE_BUILD_PARALLEL_LEVEL':'3','CC':'/usr/bin/clang','CXX':'/usr/bin/clang++','BUILD_VERSION':'0.29.1'})
    python_root=work/'python';python_root.mkdir(exist_ok=True);extract_python(download(inputs['python']),python_root);python=python_root/'bin/python3'
    environment['PATH']=str(python_root/'bin')+os.pathsep+environment.get('PATH','')
    source=work/'source'; success=False;actual=None
    with (out/'build.log').open('w') as log:
        try:
            run([python,'-I','-B','-c','import sys;assert sys.version_info[:3]==(3,13,7)'],environment=environment,log=log)
            wheelhouse=work/'wheelhouse';wheelhouse.mkdir(exist_ok=True)
            for spec in inputs['wheels']:
                if not re.fullmatch(r'[A-Za-z0-9._-]+\.whl',spec['filename']) or not spec['url'].startswith('https://files.pythonhosted.org/'):
                    raise ValueError('OFFICIAL_EXACT_WHEEL_REQUIRED')
                (wheelhouse/spec['filename']).write_bytes(download(spec))
            requirements=work/'requirements.txt';requirements.write_text(''.join(spec['name']+'=='+spec['version']+' --hash=sha256:'+spec['sha256']+'\n' for spec in inputs['wheels']))
            run([python,'-I','-B','-m','pip','--isolated','install','--no-index','--no-deps','--require-hashes','--only-binary=:all:','--find-links',wheelhouse,'-r',requirements],environment=environment,log=log)
            original_torch=work/inputs['torch']['filename'];original_torch.write_bytes(download(inputs['torch']))
            notices=download(inputs['torch']['notices']); (out/'UPSTREAM_TORCH_NOTICES.zip').write_bytes(notices)
            summary=download(inputs['torch']['summary']);(out/'UPSTREAM_TORCH_PUBLIC_PROVENANCE.json').write_bytes(summary)
            run([python,'-I','-B','-m','pip','--isolated','install','--no-index','--no-deps',original_torch],environment=environment,log=log)
            codecs=build_codecs(inputs,work,out,python,environment,log)
            environment['TORCHVISION_INCLUDE']=str(codecs/'include')
            environment['TORCHVISION_LIBRARY']=str(codecs/'lib')
            environment['PATH']=str(codecs/'bin')+os.pathsep+environment['PATH']
            run(['git','init',source],environment=environment,log=log)
            run(['git','-C',source,'remote','add','origin',inputs['vision']['repository']],environment=environment,log=log)
            run(['git','-C',source,'fetch','--depth=1','origin',inputs['vision']['commit']],environment=environment,log=log)
            run(['git','-C',source,'checkout','--detach','FETCH_HEAD'],environment=environment,log=log)
            actual=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],env=environment,text=True).strip()
            if actual != inputs['vision']['commit']:raise ValueError('ORIGINAL_SOURCE_COMMIT_MISMATCH')
            run(['git','-C',source,'submodule','update','--init','--recursive','--depth=1'],environment=environment,log=log)
            collect_notices(source,out/'licenses')
            run([python,'-I','-B','-m','pip','--isolated','wheel','-v','--no-deps','--no-build-isolation','--wheel-dir',out/'raw-wheels','.'],cwd=source,environment=environment,log=log)
            raw=list((out/'raw-wheels').glob('*.whl'))
            if len(raw)!=1:raise ValueError('SINGLE_EXACT_VISION_WHEEL_REQUIRED')
            module_spec=importlib.util.spec_from_file_location('codec_vendor',repo/'scripts/vendor-codecs.py');module=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(module)
            torch_lib=python.parent.parent/'lib/python3.13/site-packages/torch/lib'
            repaired=out/'wheels'/raw[0].name;repair=module.repair(raw[0],repaired,work,torch_lib,codecs,environment)
            (out/'CODEC_WHEEL_REPAIR_PROVENANCE.json').write_text(json.dumps(repair,indent=2)+'\n')
            run([python,'-I','-B','-m','pip','--isolated','install','--force-reinstall','--no-index','--no-deps',repaired],environment=environment,log=log)
            probe="""import importlib.metadata,json,torch,torchvision,io
from PIL import Image
assert importlib.metadata.version('torch')=='2.14.1'
assert importlib.metadata.version('torchvision')=='0.29.1'
assert torch.equal(torch.eye(3)@torch.ones(3),torch.ones(3))
assert torchvision.ops.nms(torch.tensor([[0.,0.,10.,10.],[1.,1.,9.,9.]]),torch.tensor([0.9,0.8]),0.5).tolist()==[0]
image=torch.zeros((3,8,8),dtype=torch.uint8);image[0]=80;image[1]=160;image[2]=240
assert torch.equal(torchvision.io.decode_png(torchvision.io.encode_png(image)),image)
jpeg=torchvision.io.decode_jpeg(torchvision.io.encode_jpeg(image,quality=100));assert jpeg.shape==image.shape and torch.max(torch.abs(jpeg.to(torch.int16)-image.to(torch.int16))).item()<=4
buffer=io.BytesIO();Image.fromarray(image.permute(1,2,0).numpy()).save(buffer,format='WEBP',lossless=True)
assert torch.equal(torchvision.io.decode_webp(torch.frombuffer(bytearray(buffer.getvalue()),dtype=torch.uint8)),image)
buffer=io.BytesIO();frame=Image.fromarray(image.permute(1,2,0).numpy());second=Image.new('RGB',(8,8),(240,160,80));frame.save(buffer,format='GIF',save_all=True,append_images=[second],duration=100,loop=0)
gif=torchvision.io.decode_gif(torch.frombuffer(bytearray(buffer.getvalue()),dtype=torch.uint8));assert gif.shape==(2,3,8,8)
print(json.dumps({'nativeImportVerified':True,'name':'torchvision','version':'0.29.1','jpegEncodeDecode':True,'pngEncodeDecodeLossless':True,'webpDecodeLossless':True,'gifDecode':True,'nms':True,'cpuMatmul':True,'wholeEngineAccepted':False}))"""
            run([python,'-I','-B','-c',probe],environment=environment,log=log);success=True
        finally:
            wheels=[]
            for file in sorted((out/'wheels').glob('*.whl')):
                with zipfile.ZipFile(file) as archive:metadata=archive.read(next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))).decode()
                wheels.append({'filename':file.name,'sha256':digest(file),'bytes':file.stat().st_size,'metadata':metadata[:12000]})
            licenses=[{'path':str(file.relative_to(out)),'sha256':digest(file),'bytes':file.stat().st_size} for file in (out/'licenses').rglob('*') if file.is_file()]
            (out/'BUILD_PROVENANCE.json').write_text(json.dumps({'format':'exact-oss-vision-source-wheel-v1','package':'torchvision','source':inputs['vision'],'actualCommit':actual,'python':'3.13.7','os':platform.platform(),'machine':platform.machine(),'buildSucceeded':success,'wheels':wheels,'licenses':licenses,'runId':os.environ.get('GITHUB_RUN_ID'),'torchRebuilt':False,'upstreamTorchSha256':inputs['torch']['sha256'],'publicationRequired':True,'wholeEngineAccepted':False,'versionFallback':False,'modelFallback':False,'credentialsCopied':False},indent=2)+'\n')

if __name__=='__main__':build(Path(__file__).resolve().parents[1])
