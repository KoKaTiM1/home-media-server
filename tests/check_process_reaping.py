"""Repeated cancelled decoder groups must not leave orphan zombies in a worker."""
import argparse,base64,json,pathlib,subprocess,uuid
CODE='''import subprocess,os,signal,time,json,pathlib
for i in range(24):
 p=subprocess.Popen(['python','-c','import subprocess,time;subprocess.Popen(["sleep","60"]);time.sleep(60)'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 time.sleep(.12);os.killpg(p.pid,signal.SIGKILL);p.wait();time.sleep(.08)
time.sleep(.5)
z=0
for f in pathlib.Path('/proc').glob('[0-9]*/status'):
 try:z+=any(l.startswith('State:') and 'Z' in l for l in f.read_text().splitlines())
 except OSError:pass
print(json.dumps({'cycles':24,'zombies':z}))
'''
def main():
 p=argparse.ArgumentParser();p.add_argument('--image',default='home-media-tests:1');a=p.parse_args()
 root=pathlib.Path(__file__).resolve().parents[1]
 launcher=(root/'deploy/media-stack/verification-worker/worker.ps1').read_text()
 line=next(l for l in launcher.splitlines() if 'run --detach' in l and "home-media.component=verification-worker'" in l)
 name='media-reaping-test-'+uuid.uuid4().hex[:12]
 cmd=['docker','run','--rm','--name',name,'--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','96m','--pids-limit','16']
 if '--init' in line.split():cmd+=['--init']
 cmd+=['--entrypoint','python',a.image,'-c',CODE]
 try:
  r=subprocess.run(cmd,capture_output=True,text=True,timeout=30,check=True)
  result=json.loads(r.stdout.strip());print(result,flush=True)
  if result['zombies']!=0:raise RuntimeError('Cancelled decoder children were not reaped')
 finally:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
if __name__=='__main__':main()
