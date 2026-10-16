import copy
import pathlib
import unittest
from public_cap import parse_alert, apply_lifecycle, collect_cap, collect_cap_batch, expire_warnings, feed_links, info_blocks, deduplicate_info_blocks

ROOT=pathlib.Path(__file__).resolve().parent.parent
FIXTURE=ROOT/'tests/fixtures/cap/bmkg.xml'
if not FIXTURE.exists():FIXTURE=ROOT/'scripts/fixtures/cap/bmkg.xml'
RAW=FIXTURE.read_bytes()
SOURCE={'id':'bmkg','countryCodes':['ID']}
COUNTRIES={'ID':{'code':'ID','name':'Indonesia'}}
NOW='2026-10-05T07:35:00Z'
URL='https://www.bmkg.go.id/alerts/nowcast/en/real.xml'

class CapTests(unittest.TestCase):
 def parse(self,raw=RAW):return parse_alert(SOURCE,raw,URL,NOW,COUNTRIES,[])
 def test_real_provider_polygons_not_centroid(self):
  row=self.parse()['record'];self.assertEqual(row['location']['precision'],'area');self.assertNotIn('lat',row['location'])
  self.assertEqual(len(row['location']['geometry']['coordinates']),100)
  self.assertEqual(row['state'],'reported');self.assertEqual(row['occurredAt'],'2026-10-05T07:00:00Z');self.assertEqual(row['sourceMetadata']['effective'],'2026-10-05T07:10:00Z')
 def test_test_private_messages_excluded(self):
  for raw in (RAW.replace(b'<status>Actual</status>',b'<status>Test</status>'),RAW.replace(b'<scope>Public</scope>',b'<scope>Private</scope>')):self.assertIsNone(self.parse(raw))
 def test_bad_coordinates_reject_entire_bulletin(self):
  import xml.etree.ElementTree as E
  r=E.fromstring(RAW);r.find('.//{*}polygon').text='999,1 2,3 4,5 999,1'
  with self.assertRaises(ValueError):self.parse(E.tostring(r))
 def test_entity_rejected(self):
  with self.assertRaises(ValueError):self.parse(b'<!DOCTYPE x>'+RAW)
 def test_expiry_during_outage(self):
  row=self.parse()['record'];records={row['id']:row};expire_warnings(records,'2026-10-05T12:00:00Z');self.assertEqual(row['state'],'expired')
 def control(self,kind,identifier,sent,reference,info=False):
  import xml.etree.ElementTree as E
  r=E.fromstring(RAW)
  for key,value in [('identifier',identifier),('sent',sent),('msgType',kind)]:r.find('{*}'+key).text=value
  E.SubElement(r,'{urn:oasis:names:tc:emergency:cap:1.2}references').text=reference
  if not info:
   for i in r.findall('{*}info'):r.remove(i)
  return self.parse(E.tostring(r))
 def reference(self):return 'cuaca.ekstrem@bmkg.go.id,2.49.0.1.360.0.2026.10.05.07.32.003,2026-10-05T14:00:00+07:00'
 def test_cancel_without_info_and_replay_preserves_record(self):
  original=self.parse();row=original['record'];cancel=self.control('Cancel','cancel','2026-10-05T08:00:00Z',self.reference())
  changed=apply_lifecycle([cancel],{row['id']:row},NOW);self.assertEqual(len(changed),1);self.assertEqual(changed[0]['state'],'cancelled')
  self.assertEqual(changed[0]['id'],row['id']);self.assertEqual(len(changed[0]['reports']),2)
  self.assertEqual(info_blocks(changed[0]),info_blocks(row))
  self.assertEqual(apply_lifecycle([original],{row['id']:changed[0]},NOW),[])
 def test_update_chain_is_single_incident(self):
  original=self.parse();update=self.control('Update','updated','2026-10-05T07:30:00Z',self.reference(),True)
  result=apply_lifecycle([update,original],{},NOW);self.assertEqual(len(result),1);self.assertEqual(result[0]['id'],original['record']['id']);self.assertEqual(len(result[0]['sourceMetadata']['capMessageKeys']),2)
  row=result[0]
  self.assertEqual(len(row['reports']),2)
  self.assertCountEqual([r['contentHash'] for r in row['reports']], [m['control']['contentHash'] for m in (original,update)])
  self.assertTrue(all(r['incidentId']==row['id'] for r in row['reports']))
  self.assertEqual(row['reports'][1]['sourceMetadata']['infoBlocks'], info_blocks(original['record']))
  self.assertEqual(apply_lifecycle([original,update],{row['id']:row},NOW),[])
 def test_update_across_runs_retains_original_source_evidence(self):
  original=self.parse();update=self.control('Update','updated','2026-10-05T07:30:00Z',self.reference(),True)
  before=copy.deepcopy(original['record'])
  result=apply_lifecycle([update],{before['id']:original['record']},NOW)[0]
  self.assertEqual(original['record'],before)
  self.assertEqual(len(result['reports']),2)
  self.assertEqual(result['reports'][1],before['reports'][0])
 def test_complete_xml_is_stored_once_and_bound_to_its_report(self):
  import xml.etree.ElementTree as E
  row=self.parse()['record'];expected=[E.tostring(i,encoding='unicode') for i in E.fromstring(RAW).findall('{*}info')]
  self.assertNotIn('infoBlocks',row['sourceMetadata'])
  self.assertEqual(row['sourceMetadata']['infoBlocksReportId'],row['reports'][0]['id'])
  self.assertEqual(info_blocks(row),expected)
  self.assertEqual(row['reports'][0]['sourceMetadata']['infoBlocks'],expected)
  self.assertEqual(row['reports'][0]['contentHash'],__import__('hashlib').sha256(RAW).hexdigest())
 def test_legacy_inline_blocks_and_reports_remain_lossless(self):
  row=self.parse()['record'];blocks=copy.deepcopy(info_blocks(row))
  row['sourceMetadata'].pop('infoBlocksReportId');row['sourceMetadata']['infoBlocks']=blocks
  before_reports=copy.deepcopy(row['reports']);before_location=copy.deepcopy(row['location'])
  self.assertEqual(info_blocks(row),blocks)
  deduplicate_info_blocks(row)
  self.assertNotIn('infoBlocks',row['sourceMetadata']);self.assertEqual(info_blocks(row),blocks)
  self.assertEqual(row['reports'],before_reports);self.assertEqual(row['location'],before_location)
  row['sourceMetadata'].pop('infoBlocksReportId');row['sourceMetadata']['infoBlocks']=blocks
  row['reports'][0].pop('sourceMetadata')
  self.assertIs(deduplicate_info_blocks(row),row)
  self.assertEqual(row['sourceMetadata']['infoBlocks'],blocks)
 def test_legacy_severe_downgrade_retains_original_and_update_xml(self):
  original=self.parse(RAW.replace(b'<severity>Moderate</severity>',b'<severity>Severe</severity>'))
  old=original['record'];blocks=copy.deepcopy(info_blocks(old))
  old['sourceMetadata']['infoBlocks']=blocks;old['sourceMetadata'].pop('infoBlocksReportId')
  update=self.control('Update','updated','2026-10-05T07:30:00Z',self.reference(),True)
  row=apply_lifecycle([update],{old['id']:old},NOW)[0]
  self.assertEqual(info_blocks(row),blocks)
  self.assertEqual(row['sourceMetadata']['downgradeInfoBlocks'],info_blocks(update['record']))
  self.assertEqual(row['reports'][1],old['reports'][0]);self.assertEqual(row['location'],old['location'])
 def test_same_batch_update_then_cancel_retains_all_three_messages(self):
  original=self.parse();update=self.control('Update','updated','2026-10-05T07:30:00Z',self.reference(),True)
  reference='cuaca.ekstrem@bmkg.go.id,updated,2026-10-05T07:30:00Z'
  cancel=self.control('Cancel','cancel','2026-10-05T08:00:00Z',reference)
  row=apply_lifecycle([cancel,update,original],{},NOW)[0]
  self.assertEqual(row['state'],'cancelled');self.assertEqual(len(row['reports']),3)
  self.assertEqual(info_blocks(row),info_blocks(update['record']))
  self.assertEqual(len(row['sourceMetadata']['capMessageKeys']),3)
  self.assertEqual(apply_lifecycle([cancel,update,original],{row['id']:row},NOW),[])
 def test_severe_downgrade_preserves_both_bulletins_without_duplicate_reports(self):
  from public_data_pipeline import public_incident
  original=self.parse(RAW.replace(b'<severity>Moderate</severity>',b'<severity>Severe</severity>'))
  update=self.control('Update','updated','2026-10-05T07:30:00Z',self.reference(),True)
  row=apply_lifecycle([update,original],{},NOW)[0]
  self.assertEqual(row['severity'],'severe');self.assertEqual(row['state'],'expired')
  self.assertEqual(row['sourceMetadata']['currentProviderSeverity'],'Moderate')
  self.assertEqual(len(row['reports']),2)
  self.assertEqual(row['location'],original['record']['location'])
  self.assertEqual(info_blocks(row),info_blocks(original['record']))
  self.assertEqual(row['sourceMetadata']['downgradeInfoBlocks'],info_blocks(update['record']))
  self.assertTrue(public_incident(row));self.assertFalse(public_incident(update['record']))
  self.assertEqual(apply_lifecycle([update,original],{row['id']:row},NOW),[])
 def test_conflicting_provider_times_still_reject_instead_of_being_corrected(self):
  import xml.etree.ElementTree as E
  root=E.fromstring(RAW);root.find('{*}info/{*}expires').text='2026-10-05T06:00:00Z'
  with self.assertRaisesRegex(ValueError,'expiry precedes'):
   self.parse(E.tostring(root))
 def test_same_message_revised_body_keeps_unique_evidence_ids(self):
  import xml.etree.ElementTree as E
  original=self.parse();root=E.fromstring(RAW)
  root.find('{*}info/{*}description').text='Synthetic corrected complete bulletin.'
  revised=self.parse(E.tostring(root));row=original['record']
  changed=apply_lifecycle([revised],{row['id']:row},NOW)[0]
  self.assertEqual(changed['id'],row['id'])
  self.assertEqual(len(changed['reports']),2)
  self.assertEqual(len({r['id'] for r in changed['reports']}),2)
  self.assertEqual(changed['reports'][1],row['reports'][0])
  self.assertEqual(apply_lifecycle([revised],{changed['id']:changed},NOW),[])
 def test_unknown_cancel_does_not_invent_incident(self):
  self.assertEqual(apply_lifecycle([self.control('Cancel','cancel','2026-10-05T08:00:00Z',self.reference())],{},NOW),[])
 def test_outside_country_not_admitted(self):
  self.assertIsNone(parse_alert(SOURCE,RAW,URL,NOW,{},[]))
 def test_bad_second_bulletin_rejects_whole_source(self):
  feed=b'<rss><channel><item><link>https://www.bmkg.go.id/a.xml</link></item><item><link>https://www.bmkg.go.id/b.xml</link></item></channel></rss>'
  def fetch(policy,*args):return ({},RAW if policy['url'].endswith('/a.xml') else b'badxml')
  with self.assertRaises(Exception):collect_cap(SOURCE,{'url':URL},feed,NOW,COUNTRIES,[],fetch)
 def test_external_hostname_rejected_before_fetch(self):
  calls=[]
  with self.assertRaises(ValueError):collect_cap(SOURCE,{'url':URL},b'<rss><channel><item><link>https://evil.test/a.xml</link></item></channel></rss>',NOW,COUNTRIES,[],lambda *a:calls.append(a))
  self.assertEqual(calls,[])
 def test_duplicate_link_once(self):
  self.assertEqual(feed_links(b'<rss><channel><item><link>https://x.test/a</link></item><item><link>https://x.test/a</link></item></channel></rss>'),['https://x.test/a'])

