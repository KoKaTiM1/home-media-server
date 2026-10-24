"""Live read-only catalogue/search/image checks; no playback or user-state changes."""
import json
from pathlib import Path
import time
import urllib.parse as U
import urllib.request as R

BASE='http://<SERVER_LAN_IP>:8096'
KEY=Path('/opt/media-stack/secrets/jellyfin_api_key').read_text().strip()
def request(path):
    return R.urlopen(R.Request(BASE+path,headers={'X-Emby-Token':KEY}),timeout=45)
def get(path,**params):
    with request(path+('?' + U.urlencode(params) if params else '')) as response:return json.load(response)
for attempt in range(20):
    try:
        folders=get('/Library/VirtualFolders')
        if all(f.get('ItemId') for f in folders if '/catalogue/movies' in f.get('Locations',[]) or '/catalogue/shows' in f.get('Locations',[])):break
    except Exception:pass
    time.sleep(2)
movies=next(f for f in folders if '/catalogue/movies' in f.get('Locations',[]))
shows=next(f for f in folders if '/catalogue/shows' in f.get('Locations',[]))
print('Libraries',movies['Name'],shows['Name'],flush=True)
assert movies['Name']=='סרטים צפייה ישירה' and shows['Name']=='סדרות צפייה ישירה'
common={'ParentId':movies['ItemId'],'IncludeItemTypes':'Movie','Recursive':'true','Limit':20,'Years':'2024,2025','SortBy':'CommunityRating'}
high=get('/Items',**common,SortOrder='Descending')
low=get('/Items',**common,SortOrder='Ascending')
for result,reverse in [(high,True),(low,False)]:
    items=result['Items'];assert len(items)==20
    years=[i['ProductionYear'] for i in items];assert years==sorted(years,reverse=True) and set(years)<={2024,2025}
    ratings=[i.get('CommunityRating') for i in items];assert all(r is not None for r in ratings)
    assert ratings==sorted(ratings,reverse=reverse)
print('Score sorting',[(i['Name'],i.get('CommunityRating')) for i in high['Items'][:2]],[(i['Name'],i.get('CommunityRating')) for i in low['Items'][:2]],flush=True)
assert high['Items'][0]['Id']!=low['Items'][0]['Id']
page=get('/Items',**{**common,'Limit':100},SortOrder='Descending',StartIndex=1000)
assert len(page['Items'])==100 and page['TotalRecordCount']>1100
print('Beyond 1000',page['StartIndex'],len(page['Items']),page['TotalRecordCount'],flush=True)
for kind in ['Movie','Series']:
    page=get('/Items',SearchTerm='1994-2000',IncludeItemTypes=kind,Recursive='true',Limit=100,StartIndex=100)
    assert len(page['Items'])==100 and all(1994<=i['ProductionYear']<=2000 for i in page['Items'])
    print('Year-range global search',kind,page['TotalRecordCount'],flush=True)
result=get('/Search/Hints',SearchTerm='ספיידרמן',IncludeItemTypes='Movie',Limit=100)
hints=result.get('SearchHints',[])
print('Hebrew search',len(hints),[(i.get('Name'),i.get('ProductionYear')) for i in hints[:10]],flush=True)
assert any(i.get('ProductionYear')==2002 and 'Spider-Man' in i.get('Name','') for i in hints)
assert any(i.get('ProductionYear')==2021 and 'No Way Home' in i.get('Name','') for i in hints)
posters=get('/Items',ParentId=movies['ItemId'],IncludeItemTypes='Movie',Years='1999',Limit=20,SortBy='ProductionYear')['Items']
matrix=next(i for i in posters if i.get('ProviderIds',{}).get('Imdb')=='tt0133093')
assert len(matrix['ImageTags']['Primary'])==64
with request('/Items/'+matrix['Id']+'/Images/Primary') as response:
    image=response.read();assert response.headers['Content-Type'].startswith('image/') and len(image)>1000
    import subprocess
    subprocess.run(['docker','exec','-i','catalogue-bridge','python','-c',
        'import io,sys;from PIL import Image;p=Image.open(io.BytesIO(sys.stdin.buffer.read())).getpixel((12,20));assert p[1]>p[0]*2 and p[1]>p[2]'],input=image,check=True)
print('Native movie poster delivered',len(image),'bytes',flush=True)
print('PASS',flush=True)
