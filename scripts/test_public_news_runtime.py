"""Offline admission and rejection checks; factual fixtures are paraphrased.

Complete original publisher responses remain in local audit artifacts. These
small examples exercise their observed structures without republishing articles.
"""
import copy
import datetime as dt
import hashlib
import json
import sys
import unittest
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from public_news_runtime import automatic_news, PARSER, PROFILES, event_day, category_for, news_window
from local_news_archive import parse_wordpress
from public_data_pipeline import digest

class NewsRuntimeTests(unittest.TestCase):
    now='2026-10-15T23:30:00Z'
    countries={code:dict(code=code,name=name,region='Test region') for code,name in
               [('JM','Jamaica'),('BZ','Belize'),('ZM','Zambia'),('GY','Guyana'),('KE','Kenya')]}
    areas=[dict(id='adm1:KE:Garissa',name='Garissa',countryCode='KE',level='ADM1',parentId='country:KE'),
           dict(id='adm1:ZM:Copperbelt',name='Copperbelt',countryCode='ZM',level='ADM1',parentId='country:ZM'),
           dict(id='adm2:ZM:Chingola',name='Chingola',countryCode='ZM',level='ADM2',parentId='adm1:ZM:Copperbelt')]
    def source(self,sid='gov-jm-jcf'):
        p=PROFILES[sid]
        return dict(id=sid,url='https://'+p['host']+'/',name=sid,language='en',countryCodes=[p['country']],tier='official' if sid.startswith('gov-') else 'local')
    def policy(self,sid='gov-jm-jcf'):
        return dict(newsAdapter=PARSER,format='wordpress',url='https://'+PROFILES[sid]['host']+'/wp-json/wp/v2/posts?per_page=100&orderby=modified&order=desc')
    def item(self,title='Fatal shooting in Kingston',body=None,sid='gov-jm-jcf'):
        return dict(title=title,description=body or 'Police are investigating a fatal shooting in Kingston on Tuesday, October 13, 2026. The investigation continues.',
            url='https://'+PROFILES[sid]['host']+'/example/',guid='example',publishedAt='2026-10-14T12:00:00Z',modifiedAt='2026-10-14T12:00:00Z',representationFormat='wordpress-full-content-v1')
    def run_items(self,items,source=None,policy=None,**kwargs):
        return automatic_news(source or self.source(),policy or self.policy(),items,self.now,self.countries,self.areas,**kwargs)
    def test_actual_jcf_structure_preserves_attribution_and_day_precision(self):
        rows,decisions=self.run_items([self.item()]);self.assertEqual(len(rows),1)
        row=rows[0];self.assertEqual(row['category'],'crime');self.assertEqual(row['occurredAt'],'2026-10-13T00:00:00Z')
        self.assertEqual(row['location'],{'precision':'country'});self.assertNotIn('adminAssignment',row)
        self.assertEqual(row['sourceMetadata']['eventDatePrecision'],'day');self.assertEqual(row['aiStatus'],'not-requested')
        self.assertEqual(row['severity'],'unknown');self.assertEqual(row['reports'][0]['excerpt'],'')
        self.assertEqual(row['reports'][0]['contentHash'],digest(self.item()));self.assertEqual(decisions[0]['status'],'automatic-source-record')
    def test_source_and_full_representation_opt_in(self):
        self.assertEqual(self.run_items([self.item()],policy={'format':'wordpress'})[0],[])
        for field,value in [('format','rss-atom'),('newsAdapter','generic-keywords'),('url','https://other.example/wp-json/wp/v2/posts')]:
            policy=self.policy();policy[field]=value
            with self.assertRaises(ValueError):self.run_items([self.item()],policy=policy)
        item=self.item();item.pop('representationFormat');self.assertEqual(self.run_items([item])[1][0]['reason'],'complete-source-body-required')
        with self.assertRaises(ValueError):self.run_items([self.item()],source=dict(self.source(),language='es'))
    def test_no_country_inference_from_publisher_and_no_fabricated_coordinates(self):
        item=self.item(body='Police report a fatal shooting on Tuesday, October 13, 2026. The publisher is located in Kingston.')
        self.assertEqual(self.run_items([item])[0],[])
        rows,_=automatic_news(self.source(),self.policy(),[self.item()],self.now,{},self.areas);self.assertEqual(rows,[])
    def test_country_or_area_only_never_uses_substring_match(self):
        item=self.item(body='A fatal shooting occurred in Kingstonshire on Tuesday, October 13, 2026.')
        self.assertEqual(self.run_items([item])[0],[])
        sid='gov-ke-kenya-broadcasting-corporation'
        item=self.item('Armed conflict in Garissa','An attack by armed militants occurred in Garissa County on Tuesday, October 13, 2026.',sid)
        rows,_=self.run_items([item],source=self.source(sid),policy=self.policy(sid))
        self.assertEqual(rows[0]['admin1Code'],'adm1:KE:Garissa');self.assertEqual(rows[0]['adminAssignment']['method'],'explicit_source_place_names')
    def test_shooting_on_bus_is_crime_not_transport_or_conflict(self):
        title='Fatal shooting on a bus in Kingston'
        body='A fatal shooting on a bus in Kingston killed two people on Tuesday, October 13, 2026.'
        rows,_=self.run_items([self.item(title,body)]);self.assertEqual(rows[0]['category'],'crime')
        self.assertEqual(category_for('Gunfire near a highway','Gunfire near a highway injured a bystander.'),'crime')
    def test_real_event_date_required_and_calendar_checked(self):
        for body in ['Police are investigating a fatal shooting in Kingston.',
                     'A fatal shooting occurred in Kingston on Monday, October 13, 2026.',
                     'A fatal shooting occurred in Kingston on October 32, 2026.',
                     'A fatal shooting occurred in Kingston on October 13 and October 14, 2026.']:
            self.assertEqual(self.run_items([self.item(body=body)])[0],[])
        self.assertEqual(event_day('on Thursday night',dt.datetime(2026,10,14,12,tzinfo=dt.timezone.utc),'America/Belize'),dt.date(2026,10,8))
    def test_negatives_foreign_historical_stats_routine_weather_allegations(self):
        cases=[('International News: Fatal shooting in Kingston','A fatal shooting occurred in Kingston in the United Kingdom on October 13, 2026.'),
               ('Historic decline in murder','A murder occurred in Kingston on October 13, 2026, but this is a statistical review.'),
               ('Accused man faces murder charges','A murder occurred in Kingston on October 13, 2026.'),
               ('Alleged shooting incident in Kingston','A shooting incident occurred in Kingston on October 13, 2026.'),
               ('Thunderstorm in Kingston','Thunderstorms are forecast for Kingston on October 13, 2026.'),
               ('Fatal shooting in Kingston','No shooting incident occurred in Kingston on October 13, 2026.')]
        for title,body in cases:
            with self.subTest(title=title):self.assertEqual(self.run_items([self.item(title,body)])[0],[])
    def test_future_denied_and_simulated_reports_never_become_events(self):
        sid='news-zm-lusaka-times'
        for title,body in [('Power outage in Lusaka','A power outage will occur in Lusaka next Friday for planned maintenance.'),
                           ('Police report no murder in Lusaka','No murder occurred in Lusaka on Tuesday, October 13, 2026.'),
                           ('Power outage in Lusaka','A power outage announced today will start tomorrow in Lusaka.'),
                           ('Murder in Lusaka','A murder did not occur in Lusaka on Tuesday, October 13, 2026.')]:
            self.assertEqual(self.run_items([self.item(title,body,sid)],source=self.source(sid),policy=self.policy(sid))[0],[])
        item=self.item('Shooting incident simulation in Kingston','A shooting incident simulation occurred in Kingston on October 13, 2026.')
        self.assertEqual(self.run_items([item])[0],[])
        self.assertIsNone(event_day('next Friday',dt.datetime(2026,10,14,12,tzinfo=dt.timezone.utc),'America/Jamaica'))

    def test_complete_body_inspected_even_for_late_correction(self):
        item=self.item();item['description']+=' Context. '*10000+' Correction: this account was withdrawn.'
        rows,dec=self.run_items([item]);self.assertEqual(rows,[]);self.assertEqual(dec[0]['reason'],'uncertain-or-corrected-account')
    def test_manual_reviews_including_exclusions_always_win(self):
        for decision in ('include','exclude'):
            review=dict(sourceId='gov-jm-jcf',url=self.item()['url'],decision=decision,approvedItemHash='prior')
            self.assertEqual(self.run_items([self.item()],reviews=[review]),([],[]))
    def test_preexisting_manually_reviewed_archive_record_is_preserved(self):
        old=self.run_items([self.item()])[0][0];old['sourceMetadata'].pop('parser')
        item=self.item();item['description']+=' New details.'
        self.assertEqual(self.run_items([item],existing={old['id']:old}),([],[]))

    def test_revised_or_recategorized_unsupported_account_is_not_left_asserted(self):
        old=self.run_items([self.item()])[0][0]
        for title,body in [('Correction','The publisher withdrew this account.'),('Fatal shooting in Kingston','Correction: the shooting was falsely reported.')]:
            item=self.item(title,body);item['modifiedAt']='2026-10-15T12:00:00Z'
            rows,dec=self.run_items([item],existing={old['id']:old})
            self.assertEqual(rows[0]['id'],old['id']);self.assertEqual(rows[0]['evidenceStatus'],'unverified');self.assertEqual(rows[0]['state'],'expired')
            self.assertTrue(rows[0]['sourceMetadata']['reviewRequired']);self.assertEqual(rows[0]['reports'][0],old['reports'][0]);self.assertEqual(len(rows[0]['reports']),2)
            again,_=self.run_items([item],existing={rows[0]['id']:rows[0]});self.assertEqual(again,[])
    def test_reporting_office_does_not_become_event_location(self):
        sid='news-zm-lusaka-times'
        item=self.item('Acid spill report','Police in Chingola District said an acid spill occurred in Solwezi yesterday.',sid)
        rows,_=self.run_items([item],source=self.source(sid),policy=self.policy(sid))
        self.assertEqual(rows[0]['location'],{'precision':'country'});self.assertEqual(rows[0]['sourceMetadata']['reportedPlace'],'Solwezi')
        self.assertNotIn('admin1Code',rows[0])
        item['description']='Police in Chingola District said an acid spill occurred in London yesterday.'
        self.assertEqual(self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0],[])
        item['description']='An acid spill occurred in Chingola District and Solwezi yesterday.'
        row=self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0][0]
        self.assertNotIn('admin1Code',row);self.assertEqual(row['location'],{'precision':'country'})

    def test_alternating_unsupported_revisions_never_duplicate_report_identity(self):
        old=self.run_items([self.item()])[0][0]
        for text in ['Correction: account withdrawn.','Correction: other version withdrawn.','Correction: account withdrawn.']:
            item=self.item('Correction',text);item['modifiedAt']='2026-10-15T12:00:00Z'
            rows,_=self.run_items([item],existing={old['id']:old});old=rows[0]
        self.assertEqual(len(old['reports']),3);self.assertEqual(len({r['id'] for r in old['reports']}),3)
        self.assertEqual(self.run_items([item],existing={old['id']:old})[0],[])

    def test_transport_restriction_and_infrastructure_actual_source_structures(self):
        sid='news-bz-breaking-belize-news'
        item=self.item('Hummingbird Highway closed for emergency repairs',
            'Today, the ministry closed Mile 17.5 on the Hummingbird Highway after road settlement. Emergency repairs are underway.',sid)
        rows,_=self.run_items([item],source=self.source(sid),policy=self.policy(sid));self.assertEqual(rows[0]['category'],'transport')
        sid='news-gy-demerara-waves'
        item=self.item('Demerara blackout after construction damage',
            'A blackout hit parts of Demerara on Sunday morning after construction machinery damaged transmission cables. Repair crews were deployed.',sid)
        rows,_=self.run_items([item],source=self.source(sid),policy=self.policy(sid));self.assertEqual(rows[0]['category'],'infrastructure')
    def test_unrest_and_chemical_exposure_require_actual_incident(self):
        sid='news-zm-lusaka-times'
        for title,body,category in [('Riot in Chingola','A riot erupted in Chingola District yesterday. Police reported damage.','unrest'),
            ('Acid spill in Solwezi','An acid spill occurred in Solwezi yesterday. The utility closed water intakes.','other')]:
            rows,_=self.run_items([self.item(title,body,sid)],source=self.source(sid),policy=self.policy(sid));self.assertEqual(rows[0]['category'],category)
        item=self.item('Riot in Chingola','A riot erupted in Chingola District yesterday. Another source disputed this account.',sid)
        self.assertEqual(self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0],[])
    def test_unique_administrative_parent_and_country_eligibility(self):
        sid='news-zm-lusaka-times';item=self.item('Riot in Chingola','A riot erupted in Chingola District yesterday.',sid)
        row=self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0][0]
        self.assertEqual(row['admin1Code'],'adm1:ZM:Copperbelt');self.assertEqual(row['admin2Code'],'adm2:ZM:Chingola')
    def test_duplicate_urls_and_future_publication_fail_safely(self):
        with self.assertRaises(ValueError):self.run_items([self.item(),self.item()])
        item=self.item();item['publishedAt']='2027-01-01T00:00:00Z';self.assertEqual(self.run_items([item])[0],[])
    def test_bounded_window_records_and_retains_missed_interval(self):
        items=[dict(self.item(),modifiedAt='2026-10-15T12:00:00Z') for _ in range(100)]
        first=news_window(items,{},self.now);self.assertTrue(first['newsCollectionWindow']['boundedInitialWindow'])
        gap=news_window(items,{'lastSuccessAt':'2026-10-14T00:00:00Z'},self.now)
        self.assertEqual(gap['coverageGap']['from'],'2026-10-14T00:00:00Z')
        self.assertFalse(gap['newsCollectionWindow']['reachesPreviousSuccessfulCheck'])
        recovered=news_window([self.item()],dict(gap,lastSuccessAt='2026-10-15T12:00:00Z'),self.now)
        self.assertEqual(recovered['coverageGap'],gap['coverageGap'])
        with self.assertRaises(ValueError):news_window([self.item(),dict(self.item(),modifiedAt='2026-10-15T12:00:00Z')],{},self.now)

    def test_pipeline_publication_and_corrected_explicit_area_are_preserved(self):
        import public_data_pipeline as pipeline
        sid='gov-ke-kenya-broadcasting-corporation';source=self.source(sid)
        country=dict(self.countries['KE'],status='active',reviewDue='2026-11-04')
        areas=self.areas+[dict(id='adm1:KE:Mandera',name='Mandera',countryCode='KE',level='ADM1',parentId='country:KE')]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);data=root/'docs/data';fixtures=root/'fixtures';fixtures.mkdir()
            pipeline.save(root/'config/countries.json',[country]);pipeline.save(root/'config/sources.json',[source])
            pipeline.save(root/'config/collection-policy.json',{'sources':[dict(self.policy(sid),sourceId=sid,mode='discovery',cadenceMinutes=15)]})
            pipeline.save(root/'config/admin-places.json',areas);pipeline.save(root/'config/country-iso3.json',{})
            pipeline.save(root/'config/geo/index.json',[]);pipeline.save(data/'snapshot.json',dict(coverage=[],history={'from':'2025-10-15T00:00:00Z'}))
            def response(place,modified):
                item=self.item('Fatal shooting reported',f'A fatal shooting occurred in {place} County on Tuesday, October 13, 2026.',sid)
                post=dict(id=1,link=item['url'],status='publish',type='post',date_gmt='2026-10-14T12:00:00',modified_gmt=modified,
                    title={'rendered':item['title']},content={'protected':False,'rendered':item['description']})
                (fixtures/(sid+'.response')).write_text(json.dumps([post]))
            response('Garissa','2026-10-14T12:00:00')
            first=pipeline.run(root,dt.datetime(2026,10,15,23,0,tzinfo=dt.timezone.utc),fixtures)
            self.assertEqual(len(first['changedIncidents']),1)
            row=pipeline.load(data/'history/KE.json')[0];self.assertEqual(row['admin1Code'],'adm1:KE:Garissa')
            response('Mandera','2026-10-15T23:10:00')
            pipeline.run(root,dt.datetime(2026,10,15,23,15,tzinfo=dt.timezone.utc),fixtures)
            revised=pipeline.load(data/'history/KE.json')[0]
            self.assertEqual(revised['admin1Code'],'adm1:KE:Mandera')
            self.assertEqual(revised['firstSeenAt'],row['firstSeenAt']);self.assertEqual(revised['revisions'][0]['previousReports'],row['reports'])
            replay=pipeline.run(root,dt.datetime(2026,10,15,23,30,tzinfo=dt.timezone.utc),fixtures)
            self.assertEqual(replay['changedIncidents'],[])
            self.assertEqual(pipeline.load(data/'snapshot.json')['collector']['newsPromotion'],'review-required')

    def test_complete_wordpress_adapter_no_body_truncation(self):
        text='Complete evidence and context. '*1000
        post=dict(id=1,link=self.item()['url'],status='publish',type='post',date_gmt='2026-10-14T12:00:00',modified_gmt='2026-10-14T12:00:00',title={'rendered':'Test'},content={'protected':False,'rendered':'<p>'+text+'</p>'})
        item=parse_wordpress(json.dumps([post]).encode(),self.source())[0];self.assertEqual(item['description'],text.strip())

    # Factual transformations of complete retained articles, with original URLs
    # in docs/ENGLISH_NEWS_ADMISSION.md. Original publisher bodies stay local.
    def test_fatal_stabbing_is_crime_with_event_place_not_victim_residence(self):
        sid='news-gy-demerara-waves'
        item=self.item('Kairuni man killed in Linden',
            'A man of Kairuni was Saturday night stabbed to death in Amelia’s Ward, Linden. '
            'The Guyana Police Force said it was investigating the fatal incident.',sid)
        row=self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0][0]
        self.assertEqual(row['category'],'crime');self.assertEqual(row['region'],'Linden')
        self.assertEqual(row['occurredAt'],'2026-10-10T00:00:00Z');self.assertEqual(row['location'],{'precision':'country'})
        item['description']='A man of Linden was Saturday night stabbed to death in an unnamed town. Police are investigating.'
        self.assertEqual(self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0],[])
        for body in ['A man was not stabbed to death in Linden on Saturday night.',
                     'A man was allegedly stabbed to death in Linden on Saturday night.']:
            self.assertEqual(self.run_items([dict(item,description=body)],source=self.source(sid),policy=self.policy(sid))[0],[])
        self.assertIsNone(category_for('Man killed in Linden','A man was killed in a workplace accident.'))

    def helicopter(self):
        sid='gov-ke-kenya-broadcasting-corporation'
        return sid,self.item('Five people survive helicopter crash in Kericho',
            'A helicopter has crash-landed at Kericho Golf Club. All passengers survived. '
            'Kenya Red Cross said all persons on board have been taken to hospital for treatment. '
            'The pilot sustained serious injuries.',sid)

    def test_supported_unknown_event_date_is_honest_publication_anchor(self):
        sid,item=self.helicopter();row=self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0][0]
        self.assertEqual(row['category'],'transport');self.assertEqual(row['occurredAt'],item['publishedAt'])
        self.assertEqual(row['sourceMetadata']['eventTimeBasis'],'publication')
        self.assertFalse(row['sourceMetadata']['eventDateKnown']);self.assertEqual(row['sourceMetadata']['eventDatePrecision'],'unknown')
        self.assertIn('does not specify the event date',row['summary']);self.assertEqual(row['severity'],'unknown')
        self.assertEqual(row['location'],{'precision':'country'});self.assertNotIn('adminAssignment',row)
        self.assertEqual(row['reports'][0]['publishedAt'],item['publishedAt'])
        self.assertEqual(row['reports'][0]['contentHash'],digest(item))

    def test_unknown_date_does_not_hide_retrospective_or_uncertain_source_text(self):
        sid,item=self.helicopter()
        for extra in (' This was a historical account.', ' The incident occurred in 2010.',
                      ' The accident happened last year.', ' This happened on October 1, 2020.',
                      ' This happened years ago.', ' This was an emergency drill.',
                      ' A correction withdrew the report.', ' Another source disputed this account.',
                      ' The event occurred in France.'):
            with self.subTest(extra=extra):
                changed=dict(item,description=item['description']+extra)
                self.assertEqual(self.run_items([changed],source=self.source(sid),policy=self.policy(sid))[0],[])
        for replacement in ('A helicopter may have crash-landed at Kericho Golf Club.',
                            'A helicopter will crash-land at Kericho Golf Club.',
                            'No helicopter has crash-landed at Kericho Golf Club.',
                            'It is false that a helicopter has crash-landed at Kericho Golf Club.',
                            'Officials refuted a claim that a helicopter has crash-landed at Kericho Golf Club.'):
            changed=dict(item,description=item['description'].replace('A helicopter has crash-landed at Kericho Golf Club.',replacement))
            self.assertEqual(self.run_items([changed],source=self.source(sid),policy=self.policy(sid))[0],[])
        changed=dict(item,description=item['description'].replace('Kenya Red Cross','An unnamed social account'))
        self.assertEqual(self.run_items([changed],source=self.source(sid),policy=self.policy(sid))[0],[])

    def test_unknown_date_profile_does_not_turn_arrest_speech_or_court_into_event(self):
        sid,item=self.helicopter()
        for title,body in [('Minister discusses helicopter crash in Kericho','A minister discussed a helicopter crash in Kericho. The minister spoke to Kenya Red Cross.'),
                           ('Pilot charged following helicopter crash in Kericho',item['description']),
                           ('Helicopter crash simulation in Kericho',item['description'])]:
            changed=dict(item,title=title,description=body)
            self.assertEqual(self.run_items([changed],source=self.source(sid),policy=self.policy(sid))[0],[])
        # The existing undated generic crime example remains held: there is no
        # blanket replacement of missing occurrence dates with publication dates.
        self.assertEqual(self.run_items([self.item(body='Police report a fatal shooting in Kingston.')])[0],[])

    def test_zambia_emergency_landing_unknown_date_retains_locality_and_attribution(self):
        sid='news-zm-lusaka-times'
        item=self.item('Two pilots survive emergency landing in Nangoma',
            'Two pilots aboard a corporate aircraft survived an emergency landing after the plane developed a fault near offices in Nangoma. '
            'A wildlife officer confirmed that both pilots sustained head injuries. They were evacuated to Lusaka for treatment.',sid)
        row=self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0][0]
        self.assertEqual(row['region'],'Nangoma');self.assertEqual(row['sourceMetadata']['eventTimeBasis'],'publication')
        self.assertEqual(row['evidenceStatus'],'reported');self.assertFalse(row['sourceMetadata']['eventDateKnown'])
        changed=dict(item,description=item['description'].replace('confirmed that both pilots sustained head injuries','said the story was unconfirmed'))
        self.assertEqual(self.run_items([changed],source=self.source(sid),policy=self.policy(sid))[0],[])

    def test_fatal_collision_claimed_life_phrase_keeps_actual_day(self):
        sid='news-bz-breaking-belize-news'
        item=self.item('Man killed, two injured in early morning crash',
            'A fatal road traffic accident on the Philip Goldson Highway in Ladyville claimed the life of a man early this morning. '
            'Two others were injured and are receiving treatment.',sid)
        row=self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0][0]
        self.assertEqual(row['category'],'transport');self.assertEqual(row['occurredAt'],'2026-10-14T00:00:00Z')

    def test_flood_needs_actual_human_impact_and_explicit_relative_day(self):
        sid='news-zm-lusaka-times'
        area=dict(id='adm2:ZM:Lumezi',name='Lumezi',countryCode='ZM',level='ADM2',parentId='adm1:ZM:Eastern')
        parent=dict(id='adm1:ZM:Eastern',name='Eastern',countryCode='ZM',level='ADM1',parentId='country:ZM')
        item=self.item('DMMU evacuates flood victims in Lumezi District',
            'DMMU has evacuated flood victims from Lumezi District following flooding which occurred five days ago. '
            'The victims were stranded by high water levels.',sid)
        rows,_=automatic_news(self.source(sid),self.policy(sid),[item],self.now,self.countries,[area,parent])
        self.assertEqual(rows[0]['category'],'flood');self.assertEqual(rows[0]['occurredAt'],'2026-10-09T00:00:00Z')
        for body in ['Flooding is forecast in Lumezi District; residents may be evacuated tomorrow.',
                     'Flooding occurred in Lumezi District five days ago. No people were displaced or evacuated.',
                     'People may have been evacuated after flooding in Lumezi District five days ago.']:
            self.assertEqual(automatic_news(self.source(sid),self.policy(sid),[dict(item,description=body)],self.now,self.countries,[area,parent])[0],[])
        stamp=dt.datetime(2026,1,2,12,tzinfo=dt.timezone.utc)
        self.assertEqual(event_day('flooding occurred five days ago',stamp,'Africa/Lusaka'),dt.date(2025,12,28))
        self.assertIsNone(event_day('flooding occurred five days ago and on January 1, 2026',stamp,'Africa/Lusaka'))

    def test_belize_robbery_place_is_title_and_same_event_road_not_police_office(self):
        sid='news-bz-breaking-belize-news'
        item=self.item('Armed men rob Orange Walk shop on Palmar Road',
            'Police in Orange Walk are investigating an armed robbery at a shop on Palmar Road on Thursday night. '
            'According to police, masked men entered the shop on October 8.',sid)
        row=self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0][0]
        self.assertEqual(row['region'],'Palmar Road, Orange Walk');self.assertEqual(row['location'],{'precision':'country'})
        self.assertEqual(row['occurredAt'],'2026-10-08T00:00:00Z');self.assertNotIn('adminAssignment',row)
        for changed in (dict(item,title='Police in Orange Walk investigate robbery'),
                        dict(item,description=item['description'].replace('on Palmar Road','on Unknown Road')),
                        dict(item,description=item['description']+' The incident occurred on October 9, 2026.'),
                        dict(item,description=item['description']+' The robbery occurred in Mexico.')):
            self.assertEqual(self.run_items([changed],source=self.source(sid),policy=self.policy(sid))[0],[])

    def test_physical_evidence_can_support_investigation_without_certifying_alleged_actor(self):
        sid='news-bz-breaking-belize-news'
        title='Police are investigating a shooting incident in Belize City after two people allegedly opened fire on Saturday night'
        body='By News Staff: '+title+'. According to police, it happened on October 10. Police recovered one expended 9mm shell casing from the scene. No injuries were reported.'
        item=self.item(title,body,sid);row=self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0][0]
        self.assertEqual(row['category'],'crime');self.assertEqual(row['title'],title)
        self.assertTrue(row['sourceMetadata']['allegationsRetained']);self.assertEqual(row['evidenceStatus'],'reported')
        self.assertIn('Allegations about who fired remain unverified',row['summary'])
        for changed in (dict(item,description=body.replace('Police recovered one expended 9mm shell casing from the scene.','')),
                        dict(item,description=body+' Correction: the report was false.'),
                        dict(item,description=body.replace('Police recovered one','Police did not recover any')),
                        dict(item,title='Suspect charged: '+title),
                        dict(item,description=body+' This was a simulated shooting.')):
            self.assertEqual(self.run_items([changed],source=self.source(sid),policy=self.policy(sid))[0],[])

    def test_new_unknown_date_record_correction_preserves_original_evidence(self):
        sid,item=self.helicopter();old=self.run_items([item],source=self.source(sid),policy=self.policy(sid))[0][0]
        changed=dict(item,description=item['description']+' Correction: the account was withdrawn.',modifiedAt='2026-10-15T12:00:00Z')
        rows,_=self.run_items([changed],source=self.source(sid),policy=self.policy(sid),existing={old['id']:old})
        self.assertEqual(rows[0]['state'],'expired');self.assertEqual(rows[0]['evidenceStatus'],'unverified')
        self.assertEqual(rows[0]['reports'][0],old['reports'][0]);self.assertEqual(rows[0]['occurredAt'],item['publishedAt'])
        self.assertEqual(rows[0]['firstSeenAt'],old['firstSeenAt']);self.assertEqual(len(rows[0]['reports']),2)

if __name__=='__main__':unittest.main()
