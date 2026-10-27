"""Host-only resource gate. Never restarts or kills a playback service."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from verification_jobs import DecodeJobs

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args()
    cfg=json.loads(Path(a.config).read_text());q=DecodeJobs(cfg['queue_path']);q.initialize()
    permitted=False;reason='Memory gate has not been approved';health={}
    try:
        item=json.loads(subprocess.check_output(['docker','inspect','torrserver'],timeout=5))[0]
        pid=item['State']['Pid'];path=Path('/sys/fs/cgroup')/Path('/proc/'+str(pid)+'/cgroup').read_text().strip().split('::')[-1].lstrip('/')
        current=int((path/'memory.current').read_text());limit=int((path/'memory.max').read_text())
        stat=dict(line.split() for line in (path/'memory.stat').read_text().splitlines())
        inactive_file=int(stat.get('inactive_file',0))
        working=max(0,current-inactive_file)
        events=dict(line.split() for line in (path/'memory.events').read_text().splitlines())
        mem=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
        available=int(mem['MemAvailable'].split()[0])*1024
        health={'time':time.time(),'torr_bytes':current,'torr_working_bytes':working,
                'torr_inactive_file_bytes':inactive_file,'torr_limit':limit,
                'available_bytes':available,'oom_kill':int(events.get('oom_kill',0)),'restarts':item['RestartCount']}
        status=q.status();totals=status['totals'];done=totals.get('windows_titles_verified',0)+totals.get('windows_titles_unavailable',0)
        if not cfg.get('requested'):reason='Windows verification has not been enabled'
        elif not cfg.get('memory_gate_passed'):reason='Waiting for viewing-safe memory investigation / parallel pilot'
        elif item['RestartCount']>cfg['baseline_restarts'] or int(events.get('oom_kill',0))>cfg['baseline_oom']:
            reason='TorrServer restart or OOM observed; review required'
        elif not item['State']['Running'] or limit<320*1024*1024:reason='TorrServer memory configuration needs review'
        elif done-cfg.get('pilot_start',0)>=cfg.get('pilot_limit',20) and not cfg.get('continuous'):reason='Bounded pilot complete; review throughput and memory'
        elif available<256*1024*1024:reason='Host memory pressure; Windows verification paused'
        elif working>limit*(.8 if status['enabled'] else .7):reason='TorrServer memory pressure; Windows verification paused'
        else:
            permitted=True
            lanes=max(1,min(2,int(cfg.get('max_windows_jobs',1))))
            reason=f'Parallel verification permitted: one server and up to {lanes} Windows '+('job' if lanes==1 else 'jobs')
    except Exception:reason='Resource status unavailable; Windows verification paused'
    q.set('enabled',permitted);q.set('reason',reason);q.set('health',health)
    q.set('max_windows_jobs',max(1,min(2,int(cfg.get('max_windows_jobs',1)))))
    print(reason)

if __name__=='__main__':main()
