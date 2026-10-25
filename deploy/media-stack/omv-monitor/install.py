#!/usr/bin/python3
"""Install the local read-only OMV workbench/RPC extension."""
from pathlib import Path
import os
import shutil
import subprocess
if os.geteuid()!=0:raise SystemExit('Run as root')
source=Path(__file__).resolve().parent
if source!=Path('/opt/media-stack/omv-monitor'):raise SystemExit('Restore this directory at /opt/media-stack/omv-monitor first')
for name,target in [
 ('home-media-workers.inc','/usr/share/openmediavault/engined/rpc/home-media-workers.inc'),
 ('component.yaml','/usr/share/openmediavault/workbench/component.d/omv-home-media-workers-page.yaml'),
 ('navigation.yaml','/usr/share/openmediavault/workbench/navigation.d/diagnostics.media-workers.yaml'),
 ('route.yaml','/usr/share/openmediavault/workbench/route.d/diagnostics.media-workers.yaml')]:
 shutil.copyfile(source/name,target)
 os.chmod(target,0o644)
for name in ('media-worker-status.service','media-worker-status.timer'):
 shutil.copyfile(source/name,Path('/etc/systemd/system')/name)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','--now','media-worker-status.timer'],check=True)
for task in ('navigation','route'):subprocess.run(['omv-mkworkbench',task],check=True)
subprocess.run(['systemctl','restart','openmediavault-engined'],check=True)
print('Installed OMV Diagnostics > Media workers')
