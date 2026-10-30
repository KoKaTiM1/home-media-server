#!/usr/bin/env python3
"""Run public snapshot regressions in a container without production access."""
import argparse, datetime, fcntl, hashlib, json, os, pathlib, subprocess, sys, tarfile, tempfile, time

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repository',type=pathlib.Path,required=True)
    p.add_argument('--commit',help='Test this committed tree rather than the working snapshot')
    p.add_argument('--image',default='home-media-tests:1')
    p.add_argument('--report-dir',type=pathlib.Path,default=pathlib.Path('/var/lib/media-regression'))
    a=p.parse_args();a.report_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (a.report_dir/'gate.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix='media-regression-') as temporary:
            root=a.repository.resolve(); commit=a.commit
            if commit:
                commit=subprocess.check_output(['git','-C',str(root),'rev-parse',commit+'^{commit}'],text=True).strip()
                archive=pathlib.Path(temporary)/'source.tar'
                with archive.open('wb') as f:subprocess.run(['git','-C',str(root),'archive',commit],stdout=f,check=True)
                root=pathlib.Path(temporary)/'source';root.mkdir()
                with tarfile.open(archive) as f:f.extractall(root,filter='data')
            digest=hashlib.sha256()
            for path in sorted(root.rglob('*')):
                if not path.is_file() or any(x in {'.git','__pycache__'} for x in path.relative_to(root).parts):continue
                digest.update(str(path.relative_to(root)).encode()+b'\0'+path.read_bytes()+b'\0')
            fingerprint=digest.hexdigest()
            image_id=subprocess.check_output(['docker','image','inspect',a.image,'--format','{{.Id}}'],text=True).strip()
            latest=a.report_dir/'latest.json'
            prior=json.loads(latest.read_text()) if latest.exists() else {}
            if prior.get('passed') and prior.get('fingerprint')==fingerprint and prior.get('image_id')==image_id:
                prior['commit']=commit;temp=latest.with_suffix('.tmp');temp.write_text(json.dumps(prior,indent=2)+'\n');temp.replace(latest)
                print('PASS: exact source and test image already tested; report '+prior['report_directory']);return 0
            stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            report=a.report_dir/stamp;report.mkdir(mode=0o700)
            suites=[('catalogue','deploy/media-stack/catalogue'),('cleanup','deploy/media-stack'),('monitor','deploy/media-stack/omv-monitor'),('metadata-worker','deploy/media-stack/metadata-worker/checks'),('privacy','backup')]
            results=[]
            for name,relative in suites:
                folder=root/relative
                if not folder.is_dir():raise RuntimeError('Required test folder missing: '+relative)
                command=['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','1g','--cpus','1','--pids-limit','256','--tmpfs','/tmp:rw,size=384m','--tmpfs','/data:rw,size=64m','--tmpfs','/catalogue:rw,size=64m','-e','PYTHONDONTWRITEBYTECODE=1','-e','PUBLIC_URL=http://127.0.0.1:8092','--mount',f'type=bind,src={root},dst=/project,readonly','--mount',f'type=bind,src={root / "deploy/media-stack"},dst=/opt/media-stack,readonly','--workdir','/project/'+relative,'--entrypoint','python',a.image,'/project/tests/run_suite.py']
                container='media-regression-'+stamp.lower()+'-'+name
                command[2:2]=['--name',container]
                started=time.monotonic()
                with (report/(name+'.log')).open('w') as log:
                    try:rc=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=600).returncode
                    except subprocess.TimeoutExpired:
                        rc=124;log.write('\nGate timeout\n')
                        subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                results.append({'suite':name,'exit_code':rc,'seconds':round(time.monotonic()-started,2)})
                print(name+': '+('PASS' if rc==0 else 'FAIL'),flush=True)
            result={'fingerprint':fingerprint,'image_id':image_id,'time':stamp,'commit':commit,'passed':all(r['exit_code']==0 for r in results),'suites':results,'report_directory':str(report)}
            for file in (report/'result.json',a.report_dir/'latest.json'):
                temp=file.with_suffix('.tmp');temp.write_text(json.dumps(result,indent=2)+'\n');temp.replace(file)
            print('Report: '+str(report/'result.json'))
            return 0 if result['passed'] else 1
if __name__=='__main__':sys.exit(main())
