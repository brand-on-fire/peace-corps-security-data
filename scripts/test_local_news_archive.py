"""Offline safety checks for full public archive parsing and curfew evidence."""
import datetime as dt
import importlib.util
import json
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=(ROOT/'scripts/local_news_archive.py')
spec=importlib.util.spec_from_file_location('local_news_archive',SCRIPT)
news=importlib.util.module_from_spec(spec);spec.loader.exec_module(news)

class ArchiveTests(unittest.TestCase):
    source={'id':'gov-jm-jcf','url':'https://jcf.gov.jm/'}
    def post(self,body=None):
        return dict(id=1,link='https://jcf.gov.jm/test/',status='publish',type='post',
            date_gmt='2026-01-01T12:00:00',modified_gmt='2026-01-01T13:00:00',
            title={'rendered':'48-Hour Curfew Extended in a Community'},content={'protected':False,'rendered':body or
            '<p>The 48-hour curfew has been extended. The curfew continues from 6:00 p.m. on Tuesday, December 30, 2025, and will remain in effect until 6:00 p.m. on Thursday, January 1, 2026.</p><p>North: A East: B South: C West: D</p>'})
    def parse(self,post):return news.parse_wordpress(json.dumps([post]).encode(),self.source)
    def incidents(self,post,now='2026-01-02T00:00:00Z'):
        return news.jcf_curfew_incidents(self.source,self.parse(post),now,{'JM':{}},[])
    def test_complete_body_and_unicode_preserved(self):
        text='Complete evidence Καλημέρα. '*400
        self.assertEqual(self.parse(self.post('<p>'+text+'</p>'))[0]['description'],text.strip())
    def test_source_identity_protected_and_duplicate_response_fail_closed(self):
        for field,value in [('link','https://outside.example/article'),('status','draft'),('date_gmt','')]:
            p=self.post();p[field]=value
            with self.assertRaises(ValueError):self.parse(p)
        p=self.post();p['content']['protected']=True
        with self.assertRaises(ValueError):self.parse(p)
        with self.assertRaises(ValueError):news.parse_wordpress(json.dumps([self.post(),self.post()]).encode(),self.source)
        with self.assertRaises(json.JSONDecodeError):news.parse_wordpress(b'[{',self.source)
    def test_real_calendar_rollover_and_expiration(self):
        row=self.incidents(self.post())[0]
        self.assertEqual(row['occurredAt'],'2025-12-30T23:00:00Z')
        self.assertEqual(row['sourceMetadata']['providerEnd'],'2026-01-01T23:00:00Z')
        self.assertEqual(row['state'],'expired')
        self.assertEqual(row['location'],{'precision':'area'})
        self.assertNotIn('admin1Code',row)
        self.assertEqual(self.incidents(self.post(),'2026-01-01T12:00:00Z')[0]['state'],'reported')
    def test_unknown_country_or_source_cannot_promote(self):
        items=self.parse(self.post())
        self.assertEqual(news.jcf_curfew_incidents(self.source,items,'2026-01-02T00:00:00Z',{},[]),[])
        self.assertEqual(news.jcf_curfew_incidents(dict(self.source,id='news-other'),items,'2026-01-02T00:00:00Z',{'JM':{}},[]),[])
    def test_crime_keywords_do_not_promote_and_curfew_requires_full_evidence(self):
        self.assertEqual(self.incidents(self.post('Police investigate murder and robbery in Jamaica.')),[])
        for old,new in [('Tuesday','Wednesday'),('Thursday','Friday'),('North:','North '),('6:00 p.m. on Thursday','7:00 p.m. on Thursday')]:
            p=self.post();p['content']['rendered']=p['content']['rendered'].replace(old,new)
            self.assertEqual(self.incidents(p),[])
    def test_cancellation_and_correction_need_review(self):
        for marker in ['The curfew was lifted.','Correction: dates changed.','The order is cancelled.']:
            p=self.post();p['content']['rendered']+=marker
            self.assertEqual(self.incidents(p),[])
    def test_two_windows_in_one_article_are_withheld_not_mispaired(self):
        p=self.post();p['content']['rendered']*=2
        self.assertEqual(self.incidents(p),[])
    def test_changes_have_new_hash_and_expiration_does_not_mean_resolved(self):
        first=self.incidents(self.post())[0];p=self.post();p['content']['rendered']+=' Additional boundary information.'
        second=self.incidents(p)[0]
        self.assertNotEqual(first['reports'][0]['contentHash'],second['reports'][0]['contentHash'])
        self.assertIn('does not establish resolution',first['sourceMetadata']['expirationBasis'])
    def test_future_curfew_uses_issuance_anchor_and_retains_true_start(self):
        p=self.post();p['date_gmt']='2025-12-30T12:00:00';p['modified_gmt']='2025-12-30T12:00:00'
        row=self.incidents(p,'2025-12-30T13:00:00Z')[0]
        self.assertEqual(row['occurredAt'],'2025-12-30T12:00:00Z')
        self.assertEqual(row['sourceMetadata']['providerStart'],'2025-12-30T23:00:00Z')
        self.assertEqual(row['sourceMetadata']['eventTimeBasis'],'report-publication')
    def test_expiration_without_new_source_response(self):
        row=self.incidents(self.post(),'2026-01-01T12:00:00Z')[0]
        self.assertEqual(row['state'],'reported')
        self.assertEqual(news.expire_curfews({row['id']:row},'2026-01-02T00:00:00Z'),[row['id']])
        self.assertEqual(row['state'],'expired')
        self.assertEqual(news.expire_curfews([row],'2026-01-03T00:00:00Z'),[])
    def test_redirect_identity_before_follow(self):
        request=news.urllib.request.Request('https://jcf.gov.jm/a')
        for url in ['http://jcf.gov.jm/b','https://outside.example/b']:
            with self.assertRaises(ValueError):news.SameHost().redirect_request(request,None,302,'Found',{},url)

if __name__=='__main__':unittest.main()
