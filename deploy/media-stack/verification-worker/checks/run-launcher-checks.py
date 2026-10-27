"""Exercise launchers in real PowerShell with fake Docker, no network/socket/data."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
IMAGE='mcr.microsoft.com/powershell@sha256:206a748b34deec1b64553fcfa92294fc871c2df5855e9f340df172756bef201f'

def main():
    with tempfile.TemporaryDirectory(prefix='launcher-checks-') as directory:
        test=Path(directory)
        shutil.copy2(ROOT/'worker.ps1',test/'worker.ps1')
        for name in ('mock-docker.ps1','test-launchers.ps1'):shutil.copy2(ROOT/'checks'/name,test/name)
        fake=test/'docker.exe';fake.write_text('#!/bin/sh\nexec pwsh -NoProfile -File /test/mock-docker.ps1 "$@"\n');fake.chmod(0o755)
        for leaf in ('config','data','data-2','data-3','data-4'):
            (test/leaf).mkdir();(test/leaf/'preserve.txt').write_text('saved-'+leaf)
        (test/'config/verification.local.json').write_text('{}')
        blob=b'fixture image';(test/'verification-worker-image.tar.gz').write_bytes(blob)
        (test/'SHA256SUMS.json').write_text(json.dumps({'verification-worker-image.tar.gz':hashlib.sha256(blob).hexdigest()}))
        for name in ('08-START-SECOND.cmd','unrelated.cmd'):(test/name).write_text('fixture')
        containers={}
        for lane in range(1,5):
            name='home-media-verification-worker'+('-'+str(lane) if lane>1 else '')
            containers[name]={'Name':'/'+name,'State':{'Running':lane!=3,'Status':'running' if lane!=3 else 'exited'},'Config':{'Image':'old-verifier','Labels':{'home-media.component':'verification-worker'}}}
        (test/'docker-state.json').write_text(json.dumps({'image':True,'fail_load':False,'containers':containers}))
        subprocess.run(['docker','run','--rm','--network','none','--read-only','--memory','512m','--cpus','1','--tmpfs','/tmp:rw,size=128m','-e','PATH=/test:/opt/microsoft/powershell/7:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','-v',str(test)+':/test:rw',IMAGE,'pwsh','-NoProfile','-File','/test/test-launchers.ps1'],check=True)

if __name__=='__main__':main()
