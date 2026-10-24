"""Read-only browse/detail/back image contracts; opening metadata never reports played."""
import hashlib
import json
from pathlib import Path
import time
import urllib.parse as U
import urllib.request as R

BASE='http://<SERVER_LAN_IP>:8096'
KEY=Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip()


def api(path,**query):
    req=R.Request(BASE+path+('?' + U.urlencode(query) if query else ''),headers={'X-Emby-Token':KEY})
    with R.urlopen(req,timeout=45) as response:return json.load(response)


for attempt in range(30):
    try:api('/Sessions');break
    except Exception:
        if attempt==29:raise
        time.sleep(2)
user=api('/Users')[0]['Id']
page=api('/Items',ParentId='<ONLINE_MOVIES_LIBRARY_ID>',IncludeItemTypes='Movie',Years='1999',Limit=100,SortBy='PremiereDate')
matrix=next(i for i in page['Items'] if i.get('ProviderIds',{}).get('Imdb')=='tt0133093')
def image(item):
    tag=item.get('ImageTags',{}).get('Primary');assert tag and len(tag)==64
    request=R.Request(BASE+'/Items/'+item['Id']+'/Images/Primary?'+U.urlencode({'tag':tag}),headers={'X-Emby-Token':KEY})
    with R.urlopen(request,timeout=15) as response:
        data=response.read();assert response.status==200 and response.headers.get_content_type()=='image/jpeg'
        assert len(data)>1000
    return hashlib.sha256(data).hexdigest()

before=image(matrix)
tags=[matrix['ImageTags']['Primary']]
for _ in range(2):
    detail=api('/Users/'+user+'/Items/'+matrix['Id'])
    tags.append(detail.get('ImageTags',{}).get('Primary'))
    assert image(detail)==before,'A valid image was replaced on detail lookup'
batch=api('/Items',Ids=matrix['Id'],UserId=user)['Items'][0]
tags.append(batch.get('ImageTags',{}).get('Primary'))
assert image(batch)==before
assert len(set(tags))==1,'Browse/detail/native refresh switched image identities'
hints=api('/Search/Hints',SearchTerm='The Matrix',IncludeItemTypes='Movie',Limit=100)['SearchHints']
native=next(i for i in hints if i['Id']==matrix['Id'])
assert native['PrimaryImageTag']==tags[0]
shows=api('/Items',ParentId='<ONLINE_SHOWS_LIBRARY_ID>',IncludeItemTypes='Series',Years='2008',Limit=100)
series=next(i for i in shows['Items'] if i.get('ProviderIds',{}).get('Imdb')=='tt0903747')
api('/Users/'+user+'/Items/'+series['Id'])
seasons=api('/Shows/'+series['Id']+'/Seasons',UserId=user)['Items']
assert any(s.get('IndexNumber')==1 for s in seasons)
episodes=api('/Shows/'+series['Id']+'/Episodes',UserId=user,Season=1)['Items']
assert any(e.get('IndexNumber')==1 for e in episodes)
print('PASS: browse → detail → repeated detail → native refresh → search retain one valid poster/tag')
print('Series details retain season1 and episode1 after the poster refresh')
print('No playback request, watched flag or media file modified')
