"""Build the private Windows package from locally available, pinned images."""
from pathlib import Path
import gzip
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parent
VERSION='0.3.0'
IMAGES=('home-media-verification-worker:'+VERSION,
        'home-media-verification-torrserver:'+VERSION)
FILES=('worker.ps1','START-HERE.txt','README.md','config.example.json',
       '01-UPDATE-AND-START.cmd','02-START-ALL.cmd','03-LOGS.cmd','04-STOP-ALL.cmd')

def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()

def build(destination=Path('<SERVICE_HOME>/verification-worker-v0.3.0.zip')):
    with tempfile.TemporaryDirectory(prefix='verification-worker-package-') as temporary:
        temporary=Path(temporary);folder=temporary/'HomeMediaVerificationWorker';folder.mkdir()
        for name in FILES:shutil.copy2(ROOT/name,folder/name)
        image=folder/'verification-worker-image.tar.gz'
        raw=temporary/'images.tar'
        subprocess.run(['docker','save','--output',str(raw),*IMAGES],check=True)
        with raw.open('rb') as source,image.open('wb') as target:
            with gzip.GzipFile(filename='',mode='wb',compresslevel=6,fileobj=target,mtime=0) as output:
                shutil.copyfileobj(source,output,1024*1024)
        (folder/'SHA256SUMS.json').write_text(json.dumps({image.name:digest(image)},indent=2)+'\n')
        candidate=temporary/'package.zip'
        with zipfile.ZipFile(candidate,'w') as archive:
            for path in sorted(folder.iterdir()):
                archive.write(path,str(path.relative_to(temporary)),
                              compress_type=zipfile.ZIP_STORED if path==image else zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(candidate) as archive:
            assert archive.testzip() is None
            names=archive.namelist()
            assert len([name for name in names if name.lower().endswith('.cmd')])==4
            assert len(names)==10
            assert not any('/data' in name or 'torrent-state/' in name or '.local.json' in name for name in names)
        destination.parent.mkdir(parents=True,exist_ok=True)
        staged=destination.with_suffix(destination.suffix+'.tmp')
        shutil.copyfile(candidate,staged)
        account=pwd.getpwnam('server');os.chown(staged,account.pw_uid,account.pw_gid);staged.chmod(0o644)
        staged.replace(destination)
    return {'archive':str(destination),'bytes':destination.stat().st_size,'sha256':digest(destination)}

if __name__=='__main__':print(json.dumps(build(),indent=2))
