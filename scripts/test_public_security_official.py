"""Complete synthetic documents model real captured BIPAD/CDC schemas.

Full actual response replays live under artifacts/mvp-live-20261015/official;
these small original fixtures keep CI independent of third-party page archives.
"""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_security_official as m

NOW = '2026-10-15T23:59:00Z'
COUNTRIES = {code:dict(code=code,name=name,region='Test') for code,name in
             [('NP','Nepal'),('ID','Indonesia'),('CR','Costa Rica'),('MX','Mexico')]}
AREAS = [dict(id='ID-BALI',name='Bali',countryCode='ID',level='ADM1'),
         dict(id='CR-SJ',name='San José',countryCode='CR',level='ADM1')]

class Geo:
    def country(self, lon, lat):
        return 'NP' if 80 < lon < 89 and 26 < lat < 31 else None


def source(adapter):
    return dict(id=adapter,language='en',countryCodes=list(COUNTRIES))


def incident(i=1, hazard=10):
    return dict(id=i,title='Synthetic fire at Example Municipality-10',titleNe='Synthetic original-language title',
                wards=[5805],point=dict(type='Point',coordinates=[87.2860783,26.44607222]),
                incidentOn='2026-10-15T00:00:00+05:45',reportedOn='2026-10-15T20:48:19+05:45',
                modifiedOn='2026-10-15T21:01:03.916426+05:45',verified=True,approved=True,hazard=hazard,loss=i+100)


def loss(i=101, **changes):
    value = dict(id=i,modifiedOn='2026-10-15T21:01:03.891735+05:45',**{k:0 for k in m.LOSS_FIELDS})
    value.update(familyAffectedCount=1)
    value.update(changes)
    return value


def page(items):
    return dict(count=9223372036854775807,next='https://bipadportal.gov.np/api/v1/incident/?offset=100',previous=None,results=items)


def rss(items=None):
    rows = items or [dict(slug='zika-indonesia',title='Level 2 - Zika in Indonesia',date='Thu, 15 Oct 2026 04:00:00 GMT')]
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>CDC notices synthetic fixture</title>'+''.join(
        '<item><title>'+r['title']+'</title><link>https://wwwnc.cdc.gov/travel/notices/level2/'+r['slug']+'</link><pubDate>'+r['date']+'</pubDate><description>Synthetic complete RSS item.</description></item>' for r in rows)+'</channel></rss>').encode()


def detail(title='Zika in Indonesia', statement='Cases of Zika are being reported in returning travelers from Bali, Indonesia.'):
    return ('<!doctype html><html><head><title>'+title+' - Level 2 - Travel Health Notices | CDC</title></head><body><nav>Unrelated reports in Costa Rica.</nav><div class="notice"><h3>Key points</h3><ul><li>'+statement+'</li><li>Routine prevention advice.</li></ul><h3>Background</h3><p>Complete synthetic detail document; no source text was cut.</p></div></body></html>').encode()


class Fetch:
    def __init__(self, responses=None): self.calls=[];self.responses=responses or {}
    def __call__(self,p,*args):
        self.calls.append(p['url'])
        value=self.responses.get(p['url'])
        if value is None:
            if '/incident/?' in p['url']: value=page([])
            elif '/loss/' in p['url']: value=loss(int(p['url'].rstrip('/').rsplit('/',1)[1]))
            elif '/travel/notices/' in p['url']: value=detail()
            else: raise AssertionError(p['url'])
        if value is False: return dict(httpStatus=503,message='Synthetic outage'),None
        return dict(httpStatus=200),value if isinstance(value,bytes) else json.dumps(value).encode()


def collect(adapter='np-bipad', raw=None, fetch=None, records=None, countries=None, areas=None, previous=None, fixtures=True, **kw):
    p=m.prepare_security_policy(dict(sourceId=adapter,adapter=adapter),previous or {},NOW)
    if raw is None: raw=json.dumps(page([incident()])).encode() if adapter=='np-bipad' else rss()
    return m.collect_security_official(source(adapter),p,raw,NOW,countries or COUNTRIES,Geo(),previous or {},records or {},fetch or Fetch(),fixtures=fixtures,areas=areas or AREAS,**kw)


