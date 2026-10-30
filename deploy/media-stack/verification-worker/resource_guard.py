"""Host-only resource gate. Never restarts or kills a playback service."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from verification_jobs import DecodeJobs

def sync_producers(queue,workers):
    slots=min(32,workers*2)
    if queue.setting_read('producer_slots',0)==slots:return
    wanted=['media-remote-lookahead@'+str(n)+'.timer' for n in range(4,slots+1)]
    retired=['media-remote-lookahead@'+str(n)+'.timer' for n in range(max(4,slots+1),33)]
    for action,units in (('enable',wanted),('disable',retired)):
        if units:subprocess.run(['systemctl',action,'--now',*units],check=True,timeout=45,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    queue.set('producer_slots',slots)

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args()
    cfg=json.loads(Path(a.config).read_text());q=DecodeJobs(cfg['queue_path']);q.initialize()
    permitted=False;local_permitted=False;russian_extra=False
    reason='Memory gate has not been approved';local_reason=reason;health={}
    try:
        def cgroup(name):
            item=json.loads(subprocess.check_output(['docker','inspect',name],timeout=5))[0]
            pid=item['State']['Pid'];path=Path('/sys/fs/cgroup')/Path('/proc/'+str(pid)+'/cgroup').read_text().strip().split('::')[-1].lstrip('/')
            current=int((path/'memory.current').read_text());limit=int((path/'memory.max').read_text())
            stat=dict(line.split() for line in (path/'memory.stat').read_text().splitlines())
            events=dict(line.split() for line in (path/'memory.events').read_text().splitlines())
            inactive_file=int(stat.get('inactive_file',0))
            return item,current,limit,inactive_file,max(0,current-inactive_file),events
        item,current,limit,inactive_file,working,events=cgroup('torrserver')
        bridge_item,bridge_current,bridge_limit,bridge_inactive_file,bridge_working,bridge_events=cgroup('catalogue-bridge')
        mem=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
        available=int(mem['MemAvailable'].split()[0])*1024
        health={'time':time.time(),'torr_bytes':current,'torr_working_bytes':working,
                'torr_inactive_file_bytes':inactive_file,'torr_limit':limit,
                'available_bytes':available,'oom_kill':int(events.get('oom_kill',0)),'restarts':item['RestartCount'],
                'bridge_bytes':bridge_current,'bridge_working_bytes':bridge_working,'bridge_limit':bridge_limit,
                'bridge_oom_kill':int(bridge_events.get('oom_kill',0))}
        status=q.status();totals=status['totals'];done=totals.get('windows_titles_verified',0)+totals.get('windows_titles_unavailable',0)
        bridge_oom_baseline=int(cfg.get('baseline_bridge_oom',health['bridge_oom_kill']))
        if not cfg.get('requested'):reason='Windows verification has not been enabled'
        elif not cfg.get('memory_gate_passed'):reason='Waiting for viewing-safe memory investigation / parallel pilot'
        elif item['RestartCount']>cfg['baseline_restarts'] or int(events.get('oom_kill',0))>cfg['baseline_oom']:
            reason='TorrServer restart or OOM observed; review required'
        elif health['bridge_oom_kill']>bridge_oom_baseline:reason='Catalogue bridge OOM observed; review required'
        elif not item['State']['Running'] or limit<320*1024*1024:reason='TorrServer memory configuration needs review'
        elif done-cfg.get('pilot_start',0)>=cfg.get('pilot_limit',20) and not cfg.get('continuous'):reason='Bounded pilot complete; review throughput and memory'
        elif available<256*1024*1024:reason='Host memory pressure; Windows verification paused'
        elif working>limit*(.8 if status['enabled'] else .7):reason='TorrServer memory pressure; Windows verification paused'
        elif bridge_limit<=0 or bridge_working>bridge_limit*(.8 if status['enabled'] else .7):reason='Catalogue bridge memory pressure; Windows verification paused'
        else:
            permitted=True
            lanes=max(1,min(16,int(q.setting_read('requested_windows_jobs',cfg.get('max_windows_jobs',1)))))
            reason=f'Parallel verification permitted: one server and up to {lanes} Windows '+('job' if lanes==1 else 'jobs')
            # A second Debian torrent task gets stricter headroom than the
            # established single server verifier. If these limits fail, the
            # primary verifier may run the Russian check serially while the
            # Windows fleet is online, then continue ordinary V6 work.
            russian_extra=(available>=2*1024**3 and working<=limit*.65
                and bridge_working<=bridge_limit*.65 and bridge_limit>0)
        # Complete-title Windows jobs do not read through Debian TorrServer.
        # Keep the host, OOM and bridge protections, but do not inherit the old
        # server-TorrServer working-memory pause.
        if not cfg.get('requested'):local_reason='Windows verification has not been enabled'
        elif not cfg.get('memory_gate_passed'):local_reason='Waiting for viewing-safe memory investigation / parallel pilot'
        elif health['bridge_oom_kill']>bridge_oom_baseline:local_reason='Catalogue bridge OOM observed; review required'
        elif available<256*1024*1024:local_reason='Host memory pressure; Windows-local verification paused'
        elif bridge_limit<=0 or bridge_working>bridge_limit*(.8 if status.get('local_torrent_permitted') else .7):local_reason='Catalogue bridge memory pressure; Windows-local verification paused'
        else:
            local_permitted=True
            local_reason='Windows-local torrent verification permitted; Debian fallback remains resource gated'
        sync_producers(q,max(1,min(16,int(q.setting_read('requested_windows_jobs',cfg.get('max_windows_jobs',1))))))
    except Exception:
        reason='Resource status unavailable; Windows verification paused'
        local_reason='Resource status unavailable; Windows-local verification paused'
    q.set('enabled',permitted);q.set('reason',reason);q.set('health',health)
    q.set('local_torrent_permitted',local_permitted);q.set('local_torrent_reason',local_reason)
    q.set('max_windows_jobs',max(1,min(16,int(q.setting_read('requested_windows_jobs',cfg.get('max_windows_jobs',1))))))
    q.set('prefetch_per_lane',2)
    q.set('russian_serial_permitted',permitted)
    q.set('russian_extra_permitted',bool(permitted and russian_extra))
    print(reason+'; '+local_reason)

if __name__=='__main__':main()
