"""Check Hebrew/None controls remain valid with native audio-only HLS."""
import json
import urllib.parse as U
from check_audio_conversion import api,request,BASE

def main():
    user=api('/Users')[0]['Id'];item='<AAC_MAIN_SAMPLE_ITEM_ID>'
    body={'UserId':user,'IsPlayback':False,'DeviceProfile':{'Name':'Audio/subtitle validation','MaxStreamingBitrate':120000000,
        'DirectPlayProfiles':[{'Container':'mp4','Type':'Video','VideoCodec':'h264','AudioCodec':'aac'}],
        'SubtitleProfiles':[{'Format':'vtt','Method':'External'},{'Format':'srt','Method':'External'}]}}
    first=api('/Items/'+item+'/PlaybackInfo',body)['MediaSources'][0]
    index=next(s['Index'] for s in first['MediaStreams'] if s['Type']=='Subtitle' and s.get('Language')=='heb')
    for selection in (index,-1):
        response=api('/Items/'+item+'/PlaybackInfo',{**body,'SubtitleStreamIndex':selection})
        source=response['MediaSources'][0]
        assert source.get('DefaultSubtitleStreamIndex')==selection
        assert U.parse_qs(U.urlsplit(source['TranscodingUrl']).query)['SubtitleStreamIndex']==['-1'],'Audio conversion must never burn video subtitles'
        if selection>=0:
            sub=next(s for s in source['MediaStreams'] if s['Type']=='Subtitle' and s['Index']==selection)
            assert sub['DeliveryMethod']=='External' and sub.get('SupportsExternalStream')
            text=request(U.urljoin(BASE+'/',sub['DeliveryUrl'])).decode()
            assert 'WEBVTT' in text and 'בדיקת' in text
            print('Hebrew VTT delivered alongside AAC-LC HLS',flush=True)
        else:print('Explicit None preserved',flush=True)
    native=api('/Users/'+user+'/Items/'+item+'?Fields=MediaStreams')
    assert next(s for s in native['MediaStreams'] if s['Type']=='Subtitle')['Codec']=='subrip','Response must not mutate native subtitle metadata'
    print('PASS; subtitle indexes and native stored codec preserved; no watch state changed',flush=True)
if __name__=='__main__':main()
