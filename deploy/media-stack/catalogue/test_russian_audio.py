"""Russian alternatives remain separate from the accepted V6 source proof."""
import json
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
import test_features as fixtures
from russian_audio import RussianAudio

a=fixtures.a
KEY='movie:tt1234567'
SELECTED={'hash':'a'*40,'index':1,'filename':'fixture.mkv','length':10000}


class RussianAudioTests(unittest.TestCase):
    def setUp(self):
        fixtures.FeatureTests.setUp(self)
        signing=patch.object(a,'signature',return_value='fixture')
        signing.start();self.addCleanup(signing.stop)
        self.worker=RussianAudio(a.__dict__)
        self.selected=dict(SELECTED)
        self.sid=a.REL.sid(self.selected)
        self.proof={'version':6,'source_id':self.sid,'source_hash':self.selected['hash'],
            'file_index':self.selected['index'],'filename':self.selected['filename'],
            'length':self.selected['length'],'selected_audio_index':1,'checked_at':time.time(),
            'samples':[{'video_frames':10},{'video_frames':10}],
            'speed':{'policy':1,'mode':'torrent','checked_at':time.time()}}
        a.F.validation_result(KEY,True,proof=self.proof)
        with a.F.db() as c:
            c.execute('INSERT INTO home_titles(imdb,kind,name,original,year) VALUES (?,?,?,?,?)',
                      ('tt1234567','movie','Fixture','Fixture',2026))
            c.execute('INSERT OR REPLACE INTO playback VALUES (?,?,?)',
                (KEY,json.dumps({'selected':self.selected,'verification_info':{'streams':[]}}),time.time()))
        self.speed=patch.object(a.REL,'speed_proof_valid',return_value=True)
        self.speed.start();self.addCleanup(self.speed.stop)

    def test_candidate_selection_uses_newest_year_and_requires_current_v6(self):
        with a.F.db() as c:
            older='movie:tt7654321'
            c.execute("INSERT INTO validation(key,state,checked,proof) VALUES (?,'verified',1,?)",
                      (older,json.dumps({**self.proof,'source_id':'b'*64})))
            c.execute('INSERT INTO home_titles(imdb,kind,name,original,year) VALUES (?,?,?,?,?)',
                      ('tt7654321','movie','Older','Older',2020))
        self.assertEqual(self.worker.candidate(),KEY)
        self.assertEqual(self.worker.base(KEY)['proof'],self.proof)
        with a.F.db() as c:c.execute("UPDATE validation SET proof=json_set(proof,'$.version',5) WHERE key=?",(KEY,))
        self.assertIsNone(self.worker.base(KEY))

    def test_alternative_requires_exact_source_and_source_bound_hebrew_file(self):
        alternate={**self.selected,'hash':'b'*40,'filename':'alternate.mkv'}
        sid=a.REL.sid(alternate)
        subtitle_path=self.root/'he.srt';subtitle_path.write_text('1\n00:00:00,000 --> 00:00:01,000\nשלום\n')
        info={'format':{'duration':'60','bit_rate':'100000'},'streams':[
            {'index':0,'codec_type':'video','codec_name':'h264'},
            {'index':1,'codec_type':'audio','codec_name':'aac','profile':'LC',
             'tags':{'language':'rus'},'disposition':{'default':True}}]}
        altproof={**self.proof,'source_id':sid,'source_hash':alternate['hash'],
            'filename':alternate['filename'],'selected_audio_index':1,'alternate_audio':True}
        self.worker._record(KEY,self.sid,alternate,info,altproof,
            {'lang':'heb','path':str(subtitle_path),'variant':'a'*16},2026)
        with patch.object(a.EMBED,'ready',return_value=[]):
            options=self.worker.options(KEY,self.proof,self.selected)
        self.assertEqual(len(options),1)
        self.assertEqual(options[0]['DefaultAudioStreamIndex'],1)
        self.assertEqual(options[0]['DefaultSubtitleStreamIndex'],2)
        self.assertIn('lang=heb',options[0]['MediaStreams'][-1]['DeliveryUrl'])
        with a.F.db() as c:
            c.execute('UPDATE russian_audio_alternates SET subtitle=? WHERE key=?',
                (json.dumps({'lang':'heb','path':str(subtitle_path),'variant':'a'*16,'source_id':'c'*64}),KEY))
        with patch.object(a.EMBED,'ready',return_value=[]):
            self.assertEqual(self.worker.options(KEY,self.proof,self.selected),[])

    def test_options_never_mutate_or_replace_primary_v6_proof(self):
        before=json.dumps(self.proof,sort_keys=True)
        self.assertEqual(self.worker.options(KEY,self.proof,{**self.selected,'hash':'c'*40}),[])
        with a.F.db() as c:
            saved=json.loads(c.execute('SELECT proof FROM validation WHERE key=?',(KEY,)).fetchone()[0])
        self.assertEqual(json.dumps(saved,sort_keys=True),before)

    def test_worker_guard_rejects_stale_or_failed_resource_status(self):
        class Queue:
            def __init__(self,health,reason='Parallel verification permitted: one server'):
                self.health=health;self.reason=reason
            def status(self):return {'windows_slots_online':2}
            def setting_read(self,key,default=None):
                return {'health':self.health,'reason':self.reason,'enabled':True,
                        'russian_extra_permitted':True}.get(key,default)
        with patch.object(self.worker,'_queue',return_value=(Queue({'time':time.time()}),{})):
            self.assertTrue(self.worker._guard('extra')[0])
        with patch.object(self.worker,'_queue',return_value=(Queue({'time':time.time()-120}),{})):
            self.assertFalse(self.worker._guard('extra')[0])
        with patch.object(self.worker,'_queue',return_value=(Queue({'time':time.time()},'TorrServer memory pressure'),{})):
            self.assertFalse(self.worker._guard('extra')[0])

    def test_same_file_reuses_speed_and_video_proof_but_samples_russian_audio(self):
        base={'proof':self.proof,'selected':self.selected}
        info={'format':{'duration':'60'},'streams':[]}
        audio={'index':2,'codec_name':'aac','profile':'LC'}
        sample={'position':0,'video_frames':12,'audio_frames':20}
        with patch.object(a.REL,'lock',return_value=nullcontext()), \
             patch.object(a.REL,'sample',return_value=sample) as decode, \
             patch.object(a.REL,'verify',side_effect=AssertionError('same file should reuse video proof')):
            result=self.worker.verify_audio(KEY,base,self.selected,info,audio,time.monotonic()+30)
        self.assertEqual(decode.call_count,2)
        self.assertTrue(result['speed_reused'])
        self.assertEqual(result['selected_audio_index'],2)
        self.assertEqual(result['source_id'],self.sid)

    def test_different_file_requires_complete_v6_verification(self):
        base={'proof':self.proof,'selected':self.selected}
        alternate={**self.selected,'hash':'b'*40,'filename':'other.mkv'}
        info={'streams':[]};audio={'index':3,'codec_name':'eac3','profile':'Dolby Digital Plus'}
        full={**self.proof,'version':6,'source_id':a.REL.sid(alternate),
            'source_hash':alternate['hash'],'filename':alternate['filename'],
            'selected_audio_index':3}
        with patch.object(a.REL,'verify',return_value=full) as verify:
            self.assertIs(self.worker.verify_audio(KEY,base,alternate,info,audio,time.monotonic()+30),full)
        verify.assert_called_once_with(KEY,alternate,info,3,True,unittest.mock.ANY)


if __name__=='__main__':unittest.main()
