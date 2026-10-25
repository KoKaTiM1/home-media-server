#!/usr/bin/python3
"""Configure bounded native HLS buffering through Jellyfin's supported API."""
import argparse
import json
from pathlib import Path
import urllib.request as R
parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
key=Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip()
base='http://<SERVER_LAN_IP>:8096'
def api(path,data=None):
 req=R.Request(base+path,headers={'X-Emby-Token':key,'Content-Type':'application/json'},data=None if data is None else json.dumps(data).encode())
 with R.urlopen(req,timeout=20) as response:
  body=response.read();return json.loads(body) if body else None
settings=api('/System/Configuration/encoding')
desired={'EnableThrottling':True,'ThrottleDelaySeconds':60,'EnableSegmentDeletion':True,'SegmentKeepSeconds':90}
if not args.check:
 if any(s.get('NowPlayingItem') for s in api('/Sessions')):raise SystemExit('Playback active; run configuration after playback ends')
 settings.update(desired);api('/System/Configuration/encoding',settings)
print(json.dumps({k:settings.get(k) for k in desired}))
