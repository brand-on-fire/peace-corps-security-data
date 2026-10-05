import copy
import pathlib
import unittest
from public_cap import parse_alert, apply_lifecycle, collect_cap, expire_warnings, feed_links

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
  self.assertEqual(apply_lifecycle([original],{row['id']:changed[0]},NOW),[])
 def test_update_chain_is_single_incident(self):
  original=self.parse();update=self.control('Update','updated','2026-10-05T07:30:00Z',self.reference(),True)
  result=apply_lifecycle([update,original],{},NOW);self.assertEqual(len(result),1);self.assertEqual(result[0]['id'],original['record']['id']);self.assertEqual(len(result[0]['sourceMetadata']['capMessageKeys']),2)
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
if __name__=='__main__':unittest.main()
