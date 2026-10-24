"""Exercise an explicit server save with the publisher's Big Buck Bunny torrent.

Run as root, while nobody is watching. Leaves the completed open-film download
in the managed media folder for the owner to test; normal watched cleanup applies.
"""
import json
from pathlib import Path
import sqlite3
import time
import urllib.parse as U
import urllib.request as R

BASE='http://<SERVER_LAN_IP>:8096'
KEY='movie:tt1254207'
ITEM='<OPEN_FILM_ITEM_ID>'
STATE=Path('/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>/appdata/catalogue/catalogue.sqlite')
HEADERS={'X-Emby-Token':Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip(),
         'Content-Type':'application/json'}


def api(path,data=None):
    with R.urlopen(R.Request(BASE+path,headers=HEADERS,data=None if data is None else json.dumps(data).encode()),timeout=90) as response:
        raw=response.read();return json.loads(raw) if raw else None


def record():
    conn=sqlite3.connect('file:'+str(STATE)+'?mode=ro',uri=True)
    try:row=conn.execute('SELECT value FROM downloads WHERE key=?',(KEY,)).fetchone()
    finally:conn.close()
    return json.loads(row[0]) if row else None


def main():
    assert not any(s.get('NowPlayingItem') for s in api('/Sessions')), 'Owner playback active; defer check'
    user=api('/Users')[0]['Id'];before=record()
    item=api('/Users/'+user+'/Items/'+ITEM)
    sources=item['MediaSources'];print('Source choices:',[s['Name'] for s in sources],flush=True)
    if before and before.get('state')=='ready':print('Existing managed demonstration download is ready');return
    save=next(s for s in sources if 'Download to server' in s['Name'])
    body={'UserId':user,'IsPlayback':True}
    info=api('/Items/'+ITEM+'/PlaybackInfo',body)
    assert info.get('MediaSources')
    assert record()==before, 'Ordinary Play unexpectedly queued a full download'
    print('Ordinary playback did not queue a download',flush=True)
    info=api('/Items/'+ITEM+'/PlaybackInfo',{**body,'MediaSourceId':save['Id']})
    assert info.get('MediaSources')
    queued=record();assert queued and queued['state'] in ('queued','requesting','ready')
    print('Explicit Save choice queued the requested movie',flush=True)
    deadline=time.monotonic()+600
    while time.monotonic()<deadline:
        saved=record()
        if saved and saved['state']=='ready':break
        print('Waiting for complete download and media move:',saved.get('state') if saved else None,flush=True)
        time.sleep(15)
    else:raise RuntimeError('Open-film download not completed within test deadline')
    print('Saved file:',saved['path'],'bytes',saved['size'],flush=True)
    # Use the same original item/source route; its bridge now serves the local file.
    url=BASE+'/Videos/'+ITEM+'/stream?'+U.urlencode({'MediaSourceId':save['Id'],'Static':'true'})
    for start in [0,1048576]:
        with R.urlopen(R.Request(url,headers={**HEADERS,'Range':f'bytes={start}-{start+65535}'}),timeout=25) as response:
            chunk=response.read(65536)
            assert response.status==206 and len(chunk)==65536
        print('Local-file range through original Jellyfin item:',start,'HTTP 206',flush=True)
    print('Managed open-film download retained for owner playback; no watch state changed',flush=True)


if __name__=='__main__':main()
