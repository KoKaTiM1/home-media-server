"""Live TV API contracts; temporarily simulates an empty listing, never watch state."""
import json
from pathlib import Path
import sqlite3
import time
import urllib.parse as U
import urllib.request as R

BASE='http://<SERVER_LAN_IP>:8096'
KEY=Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip()
DB=Path('/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>/appdata/catalogue/catalogue.sqlite')


def api(path,data=None):
    request=R.Request(BASE+path,headers={'X-Emby-Token':KEY,'Content-Type':'application/json'},
        data=None if data is None else json.dumps(data).encode())
    with R.urlopen(request,timeout=60) as response:return json.load(response)


def main():
    for attempt in range(30):
        try:
            sessions=api('/Sessions');break
        except Exception:
            if attempt==29:raise
            time.sleep(2)
    assert not any(s.get('NowPlayingItem') for s in sessions),'Active owner playback; defer check'
    for parent,kind in [('<LEGACY_MOVIES_LIBRARY_ID>','Movie'),('<LEGACY_SHOWS_LIBRARY_ID>','Series')]:
        page=api('/Items?'+U.urlencode({'ParentId':parent,'IncludeItemTypes':kind,'Limit':5}))
        assert len(page['Items'])==5
    print('Old library IDs: both browse successfully',flush=True)
    page=api('/Items?'+U.urlencode({'ParentId':'<ONLINE_MOVIES_LIBRARY_ID>','IncludeItemTypes':'Movie','Years':'1999','Limit':20,'SortBy':'ProductionYear'}))
    matrix=next(i for i in page['Items'] if i.get('ProviderIds',{}).get('Imdb')=='tt0133093')
    intros=api('/Items/'+matrix['Id']+'/Intros')
    assert isinstance(intros,dict) and intros['Items']==[] and intros['TotalRecordCount']==0
    print('Intro response: correct QueryResult object',flush=True)
    user=api('/Users')[0]['Id']
    api('/Users/'+user+'/Items/'+matrix['Id'])
    cachekeys=[prefix+'movie:tt0133093' for prefix in ['streams:','resolved:','prepared:','source-status:']]
    with sqlite3.connect(DB) as conn:
        original=conn.execute('SELECT key,value,expires FROM cache WHERE key IN (?,?,?,?)',cachekeys).fetchall()
        conn.execute('DELETE FROM cache WHERE key IN (?,?,?,?)',cachekeys)
        conn.execute('INSERT INTO cache VALUES (?,?,?)',(cachekeys[0],'[]',time.time()+120))
    try:
        failed=api('/Items/'+matrix['Id']+'/PlaybackInfo',{'UserId':user,'IsPlayback':True})
        assert failed.get('ErrorCode')=='NoCompatibleStream' and failed.get('MediaSources')==[],failed.get('ErrorCode')
        print('Empty torrent listing: explicit NoCompatibleStream (prevents TV null-source crash)',flush=True)
        with sqlite3.connect(DB) as conn:
            conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',(cachekeys[0],json.dumps([{'infoHash':'0'*40}]),time.time()+120))
            conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',(cachekeys[2],json.dumps({'MediaStreams':[]}),time.time()+120))
        failed=api('/Items/'+matrix['Id']+'/PlaybackInfo',{'UserId':user,'IsPlayback':True})
        assert failed.get('ErrorCode')=='NoCompatibleStream' and failed.get('MediaSources')==[]
        print('Listed source with failed preparation: explicit NoCompatibleStream',flush=True)
    finally:
        with sqlite3.connect(DB) as conn:
            conn.execute('DELETE FROM cache WHERE key IN (?,?,?,?)',cachekeys)
            conn.executemany('INSERT INTO cache VALUES (?,?,?)',original)
    source=api('/Items/<OPEN_FILM_ITEM_ID>/PlaybackInfo',{'UserId':user,'IsPlayback':True})
    assert source.get('MediaSources') and not source.get('ErrorCode')
    request=R.Request(source['MediaSources'][0]['Path'],headers={'Range':'bytes=1048576-1049599'})
    with R.urlopen(request,timeout=15) as response:
        assert response.status==206 and len(response.read())==1024
    print('Saved Big Buck Bunny: healthy PlaybackInfo and HTTP206 mid-file bytes',flush=True)
    with R.urlopen('http://<SERVER_LAN_IP>:8092/poster/movie/tt11423766.jpg?style=3',timeout=15) as response:
        assert response.status==200 and response.headers.get_content_type()=='image/jpeg' and len(response.read())>1000
    print('Missing provider artwork: usable JPEG fallback, no 404',flush=True)
    for sort in ['SortName','CommunityRating']:
        started=time.monotonic()
        page=api('/Items?'+U.urlencode({'ParentId':'<ONLINE_MOVIES_LIBRARY_ID>','IncludeItemTypes':'Movie','Limit':100,'SortBy':sort,'SortOrder':'Descending'}))
        assert len(page['Items'])==100
        print('100-item browse',sort,round(time.monotonic()-started,2),'seconds',flush=True)
    print('PASS; no watch state or media files modified',flush=True)


if __name__=='__main__':main()
