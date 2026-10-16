"""Offline archive pagination tests. No provider or collector state is changed."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import unittest
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_news_backfill as archive


NOW = '2026-10-16T04:00:00Z'
LATER = '2026-10-16T05:00:00Z'
WINDOW = {'from':'2025-10-16T00:00:00Z', 'through':'2026-10-16T00:00:00Z'}


def profile(source_id='gov-jm-jcf'):
    reviewed = archive.PROFILES[source_id]
    origin = 'https://' + reviewed['host']
    url = origin + '/wp-json/wp/v2/posts?per_page=100&orderby=modified&order=desc'
    source = dict(id=source_id, url=origin+'/', feedUrl=url, enabled=True, language='en',
        countryCodes=[reviewed['country']], kind='rss', access='public-feed',
        tier='official' if source_id.startswith('gov-') else 'local-news')
    policy = dict(sourceId=source_id, url=url, format='wordpress', mode='discovery',
        newsAdapter=archive.PARSER, cadenceMinutes=15, wordpressHistory=copy.deepcopy(WINDOW))
    return source,policy


def post(index, published=None, body=None, host='jcf.gov.jm'):
    published = published or (dt.datetime(2026,10,15,12)-dt.timedelta(minutes=index)).isoformat()
    return dict(id=index, link='https://'+host+'/article-'+str(index)+'/', status='publish', type='post',
        title={'rendered':'Complete source headline '+str(index)},
        content={'protected':False, 'rendered':body or '<p>Complete retained source evidence '+str(index)+'.</p>'},
        date_gmt=published, modified_gmt=published)


class Fetch:
    def __init__(self, pages=None, total=102):
        self.pages = pages if pages is not None else {1:[post(i) for i in range(1,101)], 2:[post(101),post(102)]}
        self.total = total
        self.calls = []
        self.override = None

    def __call__(self, policy, previous, now, fixtures):
        self.calls.append(copy.deepcopy((policy,previous,now,fixtures)))
        page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(policy['url']).query)['page'][0])
        raw = json.dumps(self.pages[page],ensure_ascii=False).encode()
        result = dict(httpStatus=200, finalUrl=policy['url'],
            wordpressPagination={'total':self.total, 'pages':(self.total+99)//100})
        if self.override:
            return self.override(page,result,raw)
        return result,raw


class WordPressHistoryTests(unittest.TestCase):
    def setUp(self):
        self.source,self.policy = profile()

    def first(self, fetch=None):
        return archive.advance(self.source,self.policy,None,NOW,fetch or Fetch())

    def assert_unchanged_failure(self, fetch, previous=None, exception=ValueError):
        before = copy.deepcopy((self.source,self.policy,previous))
        with self.assertRaises(exception):
            archive.advance(self.source,self.policy,previous,LATER,fetch)
        self.assertEqual((self.source,self.policy,previous),before)

    def test_two_page_resume_checks_anchor_and_retains_exact_evidence_receipts(self):
        fetch = Fetch()
        first = self.first(fetch)
        self.assertEqual(first['requested'],1)
        self.assertEqual(first['checkpoint']['pagesAccepted'],1)
        self.assertEqual(first['checkpoint']['windowPosts'],100)
        self.assertFalse(first['checkpoint']['complete'])
        before = copy.deepcopy(first['checkpoint'])
        resumed = archive.advance(self.source,self.policy,first['checkpoint'],LATER,fetch)
        self.assertEqual(first['checkpoint'],before)
        self.assertEqual(resumed['requested'],2)
        self.assertEqual([r['page'] for r in resumed['receipts']],[1,2])
        self.assertEqual(len(resumed['items']),102)
        self.assertEqual(resumed['checkpoint']['seenIds'],[str(i) for i in range(1,103)])
        self.assertEqual(resumed['checkpoint']['windowPosts'],102)
        self.assertTrue(resumed['checkpoint']['complete'])
        self.assertEqual(resumed['checkpoint']['completedAt'],LATER)
        for receipt in resumed['receipts']:
            raw=json.dumps(fetch.pages[receipt['page']],ensure_ascii=False).encode()
            self.assertEqual(receipt['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(receipt['bytes'],len(raw))

    def test_complete_checkpoint_does_not_make_http_request(self):
        done=self.first(Fetch({1:[post(1)]},1))['checkpoint']
        def forbidden(*args):
            self.fail('Completed archive must not request another page')
        result=archive.advance(self.source,self.policy,done,LATER,forbidden)
        self.assertEqual(result,{'items':[],'checkpoint':done,'receipts':[],'requested':0})
        self.assertIsNot(result['checkpoint'],done)

    def test_complete_anchor_body_correction_is_returned_even_without_id_or_date_change(self):
        fetch=Fetch()
        prior=self.first(fetch)['checkpoint']
        original=copy.deepcopy(prior)
        full_body='Correction with complete evidence. '*8000+'Distinct final sentence.'
        fetch.pages[1][0]['content']['rendered']='<p>'+full_body+'</p>'
        fetch.pages[1][0]['modified_gmt']='2026-10-16T03:00:00'
        result=archive.advance(self.source,self.policy,prior,LATER,fetch)
        corrected=next(row for row in result['items'] if row['guid']=='1')
        self.assertEqual(corrected['description'],full_body)
        self.assertEqual(corrected['modifiedAt'],'2026-10-16T03:00:00Z')
        self.assertEqual(result['checkpoint']['generation'],1)
        self.assertEqual(prior,original)

    def test_changed_anchor_ids_or_dates_restart_without_mutating_accepted_state(self):
        for field,value in [('id',900),('date_gmt','2026-10-15T11:59:01')]:
            with self.subTest(field=field):
                fetch=Fetch();prior=self.first(fetch)['checkpoint'];before=copy.deepcopy(prior)
                fetch.pages[1][0][field]=value
                if field=='date_gmt':
                    fetch.pages[1][0]['modified_gmt']=value
                result=archive.advance(self.source,self.policy,prior,LATER,fetch)
                self.assertEqual(result['items'],[])
                self.assertEqual(result['requested'],1)
                self.assertEqual(result['checkpoint']['status'],'restart-required')
                self.assertEqual(result['checkpoint']['generation'],2)
                self.assertEqual(result['checkpoint']['seenIds'],[])
                self.assertEqual(prior,before)
                restarted=archive.advance(self.source,self.policy,result['checkpoint'],LATER,fetch)
                self.assertEqual(restarted['checkpoint']['pagesAccepted'],1)

    def test_changed_totals_at_anchor_restart_before_new_page(self):
        fetch=Fetch();prior=self.first(fetch)['checkpoint'];before=copy.deepcopy(prior)
        fetch.total=103
        result=archive.advance(self.source,self.policy,prior,LATER,fetch)
        self.assertEqual(result['requested'],1)
        self.assertEqual(result['items'],[])
        self.assertEqual(result['checkpoint']['pagesAccepted'],0)
        self.assertEqual(prior,before)

    def test_changed_totals_between_anchor_and_new_page_restart(self):
        fetch=Fetch();prior=self.first(fetch)['checkpoint'];before=copy.deepcopy(prior)
        fetch.pages[2].append(post(103))
        def changed(page,result,raw):
            if page==2:
                result['wordpressPagination']['total']=103
            return result,raw
        fetch.override=changed
        result=archive.advance(self.source,self.policy,prior,LATER,fetch)
        self.assertEqual(result['requested'],2)
        self.assertEqual(result['items'],[])
        self.assertEqual(result['checkpoint']['status'],'restart-required')
        self.assertEqual(prior,before)

    def test_repeated_identity_on_next_page_restarts(self):
        fetch=Fetch();prior=self.first(fetch)['checkpoint'];before=copy.deepcopy(prior)
        fetch.pages[2][0]['id']=1
        result=archive.advance(self.source,self.policy,prior,LATER,fetch)
        self.assertEqual(result['checkpoint']['status'],'restart-required')
        self.assertEqual(result['requested'],2)
        self.assertEqual(result['items'],[])
        self.assertEqual(prior,before)

    def test_failed_second_fetch_does_not_commit_anchor_or_progress(self):
        fetch=Fetch();prior=self.first(fetch)['checkpoint']
        def unavailable(page,result,raw):
            if page==2:
                raise TimeoutError('complete response unavailable')
            return result,raw
        fetch.override=unavailable
        self.assert_unchanged_failure(fetch,prior,TimeoutError)

    def test_private_deadline_interrupt_is_not_swallowed(self):
        class Deadline(BaseException):
            pass
        def interrupted(*args):
            raise Deadline()
        self.assert_unchanged_failure(interrupted,exception=Deadline)

    def test_noncomplete_or_redirected_response_is_rejected(self):
        overrides=[lambda p,r,b:(r,None),lambda p,r,b:(None,b),lambda p,r,b:(r,b.decode()),
            lambda p,r,b:(dict(r,httpStatus=304),b),lambda p,r,b:(dict(r,httpStatus=500),b),
            lambda p,r,b:(dict(r,httpStatus=200.0),b),lambda p,r,b:(dict(r,finalUrl=r['finalUrl']+'&extra=1'),b)]
        for override in overrides:
            with self.subTest(override=overrides.index(override)):
                fetch=Fetch();fetch.override=override
                self.assert_unchanged_failure(fetch)

    def test_missing_malformed_or_inconsistent_pagination_refuses_archive(self):
        for counts in [None,[],{}, {'total':'102','pages':2},{'total':True,'pages':1},
                       {'total':102,'pages':1},{'total':50001,'pages':501},
                       {'total':102,'pages':501},{'total':-1,'pages':0}]:
            with self.subTest(counts=counts):
                fetch=Fetch();fetch.override=lambda p,r,b:(dict(r,wordpressPagination=counts),b)
                self.assert_unchanged_failure(fetch)

    def test_wrong_page_count_fails_before_checkpoint_advances(self):
        for pages in [{1:[post(i) for i in range(1,100)]}, {1:[post(i) for i in range(1,102)]}]:
            with self.subTest(count=len(pages[1])):
                self.assert_unchanged_failure(Fetch(pages))

    def test_invalid_complete_article_payloads_are_rejected(self):
        mutations=[lambda p:p.update(id=0), lambda p:p.update(id=-1),lambda p:p.update(id=10**20),
            lambda p:p.update(link='https://unreviewed.example/article'),lambda p:p.update(status='draft'),
            lambda p:p['content'].update(protected=True),lambda p:p['content'].update(rendered=''),
            lambda p:p.update(date_gmt='2026-10-15T12:00:00+02:00')]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                article=post(1);mutation(article)
                self.assert_unchanged_failure(Fetch({1:[article]},1))
        fetch=Fetch({1:[post(1),post(1)]},2)
        self.assert_unchanged_failure(fetch)
        fetch=Fetch();fetch.override=lambda p,r,b:(r,b'{"not":"a post list"}')
        self.assert_unchanged_failure(fetch)
        fetch.override=lambda p,r,b:(r,b'[{"id":1')
        self.assert_unchanged_failure(fetch)

    def test_within_and_cross_page_chronology_must_descend(self):
        fetch=Fetch();fetch.pages[1][0],fetch.pages[1][1]=fetch.pages[1][1],fetch.pages[1][0]
        self.assert_unchanged_failure(fetch)
        fetch=Fetch();prior=self.first(fetch)['checkpoint']
        fetch.pages[2][0]=post(101,'2026-10-15T11:59:00')
        self.assert_unchanged_failure(fetch,prior)

    def test_modification_clock_cannot_precede_publication_or_be_in_future(self):
        for modified in ['2026-10-15T00:00:00','2026-10-16T05:00:01']:
            with self.subTest(modified=modified):
                article=post(1);article['modified_gmt']=modified
                self.assert_unchanged_failure(Fetch({1:[article]},1))

    def test_empty_provider_window_completes_once(self):
        result=self.first(Fetch({1:[]},0))
        self.assertEqual(result['items'],[])
        self.assertEqual(result['checkpoint']['windowPosts'],0)
        self.assertEqual(result['checkpoint']['totalPages'],0)
        self.assertTrue(result['checkpoint']['complete'])
        self.assertEqual(result['checkpoint']['nextPage'],2)
        self.assertEqual(archive.advance(self.source,self.policy,result['checkpoint'],LATER,
            lambda *args:self.fail('Empty completed window fetched again'))['requested'],0)

    def test_utc_overlap_query_is_explicit_but_only_half_open_window_is_returned(self):
        dates=['2026-10-16T00:00:01','2026-10-16T00:00:00','2026-10-15T23:59:59',
               '2025-10-16T00:00:00','2025-10-15T23:59:59']
        fetch=Fetch({1:[post(i+1,date) for i,date in enumerate(dates)]},5)
        result=self.first(fetch)
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(fetch.calls[0][0]['url']).query)
        self.assertEqual(query,{'after':['2025-10-15T23:59:59Z'],'before':['2026-10-16T00:00:01Z'],
            'per_page':['100'],'page':['1'],'orderby':['date'],'order':['desc']})
        self.assertEqual([row['guid'] for row in result['items']],['3','4'])
        self.assertEqual(result['checkpoint']['windowPosts'],2)
        self.assertEqual(len(result['checkpoint']['seenIds']),5)

    def test_response_outside_query_overlap_is_rejected(self):
        for date in ['2025-10-15T23:59:58','2026-10-16T00:00:02']:
            with self.subTest(date=date):
                self.assert_unchanged_failure(Fetch({1:[post(1,date)]},1))

    def test_falsy_or_missing_checkpoint_never_silently_starts_over(self):
        valid=self.first()['checkpoint']
        checkpoints=[{},[],False,'',0,None]
        checkpoints.remove(None)
        checkpoints.extend([{key:value for key,value in valid.items() if key!='totalPosts'},dict(valid,unexpected=True)])
        for previous in checkpoints:
            with self.subTest(previous=type(previous).__name__):
                def forbidden(*args):
                    self.fail('Invalid checkpoint made HTTP request')
                self.assert_unchanged_failure(forbidden,previous)

    def test_malformed_checkpoint_counters_ids_status_and_clocks_fail_before_fetch(self):
        valid=self.first()['checkpoint']
        mutations=[{'schemaVersion':True},{'generation':True},{'generation':2},{'restarts':-1},
            {'pagesAccepted':2},{'nextPage':1},{'windowPosts':101},{'seenIds':[['unhashable']]},
            {'seenIds':['0']},{'seenIds':valid['seenIds'][:-1]+['1']},{'previousPageIdentity':'not a hash'},
            {'configurationHash':'0'*64},{'sourceId':'other'},{'complete':1},{'status':'scanned'},
            {'startedAt':'2025-10-15T00:00:00Z'},{'lastAttemptAt':'2026-10-16T05:00:01Z'},
            {'lastPageAcceptedAt':'2026-10-16T03:00:00Z'},{'completedAt':NOW},
            {'restartReason':'x'*257}]
        for change in mutations:
            with self.subTest(change=change):
                def forbidden(*args):
                    self.fail('Malformed checkpoint made HTTP request')
                self.assert_unchanged_failure(forbidden,dict(valid,**change))
        done=self.first(Fetch({1:[post(1)]},1))['checkpoint']
        self.assert_unchanged_failure(lambda *args:self.fail('Bad completion fetched'),dict(done,completedAt=LATER))

    def test_all_five_profiles_are_accepted_with_their_exact_country_and_host(self):
        for source_id in sorted(archive.REVIEWED_SOURCES):
            with self.subTest(source=source_id):
                source,policy=profile(source_id)
                fetch=Fetch({1:[post(1,host=archive.PROFILES[source_id]['host'])]},1)
                result=archive.advance(source,policy,None,NOW,fetch)
                self.assertTrue(result['checkpoint']['complete'])
                self.assertEqual(result['checkpoint']['sourceId'],source_id)

    def test_source_identity_and_policy_are_strict_before_fetch(self):
        source_changes=[{'enabled':False},{'id':'unreviewed'},{'countryCodes':['JM','BZ']},
            {'countryCodes':['BZ']},{'tier':'local-news'},{'language':'es'},{'kind':'website'},
            {'access':'browser'},{'feedUrl':self.policy['url']+'&extra=1'},
            {'url':'https://jcf.gov.jm/?different=1'},{'url':'https://jcf.gov.jm/#fragment'},
            {'url':'https://jcf.gov.jm:443/'},{'url':'https://user@jcf.gov.jm/'},{'language':None}]
        policy_changes=[{'sourceId':'other'},{'newsAdapter':'unknown'},{'format':'rss'},
            {'mode':'official'},{'url':self.policy['url']+'&extra='},
            {'url':self.policy['url']+'&per_page=100'},{'url':self.policy['url']+'#fragment'},
            {'url':self.policy['url'].replace('jcf.gov.jm','other.example')},
            {'url':self.policy['url'].replace('https:','http:')}]
        for change in source_changes:
            with self.subTest(source=change):
                source=dict(self.source,**change)
                with self.assertRaises(ValueError):
                    archive.advance(source,self.policy,None,NOW,lambda *args:self.fail('Unreviewed source fetched'))
        for change in policy_changes:
            with self.subTest(policy=change):
                policy=dict(self.policy,**change)
                # Keep the feed binding exact so endpoint checks are tested independently.
                source=dict(self.source,feedUrl=policy['url'])
                with self.assertRaises(ValueError):
                    archive.advance(source,policy,None,NOW,lambda *args:self.fail('Unreviewed policy fetched'))

    def test_fixed_history_window_and_complete_utc_clocks_are_required(self):
        windows=[None,{},dict(WINDOW,extra=True),{'from':WINDOW['through'],'through':WINDOW['from']},
            {'from':'2024-10-16T00:00:00Z','through':WINDOW['through']},
            {'from':WINDOW['from'],'through':'2026-10-17T00:00:00Z'},
            {'from':'2025-10-16T00:00:00+00:00','through':WINDOW['through']},
            {'from':'0001-01-01T00:00:00Z','through':'0001-01-02T00:00:00Z'}]
        for window in windows:
            with self.subTest(window=window), self.assertRaises(ValueError):
                archive.advance(self.source,dict(self.policy,wordpressHistory=window),None,NOW,
                    lambda *args:self.fail('Invalid window fetched'))
        for now in ['2026-10-16T04:00:00+00:00','2026-10-16T04:00:00.000Z','not a date']:
            with self.subTest(now=now), self.assertRaises(ValueError):
                archive.advance(self.source,self.policy,None,now,lambda *args:self.fail('Invalid clock fetched'))

    def test_restart_bound_requires_review_instead_of_overflowing_state(self):
        fetch=Fetch();prior=self.first(fetch)['checkpoint']
        prior.update(generation=1_000_000,restarts=999_999)
        fetch.total=103
        self.assert_unchanged_failure(fetch,prior)

    def test_fixture_context_is_forwarded_without_fabricated_live_observation(self):
        fetch=Fetch({1:[post(1)]},1)
        marker={'offline':'complete fixtures'}
        result=archive.advance(self.source,self.policy,None,NOW,fetch,marker)
        self.assertEqual(fetch.calls[0][3],marker)
        self.assertEqual(fetch.calls[0][1],{})
        self.assertEqual(fetch.calls[0][0]['sourceId'],'gov-jm-jcf-history-page-1')
        self.assertNotIn('transportTiming',result)


if __name__=='__main__':
    unittest.main()
