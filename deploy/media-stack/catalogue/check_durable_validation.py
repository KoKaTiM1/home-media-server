"""Read-only checks of live durable catalogue and saved media delivery."""
import json
from pathlib import Path
import sqlite3
import time
import urllib.parse as U
import urllib.request as R

ROOT=Path('/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>')
KEY=Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip()
def get(base,path,data=None):
    request=R.Request(base+path,headers={'X-Emby-Token':KEY,'Content-Type':'application/json'},data=None if data is None else json.dumps(data).encode())
    with R.urlopen(request,timeout=35) as response:return json.load(response)
def bridge(path):return get('http://<SERVER_LAN_IP>:8092',path)
def jf(path,data=None):return get('http://<SERVER_LAN_IP>:8096',path,data)
def main():
    for _ in range(20):
        try:status=bridge('/validation');break
        except Exception:time.sleep(2)
    else:raise RuntimeError('Bridge unavailable')
    assert sum(status['states'].values())>1000000,status
    print('Persistent queue exceeds one million titles:',status['states'],flush=True)
    ready=set(bridge('/ready')['titles'])
    with sqlite3.connect('file:'+str(ROOT/'appdata/catalogue/catalogue.sqlite')+'?mode=ro',uri=True) as conn:
        for imdb in ('tt16155872','tt14330326','tt14487192'):
            row=conn.execute('SELECT state,attempts FROM validation WHERE key=?',('movie:'+imdb,)).fetchone()
            assert row is not None
            if row[0]!='verified':
                assert 'movie:'+imdb not in ready
                assert bridge('/stream/movie/'+imdb+'.json')['streams']==[]
            print('Reported title',imdb,'retained:',row,flush=True)
    for kind in ('movie','series'):
        page=bridge('/browse/'+kind+'?limit=100')
        assert all(kind+':'+m['id'] in ready for m in page['metas'])
        print(kind,'browse ready-only:',len(page['metas']),'total',page['total'],flush=True)
        root='<ONLINE_MOVIES_LIBRARY_ID>' if kind=='movie' else '<ONLINE_SHOWS_LIBRARY_ID>'
        native=jf('/Items?'+U.urlencode({'ParentId':root,'IncludeItemTypes':'Movie' if kind=='movie' else 'Series','Limit':100,'Fields':'ProviderIds,Path'}))
        assert all(kind+':'+i['ProviderIds']['Imdb'] in ready for i in native['Items'])
        print('Native',kind,'items ready-only:',len(native['Items']),flush=True)
    for query,failed in [('King of Dope','<KING_OF_DOPE_ITEM_ID>'),("Jerusalem '67",'<JERUSALEM_ITEM_ID>')]:
        hints=jf('/Search/Hints?'+U.urlencode({'searchTerm':query,'limit':25}))
        assert failed not in [h['Id'].replace('-','') for h in hints['SearchHints']]
    print('Native search excludes reported unavailable titles',flush=True)
    user=jf('/Users')[0]['Id']
    source=jf('/Items/<OPEN_FILM_ITEM_ID>/PlaybackInfo',{'UserId':user,'IsPlayback':True})
    assert source.get('MediaSources') and not source.get('ErrorCode')
    request=R.Request(source['MediaSources'][0]['Path'],headers={'Range':'bytes=1048576-1049599'})
    with R.urlopen(request,timeout=15) as response:assert response.status==206 and len(response.read())==1024
    print('Saved movie: PlaybackInfo and HTTP206 range delivery PASS',flush=True)
    print('PASS; no watch state or media files modified',flush=True)
if __name__=='__main__':main()
