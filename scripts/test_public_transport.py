"""Small complete synthetic WordPress documents, modeled on saved FRA responses."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_transport as m

NOW = '2026-10-09T12:00:00Z'
COUNTRIES = {'FJ': dict(code='FJ', name='Fiji', region='Pacific', status='active')}
SOURCE = dict(id=m.SOURCE_ID, countryCodes=['FJ'], language='en')
POLICY = m.prepare_transport_policy(dict(sourceId=m.SOURCE_ID, adapter='fj-fra', mode='official', cadenceMinutes=60), {}, NOW)


def document(identity=1, kind='bridge', **changes):
    bridge = ('The Fiji Roads Authority wishes to advise members of the public that Example Bridge along Example Road in Example Town '
        'will be closed temporarily to carry out emergency deck replacement works. '
        'Bridge Closure will be in place from 9:00pm Friday, 9 October to 7:00am Saturday, 10 October 2026. '
        'There is no alternative diversion route available during the closure.')
    road = ('The Fiji Roads Authority wishes to advise members of the public that Example Road in Example Town '
        'will be temporarily closed to allow for the replacement of a dislocated and broken culvert along the road. '
        'The road will be closed on Sunday, 11 October 2026, from 9:00am to 4:00pm. '
        'During the culvert replacement works, vehicle access will be prohibited, and a pedestrian bypass will be provided.')
    value = dict(id=identity, type='post', status='publish', date_gmt='2026-10-04T23:25:25',
        modified_gmt='2026-10-05T04:52:10', categories=[26, 3],
        link=m.API.split('/wp-json')[0]+'/2026/10/04/example-'+str(identity)+'/',
        title={'rendered':'Emergency closure of Example '+('Bridge' if kind=='bridge' else 'Road')+', Example Town'},
        content={'rendered':'<p>'+(bridge if kind=='bridge' else road)+'</p>', 'protected':False})
    value.update(changes)
    return value


class Fetch:
    def __init__(self, rows, exact=None, overrides=None):
        self.rows, self.exact, self.overrides, self.calls = rows, exact or {}, overrides or {}, []

    def __call__(self, request, *args):
        url = request['url']; self.calls.append(url)
        if url in self.overrides:
            value = self.overrides[url]
            if value is False:
                return dict(httpStatus=503), None
        elif '?' in url:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            field = 'date_gmt' if q['orderby']==['date'] else 'modified_gmt'
            rows = sorted(self.rows, key=lambda r:r[field], reverse=True)
            page = int(q.get('page',['1'])[0]); size = int(q['per_page'][0])
            value = rows[(page-1)*size:page*size]
        else:
            identity = int(url.rsplit('/',1)[1])
            value = self.exact.get(identity, next((r for r in self.rows if r['id']==identity), None))
            if value is None:
                return dict(httpStatus=404), json.dumps({'code':'rest_post_invalid_id'}).encode()
            if value is False:
                return dict(httpStatus=503), None
        return dict(httpStatus=200), value if isinstance(value,bytes) else json.dumps(value).encode()


def collect(rows=None, now=NOW, previous=None, records=None, fetch=None, **kw):
    rows = [document()] if rows is None else rows
    f = fetch or Fetch(rows)
    policy = m.prepare_transport_policy(POLICY, previous or {}, now)
    _, raw = f(policy)
    return m.collect_transport(SOURCE,policy,raw,now,COUNTRIES,previous or {},records or {},f,
                               fixtures=True,history_from='2025-10-09T00:00:00Z',**kw)


class TransportTests(unittest.TestCase):
    def test_actual_template_timezone_dates_and_country_only_geometry(self):
        result=collect();row=result['incidents'][0];meta=row['sourceMetadata']
        self.assertEqual(row['occurredAt'],'2026-10-09T09:00:00Z')
        self.assertEqual(meta['effectiveUntil'],'2026-10-09T19:00:00Z')
        self.assertEqual(row['reports'][0]['publishedAt'],'2026-10-04T23:25:25Z')
        self.assertEqual(row['reports'][0]['title'],document()['title']['rendered'])
        self.assertEqual(row['reports'][0]['sourceMetadata']['providerModifiedAt'],'2026-10-05T04:52:10Z')
        self.assertEqual((row['state'],row['severity'],row['category']),('reported','unknown','transport'))
        self.assertEqual(row['location'],{'precision':'country'})
        self.assertNotIn('admin1Code',row);self.assertNotIn('admin2Code',row)
        self.assertTrue(meta['noDiversion']);self.assertFalse(meta['fieldConditionIndependentlyVerified'])

    def test_culvert_all_vehicle_prohibition_and_pedestrian_bypass(self):
        row=collect([document(kind='road')],now='2026-10-11T00:00:00Z')['incidents'][0]
        self.assertEqual(row['occurredAt'],'2026-10-10T21:00:00Z')
        self.assertEqual(row['sourceMetadata']['effectiveUntil'],'2026-10-11T04:00:00Z')
        self.assertTrue(row['sourceMetadata']['pedestrianBypass'])

    def test_future_is_pending_then_exact_recheck_admits_at_start(self):
        first=collect(now='2026-10-08T12:00:00Z')
        self.assertEqual(first['incidents'],[]);self.assertEqual(first['state']['transportPendingPostIds'],[1])
        f=Fetch([],exact={1:document()})
        second=collect([],previous=first['state'],fetch=f,now='2026-10-09T09:00:00Z')
        self.assertEqual(second['incidents'][0]['state'],'reported');self.assertIn(m.API+'/1',f.calls)

    def test_end_is_expiry_not_reopening_and_cms_date_is_ignored(self):
        d=document(meta={'publishpress_future_action':{'enabled':False,'date':'2026-10-22T00:00:00'}})
        r=collect([d],now='2026-10-16T00:00:00Z')['incidents'][0]
        self.assertEqual(r['state'],'expired');self.assertNotIn('reopenedAt',r['sourceMetadata'])
        self.assertEqual(r['sourceMetadata']['effectiveUntil'],'2026-10-09T19:00:00Z')

    def test_cancelled_pending_notice_stops_pending_rechecks_without_incident(self):
        first=collect(now='2026-10-08T12:00:00Z')
        changed=document();changed['content']['rendered']+='<p>The closure is cancelled.</p>'
        result=collect([],now='2026-10-08T13:00:00Z',previous=first['state'],fetch=Fetch([],exact={1:changed}))
        self.assertFalse(result['incidents']);self.assertFalse(result['state']['transportPendingPostIds'])

    def test_ordinary_partial_uncertain_and_image_only_stay_out(self):
        bodies=[
            '<p>Routine milling and resurfacing with one lane open.</p>',
            '<p>Bridge closure details are in this image.</p><img src="https://example.org/map.png">',
            document()['content']['rendered'].replace('emergency deck replacement','routine resurfacing'),
            document()['content']['rendered']+'<p>The closure is postponed.</p>',
            document()['content']['rendered']+'<p>The bridge will not be closed.</p>',
            document()['content']['rendered']+'<p>Only a single lane is affected.</p>',
            document()['content']['rendered']+'<p>This is a nightly recurring closure.</p>',
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(collect([document(content={'rendered':body,'protected':False})])['incidents'],[])

    def test_contradiction_at_end_of_complete_large_text_is_not_truncated(self):
        body=document()['content']['rendered']+'<p>'+'context '*20000+'</p><p>The closure is cancelled.</p>'
        self.assertFalse(collect([document(content={'rendered':body,'protected':False})])['incidents'])

    def test_bad_weekday_year_time_and_multiple_intervals_stay_review_only(self):
        body=document()['content']['rendered']
        for text in [body.replace('Friday,','Thursday,'),body.replace('2026.','.'),
                     body.replace('9:00pm','29:00pm'),body.replace('Saturday, 10 October','Friday, 9 October'),
                     body+body]:
            self.assertFalse(collect([document(content={'rendered':text,'protected':False})])['incidents'])

    def test_missing_prohibition_culvert_is_not_automatically_admitted(self):
        row=document(kind='road');row['content']['rendered']=row['content']['rendered'].replace('vehicle access will be prohibited','one lane will remain open')
        self.assertFalse(collect([row],now='2026-10-11T00:00:00Z')['incidents'])

    def test_reviewed_deterioration_full_closures_with_and_without_detours(self):
        for off, emergency, prohibition in [(' off Main Road,','emergency ',''),('', '', 'Access over the bridge will be prohibited and a bypass provided.')]:
            body=('The authority advises the public that the Example Bridge 2 along Example Road,'+off+
                ' will be temporarily closed to allow for '+emergency+'repair and improvement works following accelerated deterioration. '
                'The bridge will be closed from 9:00am Friday, 9 October 2026 to 5:00pm Saturday, 10 October 2026. '+prohibition)
            d=document(title={'rendered':'Temporary closure – Example Bridge 2'},content={'protected':False,'rendered':body})
            r=collect([d],now='2026-10-11T00:00:00Z')['incidents'][0]
            self.assertEqual(r['sourceMetadata']['geographicContextKind'],'road_reference')
            self.assertEqual(r['location'],{'precision':'country'})
            self.assertEqual(r['sourceMetadata']['effectiveUntil'],'2026-10-10T05:00:00Z')
            no_impact=copy.deepcopy(d);no_impact['content']['rendered']=body.replace('following accelerated deterioration','for routine improvements')
            self.assertFalse(collect([no_impact],now='2026-10-11T00:00:00Z')['incidents'])

    def test_reviewed_urgent_deterioration_interval_keeps_weather_dependency(self):
        body=('The authority advises the public that Example Bridge 1 along Example Road in Example Town '
            'will be temporarily closed from 9:00pm on Friday, 9 October to 6:00am on Saturday, 10 October 2026 '
            'to facilitate urgent deck repair works due to accelerated deterioration. An alternative route will be available. '
            'Please note that all works are weather-dependent.')
        d=document(title={'rendered':'Example Bridge 1 Example Town – emergency closure'},content={'protected':False,'rendered':body})
        row=collect([d])['incidents'][0]
        self.assertEqual(row['sourceMetadata']['effectiveUntil'],'2026-10-09T18:00:00Z')
        self.assertTrue(row['sourceMetadata']['worksWeatherDependent'])
        d['content']['rendered']+=' The works may be postponed and rescheduled.'
        self.assertFalse(collect([d])['incidents'])

    def test_scheduled_full_bridge_closure_is_material_even_without_emergency(self):
        body=('The authority advises the public that Example Bridge along Example Road off Main Road '
            'will be temporarily closed to carry out repair and improvement works. '
            'Bridge Closure will be in place from 7:00pm Friday 9th October to 7:00am Saturday, 10th October 2026. '
            'Pedestrian access will be maintained throughout the repair works.')
        d=document(title={'rendered':'Example Bridge, off Main Road – scheduled temporary closure'},
            content={'protected':False,'rendered':body})
        row=collect([d])['incidents'][0]
        self.assertEqual(row['occurredAt'],'2026-10-09T07:00:00Z')
        self.assertEqual(row['sourceMetadata']['effectiveUntil'],'2026-10-09T19:00:00Z')
        self.assertTrue(row['sourceMetadata']['pedestrianAccessMaintained'])
        self.assertIn('Pedestrian access was to be maintained.',row['summary'])
        self.assertEqual(row['location'],{'precision':'country'})
        for change in [body+' Only one lane will close.',body+' The closure is cancelled.',
                       body.replace('will be temporarily closed','will remain open'),
                       body.replace('Saturday, 10th October 2026','Friday, 10th October 2026')]:
            d['content']['rendered']=change
            self.assertFalse(collect([d])['incidents'])

    def test_unknown_canonical_host_bad_dates_and_incomplete_body_fail_atomically(self):
        for changes in [dict(link='https://evil.example/notice'),dict(modified_gmt='2026-10-20T00:00:00'),
                        dict(date_gmt='2026-10-06T00:00:00'),dict(content={'rendered':'body'})]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):collect([document(**changes)])

    def test_no_copied_article_bodies_or_contact_details_in_public_record(self):
        d=document();d['content']['rendered']+='<p>CONTACT-SENTINEL full lengthy terms information.</p>'
        result=collect([d]);encoded=json.dumps(result)
        self.assertNotIn('CONTACT-SENTINEL',encoded);self.assertNotIn('rendered',encoded)
        self.assertEqual(result['incidents'][0]['reports'][0]['contentHash'],m.post(d,NOW)['hash'])

    def test_same_post_extension_preserves_old_evidence_and_first_seen(self):
        old=collect()['incidents'][0];d=document();d['modified_gmt']='2026-10-09T13:00:00'
        d['content']['rendered']=d['content']['rendered'].replace('7:00am','8:00am')
        row=collect([d],now='2026-10-09T14:00:00Z',records={old['id']:old})['incidents'][0]
        self.assertEqual(row['occurredAt'],old['occurredAt']);self.assertEqual(row['firstSeenAt'],old['firstSeenAt'])
        self.assertEqual(row['reports'][1],old['reports'][0]);self.assertEqual(row['sourceMetadata']['effectiveUntil'],'2026-10-09T20:00:00Z')

    def test_changed_asset_interval_title_or_removed_evidence_ends_prior_assertion(self):
        old=collect()['incidents'][0]
        for change in ('asset','interval','title','body','cancelled','protected','unpublished'):
            d=document();d['modified_gmt']='2026-10-09T13:00:00'
            if change=='asset':
                d['content']['rendered']=d['content']['rendered'].replace('Example Bridge','Other Bridge');d['title']['rendered']=d['title']['rendered'].replace('Example Bridge','Other Bridge')
            elif change=='interval':d['content']['rendered']=d['content']['rendered'].replace('9:00pm','10:00pm')
            elif change=='title':d['title']['rendered']='Another bridge closure'
            elif change=='body':d['content']['rendered']='<p>Details removed pending review.</p>'
            elif change=='cancelled':d['content']['rendered']+='<p>This closure is cancelled.</p>'
            elif change=='protected':d['content']['protected']=True
            else:d['status']='draft'
            with self.subTest(change=change):
                row=collect([d],now='2026-10-09T14:00:00Z',records={old['id']:old})['incidents'][0]
                self.assertEqual((row['state'],row['evidenceStatus']),('expired','unverified'))
                self.assertEqual(row['reports'][1],old['reports'][0]);self.assertNotIn('reopenedAt',row['sourceMetadata'])
                self.assertEqual(row['reports'][0]['title'],d['title']['rendered'])

    def test_a_b_a_same_modified_timestamp_restores_current_source(self):
        old=collect()['incidents'][0];d=document();d['content']['rendered']='<p>Closure details withdrawn.</p>'
        changed=collect([d],now='2026-10-09T13:00:00Z',records={old['id']:old})['incidents'][0]
        restored=collect(now='2026-10-09T14:00:00Z',records={changed['id']:changed})['incidents'][0]
        self.assertEqual(restored['state'],'reported');self.assertEqual(restored['reports'][0]['contentHash'],old['reports'][0]['contentHash'])
        self.assertEqual(len(restored['reports']),2)
        again=collect(now='2026-10-09T15:00:00Z',records={restored['id']:restored})['incidents'][0]
        self.assertEqual(again,restored)

    def test_older_provider_modification_cannot_replace_latest(self):
        old=collect()['incidents'][0];d=document(modified_gmt='2026-10-05T04:52:09')
        with self.assertRaisesRegex(ValueError,'provider modification moved backwards'):
            collect([d],records={old['id']:old})

    def test_exact_404_withholds_but_does_not_claim_reopened_and_can_restore(self):
        old=collect()['incidents'][0]
        gone=collect([],records={old['id']:old},now='2026-10-09T13:00:00Z')['incidents'][0]
        self.assertEqual((gone['state'],gone['evidenceStatus']),('expired','unverified'))
        self.assertEqual(gone['reports'][1],old['reports'][0])
        self.assertEqual(gone['reports'][0]['title'],'Fiji Roads Authority post unavailable')
        self.assertNotIn('providerModifiedAt',gone['reports'][0]['sourceMetadata'])
        restored=collect(records={gone['id']:gone},now='2026-10-09T14:00:00Z')['incidents'][0]
        self.assertEqual(restored['state'],'reported')

    def test_outage_is_not_deletion_or_reopening(self):
        old=collect()['incidents'][0]
        with self.assertRaisesRegex(ValueError,'unavailable'):
            collect([],records={old['id']:old},fetch=Fetch([],exact={1:False}))
        self.assertEqual(old['state'],'reported')

    def test_duplicate_same_episode_merges_but_unrelated_repeated_asset_does_not(self):
        a=document();b=document(2);b['modified_gmt']='2026-10-06T00:00:00'
        result=collect([b,a]);self.assertEqual(len(result['incidents']),1)
        row=result['incidents'][0];self.assertEqual(row['sourceMetadata']['recordIds'],[1,2]);self.assertEqual(len(row['reports']),2)

    def reopened(self, identity=1, wording='has reopened', time='Friday, 9 October 2026 at 11:00pm', extra=''):
        return document(identity,modified_gmt='2026-10-09T12:30:00',content={'protected':False,
            'rendered':'<p>Example Bridge along Example Road in Example Town '+wording+' to all traffic on '+time+'.</p>'+extra})

    def test_same_post_explicit_all_traffic_reopening_resolves_exact_episode(self):
        old=collect()['incidents'][0]
        row=collect([self.reopened()],now='2026-10-09T13:00:00Z',records={old['id']:old})['incidents'][0]
        self.assertEqual(row['state'],'resolved');self.assertEqual(row['sourceMetadata']['reopenedAt'],'2026-10-09T11:00:00Z')
        self.assertEqual(row['reports'][1],old['reports'][0]);self.assertEqual(row['occurredAt'],old['occurredAt'])

    def test_unlinked_new_post_reopening_cannot_resolve_prior_closure(self):
        old=collect()['incidents'][0];new=self.reopened(2)
        row=collect([new,document()],now='2026-10-09T13:00:00Z',records={old['id']:old})['incidents'][0]
        self.assertEqual(row['state'],'reported')

    def test_linked_new_post_reopening_preserves_one_episode(self):
        old=collect()['incidents'][0];new=self.reopened(2,extra='<a href="'+old['reports'][0]['url']+'">Earlier closure</a>')
        result=collect([new,document()],now='2026-10-09T13:00:00Z',records={old['id']:old})
        self.assertEqual(len(result['incidents']),1);row=result['incidents'][0]
        self.assertEqual(row['state'],'resolved');self.assertEqual(row['sourceMetadata']['recordIds'],[1,2])

    def resolved_episode(self):
        old=collect()['incidents'][0]
        reopened=self.reopened(2,extra='<a href="'+old['reports'][0]['url']+'">Earlier closure</a>')
        result=collect([reopened,document()],now='2026-10-09T13:00:00Z',records={old['id']:old})
        return result['incidents'][0],result['state']

    def test_rotating_original_post_check_preserves_separate_reopening(self):
        old,previous=self.resolved_episode()
        fetch=Fetch([],exact={1:document()})
        with patch.object(m,'EXACT_LIMIT',1):
            result=collect([],previous=previous,records={old['id']:old},fetch=fetch,now='2026-10-09T14:00:00Z')
        self.assertEqual(result['incidents'][0],old)
        self.assertNotIn(m.API+'/2',fetch.calls)

    def test_older_new_duplicate_adds_evidence_without_undoing_reopening(self):
        old,_=self.resolved_episode()
        duplicate=document(3,modified_gmt='2026-10-06T00:00:00')
        with patch.object(m,'EXACT_LIMIT',1):
            result=collect([duplicate],records={old['id']:old},fetch=Fetch([duplicate],exact={1:document()}),now='2026-10-09T14:00:00Z')
        row=result['incidents'][0]
        self.assertEqual((row['state'],row['summary'],row['occurredAt']),('resolved',old['summary'],old['occurredAt']))
        self.assertEqual(row['sourceMetadata']['reopenedAt'],old['sourceMetadata']['reopenedAt'])
        self.assertEqual(row['sourceMetadata']['recordIds'],[1,2,3])
        self.assertEqual(len(row['reports']),3)
        self.assertTrue({r['contentHash'] for r in old['reports']} <= {r['contentHash'] for r in row['reports']})

    def test_changed_interval_after_reopening_requires_review_without_reactivation(self):
        old,previous=self.resolved_episode()
        changed=document(modified_gmt='2026-10-09T13:30:00')
        changed['content']['rendered']=changed['content']['rendered'].replace('7:00am','8:00am')
        with patch.object(m,'EXACT_LIMIT',1):
            row=collect([changed],previous=previous,records={old['id']:old},now='2026-10-09T14:00:00Z')['incidents'][0]
        self.assertEqual((row['state'],row['evidenceStatus']),('expired','unverified'))
        self.assertEqual(row['sourceMetadata']['automaticEvidenceState'],'closure_changed_after_reopening_requires_review')
        self.assertTrue({r['contentHash'] for r in old['reports']} <= {r['contentHash'] for r in row['reports']})

    def test_old_duplicate_cannot_reverse_later_withdrawal_on_different_post(self):
        duplicate=document(2,modified_gmt='2026-10-06T00:00:00')
        initial=collect([document(),duplicate])['incidents'][0]
        withdrawal=document(2,modified_gmt='2026-10-09T13:00:00',content={'protected':False,'rendered':'<p>Closure details withdrawn.</p>'})
        withdrawn=collect([document(),withdrawal],records={initial['id']:initial},now='2026-10-09T14:00:00Z')['incidents'][0]
        self.assertEqual(withdrawn['evidenceStatus'],'unverified')
        another=document(3,modified_gmt='2026-10-07T00:00:00')
        with patch.object(m,'EXACT_LIMIT',1):
            row=collect([another],records={withdrawn['id']:withdrawn},fetch=Fetch([another],exact={1:document()}),now='2026-10-09T15:00:00Z')['incidents'][0]
        self.assertEqual((row['state'],row['evidenceStatus'],row['summary']),('expired','unverified',withdrawn['summary']))
        self.assertIn(3,row['sourceMetadata']['recordIds'])
        self.assertTrue({r['contentHash'] for r in withdrawn['reports']} <= {r['contentHash'] for r in row['reports']})

    def test_forecast_restricted_future_wrong_place_reopening_does_not_resolve(self):
        old=collect()['incidents'][0]
        examples=[self.reopened(wording='will reopen'),self.reopened(extra='<p>Weight restrictions remain.</p>'),
                  self.reopened(time='Saturday, 10 October 2026 at 11:00pm'),self.reopened(extra='<p>The bridge remains closed.</p>')]
        bad=self.reopened();bad['content']['rendered']=bad['content']['rendered'].replace('Example Town','Other Town');examples.append(bad)
        for new in examples:
            row=collect([new],now='2026-10-09T13:00:00Z',records={old['id']:old})['incidents'][0]
            self.assertEqual((row['state'],row['evidenceStatus']),('expired','unverified'))

    def test_baseline_resumes_from_ordered_overlap_without_skips(self):
        rows=[document(i, date_gmt=f'2026-10-0{i}T00:00:00',modified_gmt=f'2026-10-0{i}T00:00:00') for i in range(1,6)]
        with patch.object(m,'PAGE_SIZE',2),patch.object(m,'BASELINE_PAGES',1):
            first=collect(rows,now='2026-10-16T00:00:00Z')
            self.assertFalse(first['state']['transportBaseline']['complete'])
            self.assertEqual(first['state']['transportBaseline']['nextPage'],2)
            second=collect(rows,now='2026-10-16T01:00:00Z',previous=first['state'])
            self.assertEqual(second['state']['transportBaseline']['nextPage'],3)
            third=collect(rows,now='2026-10-16T02:00:00Z',previous=second['state'])
            self.assertTrue(third['state']['transportBaseline']['complete'])

    def test_changed_overlap_restarts_baseline_and_stays_incomplete(self):
        rows=[document(i,date_gmt=f'2026-10-0{i}T00:00:00',modified_gmt=f'2026-10-0{i}T00:00:00') for i in range(1,6)]
        with patch.object(m,'PAGE_SIZE',2),patch.object(m,'BASELINE_PAGES',1):
            first=collect(rows,now='2026-10-16T00:00:00Z')
            rows.append(document(6,date_gmt='2026-10-06T00:00:00',modified_gmt='2026-10-06T00:00:00'))
            second=collect(rows,now='2026-10-16T01:00:00Z',previous=first['state'])
            self.assertTrue(second['state']['transportBaseline']['resetForChangedAnchor'])
            self.assertFalse(second['state']['transportBaseline']['complete']);self.assertEqual(second['state']['transportBaseline']['nextPage'],2)

    def test_live_ceiling_does_not_advance_checkpoint_or_publish_prefix(self):
        rows=[document(i,modified_gmt='2026-10-09T10:00:00') for i in range(1,6)]
        with patch.object(m,'PAGE_SIZE',2),patch.object(m,'LIVE_PAGES',2),self.assertRaisesRegex(ValueError,'backlog'):
            collect(rows)

    def test_pagination_duplicate_order_and_missing_later_page_fail_atomically(self):
        rows=[document(i,modified_gmt='2026-10-09T10:00:00') for i in range(1,4)]
        with patch.object(m,'PAGE_SIZE',2):
            for second in [False,[rows[0]], [document(4,modified_gmt='2026-10-09T11:00:00')]]:
                with self.subTest(second=second),self.assertRaises(ValueError):
                    collect(rows,fetch=Fetch(rows,overrides={m.endpoint(page=2):second}))

    def test_full_response_byte_budget_and_bad_provider_identity_fail(self):
        with patch.object(m,'MAX_RESPONSE',10),self.assertRaisesRegex(ValueError,'budget'):collect()
        with self.assertRaisesRegex(ValueError,'identity'):
            m.collect_transport(dict(SOURCE,id='other'),POLICY,b'[]',NOW,COUNTRIES,{}, {},Fetch([]))


if __name__=='__main__':unittest.main()
