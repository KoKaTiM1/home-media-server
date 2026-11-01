#!/usr/bin/env python3
"""Exercise actual Dockerfile permissions, including restrictive build inputs."""
import argparse,runpy,pathlib,shutil,subprocess,tempfile,uuid

def main():
 p=argparse.ArgumentParser();p.add_argument('--repository',type=pathlib.Path,required=True);p.add_argument('--image',default='home-media-tests:1');a=p.parse_args()
 source=a.repository/'deploy/media-stack';tag='home-media-worker-permission-test:'+uuid.uuid4().hex
 with tempfile.TemporaryDirectory(prefix='worker-image-contract-') as t:
  root=pathlib.Path(t);(root/'catalogue').mkdir();(root/'verification-worker').mkdir()
  for f in (source/'catalogue').glob('*.py'):
   shutil.copyfile(f,root/'catalogue'/f.name);(root/'catalogue'/f.name).chmod(0o600)
  for name in ('verification_worker.py','local_verifier.py'):
   shutil.copyfile(source/'verification-worker'/name,root/'verification-worker'/name)
  text=(source/'verification-worker/Dockerfile').read_text();text=text.replace(text.splitlines()[0],'FROM '+a.image,1);(root/'Dockerfile').write_text(text)
  smoke=runpy.run_path(str(source/'verification-worker/package.py'),run_name='worker_package')['IMAGE_SMOKE']
  try:
   subprocess.run(['docker','build','--network','none','-t',tag,str(root)],check=True)
   subprocess.run(['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--entrypoint','python',tag,'-c',smoke],check=True)
  finally:subprocess.run(['docker','image','rm',tag],stdout=subprocess.DEVNULL)
if __name__=='__main__':main()