class NepalTests(unittest.TestCase):
    def test_old_modified_record_outside_published_history_is_held(self):
        item=incident();item.update(incidentOn='2020-01-01T00:00:00Z',reportedOn='2020-01-01T01:00:00Z',modifiedOn='2026-10-15T23:00:00Z')
        f=Fetch({m.BIPAD+'incident/?ordering=-modified_on&limit=100':page([item])})
        result=collect(raw=json.dumps(page([])).encode(),fetch=f,history_from='2021-10-05')
        self.assertFalse(result['incidents']);self.assertEqual(len(result['discoveries']),1)
        self.assertEqual(result['state']['outsideHistoryWindowCount'],1)
        self.assertTrue(result['evidence'])

    def test_changed_hazard_and_foreign_point_withhold_prior_assertion(self):
        old=collect()['incidents'][0]
        old.update(admin1Code='NP-A',admin2Code='NP-B')
        for change in [{'hazard':22},{'point':{'type':'Point','coordinates':[1,2]}},{'loss':None}]:
            item=incident();item.update(change,modifiedOn='2026-10-15T23:00:00Z')
            with self.subTest(change=change):
                new=collect(raw=json.dumps(page([item])).encode(),records={old['id']:old})['incidents'][0]
                self.assertEqual((new['state'],new['evidenceStatus']),('expired','unverified'))
                self.assertEqual(new['location'],{'precision':'country'})
                self.assertNotIn('admin1Code',new);self.assertNotIn('admin2Code',new)
                self.assertEqual(len(new['reports']),2);self.assertEqual(new['reports'][1],old['reports'][0])

    def test_old_incident_revision_found_by_supported_modification_order(self):
        old=collect()['incidents'][0]
        item=incident();item.update(reportedOn='2026-09-01T00:00:00Z',incidentOn='2026-08-31T00:00:00Z',modifiedOn='2026-10-15T23:00:00Z',verified=False)
        stale=incident(i=2);stale.update(incidentOn='2026-09-01T00:00:00Z',reportedOn='2026-09-01T00:00:00Z',modifiedOn='2026-09-01T00:00:00Z')
        f=Fetch({m.BIPAD+'incident/?ordering=-modified_on&limit=100':page([item,stale])})
        result=collect(raw=json.dumps(page([])).encode(),fetch=f,records={old['id']:old})
        self.assertEqual(result['incidents'][0]['state'],'cancelled')
        self.assertEqual(result['state']['modificationRecordCount'],1)
        self.assertFalse(result['state']['modifiedFilterSupported'])

    def test_modification_order_violation_atomically_rejects(self):
        a=incident();b=incident(i=2);b['modifiedOn']='2026-10-15T23:00:00Z'
        f=Fetch({m.BIPAD+'incident/?ordering=-modified_on&limit=100':page([a,b])})
        with self.assertRaisesRegex(ValueError,'modification ordering'):collect(fetch=f)

    def test_current_actual_schema_point_and_separate_dates(self):
        result=collect();r=result['incidents'][0]
        self.assertEqual(r['id'],'bipad-1-NP')
        self.assertEqual(r['occurredAt'],'2026-10-14T18:15:00Z')
        self.assertEqual(r['reports'][0]['publishedAt'],'2026-10-15T15:03:19Z')
        self.assertEqual(r['sourceMetadata']['eventDatePrecision'],'day')
        self.assertEqual(r['location']['precision'],'point')
        self.assertNotIn('admin1Code',r)
        self.assertEqual(r['severity'],'unknown')
        self.assertEqual(r['sourceMetadata']['providerWardIds'],[5805])

    def test_false_next_on_empty_stops(self):
        f=Fetch();r=collect(fetch=f)
        self.assertEqual(len(f.calls),3)
        self.assertEqual(r['state']['fetchedIncidentCount'],1)

    def test_repeated_page_cannot_loop_or_partially_commit(self):
        f=Fetch();original=f.__call__
        def repeat(p,*args):
            return (dict(httpStatus=200),json.dumps(page([incident()])).encode()) if '/incident/?' in p['url'] else original(p,*args)
        with self.assertRaisesRegex(ValueError,'duplicate/order'):collect(fetch=repeat)

    def test_missing_later_page_rejects_all(self):
        with self.assertRaisesRegex(ValueError,'child response'):
            collect(fetch=lambda *a:(dict(httpStatus=503),None))

    def test_future_and_reversed_dates_reject(self):
        for field,value in [('reportedOn','2026-10-17T00:00:00Z'),('incidentOn','2026-10-15T23:00:00Z'),('modifiedOn','2026-10-15T00:00:00Z')]:
            item=incident();item[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):collect(raw=json.dumps(page([item])).encode())

    def test_unverified_unapproved_and_ordinary_weather_not_published(self):
        for field,value in [('verified',False),('approved',False),('hazard',19),('hazard',22),('hazard',14),('hazard',20)]:
            item=incident();item[field]=value
            with self.subTest(field=field,value=value):self.assertEqual(collect(raw=json.dumps(page([item])).encode())['incidents'],[])

    def test_false_string_flag_is_invalid(self):
        item=incident();item['verified']='true'
        with self.assertRaises(ValueError):collect(raw=json.dumps(page([item])).encode())

    def test_missing_loss_and_minor_road_accident_not_public(self):
        item=incident(hazard=34)
        f=Fetch({m.BIPAD+'loss/101/':loss(familyAffectedCount=0,peopleInjuredCount=1)})
        self.assertEqual(collect(raw=json.dumps(page([item])).encode(),fetch=f)['incidents'],[])
        item['loss']=None
        self.assertEqual(len(collect(raw=json.dumps(page([item])).encode())['discoveries']),1)

    def test_serious_transport_and_health_with_actual_impact(self):
        for hazard,category in [(34,'transport'),(9,'health')]:
            with self.subTest(hazard=hazard):
                result=collect(raw=json.dumps(page([incident(hazard=hazard)])).encode(),fetch=Fetch({m.BIPAD+'loss/101/':loss(peopleDeathCount=1)}))
                self.assertEqual(result['incidents'][0]['category'],category)

    def test_negative_or_missing_loss_counts_reject(self):
        for bad in [loss(peopleDeathCount=-1),{'id':101}]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):collect(fetch=Fetch({m.BIPAD+'loss/101/':bad}))

    def test_foreign_point_is_held_without_false_country(self):
        item=incident();item['point']['coordinates']=[1,2]
        result=collect(raw=json.dumps(page([item])).encode())
        self.assertFalse(result['incidents']);self.assertEqual(len(result['discoveries']),1)

    def test_malformed_point_atomically_rejects(self):
        for bad in ['wrong',{'type':'Point','coordinates':[999,26]}, {'type':'Point','coordinates':[True,26]}]:
            item=incident();item['point']=bad
            with self.subTest(bad=bad),self.assertRaises(ValueError):collect(raw=json.dumps(page([item])).encode())

    def test_replay_and_correction_preserve_every_report(self):
        old=collect()['incidents'][0]
        same=collect(records={old['id']:old})['incidents'][0]
        self.assertEqual(old,same)
        changed=loss(peopleInjuredCount=2,modifiedOn='2026-10-15T23:00:00Z')
        new=collect(records={old['id']:old},fetch=Fetch({m.BIPAD+'loss/101/':changed}))['incidents'][0]
        self.assertEqual(len(new['reports']),2)
        self.assertEqual(new['reports'][1],old['reports'][0])
        self.assertEqual(old['sourceMetadata']['reportedImpact']['peopleInjuredCount'],0)

    def test_withdrawn_verification_retains_prior_evidence(self):
        old=collect()['incidents'][0];item=incident();item.update(verified=False,modifiedOn='2026-10-15T23:00:00Z')
        new=collect(raw=json.dumps(page([item])).encode(),records={old['id']:old})['incidents'][0]
        self.assertEqual(new['state'],'cancelled');self.assertEqual(len(new['reports']),2)
        self.assertEqual(old['state'],'reported')

    def test_inactive_country_cannot_be_published(self):
        self.assertFalse(collect(countries={'ID':COUNTRIES['ID']})['incidents'])

    def test_long_outage_gap_and_short_catchup_are_explicit(self):
        result=collect(raw=json.dumps(page([])).encode(),previous={'lastSuccessAt':'2026-09-01T00:00:00Z'})
        self.assertEqual(result['state']['coverageGap']['from'],'2026-09-01T00:00:00Z')


