#!/usr/bin/python3
"""Install or remove the isolated phone layout; never edit packaged OMV assets."""
from pathlib import Path
import argparse
import os
import shutil
import subprocess

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--remove',action='store_true')
args=parser.parse_args()
if os.geteuid()!=0:raise SystemExit('Run as root')
source=Path('/opt/media-stack/omv-mobile')
if Path(__file__).resolve().parent!=source:raise SystemExit('Restore this folder to /opt/media-stack/omv-mobile first')
target=Path('/etc/nginx/openmediavault-webgui.d/90-home-media-mobile.conf')
old=target.read_bytes() if target.exists() else None
assets=Path('/usr/local/share/omv-mobile')
if not args.remove:
    assets.mkdir(parents=True,exist_ok=True,mode=0o755)
    for name in ('mobile.css','mobile.js'):
        shutil.copyfile(source/name,assets/name)
        (assets/name).chmod(0o644)
try:
    if args.remove:target.unlink(missing_ok=True)
    else:target.write_bytes((source/'mobile.conf').read_bytes())
    subprocess.run(['nginx','-t'],check=True)
except Exception:
    if old is None:target.unlink(missing_ok=True)
    else:target.write_bytes(old)
    raise
subprocess.run(['systemctl','reload','nginx'],check=True)
print('Phone layout '+('removed' if args.remove else 'installed')+'; desktop layout unchanged')
