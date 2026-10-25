"""Compare online-library unwatched badges with native Jellyfin series data.

Read-only: does not play anything or change watched state.
"""
import json
import argparse
import time
import urllib.parse as U
import urllib.request as R
from pathlib import Path

base='http://<SERVER_LAN_IP>:8096'
key=Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip()

def api(path,**query):
    request=R.Request(base+path+('?' + U.urlencode(query) if query else ''),headers={'X-Emby-Token':key})
    with R.urlopen(request,timeout=45) as response:return json.load(response)

user=api('/Users')[0]['Id']
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--all',action='store_true',help='Check every currently visible online show')
args=parser.parse_args()
started=time.monotonic()
online=[]
if args.all:
    offset=0;total=1
    while offset<total:
        page=api('/Items',ParentId='<ONLINE_SHOWS_LIBRARY_ID>',
                 IncludeItemTypes='Series',UserId=user,Limit=100,StartIndex=offset)
        online.extend(page['Items']);total=page['TotalRecordCount'];offset+=100
else:
    for extra in ({},{'Years':'2008'}):
        online.extend(api('/Items',ParentId='<ONLINE_SHOWS_LIBRARY_ID>',
                          IncludeItemTypes='Series',UserId=user,Limit=20,**extra)['Items'])
online=list({i['Id']:i for i in online}.values())
assert online,'No ready series to check'
native=[]
for start in range(0,len(online),100):
    native.extend(api('/Items',Ids=','.join(i['Id'] for i in online[start:start+100]),UserId=user,
                      Fields='ChildCount,RecursiveItemCount',EnableUserData='true')['Items'])
indexed={i['Id']:i for i in native}
metadata={};offset=0;total=1
while offset<total:
    with R.urlopen('http://<SERVER_LAN_IP>:8092/browse/series?'+U.urlencode({'offset':offset,'limit':100}),timeout=30) as response:
        page=json.load(response)
    metadata.update({i['id']:i.get('episodeCount') for i in page['metas']})
    total=page['total'];offset+=100
checked=0;previews=0;unknown=[]
for preview in online:
    item=indexed.get(preview['Id'])
    expected=(item or {}).get('UserData') or {}
    actual=preview.get('UserData') or {}
    if 'UnplayedItemCount' not in expected:
        imdb=preview.get('ProviderIds',{}).get('Imdb')
        known=metadata.get(imdb)
        if known is not None:
            assert actual.get('UnplayedItemCount')==known,(preview['Name'],'preview count mismatch')
            previews+=1
        else:unknown.append(preview['Name'])
        continue
    for field in ('PlayCount','PlaybackPositionTicks','IsFavorite','LastPlayedDate'):
        assert actual.get(field)==expected.get(field),(preview['Name'],field,actual.get(field),expected.get(field))
    imdb=preview.get('ProviderIds',{}).get('Imdb')
    known=metadata.get(imdb)
    native_total=item.get('RecursiveItemCount')
    if known and native_total is not None:
        total=max(native_total,known)
        watched=max(0,min(native_total,native_total-expected['UnplayedItemCount']))
        assert actual.get('UnplayedItemCount')==total-watched,preview['Name']
        assert actual.get('Played')==(watched==total),preview['Name']
        assert preview.get('RecursiveItemCount')==total,preview['Name']
    elif known:
        assert actual.get('Played') is False,preview['Name']
        assert actual.get('UnplayedItemCount')>=known,preview['Name']
    else:
        assert actual.get('UnplayedItemCount')==expected.get('UnplayedItemCount'),preview['Name']
    checked+=1
    print('PASS:',preview['Name'],'unwatched episodes:',actual['UnplayedItemCount'])
assert checked,'No native indexed shows with count data found'
print('PASS:',checked,'indexed shows preserve native user data and include full released-episode totals;',
      previews,'previews match saved released-episode counts;',len(unknown),'counts unknown;',
      round(time.monotonic()-started,2),'seconds')
assert not unknown,unknown
