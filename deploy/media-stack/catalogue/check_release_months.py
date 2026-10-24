"""Read-only integration checks for real release dates, month search and sorting."""
import json
from pathlib import Path
import time
import urllib.parse as U
import urllib.request as R

KEY=Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip()
BASE='http://<SERVER_LAN_IP>:8096'


def api(path,**query):
    request=R.Request(BASE+path+('?' + U.urlencode(query) if query else ''),headers={'X-Emby-Token':KEY})
    with R.urlopen(request,timeout=45) as response:return json.load(response)


for attempt in range(30):
    try:api('/Sessions');break
    except Exception:
        if attempt==29:raise
        time.sleep(2)
root='<ONLINE_MOVIES_LIBRARY_ID>'
common={'ParentId':root,'IncludeItemTypes':'Movie','Years':'2026','Limit':100,'SortOrder':'Descending'}
page=api('/Items',**common,SortBy='PremiereDate')
assert len(page['Items'])==100
dates=[i.get('PremiereDate','')[:10] for i in page['Items']]
assert all(dates) and dates==sorted(dates,reverse=True)
default=api('/Items',**common,SortBy='ProductionYear')
assert [i['Id'] for i in default['Items'][:10]]==[i['Id'] for i in page['Items'][:10]]
newest=api('/Items',**common,SortBy='DateCreated')
assert [i['Id'] for i in newest['Items'][:10]]==[i['Id'] for i in page['Items'][:10]]
print('Default/newest/year order uses actual newest release dates:',dates[:5],flush=True)
movie_ids=set()
for kind in ['Movie','Series']:
    page=api('/Items',SearchTerm='2026-07',IncludeItemTypes=kind,Recursive='true',Limit=100)
    assert page['Items'] and all(i['PremiereDate'].startswith('2026-07') for i in page['Items'])
    print(kind,'July2026',page['TotalRecordCount'],'verified month dates',flush=True)
    if kind=='Movie':movie_ids={i['Id'] for i in page['Items']}
page=api('/Items',SearchTerm='2026-07..2026-08',IncludeItemTypes='Movie',Recursive='true',Limit=100)
assert page['Items'] and all(i['PremiereDate'][:7] in ['2026-07','2026-08'] for i in page['Items'])
print('Inclusive month range July-August2026:',page['TotalRecordCount'],flush=True)
hints=api('/Search/Hints',SearchTerm='2026-07',IncludeItemTypes='Movie',Limit=100)['SearchHints']
assert hints and all(i['Id'] in movie_ids for i in hints)
print('Native TV month search hints:',len(hints),'valid results; native result limits remain',flush=True)
print('PASS',flush=True)
