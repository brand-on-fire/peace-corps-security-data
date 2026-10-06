"""Network-free adapter contract checks. Every constructed payload is synthetic."""
import copy,datetime as dt,json,unittest
from urllib.parse import parse_qs,urlsplit
import public_official as m
NOW='2026-10-05T08:00:00Z'
COUNTRIES={c:{'code':c,'name':n,'region':'Test'} for c,n in [('UG','Uganda'),('TH','Thailand'),('GN','Guinea'),('SN','Senegal')]}
class Geo:
 def country(self,x,y):return 'TH' if 0<x<180 and -90<y<90 else None

def who(i=1,title='Example disease - Uganda',pub='2026-10-01T00:00:00Z',event=True):
 a={'Id':f'00000000-0000-4000-8000-{i:012d}','Title':title,'DonId':f'2026-TEST{i}','ItemDefaultUrl':f'/2026-TEST{i}','PublicationDateAndTime':pub,'LastModified':pub,'Summary':'Synthetic fixture only.','Overview':'Synthetic fixture only.'}
 if event:a['EmergencyEvent']={'Id':'11111111-1111-4111-8111-111111111111','EventId':'2026-E-TEST'}
 return a

def gdacs(i=1):
 return {'type':'Feature','geometry':{'type':'Point','coordinates':[100,10]},'properties':{'eventtype':'FL','eventid':i,'episodeid':1,'fromdate':'2026-10-01T00:00:00','todate':'2026-10-03T00:00:00','datemodified':'2026-10-01T01:00:00','country':'Other country','affectedcountries':[{'iso2':'TH'}],'url':{'report':f'https://www.gdacs.org/report.aspx?eventid={i}&eventtype=FL'},'alertlevel':'green','source':'TEST','Class':'Point_Centroid','polygonlabel':'Centroid'}}
def quake(i='test1',when='2026-10-01T00:00:00Z'):
 t=int(m.moment(when).timestamp()*1000)
 return {'type':'Feature','id':i,'geometry':{'type':'Point','coordinates':[100,10,4]},'properties':{'type':'earthquake','title':'Synthetic M1 earthquake','mag':1.0,'time':t,'updated':t,'url':'https://earthquake.usgs.gov/earthquakes/eventpage/'+i,'status':'reviewed','alert':None}}
def document(rows,kind):return {'@odata.count':len(rows),'value':rows} if kind=='who-don' else {'type':'FeatureCollection','features':rows,'metadata':{'count':len(rows)}}
def source(kind):return {'id':{'who-don':'who-don','gdacs-api':'gdacs','usgs-week':'usgs'}[kind],'kind':{'who-don':'directory','gdacs-api':'gdacs','usgs-week':'usgs'}[kind],'language':'en','countryCodes':list(COUNTRIES)}
def policy(kind):return m.prepare_official_policy({'sourceId':source(kind)['id'],'adapter':kind,'url':'https://example.invalid'}, {}, NOW)
def forbidden(*args):raise AssertionError('Unexpected network request')
def collect(kind,obj,previous=None,records=None,fetch=forbidden):return m.collect_official(source(kind),policy(kind),json.dumps(obj).encode(),NOW,COUNTRIES,Geo(),previous or {},records or {},fetch)