class CDCTests(unittest.TestCase):
    def test_changed_notice_level_url_does_not_overwrite_accepted_revision_with_absence(self):
        old=collect('cdc-notices')['incidents'][0]
        raw=rss([dict(slug='zika-indonesia',title='Level 3 - Zika in Indonesia',date='Thu, 15 Oct 2026 05:00:00 GMT')]).replace(b'/level2/',b'/level3/')
        f=Fetch({'https://wwwnc.cdc.gov/travel/notices/level3/zika-indonesia':detail().replace(b' - Level 2 - ',b' - Level 3 - ')})
        result=collect('cdc-notices',raw=raw,fetch=f,records={old['id']:old})
        self.assertEqual(len(result['incidents']),1)
        new=result['incidents'][0]
        self.assertEqual(new['state'],'reported');self.assertTrue(new['sourceMetadata']['providerActiveNotice'])
        self.assertEqual(new['sourceMetadata']['providerNoticeLevel'],3)
        self.assertEqual(new['occurredAt'],old['occurredAt']);self.assertEqual(len(new['reports']),2)

    def test_same_url_changed_identity_withholds_prior_claim_and_restores_after_reviewed_return(self):
        old=collect('cdc-notices')['incidents'][0]
        for title in ['Level 2 - Example virus in Indonesia','Level 2 - Global Zika']:
            raw=rss([dict(slug='zika-indonesia',title=title,date='Thu, 15 Oct 2026 05:00:00 GMT')])
            with self.subTest(title=title):
                result=collect('cdc-notices',raw=raw,records={old['id']:old})
                new=result['incidents'][0]
                self.assertEqual((new['state'],new['evidenceStatus']),('expired','unverified'))
                self.assertFalse(new['sourceMetadata']['providerActiveNotice'])
                self.assertEqual(new['occurredAt'],old['occurredAt']);self.assertEqual(len(new['reports']),2)
                self.assertNotIn('admin1Code',new)
                self.assertFalse(collect('cdc-notices',raw=raw,records={new['id']:new})['incidents'])
                restored=rss([dict(slug='zika-indonesia',title='Level 2 - Zika in Indonesia',date='Thu, 15 Oct 2026 06:00:00 GMT')])
                back=collect('cdc-notices',raw=restored,records={new['id']:new})['incidents'][0]
                self.assertTrue(back['sourceMetadata']['providerActiveNotice']);self.assertEqual(back['state'],'reported')

    def test_reviewed_profile_rejects_hypothetical_historical_wrong_disease_or_advice_location(self):
        cases=[
            'Cases of Zika may be reported in Indonesia if an outbreak occurs.',
            'Cases of Zika were reported in Indonesia in 2019.',
            'There is an outbreak of Zika in Costa Rica. Travelers departing Indonesia should be vaccinated.',
            'Cases of Zika are being reported in Indonesia. Travelers departing Bali should be vaccinated.',
            'There is an outbreak of influenza in Indonesia. Zika remains under investigation.',
        ]
        for statement in cases:
            with self.subTest(statement=statement):
                r=collect('cdc-notices',fetch=Fetch({'https://wwwnc.cdc.gov/travel/notices/level2/zika-indonesia':detail(statement=statement)}))
                self.assertEqual(r['incidents'],[]);self.assertEqual(len(r['discoveries']),1)

    def test_still_listed_withdrawal_withholds_old_assertion_and_retains_history(self):
        old=collect('cdc-notices')['incidents'][0]
        f=Fetch({'https://wwwnc.cdc.gov/travel/notices/level2/zika-indonesia':detail(statement='CDC has withdrawn this notice. No current health alert remains.')})
        new=collect('cdc-notices',fetch=f,records={old['id']:old})['incidents'][0]
        self.assertEqual((new['state'],new['evidenceStatus']),('expired','unverified'))
        self.assertFalse(new['sourceMetadata']['providerActiveNotice']);self.assertTrue(new['sourceMetadata']['providerListed'])
        self.assertNotIn('admin1Code',new);self.assertEqual(len(new['reports']),2)
        self.assertEqual(new,collect('cdc-notices',fetch=f,records={new['id']:new})['incidents'][0])

    def test_same_publication_a_b_a_restores_current_a(self):
        a=collect('cdc-notices')['incidents'][0]
        f=Fetch({'https://wwwnc.cdc.gov/travel/notices/level2/zika-indonesia':detail(statement='Cases of Zika have been reported in returning travelers from Bali, Indonesia.')})
        b=collect('cdc-notices',fetch=f,records={a['id']:a})['incidents'][0]
        restored=collect('cdc-notices',records={a['id']:b})['incidents'][0]
        self.assertEqual(restored['summary'],a['summary'])
        self.assertEqual(restored['reports'][0]['contentHash'],a['reports'][0]['contentHash'])
        self.assertEqual(len(restored['reports']),2)
        self.assertEqual(restored,collect('cdc-notices',records={a['id']:restored})['incidents'][0])

    def test_later_publication_keeps_first_anchor_and_rejects_rollback(self):
        a=collect('cdc-notices')['incidents'][0]
        newer=rss([dict(slug='zika-indonesia',title='Level 2 - Zika in Indonesia',date='Thu, 15 Oct 2026 05:00:00 GMT')])
        b=collect('cdc-notices',raw=newer,records={a['id']:a})['incidents'][0]
        self.assertEqual(b['occurredAt'],a['occurredAt']);self.assertEqual(b['firstSeenAt'],a['firstSeenAt'])
        self.assertEqual(b['updatedAt'],'2026-10-15T05:00:00Z')
        self.assertEqual(b,collect('cdc-notices',records={a['id']:b})['incidents'][0])

    def test_explicit_country_and_place_from_key_points(self):
        r=collect('cdc-notices')['incidents'][0]
        self.assertEqual(r['countryCode'],'ID');self.assertEqual(r['admin1Code'],'ID-BALI')
        self.assertEqual(r['location'],{'precision':'area'})
        self.assertEqual(r['sourceMetadata']['providerNoticeLevel'],2)
        self.assertEqual(r['severity'],'unknown');self.assertTrue(r['sourceMetadata']['providerActiveNotice'])
        self.assertEqual(r['occurredAt'],'2026-10-15T04:00:00Z')

    def test_global_and_multicountry_are_not_automatically_geocoded(self):
        for title in ['Level 2 - Global Example','Level 2 - Example in Indonesia and Costa Rica']:
            f=Fetch();r=collect('cdc-notices',raw=rss([dict(slug='global',title=title,date='Thu, 15 Oct 2026 04:00:00 GMT')]),fetch=f)
            self.assertFalse(r['incidents']);self.assertFalse(f.calls);self.assertEqual(len(r['discoveries']),1)

    def test_advice_or_country_in_navigation_is_insufficient(self):
        for statement in ['Travelers to Indonesia should be vaccinated.','There is an outbreak of Zika in Costa Rica.']:
            f=Fetch({'https://wwwnc.cdc.gov/travel/notices/level2/zika-indonesia':detail(statement=statement)})
            self.assertFalse(collect('cdc-notices',fetch=f)['incidents'])

    def test_complete_detail_title_and_structure_required(self):
        for raw in [detail(title='Other virus in Indonesia'),b'<html><head><title>Zika in Indonesia - Level 2</title></head><body>No Key points.</body></html>']:
            with self.subTest(raw=raw),self.assertRaises(ValueError):collect('cdc-notices',fetch=Fetch({'https://wwwnc.cdc.gov/travel/notices/level2/zika-indonesia':raw}))

    def test_cdc_host_delay_is_serialized_from_initial_fetch(self):
        t=[0.0];sleeps=[]
        def sleep(seconds):sleeps.append(seconds);t[0]+=seconds
        two=rss([dict(slug='zika-indonesia',title='Level 2 - Zika in Indonesia',date='Thu, 15 Oct 2026 04:00:00 GMT'),dict(slug='chikungunya-costa-rica',title='Level 2 - Chikungunya in Costa Rica',date='Thu, 15 Oct 2026 05:00:00 GMT')])
        f=Fetch({'https://wwwnc.cdc.gov/travel/notices/level2/chikungunya-costa-rica':detail(title='Chikungunya in Costa Rica',statement='There is an outbreak of chikungunya in northwest Costa Rica.')})
        result=collect('cdc-notices',raw=two,fetch=f,fixtures=None,sleeper=sleep,clock=lambda:t[0])
        self.assertEqual(sleeps,[20,20]);self.assertEqual(len(result['incidents']),2)

    def test_unsafe_citation_or_duplicate_rejects(self):
        for raw in [rss().replace(b'https://wwwnc.cdc.gov/',b'https://evil.invalid/'),rss().replace(b'/zika-indonesia',b'/zika%2findonesia'),rss().replace(b'</channel>',rss().split(b'<item>')[1].split(b'</item>')[0].join([b'<item>',b'</item></channel>']))]:
            with self.subTest(raw=raw),self.assertRaises(ValueError):collect('cdc-notices',raw=raw)

    def test_entity_declarations_reject(self):
        with self.assertRaises(ValueError):collect('cdc-notices',raw=b'<!DOCTYPE rss [<!ENTITY x "bad">]>'+rss())

    def test_bad_or_future_dates_reject(self):
        for date in ['not a date','Fri, 16 Oct 2026 04:00:00 GMT']:
            with self.subTest(date=date),self.assertRaises(ValueError):collect('cdc-notices',raw=rss([dict(slug='bad',title='Level 2 - Zika in Indonesia',date=date)]))

    def test_replay_and_text_correction_no_new_outbreak_time(self):
        old=collect('cdc-notices')['incidents'][0]
        self.assertEqual(old,collect('cdc-notices',records={old['id']:old})['incidents'][0])
        changed=detail(statement='Cases of Zika have been reported in returning travelers from Bali, Indonesia.')
        new=collect('cdc-notices',records={old['id']:old},fetch=Fetch({'https://wwwnc.cdc.gov/travel/notices/level2/zika-indonesia':changed}))['incidents'][0]
        self.assertEqual(new['occurredAt'],old['occurredAt']);self.assertEqual(new['updatedAt'],NOW)
        self.assertEqual(len(new['reports']),2);self.assertEqual(old['updatedAt'],'2026-10-15T04:00:00Z')

    def test_notice_absence_expires_listing_without_claiming_resolution(self):
        old=collect('cdc-notices')['incidents'][0]
        changed=rss([dict(slug='global',title='Level 2 - Global Example',date='Thu, 15 Oct 2026 04:00:00 GMT')])
        new=collect('cdc-notices',raw=changed,records={old['id']:old})['incidents'][0]
        self.assertFalse(new['sourceMetadata']['providerActiveNotice']);self.assertEqual(new['state'],'expired')
        self.assertEqual(new['reports'],old['reports']);self.assertEqual(new['occurredAt'],old['occurredAt'])

    def test_ambiguous_same_name_never_assigns_an_area(self):
        duplicate=AREAS+[dict(id='ID-OTHER',name='Bali',countryCode='ID',level='ADM1')]
        r=collect('cdc-notices',areas=duplicate)['incidents'][0]
        self.assertNotIn('admin1Code',r);self.assertEqual(r['location']['precision'],'country')

    def test_country_name_never_assigns_same_named_state(self):
        areas=[dict(id='MX-MEX',name='Mexico',countryCode='MX',level='ADM1')]
        self.assertEqual(m.cdc_areas('An outbreak is reported in Mexico.','MX',COUNTRIES,areas),{})

    def test_costa_rica_province_prefix_requires_explicit_province(self):
        areas=[dict(id='CR-SJ',name='Provincia San José',countryCode='CR',level='ADM1')]
        self.assertEqual(set(m.cdc_areas('Cases have been reported in San José province in Costa Rica.','CR',COUNTRIES,areas)),{'CR-SJ'})
        self.assertEqual(m.cdc_areas('Cases have been reported in San José city in Costa Rica.','CR',COUNTRIES,areas),{})

    def test_embedded_shorter_area_is_not_another_affected_area(self):
        areas=[dict(id='MX-BC',name='Baja California',countryCode='MX',level='ADM1'),dict(id='MX-BCS',name='Baja California Sur',countryCode='MX',level='ADM1')]
        self.assertEqual(set(m.cdc_areas('Reported in Baja California Sur, Mexico.','MX',COUNTRIES,areas)),{'MX-BCS'})

    def test_negated_outbreak_and_script_text_are_not_incidents(self):
        for statement in ['There is no outbreak of Zika in Indonesia.','Cases of Zika have not been reported in Indonesia.','<script>There is an outbreak of Zika in Indonesia.</script>Routine prevention advice.']:
            f=Fetch({'https://wwwnc.cdc.gov/travel/notices/level2/zika-indonesia':detail(statement=statement)})
            self.assertFalse(collect('cdc-notices',fetch=f)['incidents'])

    def test_reappearing_identical_notice_reopens_listing_with_original_date(self):
        old=collect('cdc-notices')['incidents'][0]
        old['sourceMetadata']['providerActiveNotice']=False;old['state']='expired'
        new=collect('cdc-notices',records={old['id']:old})['incidents'][0]
        self.assertTrue(new['sourceMetadata']['providerActiveNotice']);self.assertEqual(new['state'],'reported')
        self.assertEqual(new['occurredAt'],old['occurredAt']);self.assertEqual(len(new['reports']),1)

    def test_empty_feed_does_not_expire_everything(self):
        with self.assertRaises(ValueError):collect('cdc-notices',raw=b'<rss><channel/></rss>')


if __name__=='__main__':unittest.main()
