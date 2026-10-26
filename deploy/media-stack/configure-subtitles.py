#!/usr/bin/python3
"""Collect provider credentials locally without printing or logging them."""
import getpass
import json
import os
import urllib.request
import urllib.error
import sqlite3
import subprocess
from pathlib import Path
if os.geteuid() != 0:
    raise SystemExit('Run as root: python3 /opt/media-stack/configure-subtitles.py')
api_key=getpass.getpass('OpenSubtitles API key (hidden): ').strip()
username=input('OpenSubtitles username: ').strip()
password=getpass.getpass('OpenSubtitles password (hidden): ')
if not api_key or not username or not password:
    raise SystemExit('All three fields are required; nothing saved.')
try:
    req=urllib.request.Request('https://api.opensubtitles.com/api/v1/login',
        json.dumps({'username':username,'password':password}).encode(),
        {'Api-Key':api_key,'Content-Type':'application/json','User-Agent':'HomeMediaCatalogue v1.0'})
    with urllib.request.urlopen(req,timeout=15) as response:
        result=json.load(response)
    if not result.get('token'): raise ValueError('No login token returned')
except Exception:
    raise SystemExit('OpenSubtitles login failed; verify account/API key. Existing settings were preserved.')
path=Path('/opt/media-stack/secrets/opensubtitles.json')
os.umask(0o077)
with path.open('w') as target:
    json.dump({'api_key':api_key,'username':username,'password':password},target)
path.chmod(0o600)
database=Path('/srv/dev-disk-by-uuid-bd267f22-f18a-4966-94ae-409a27252905/appdata/catalogue/catalogue.sqlite')
if database.exists():
    connection=sqlite3.connect(database,timeout=30)
    with connection:
        connection.execute("DELETE FROM cache WHERE key='opensubtitles-login' OR key LIKE 'subtitles:%' OR key LIKE 'prepared:%'")
    connection.close()
subprocess.run(['systemctl','start','--no-block','media-catalogue-badges.service'],check=True)
print('Credentials saved and subtitle availability checks started. No stream restart is required.')
