"""Vendor only locally source-built codecs; leave the installed original Torch shared libraries separate."""
import base64, csv, hashlib, io, json, os, re, shutil, subprocess, zipfile
from pathlib import Path

def sha(file):return hashlib.sha256(file.read_bytes()).hexdigest()
def inside(file,root):
    try:Path(file).resolve().relative_to(Path(root).resolve());return True
    except ValueError:return False

def classify_dependency(name,torch,codecs):
    if name.startswith(('/usr/lib/','/System/Library/')):return 'system',None
    basename=Path(name).name
    if re.fullmatch(r'lib(?:torch(?:_[A-Za-z0-9_]+)?|c10)\.dylib',basename):
        target=torch/basename
        if not target.is_file() or not (name=='@rpath/'+basename or Path(name).is_absolute() and Path(name).resolve()==target.resolve()):raise ValueError('TORCH_LIBRARY_REFERENCE_INVALID')
        return 'torch',target.resolve()
    target=Path(name)
    if target.is_absolute() and inside(target,codecs) and target.is_file():return 'codec',target.resolve()
    if name.startswith('@rpath/'):
        target=codecs/'lib'/basename
        if target.is_file() and inside(target,codecs):return 'codec',target.resolve()
    raise ValueError('EXTERNAL_LIBRARY_NOT_ALLOWED:'+name)

def minimum_supported(loads):
    values=re.findall(r'\bminos\s+(\d+(?:\.\d+){1,2})',loads)
    if not values:values=re.findall(r'cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+(?:\.\d+){1,2})',loads)
    if not values or any(tuple(map(int,value.split('.')))+(0,)*(3-len(value.split('.')))>(14,0,0) for value in values):raise ValueError('MACOS_14_MINIMUM_NOT_PRESERVED')
    return max(values,key=lambda value:tuple(map(int,value.split('.'))))

def capture(args,environment):return subprocess.check_output([str(arg) for arg in args],env=environment,text=True)
def load_names(file,environment):
    output=capture(['/usr/bin/otool','-L',file],environment)
    own=capture(['/usr/bin/otool','-D',file],environment).splitlines()[1:]
    return [line.strip().split(' (compatibility version')[0] for line in output.splitlines()[1:] if line.strip().split(' (compatibility version')[0] not in own]

def repair(raw,output,work,torch,codecs,environment):
    tree=work/'repair';tree.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(raw) as archive:
        for info in archive.infolist():
            if info.is_dir():continue
            if info.filename.startswith('/') or '\\' in info.filename or any(part in ('','..','.') for part in info.filename.split('/')):raise ValueError('WHEEL_PATH_REJECTED')
            target=tree/info.filename;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(info))
    native=list((tree/'torchvision').glob('*.so'));vendor=tree/'torchvision/.codec_dylibs';vendor.mkdir()
    pending=list(native);mapped={};mapped_hashes={};mapped_sources={};records=[];torch_records={}
    while pending:
        file=pending.pop(0);loads=capture(['/usr/bin/otool','-l',file],environment);minimum=minimum_supported(loads)
        if not re.search(r'\barchitecture:? x86_64\b',capture(['/usr/bin/lipo','-info',file],environment)):raise ValueError('CODEC_X86_64_REQUIRED')
        original_sha=sha(file);changes=[]
        for dependency in load_names(file,environment):
            if dependency.startswith('@loader_path/'):
                resolved=(file.parent/dependency[len('@loader_path/'):]).resolve()
                if inside(resolved,tree) and resolved.is_file():continue
            kind,resolved=classify_dependency(dependency,torch,codecs)
            if kind=='system':continue
            if kind=='torch':
                torch_records[resolved.name]={'name':resolved.name,'sha256':sha(resolved),'bytes':resolved.stat().st_size,'copied':False}
                replacement='@rpath/'+resolved.name
            else:
                key=resolved.name
                if key not in mapped:
                    target=vendor/key;shutil.copyfile(resolved,target);mapped[key]=target;mapped_hashes[key]=sha(resolved);mapped_sources[key]=str(resolved);pending.append(target)
                elif sha(resolved)!=mapped_hashes[key]:
                    raise ValueError('CODEC_FILENAME_CONFLICT')
                replacement='@loader_path/'+os.path.relpath(mapped[key],file.parent).replace(os.sep,'/')
            if replacement!=dependency:subprocess.run(['/usr/bin/install_name_tool','-change',dependency,replacement,str(file)],env=environment,check=True);changes.append({'from':dependency,'to':replacement})
        rpaths=re.findall(r'cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset',loads)
        for rpath in rpaths:
            if not rpath.startswith('@loader_path') or ':' in rpath:subprocess.run(['/usr/bin/install_name_tool','-delete_rpath',rpath,str(file)],env=environment,check=True)
        if file in native and '@loader_path/../torch/lib' not in rpaths:subprocess.run(['/usr/bin/install_name_tool','-add_rpath','@loader_path/../torch/lib',str(file)],env=environment,check=True)
        if file.suffix=='.dylib':subprocess.run(['/usr/bin/install_name_tool','-id','@rpath/'+file.name,str(file)],env=environment,check=True)
        subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(file)],env=environment,check=True)
        records.append({'path':str(file.relative_to(tree)),'sourceSha256':original_sha,'sha256':sha(file),'minimumMacOS':minimum,'changes':changes,'sourcePath':mapped_sources.get(file.name),'rawOtoolLoadCommandsBeforeRepair':loads,'rawOtoolLoadCommandsAfterRepair':capture(['/usr/bin/otool','-l',file],environment),'rawOtoolLibrariesAfterRepair':capture(['/usr/bin/otool','-L',file],environment)})
    # Recheck actual repaired dependencies after all install-name changes; no Brew or build-temp paths survive.
    for file in native+list(vendor.glob('*.dylib')):
        minimum_supported(capture(['/usr/bin/otool','-l',file],environment))
        for dependency in load_names(file,environment):
            if dependency.startswith('@loader_path/'):
                target=(file.parent/dependency[len('@loader_path/'):]).resolve()
                if not target.is_file() or not inside(target,tree):raise ValueError('VENDORED_CODEC_CLOSURE_FAILED')
            elif classify_dependency(dependency,torch,codecs)[0] not in ('system','torch'):raise ValueError('EXTERNAL_CODEC_NOT_VENDORED')
    metadata=next(tree.glob('*.dist-info'));record=metadata/'RECORD';rows=[]
    for file in sorted(tree.rglob('*')):
        if file.is_file() and file!=record:rows.append([str(file.relative_to(tree)).replace(os.sep,'/'),'sha256='+base64.urlsafe_b64encode(hashlib.sha256(file.read_bytes()).digest()).decode().rstrip('='),str(file.stat().st_size)])
    rows.append([str(record.relative_to(tree)).replace(os.sep,'/'),'','']);stream=io.StringIO();csv.writer(stream,lineterminator='\n').writerows(rows);record.write_text(stream.getvalue())
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(tree.rglob('*')):
            if file.is_file():archive.write(file,str(file.relative_to(tree)).replace(os.sep,'/'))
    return {'inputWheelSha256':sha(raw),'outputWheelSha256':sha(output),'minimumMacOS':'14.0','architecture':'x86_64','nativeImages':records,'originalTorchLibraries':list(torch_records.values()),'torchLibrariesCopied':False,'codecLibrariesCopied':len(mapped),'externalHomebrewDependenciesRemaining':0,'licenseAndSourceProvenanceRequired':True}
