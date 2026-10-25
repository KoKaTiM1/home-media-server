"""Compare online-library unwatched badges with native Jellyfin series data.

Read-only: does not play anything or change watched state.
"""
import json
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
started=time.monotonic()
online=[]
for extra in ({},{'Years':'2008'}):
    online.extend(api('/Items',ParentId='<ONLINE_SHOWS_LIBRARY_ID>',
                      IncludeItemTypes='Series',UserId=user,Limit=20,**extra)['Items'])
online=list({i['Id']:i for i in online}.values())
assert online,'No ready series to check'
native=api('/Items',Ids=','.join(i['Id'] for i in online),UserId=user,
           Fields='ChildCount,RecursiveItemCount',EnableUserData='true')['Items']
indexed={i['Id']:i for i in native}
checked=0
for preview in online:
    item=indexed.get(preview['Id'])
    if item is None:continue
    expected=item.get('UserData') or {}
    actual=preview.get('UserData') or {}
    if 'UnplayedItemCount' not in expected:continue
    for field in ('UnplayedItemCount','Played','PlayCount','PlaybackPositionTicks','IsFavorite','LastPlayedDate'):
        assert actual.get(field)==expected.get(field),(preview['Name'],field,actual.get(field),expected.get(field))
    assert preview.get('RecursiveItemCount')==item.get('RecursiveItemCount'),preview['Name']
    checked+=1
    print('PASS:',preview['Name'],'unwatched episodes:',actual['UnplayedItemCount'])
assert checked,'No native indexed shows with count data found'
print('PASS:',checked,'indexed shows match native counts and watched/favorite state;',
      len(online)-checked,'previews have no native count yet;',round(time.monotonic()-started,2),'seconds')