class CapPartialTests(unittest.TestCase):
 def bulletin(self, identifier='a', kind='Alert', severity='Severe', sent='2026-10-13T04:00:00Z', expires='2026-10-13T07:00:00Z', reference=''):
  # Complete synthetic source documents. The conflicting offsets reproduce the
  # retained VMGD failure without copying a third-party bulletin into fixtures.
  return f'''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
   <identifier>{identifier}</identifier><sender>issuer@example.test</sender><sent>{sent}</sent>
   <status>Actual</status><msgType>{kind}</msgType><scope>Public</scope><references>{reference}</references>
   <info><language>en</language><category>Met</category><event>Severe wind</event>
   <urgency>Immediate</urgency><severity>{severity}</severity><certainty>Observed</certainty>
   <expires>{expires}</expires><headline>Wind warning</headline><description>Damaging winds reported.</description>
   <area><areaDesc>Named area</areaDesc><polygon>-10,160 -10,161 -11,161 -10,160</polygon></area></info>
   </alert>'''.encode()
 def bad(self, **kwargs):
  return self.bulletin(sent='2026-10-13T05:54:56+01:00',expires='2026-10-13T15:00:00+11:00',severity='Moderate',**kwargs)
 def feed(self, names):
  return ('<rss><channel>'+''.join(f'<item><link>https://www.bmkg.go.id/{name}</link></item>' for name in names)+'</channel></rss>').encode()
 def collect(self, bodies, names=None, previous=()):
  self.calls=[]
  def fetch(policy,*args):
   self.calls.append(policy['url']);return ({}, bodies[policy['url'].rsplit('/',1)[-1]])
  return collect_cap_batch(SOURCE,{'url':URL},self.feed(names if names is not None else bodies),'2026-10-13T06:00:00Z',COUNTRIES,[],fetch,previous_quarantined=previous)
 def test_bad_moderate_does_not_block_unrelated_severe(self):
  from public_data_pipeline import public_incident
  import hashlib
  bad=self.bad();result=self.collect({'bad':bad,'good':self.bulletin()})
  self.assertEqual(result['status'],'partial');self.assertEqual(len(result['messages']),1)
  row=apply_lifecycle(result['messages'],{},'2026-10-13T06:00:00Z')[0]
  self.assertTrue(public_incident(row));self.assertEqual(row['severity'],'severe')
  q=result['quarantined'][0];self.assertEqual(q['contentHash'],hashlib.sha256(bad).hexdigest())
  self.assertEqual(q['bytes'],len(bad));self.assertEqual(q['reason'],'CAP expiry precedes effective/onset time')
  self.assertEqual(q['identity']['sent'],'2026-10-13T04:54:56Z')
  self.assertNotIn('raw',q);self.assertNotIn('content',q)
 def test_valid_moderate_still_excluded(self):
  from public_data_pipeline import public_incident
  result=self.collect({'bad':self.bad(),'routine':self.bulletin(severity='Moderate')})
  self.assertFalse(public_incident(result['messages'][0]['record']))
 def test_malformed_update_keeps_prior_evidence_and_cannot_cancel(self):
  original=parse_alert(SOURCE,self.bulletin(),URL,NOW,COUNTRIES,[]);row=original['record'];before=copy.deepcopy(row)
  ref='issuer@example.test,a,2026-10-13T04:00:00Z'
  for kind in ('Update','Cancel'):
   result=self.collect({'bad':self.bad(identifier='bad',kind=kind,reference=ref),'new':self.bulletin(identifier='new')})
   self.assertEqual(result['quarantined'][0]['identity']['references'],[original['control']['key']])
   self.assertEqual(result['quarantined'][0]['identity']['kind'],kind)
   changed=apply_lifecycle(result['messages'],{row['id']:row},NOW)
   self.assertEqual(len(changed),1);self.assertNotEqual(changed[0]['id'],row['id']);self.assertEqual(row,before)
 def test_valid_update_and_cancel_history_survives_unrelated_quarantine(self):
  ref='issuer@example.test,a,2026-10-13T04:00:00Z'
  update=self.bulletin(identifier='b',kind='Update',sent='2026-10-13T05:00:00Z',reference=ref)
  cancel=self.bulletin(identifier='c',kind='Cancel',sent='2026-10-13T06:00:00Z',reference='issuer@example.test,b,2026-10-13T05:00:00Z')
  result=self.collect({'bad':self.bad(identifier='bad'),'a':self.bulletin(),'b':update,'c':cancel})
  row=apply_lifecycle(result['messages'],{},'2026-10-13T06:00:00Z')[0]
  self.assertEqual(row['state'],'cancelled');self.assertEqual(len(row['reports']),3)
  self.assertEqual(apply_lifecycle(result['messages'],{row['id']:row},NOW),[])
 def test_unlisted_quarantine_is_retried_until_valid_response(self):
  first=self.collect({'bad':self.bad()});saved=copy.deepcopy(first['quarantined'])
  again=self.collect({'bad':self.bad(),'new':self.bulletin(identifier='new')},names=['new'],previous=saved)
  self.assertEqual(len(again['quarantined']),1);self.assertEqual(len(self.calls),2);self.assertEqual(saved,first['quarantined'])
  recovered=self.collect({'bad':self.bulletin()},names=[],previous=saved)
  self.assertEqual(recovered['status'],'complete');self.assertEqual(recovered['quarantined'],[]);self.assertEqual(len(recovered['messages']),1)
 def test_duplicate_current_and_queued_url_is_fetched_once(self):
  q=self.collect({'bad':self.bad()})['quarantined']
  self.collect({'bad':self.bad()},previous=q);self.assertEqual(len(self.calls),1)
 def test_bad_envelope_never_invents_reference(self):
  for raw in (b'not XML',self.bad(reference='not,a,valid-time'),self.bad().replace(b'</sent>',b'</sent><sent>2026-10-13T04:00:00Z</sent>')):
   q=self.collect({'bad':raw})['quarantined'][0];self.assertNotIn('identity',q)
 def test_fetch_failure_is_still_atomic_including_unlisted_queue(self):
  q=self.collect({'bad':self.bad()})['quarantined']
  with self.assertRaisesRegex(ValueError,'CAP bulletin fetch failed'):
   collect_cap_batch(SOURCE,{'url':URL},self.feed([]),NOW,COUNTRIES,[],lambda *args:({'message':'unavailable'},None),previous_quarantined=q)
 def test_invalid_listing_and_unreviewed_queue_are_atomic_before_fetch(self):
  calls=[]
  for raw,q in [(b'not XML',()),(self.feed([]),[{'sourceId':SOURCE['id'],'url':'https://unreviewed.test/bad'}]),(self.feed([]),[{'sourceId':'other','url':URL}])]:
   with self.assertRaises(Exception):collect_cap_batch(SOURCE,{'url':URL},raw,NOW,COUNTRIES,[],lambda *a:calls.append(a),previous_quarantined=q)
  self.assertEqual(calls,[])
 def test_combined_queue_and_aggregate_bytes_remain_bounded(self):
  from unittest.mock import patch
  q=self.collect({'bad':self.bad()})['quarantined']
  with patch('public_cap.MAX_BULLETINS',1):
   with self.assertRaisesRegex(ValueError,'combined index'):self.collect({'good':self.bulletin()},previous=q)
  with patch('public_cap.MAX_BYTES',len(self.bulletin())+10):
   with self.assertRaisesRegex(ValueError,'complete source exceeds'):self.collect({'good':self.bulletin()})
 def test_direct_cap_quarantine_and_recovery(self):
  def collect(raw,previous=()):return collect_cap_batch(SOURCE,{'url':URL},raw,NOW,COUNTRIES,[],lambda *a:self.fail('direct document fetched twice'),previous_quarantined=previous)
  bad=collect(self.bad());self.assertEqual(bad['status'],'partial')
  good=collect(self.bulletin(),bad['quarantined']);self.assertEqual(good['status'],'complete');self.assertEqual(len(good['messages']),1)
if __name__=='__main__':unittest.main()