class OfficialAdapters(unittest.TestCase):
 def test_usgs_preserves_provider_millisecond_dates(self):
  feature=quake(when='2026-10-01T00:00:00.685Z');feature['properties']['updated']+=40
  row=collect('usgs-week',document([feature],'usgs-week'))['incidents'][0]
  self.assertEqual(row['occurredAt'],'2026-10-01T00:00:00.685Z')
  self.assertEqual(row['updatedAt'],'2026-10-01T00:00:00.725Z')
  self.assertEqual(row['reports'][0]['publishedAt'],row['updatedAt'])
 def test_small_quake_and_provider_id_preserved(self):
  x=collect('usgs-week',document([quake()],'usgs-week'))['incidents'][0];self.assertEqual(x['id'],'usgs-test1');self.assertEqual(x['sourceMetadata']['magnitude'],1);self.assertEqual(x['location']['precision'],'point')
 def test_usgs_count_mismatch_rejects_whole(self):
  x=document([quake()],'usgs-week');x['metadata']['count']=2
  with self.assertRaises(ValueError):collect('usgs-week',x)
 def test_usgs_duplicate_ids_reject(self):
  with self.assertRaises(ValueError):collect('usgs-week',document([quake(),quake()],'usgs-week'))
 def test_usgs_future_rejects(self):
  with self.assertRaises(ValueError):collect('usgs-week',document([quake(when='2026-10-06T00:00:00Z')],'usgs-week'))
 def test_usgs_catchup_deduplicates_inclusive_windows(self):
  calls=[]
  def fetch(p,previous,now,fixtures):
   calls.append(p['url']);obj={'count':1} if '/count?' in p['url'] else document([quake('old','2026-09-25T00:00:00Z')],'usgs-week')
   return {'httpStatus':200},json.dumps(obj).encode()
  x=collect('usgs-week',document([quake()],'usgs-week'),{'lastSuccessAt':'2026-09-20T00:00:00Z'},fetch=fetch)
  self.assertEqual({r['id'] for r in x['incidents']},{'usgs-test1','usgs-old'});self.assertTrue(calls);self.assertNotIn('coverageGap',x['state'])
 def test_usgs_long_outage_gap_is_explicit(self):
  def fetch(*args):return {'httpStatus':200},b'{"count":0}'
  x=collect('usgs-week',document([],'usgs-week'),{'lastSuccessAt':'2026-06-01T00:00:00Z'},fetch=fetch)
  self.assertEqual(x['state']['coverageGap']['from'],'2026-06-01T00:00:00Z');self.assertIn('bounded 30-day',x['state']['coverageGap']['reason'])
 def test_usgs_catalog_count_mismatch_atomically_rejects(self):
  def fetch(p,*args):return {'httpStatus':200},json.dumps({'count':2} if '/count?' in p['url'] else document([quake()],'usgs-week')).encode()
  with self.assertRaises(ValueError):collect('usgs-week',document([quake()],'usgs-week'),{'lastSuccessAt':'2026-09-25T00:00:00Z'},fetch=fetch)
 def test_gdacs_uses_affected_countries_and_not_centroid(self):
  x=collect('gdacs-api',document([gdacs()],'gdacs-api'))['incidents'][0];self.assertEqual(x['countryCode'],'TH');self.assertEqual(x['location'],{'precision':'country'});self.assertEqual(x['id'],'gdacs-FL-1-TH');self.assertNotIn('admin2Code',x)
 def test_gdacs_future_is_not_reported_as_occurred(self):
  f=gdacs();f['properties'].update(fromdate='2026-10-06T00:00:00',todate='2026-10-07T00:00:00')
  self.assertEqual(collect('gdacs-api',document([f],'gdacs-api'))['incidents'],[])
 def test_gdacs_tied_page_overlap_retained_without_inflation(self):
  rows=[gdacs(i) for i in range(1,101)]
  def fetch(*args):return {'httpStatus':200},json.dumps(document([gdacs(100),gdacs(101)],'gdacs-api')).encode()
  x=collect('gdacs-api',document(rows,'gdacs-api'),fetch=fetch);self.assertEqual(len(x['incidents']),101);self.assertEqual(x['state']['pageBoundaryOverlaps'],1);self.assertFalse(x['state']['exhaustiveArchiveGuarantee'])
 def test_gdacs_missing_later_page_discards_source(self):
  with self.assertRaises(ValueError):collect('gdacs-api',document([gdacs(i) for i in range(100)],'gdacs-api'),fetch=lambda *args:({'message':'fixture unavailable'},None))
 def test_gdacs_http204_is_distinct_from_empty200(self):
  p=policy('gdacs-api');x=m.collect_official(source('gdacs-api'),p,b'',NOW,COUNTRIES,Geo(),{}, {},forbidden,initial_http_status=204);self.assertEqual(x['incidents'],[])
  with self.assertRaises(ValueError):m.collect_official(source('gdacs-api'),p,b'',NOW,COUNTRIES,Geo(),{}, {},forbidden)
 def test_who_exact_title_country_only(self):
  self.assertEqual(m.title_countries('Example - Uganda',COUNTRIES),['UG']);self.assertEqual(m.title_countries('Example- Mauritania and Senegal',COUNTRIES),['SN']);self.assertEqual(m.title_countries('Example, Democratic Republic of the Congo & Uganda',COUNTRIES),['UG'])
  self.assertEqual(m.title_countries('Example - Equatorial Guinea',COUNTRIES),[]);self.assertEqual(m.title_countries('Travel from Uganda - United Kingdom',COUNTRIES),[])
 def test_who_no_body_country_inference(self):
  a=who(title='Example - Global');a['Overview']='Synthetic: historical travel through Uganda, not an outbreak there.'
  x=collect('who-don',document([a],'who-don'));self.assertEqual(x['incidents'],[]);self.assertEqual(len(x['discoveries']),1)
 def test_who_new_report_has_unknown_risk_and_report_date(self):
  r=collect('who-don',document([who()],'who-don'))['incidents'][0]
  self.assertEqual(r['location'],{'precision':'country'});self.assertEqual(r['severity'],'unknown');self.assertEqual(r['state'],'reported');self.assertEqual(r['occurredAt'],who()['PublicationDateAndTime']);self.assertEqual(r['sourceMetadata']['eventTimeBasis'],'report_publication_not_onset')
 def test_who_declared_count_mismatch_rejects(self):
  d=document([who()],'who-don');d['@odata.count']=2
  with self.assertRaises(ValueError):collect('who-don',d)
 def test_who_same_event_becomes_one_incident_two_reports(self):
  x=collect('who-don',document([who(1),who(2,pub='2026-10-02T00:00:00Z')],'who-don'))
  self.assertEqual(len(x['incidents']),1);self.assertEqual(len(x['incidents'][0]['reports']),2)
 def test_who_does_not_reopen_reviewed_resolved_outbreak(self):
  first=collect('who-don',document([who()],'who-don'))['incidents'][0];first['id']='who-reviewed-existing-UG';first['state']='resolved';first['reports'][0]['incidentId']=first['id']
  x=collect('who-don',document([who(2,pub='2026-10-02T00:00:00Z')],'who-don'),records={first['id']:first})['incidents'][0]
  self.assertEqual(x['id'],first['id']);self.assertEqual(x['state'],'resolved');self.assertTrue(x['sourceMetadata']['statusReviewRequired']);self.assertEqual(len(x['reports']),2)
 def test_reviewed_event_alias_preserves_backfill_id_in_first_incremental_window(self):
  old=collect('who-don',document([who()],'who-don'))['incidents'][0];old['id']='who-bundibugyo-2026-UG';old['sourceMetadata'].pop('whoEmergencyEventId');old['reports'][0]['incidentId']=old['id'];old['state']='resolved'
  p=policy('who-don');p['eventAliases']=[{'eventId':who()['EmergencyEvent']['Id'],'countryCode':'UG','incidentId':old['id']}]
  x=m.collect_official(source('who-don'),p,json.dumps(document([who(2,pub='2026-10-02T00:00:00Z')],'who-don')).encode(),NOW,COUNTRIES,Geo(),{}, {old['id']:old},forbidden)
  self.assertEqual(x['incidents'][0]['id'],old['id']);self.assertEqual(x['incidents'][0]['state'],'resolved');self.assertNotIn('whoEmergencyEventId',old['sourceMetadata'])
 def test_who_each_page_evidence_keeps_its_own_complete_response_hash(self):
  first=document([who()],'who-don');first['@odata.count']=2;first['@odata.nextLink']=m.WHO+'?$skip=1';second=document([who(2,pub='2026-10-02T00:00:00Z')],'who-don');second['@odata.count']=2;raw=json.dumps(second).encode()
  x=collect('who-don',first,fetch=lambda *args:({'httpStatus':200},raw));self.assertEqual(x['incidents'][0]['reports'][-1]['sourceMetadata']['rawResponseSha256'],m.sha(raw))
 def test_who_external_pagination_rejected(self):
  d=document([who()],'who-don');d['@odata.nextLink']='https://example.org/steal'
  with self.assertRaises(ValueError):collect('who-don',d)
 def test_changed_query_does_not_reuse_old_etag(self):
  state={'url':'https://www.who.int/old','etag':'abc','lastModified':'date','lastSuccessAt':NOW};self.assertNotIn('etag',m.request_state(policy('who-don'),state));self.assertEqual(m.request_state({'url':state['url']},state),state)
 def test_policy_has_fixed_free_hosts_and_bounded_windows(self):
  self.assertEqual(policy('usgs-week')['url'],m.USGS_WEEK);p=parse_qs(urlsplit(policy('who-don')['url']).query);self.assertEqual(p['$expand'],['EmergencyEvent']);self.assertIn('LastModified',p['$filter'][0]);self.assertEqual(parse_qs(urlsplit(policy('gdacs-api')['url']).query)['pagesize'],['100'])
if __name__=='__main__':unittest.main()
