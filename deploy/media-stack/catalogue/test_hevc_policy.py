"""Physical TV acceptance is limited to HEVC Main 8-bit SDR; verify admission and recipes."""
import copy,json,subprocess,tempfile,unittest
from pathlib import Path
from verification_media import validate_video,execute,validate_result

class HEVCPolicyTests(unittest.TestCase):
    def test_probe_policy_rejects_unaccepted_hevc_variants(self):
        video={'codec_name':'hevc','profile':'Main','pix_fmt':'yuv420p','width':1920,'height':1080}
        validate_video(video)
        from verification_title import audio_selection
        selected,_,_=audio_selection({'streams':[{**video,'codec_type':'video','index':0}]})
        self.assertEqual(selected['codec_name'],'hevc')
        for change in ({'profile':'Main 10'},{'pix_fmt':'yuv420p10le'}, {'width':3840},
                       {'height':2160},{'width':None},{'color_transfer':'smpte2084'},
                       {'color_transfer':'arib-std-b67'},
                       {'side_data_list':[{'side_data_type':'DOVI configuration record'}]},
                       {'side_data_list':[{'side_data_type':'Mastering display metadata'}]},
                       {'codec_name':'av1'}):
            with self.subTest(change=change),self.assertRaises(ValueError):validate_video({**video,**change})
    def test_real_hevc_two_samples_and_aac_conversion_preserve_video(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'clip.mkv'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=s=160x90:r=12:d=12','-f','lavfi','-i','sine=frequency=440:duration=12','-c:v','libx265','-preset','ultrafast','-x265-params','pools=1:frame-threads=1:log-level=error','-pix_fmt','yuv420p','-c:a','ac3','-y',str(source)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
            def run(command,phase):return subprocess.run(command,capture_output=True,check=True,timeout=20)
            info=execute('probe',{},str(source),run)['info']
            validate_video(next(s for s in info['streams'] if s['codec_type']=='video'))
            for conversion in (False,True):
                for position,seconds in ((0,6),(6,2)):
                    params={'position':position,'seconds':seconds,'video_index':0,'video_codec':'hevc','audio_index':1,'conversion':conversion}
                    result=execute('sample',params,str(source),run,directory)
                    proof=validate_result('sample',params,result)
                    self.assertGreater(proof['video_frames'],10)
                    self.assertGreater(proof['audio_frames'],10)
                    if conversion:self.assertEqual(result['conversion_streams'][0]['codec_name'],'hevc')

if __name__=='__main__':unittest.main()
