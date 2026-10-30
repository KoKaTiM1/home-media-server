"""Playback reuses exact accepted evidence; audits and invalidation cannot."""
import json
import time
import unittest
from unittest.mock import patch
import test_features as fixtures

a=fixtures.a
KEY='movie:tt1234567'
SELECTED={'hash':'a'*40,'index':1,'filename':'fixture.mkv','length':10000}
INFO={'streams':[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},
                 {'index':1,'codec_type':'audio','codec_name':'aac','profile':'LC','tags':{'language':'eng'}}],
      'format':{'duration':'60','bit_rate':'100000'}}

class DurablePlaybackTests(unittest.TestCase):
    def setUp(self):
        fixtures.FeatureTests.setUp(self)
        secret=patch.object(a,'signature',return_value='fixture')
        secret.start();self.addCleanup(secret.stop)

    def seed(self,conversion=False):
        proof={'version':5,'audio_policy':2,'source_id':a.REL.sid(SELECTED),
               'source_hash':SELECTED['hash'],'file_index':1,'filename':SELECTED['filename'],'length':10000,
               'selected_audio_index':1,'audio_conversion':conversion,'checked_at':time.time()-3600,
               'samples':[{'position':0,'video_frames':60,'audio_frames':100},
                          {'position':15,'video_frames':20,'audio_frames':50}]}
        info=json.loads(json.dumps(INFO))
        if conversion:info['streams'][1].update(codec_name='eac3',profile='Dolby Digital Plus')
        with patch.object(a,'resolve',return_value=SELECTED),patch.object(a,'probe',return_value=info),patch.object(a.REL,'verify',return_value=proof):
            a.prepared(KEY,background=True)
        # A process restart does not retain Python locks/response caches. Both
        # short-lived cache records are already expired here.
        with a.F.db() as c:
            c.execute('UPDATE cache SET expires=0 WHERE key IN (?,?)',('prepared:'+KEY,'verified-prepared:'+KEY))
        return proof

    def test_expired_cache_uses_persisted_proof_and_refreshes_transport_and_subtitles(self):
        proof=self.seed()
        with patch.object(a,'resolve',return_value=SELECTED) as transport,patch.object(a,'probe',side_effect=AssertionError('duplicate probe')),patch.object(a.REL,'verify',side_effect=AssertionError('duplicate decode')),patch.object(a,'external_subtitles',return_value=[]) as subs:
            result=a.prepared(KEY)
        transport.assert_called_once();subs.assert_called_once()
        self.assertEqual(result['checked_at'],proof['checked_at'])
        self.assertEqual(result['DefaultAudioStreamIndex'],1)
        with a.F.db() as c:current=json.loads(c.execute('SELECT proof FROM validation WHERE key=?',(KEY,)).fetchone()[0])
        self.assertEqual(current,proof,'Playback cannot manufacture a new audit pass')

    def test_saved_russian_audio_survives_english_default_preference(self):
        proof=self.seed()
        with a.F.db() as c:
            saved=json.loads(c.execute('SELECT value FROM playback WHERE key=?',(KEY,)).fetchone()[0])
            info=saved['verification_info']
            info['streams'][1]['tags']['language']='rus'
            info['streams'].append({'index':3,'codec_type':'audio','codec_name':'eac3','profile':'Dolby Digital Plus','tags':{'language':'eng'}})
            c.execute('UPDATE playback SET value=? WHERE key=?',(json.dumps(saved),KEY))
        with patch.object(a,'resolve',return_value=SELECTED),patch.object(a.REL,'verify',side_effect=AssertionError('Unexpected audio switch/reverification')),patch.object(a,'external_subtitles',return_value=[]):
            result=a.prepared(KEY)
        self.assertEqual(result['DefaultAudioStreamIndex'],1)
        self.assertFalse(result['AudioConversionRequired'])
        self.assertEqual(result['checked_at'],proof['checked_at'])

    def test_converted_audio_stays_original_for_native_lc_transcoding(self):
        self.seed(conversion=True)
        with patch.object(a,'resolve',return_value=SELECTED),patch.object(a,'probe',side_effect=AssertionError('duplicate probe')),patch.object(a.REL,'verify',side_effect=AssertionError('duplicate conversion')),patch.object(a,'external_subtitles',return_value=[]):
            result=a.prepared(KEY)
        self.assertTrue(result['AudioConversionRequired'])
        self.assertEqual(result['MediaStreams'][1]['Codec'],'eac3')
        self.assertEqual(result['DefaultAudioStreamIndex'],1)

    def test_older_playback_rows_reconstruct_codec_metadata(self):
        self.seed()
        with a.F.db() as c:
            saved=json.loads(c.execute('SELECT value FROM playback WHERE key=?',(KEY,)).fetchone()[0])
            saved.pop('verification_info')
            c.execute('UPDATE playback SET value=? WHERE key=?',(json.dumps(saved),KEY))
        evidence=a.durable_playback_evidence(KEY)
        self.assertEqual(evidence['info']['format']['duration'],60)
        self.assertEqual(evidence['info']['streams'][1]['profile'],'LC')

    def test_audit_reuses_probe_info_but_runs_verification_gate(self):
        self.seed()
        with patch.object(a,'resolve',return_value=SELECTED),patch.object(a,'probe',return_value=INFO) as probe,patch.object(a.REL,'verify',return_value={'version':5}) as verify:
            a.prepared(KEY,background=True)
        probe.assert_not_called();verify.assert_called_once()

    def test_new_source_cannot_inherit_old_proof(self):
        proof=self.seed();other={**SELECTED,'hash':'b'*40}
        with patch.object(a,'resolve',return_value=other),patch.object(a,'probe',return_value=INFO) as probe,patch.object(a.REL,'verify',return_value={**proof,'source_id':a.REL.sid(other),'source_hash':other['hash']}) as verify,patch.object(a,'external_subtitles',return_value=[]):
            result=a.prepared(KEY)
        probe.assert_called_once();verify.assert_called_once()
        self.assertEqual(result['selected'],other)

    def test_newer_failure_manual_recheck_and_exclusion_block_reuse(self):
        self.seed()
        a.F.validation_result(KEY,False)
        self.assertIsNone(a.durable_playback_evidence(KEY))
        self.seed()
        with a.F.db() as c:c.execute("UPDATE validation SET state='pending' WHERE key=?",(KEY,))
        self.assertIsNone(a.durable_playback_evidence(KEY))
        self.seed();a.REL.exclude(KEY,SELECTED)
        self.assertIsNone(a.durable_playback_evidence(KEY))

    def test_newer_source_status_failure_blocks_reuse_without_overwriting_proof(self):
        proof=self.seed()
        a.remember('source-status:'+KEY,{'state':'unavailable','checked_at':time.time()},900)
        self.assertIsNone(a.durable_playback_evidence(KEY))
        with a.F.db() as c:current=json.loads(c.execute('SELECT proof FROM validation WHERE key=?',(KEY,)).fetchone()[0])
        self.assertEqual(current,proof)

    def test_audio_source_policy_and_sample_mismatches_block_reuse(self):
        proof=self.seed()
        self.assertIsNone(a.durable_playback_evidence(KEY,2))
        variants=[{'version':4},{'audio_policy':1},{'source_id':'b'*64},{'length':999},
                  {'selected_audio_index':2},{'audio_conversion':True},{'checked_at':time.time()},
                  {'samples':[proof['samples'][0]]},
                  {'samples':[proof['samples'][0],{'position':15,'video_frames':0,'audio_frames':20}]}]
        for changes in variants:
            with self.subTest(changes=changes):
                with a.F.db() as c:c.execute('UPDATE validation SET proof=? WHERE key=?',(json.dumps({**proof,**changes}),KEY))
                self.assertIsNone(a.durable_playback_evidence(KEY))
        with a.F.db() as c:c.execute('UPDATE validation SET proof=? WHERE key=?',(json.dumps(proof),KEY))
        with patch.object(a.F,'local_download',return_value={**SELECTED,'local':'fixture.mp4'}):
            self.assertIsNone(a.durable_playback_evidence(KEY))

if __name__=='__main__':unittest.main()
