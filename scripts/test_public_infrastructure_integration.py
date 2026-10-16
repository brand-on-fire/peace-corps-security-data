"""Offline opt-in, exact queue persistence, degraded health and archive gates."""
import copy
import concurrent.futures
import datetime as dt
import io
import os
import signal
import shutil
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

import public_data_pipeline as pipeline
import public_data_refresh as refresh
import public_official as official
import public_infrastructure as water
from test_public_infrastructure import NOW, POLICY, TITLE, BODY, entry, document, Fetch
import test_public_infrastructure as fixtures

COUNTRY=dict(code='KG',name='Kyrgyz Republic',region='Central Asia',lat=41.2,lon=74.8,status='active',
    evidenceUrl='https://www.peacecorps.gov/kyrgyz-republic/',evidenceText='Synthetic reviewed-country fixture.',
    evidenceDate='2026-10-01',verifiedAt='2026-10-01T00:00:00Z',reviewDue='2026-11-15',languages=['ky','ru'])
SOURCE=dict(id=water.SOURCE_ID,name='Bishkek municipality — drinking-water interruptions',url=water.HOST+'/ru',
    feedUrl=water.endpoint(),countryCodes=['KG'],kind='html',tier='official',language='ru',cadenceMinutes=60,
    enabled=False,access='public-page',verifiedAt='2026-10-01T00:00:00Z',notes='Synthetic test source; no external requests.')
POLICY=dict(POLICY,mode='official',cadenceMinutes=60)


def initialize(root,enabled=True):
    source=dict(SOURCE,enabled=enabled)
    for name,value in [('countries.json',[COUNTRY]),('sources.json',[source]),
        ('collection-policy.json',{'sources':[POLICY] if enabled else []}),
        ('staged-infrastructure-policy.json',{'enabled':False,'source':SOURCE,'policy':POLICY}),
        ('geo/index.json',[]),('country-iso3.json',{'KGZ':'KG'}),('admin-places.json',[])]:
        pipeline.save(root/'config'/name,value)
    data=root/'docs/data'
    snapshot=dict(schemaVersion=1,mode='live',generatedAt='2026-10-13T00:00:00Z',countries=[COUNTRY],
        sources=[source],incidents=[],coverage=[],archiveTotal=0,
        history={'recordCount':0,'from':'2025-10-16T00:00:00Z','to':'2026-10-13T00:00:00Z','sources':[SOURCE['id']],'notes':[]})
    pipeline.save(data/'snapshot.json',snapshot);pipeline.write_archive(data,{}, {'KG':COUNTRY})
    return data


def client(rows=None,documents=None,overrides=None):
    fetcher=Fetch([entry()] if rows is None else rows,documents,overrides)
    def fetch(policy,previous,now,fixtures):
        result,raw=fetcher(policy)
        result.update(sourceId=policy['sourceId'],url=policy['url'],lastAttemptAt=now)
        if raw is None:result.update(status='unavailable',lastSuccessAt=previous.get('lastSuccessAt'),message='Synthetic failed response')
        return result,raw
    fetch.calls=fetcher.calls
    return fetch


class InfrastructureIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'collector';self.data=initialize(self.root)

    def tearDown(self):self.temp.cleanup()

    def run_pipeline(self,fetch=None,now=NOW):
        with patch.object(pipeline,'fetch',fetch or client()):
            return pipeline.run(self.root,dt.datetime.fromisoformat(now.replace('Z','+00:00')))

    def run_refresh(self,fetch=None,now=NOW):
        def runner(command,**kwargs):
            stage=Path(command[command.index('--root')+1])
            with patch.object(pipeline,'fetch',fetch or client()):
                pipeline.run(stage,dt.datetime.fromisoformat(now.replace('Z','+00:00')))
        return refresh.refresh(self.root,dt.datetime.fromisoformat(now.replace('Z','+00:00')),runner=runner,environment={})

    def state(self):return pipeline.load(self.data/'collector-state.json')[SOURCE['id']]
    def records(self):return pipeline.read_archive(self.data,{'KG':COUNTRY})

    def test_disabled_source_never_requests_even_if_policy_is_accidentally_listed(self):
        initialize(self.root,False)
        for policies in [[],[POLICY]]:
            pipeline.save(self.root/'config/collection-policy.json',{'sources':policies})
            with patch.object(pipeline,'fetch',side_effect=AssertionError('Disabled source requested')):
                result=pipeline.run(self.root,dt.datetime.fromisoformat(NOW))
            self.assertEqual(result['requested'],0)
            self.assertFalse(pipeline.load(self.data/'snapshot.json')['sources'][0]['enabled'])
            self.assertEqual(pipeline.load(self.data/'snapshot.json')['coverage'][0]['status'],'disabled')

    def test_opt_in_has_exact_canonical_projection_original_dates_and_no_fabricated_geometry(self):
        baseline=Path(self.temp.name)/'baseline';shutil.copytree(self.data,baseline)
        result=self.run_pipeline();self.assertEqual(result['sources'][0]['status'],'healthy')
        snapshot=refresh.validate_candidate(self.root,baseline);row=next(iter(self.records().values()))
        self.assertEqual(snapshot['incidents'],[pipeline.public_snapshot_record(row)])
        self.assertEqual((row['category'],row['location']),('infrastructure',{'precision':'country'}))
        self.assertEqual(row['reports'][0]['title'],TITLE);self.assertEqual(row['occurredAt'],'2026-10-15T04:00:00Z')
        self.assertEqual(self.state()['lastSuccessAt'],NOW)
        self.assertNotIn('validatedPartial',result['sources'][0])

    def test_future_queue_survives_restart_and_no_listing_then_expires_without_restoration(self):
        first=self.run_refresh(now='2026-10-14T06:00:00Z');self.assertEqual(first['outcome'],'complete')
        self.assertEqual([e['id'] for e in self.state()['infrastructurePending']],[1]);self.assertFalse(self.records())
        result=self.run_refresh(client([]),now=NOW);self.assertEqual(result['outcome'],'complete')
        old=next(iter(self.records().values()));self.assertEqual(old['state'],'reported')
        result=self.run_refresh(client([]),now='2026-10-15T15:00:00Z');self.assertEqual(result['outcome'],'complete')
        row=self.records()[old['id']]
        self.assertEqual(row['state'],'expired');self.assertNotIn('restoredAt',row['sourceMetadata'])
        self.assertEqual(row['occurredAt'],old['occurredAt']);self.assertEqual(row['firstSeenAt'],old['firstSeenAt'])

    def test_all_partial_refresh_preserves_success_clocks_and_reconciles_later_original(self):
        self.run_refresh()
        prior=self.state()['lastSuccessAt'];old=copy.deepcopy(self.records())
        correction=fixtures.InfrastructureTests().correction_document(identity=2,deferred=True,area='ул. Другая — ул. Первая — западная сторона проспекта Второго.')
        rows=[entry(2,title='Отключение воды переносится')]
        result=self.run_refresh(client(rows,{2:correction}),now='2026-10-15T07:00:00Z')
        self.assertEqual(result['outcome'],'degraded',result['errors']);self.assertEqual(result['lastSuccessfulCollectionAt'],prior)
        self.assertEqual(result['publicationGeneratedAt'],'2026-10-15T07:00:00Z')
        state=self.state();self.assertEqual(state['status'],'unavailable');self.assertEqual(state['lastSuccessAt'],prior)
        self.assertEqual(state['infrastructureUnresolvedCorrectionIds'],[2]);self.assertEqual(state['lastPartialSuccessAt'],'2026-10-15T07:00:00Z')
        self.assertEqual(self.records(),old)
        # The unrelated original remains unchanged; a newly found matching one
        # settles the queued correction even when its notice leaves the listing.
        third=document(3,body=BODY.replace('Примерная','Другая'))
        fetched=client([entry(3,date='2026-10-14')],{2:correction,3:third})
        result=self.run_refresh(fetched,now='2026-10-15T08:00:00Z')
        self.assertEqual(result['outcome'],'complete',result['errors'])
        self.assertIn(water.HOST+'/ru/post/2',fetched.calls)
        state=self.state();self.assertEqual(state['infrastructureUnresolvedCorrectionIds'],[])
        self.assertNotIn('validatedPartial',state);self.assertEqual(state['lastSuccessAt'],'2026-10-15T08:00:00Z')
        new=next(r for r in self.records().values() if r['sourceMetadata']['recordId']==3)
        self.assertEqual(new['state'],'cancelled');self.assertEqual(self.records()[next(iter(old))],next(iter(old.values())))

    def test_first_partial_cycle_preserves_queue_without_inventing_success(self):
        correction=fixtures.InfrastructureTests().correction_document(deferred=True)
        result=self.run_refresh(client([entry(2,title='Отключение воды переносится')],{2:correction}))
        self.assertEqual(result['outcome'],'degraded',result['errors']);self.assertIsNone(result['lastSuccessfulCollectionAt'])
        self.assertIsNone(self.state()['lastSuccessAt']);self.assertEqual(self.state()['infrastructureUnresolvedCorrectionIds'],[2])
        self.assertFalse(self.records())

    def test_missing_accepted_deferred_correction_allows_new_notice_and_later_recovers(self):
        self.run_refresh()
        correction=fixtures.InfrastructureTests().correction_document(deferred=True)
        self.run_refresh(client([entry(2,title='Отключение воды переносится')],{2:correction}),now='2026-10-15T07:00:00Z')
        old=copy.deepcopy(self.records()['bishkek-water-1-KG']);prior=self.state()['lastSuccessAt']
        third=document(3,body=BODY.replace('Примерная','Другая'))
        result=self.run_refresh(client([entry(3,date='2026-10-14')],{2:False,3:third}),now='2026-10-15T08:00:00Z')
        self.assertEqual(result['outcome'],'degraded',result['errors'])
        self.assertEqual(result['lastSuccessfulCollectionAt'],prior)
        self.assertEqual(self.state()['lastSuccessAt'],prior)
        withheld=self.records()[old['id']]
        self.assertEqual((withheld['state'],withheld['evidenceStatus']),('expired','unverified'))
        self.assertEqual(self.records()['bishkek-water-3-KG']['state'],'reported')
        self.assertEqual(self.state()['infrastructureUnresolvedCorrectionIds'],[2])
        receipt=next(r for r in self.state()['evidenceReceipts'] if r['url']==water.HOST+'/ru/post/2')
        self.assertEqual(receipt['httpStatus'],404)
        self.assertEqual(withheld['reports'][0]['sourceMetadata']['rawResponseSha256'],receipt['sha256'])
        self.assertTrue({r['contentHash'] for r in old['reports']} <= {r['contentHash'] for r in withheld['reports']})
        result=self.run_refresh(client([],{2:correction,3:third}),now='2026-10-15T09:00:00Z')
        self.assertEqual(result['outcome'],'complete',result['errors'])
        restored=self.records()[old['id']]
        self.assertEqual((restored['state'],restored['evidenceStatus']),('cancelled','official'))
        self.assertEqual(self.state()['infrastructureUnresolvedCorrectionIds'],[])
        self.assertEqual(self.state()['lastSuccessAt'],'2026-10-15T09:00:00Z')
        self.assertTrue({r['contentHash'] for r in withheld['reports']} <= {r['contentHash'] for r in restored['reports']})

    def test_unknown_or_pending_original404_still_preserves_prior_batch(self):
        for pending in [False,True]:
            if pending:self.run_refresh(now='2026-10-14T06:00:00Z')
            before={p.name:p.read_bytes() for p in (self.data/'snapshot.json',self.data/'collector-state.json') if p.exists()}
            result=self.run_refresh(client(documents={1:False}),now=NOW)
            self.assertEqual(result['outcome'],'failed')
            self.assertIn('listed/pending article disappeared',result['sources'][0]['message'])
            for name,raw in before.items():self.assertEqual((self.data/name).read_bytes(),raw)

    def test_prolonged_partial_cycles_advance_only_the_validated_listing_checkpoint(self):
        self.run_refresh()
        correction=fixtures.InfrastructureTests().correction_document(deferred=True,area='Другая западная область номер 2')
        result=self.run_refresh(client([entry(2,title='Отключение воды переносится')],{2:correction}),now='2026-10-15T07:00:00Z')
        self.assertEqual(result['outcome'],'degraded')
        with patch.object(water,'PAGE_SIZE',1),patch.object(water,'LIVE_PAGES',2):
            for day in range(16,31):
                today=dt.date(2026,10,day)
                rows=[entry(100+day,title='Городские новости',date=today.isoformat()),
                      entry(200+day,title='Городские новости',date=(today-dt.timedelta(days=3)).isoformat())]
                result=self.run_refresh(client(rows,{2:correction}),now=today.isoformat()+'T06:00:00Z')
                self.assertEqual(result['outcome'],'degraded',result['errors'])
                self.assertEqual(self.state()['lastSuccessAt'],NOW)
                self.assertEqual(self.state()['infrastructureLiveThrough'],today.isoformat()+'T06:00:00Z')
                self.assertEqual(self.state()['infrastructureUnresolvedCorrectionIds'],[2])
        self.assertEqual(self.state()['infrastructureLiveFrom'],'2026-10-27T06:00:00Z')

    def test_invalid_or_future_listing_checkpoint_fails_without_state_advancement(self):
        self.run_refresh()
        original=pipeline.load(self.data/'collector-state.json')
        for value in ['not-a-date','2026-10-15T06:00:00','2026-10-16T06:00:00Z','2026-10-15T07:00:00.500Z']:
            state=copy.deepcopy(original);state[SOURCE['id']]['infrastructureLiveThrough']=value
            pipeline.save(self.data/'collector-state.json',state)
            before=(self.data/'collector-state.json').read_bytes()
            result=self.run_refresh(now='2026-10-15T07:00:00Z')
            self.assertEqual(result['outcome'],'failed')
            self.assertIn('scan watermark',result['sources'][0]['message'])
            self.assertEqual((self.data/'collector-state.json').read_bytes(),before)

    def test_failed_response_preserves_all_prior_queues_archive_and_publication(self):
        self.run_refresh(now='2026-10-14T06:00:00Z')
        before={p.name:p.read_bytes() for p in (self.data/'snapshot.json',self.data/'collector-state.json')}
        for fetch in [client(overrides={water.endpoint():None}),client(documents={1:None})]:
            result=self.run_refresh(fetch,now='2026-10-14T07:00:00Z')
            self.assertEqual(result['outcome'],'failed')
            for name,raw in before.items():self.assertEqual((self.data/name).read_bytes(),raw)
            self.assertNotIn('validatedPartial',result['sources'][0])

    def test_record_size_and_archive_gate_failure_cannot_commit_infrastructure_state(self):
        self.run_refresh();before={p.name:p.read_bytes() for p in (self.data/'snapshot.json',self.data/'collector-state.json')}
        docs={1:document(body=BODY.replace('20:00','21:00')),2:fixtures.InfrastructureTests().correction_document(deferred=True,area='Другая западная область номер 2')}
        fetched=lambda:client([entry(2,title='Отключение воды переносится'),entry()],docs)
        limit=len(pipeline.encode(next(iter(self.records().values()))))+100
        with patch.object(pipeline,'PART_LIMIT',limit):result=self.run_refresh(fetched(),now='2026-10-15T07:00:00Z')
        self.assertEqual(result['outcome'],'failed');self.assertNotIn('validatedPartial',result['sources'][0])
        for name,raw in before.items():self.assertEqual((self.data/name).read_bytes(),raw)
        with patch.object(refresh,'validate_candidate',side_effect=ValueError('Synthetic archive rejection')):
            result=self.run_refresh(fetched(),now='2026-10-15T07:00:00Z')
        self.assertEqual(result['outcome'],'failed')
        for name,raw in before.items():self.assertEqual((self.data/name).read_bytes(),raw)

    def test_fixed_baseline_resume_and_failed_page_keep_previous_cursor(self):
        rows=[entry(3,date='2026-10-13'),entry(2),entry(1,date='2026-10-11')]
        with patch.object(water,'PAGE_SIZE',2):
            self.run_refresh(client(rows));prior=copy.deepcopy(self.state())
            self.assertEqual(prior['infrastructureBaseline']['nextPage'],2)
            self.assertEqual(prior['infrastructureBaseline']['fromDate'],'2025-10-16T00:00:00Z')
            failed=client(rows,overrides={water.endpoint(2,'воды'):None})
            result=self.run_refresh(failed,now='2026-10-16T06:00:00Z');self.assertEqual(result['outcome'],'failed')
            self.assertEqual(self.state(),prior)
            self.run_refresh(client(rows),now='2026-10-16T07:00:00Z')
            self.assertEqual(self.state()['infrastructureBaseline']['fromDate'],prior['infrastructureBaseline']['fromDate'])
            self.assertEqual(self.state()['infrastructureBaseline']['searchIndex'],1)

    def test_missing_fixed_baseline_is_rejected(self):
        bad={k:v for k,v in POLICY.items() if k!='historyFrom'}
        pipeline.save(self.root/'config/collection-policy.json',{'sources':[bad]})
        result=self.run_refresh();self.assertEqual(result['outcome'],'failed')
        self.assertIn('fixed historyFrom',result['sources'][0]['message']);self.assertFalse(self.records())

    def test_source_deadline_interrupts_initial_or_child_request_without_blocking_other_publication(self):
        self.run_refresh()
        before_records=copy.deepcopy(self.records());before_state=copy.deepcopy(self.state())
        other=dict(SOURCE,id='other-reviewed-source',enabled=True,kind='rss',feedUrl='https://example.org/feed')
        pipeline.save(self.root/'config/sources.json',[dict(SOURCE,enabled=True),other])
        policy=dict(sourceId=other['id'],mode='discovery',cadenceMinutes=60,url=other['feedUrl'])
        pipeline.save(self.root/'config/collection-policy.json',{'sources':[POLICY,policy]})
        original_handler=signal.getsignal(signal.SIGALRM)
        for number,address in enumerate([water.endpoint(),water.HOST+'/ru/post/1'],start=7):
            ordinary=client(documents={1:document(body=BODY.replace('20:00','21:00'))})
            now=f'2026-10-15T{number:02d}:00:00Z'; swallowed=[]
            def fetched(p,previous,at,fixture):
                if p['sourceId']==other['id']:
                    raw=(f'<rss><channel><item><title>Robbery reported in Bishkek</title>'
                         f'<link>https://example.org/report/{number}</link>'
                         f'<pubDate>{now}</pubDate><description>Police reported a robbery.</description>'
                         '</item></channel></rss>').encode()
                    return dict(sourceId=p['sourceId'],url=p['url'],lastAttemptAt=at),raw
                if p['url']==address:
                    try:time.sleep(0.5)
                    except Exception:swallowed.append(True)
                return ordinary(p,previous,at,fixture)
            started=time.monotonic()
            with patch.object(pipeline,'INFRASTRUCTURE_DEADLINE_SECONDS',0.03):
                result=self.run_refresh(fetched,now=now)
            self.assertLess(time.monotonic()-started,2)
            self.assertEqual(swallowed,[])
            self.assertEqual(result['outcome'],'degraded',result['errors'])
            self.assertEqual(result['publicationGeneratedAt'],now)
            statuses={row['sourceId']:row for row in result['sources']}
            self.assertEqual(statuses[other['id']]['status'],'healthy')
            self.assertIn('source wall-time budget',statuses[SOURCE['id']]['message'])
            self.assertEqual(self.records(),before_records)
            self.assertEqual(self.state()['lastSuccessAt'],before_state['lastSuccessAt'])
            self.assertEqual({k:v for k,v in self.state().items() if k.startswith('infrastructure')},
                             {k:v for k,v in before_state.items() if k.startswith('infrastructure')})
            self.assertTrue(any(row['url']==f'https://example.org/report/{number}'
                                for row in pipeline.load(self.data/'discovery.json')))
            self.assertEqual(signal.getsignal(signal.SIGALRM),original_handler)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))
        # The interrupted batch is still due, and its original checkpoint can
        # resume on a successful next run without manufacturing prior success.
        result=self.run_refresh(client(),now='2026-10-15T09:00:00Z')
        self.assertEqual(next(row for row in result['sources'] if row['sourceId']==SOURCE['id'])['status'],'healthy')

    def test_source_deadline_refuses_unisolated_execution_before_fetching(self):
        def guarded():
            with pipeline.infrastructure_deadline():
                raise AssertionError('Unisolated source began work')
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaisesRegex(ValueError,'POSIX main thread'):pool.submit(guarded).result()
        original_handler=signal.getsignal(signal.SIGALRM)
        signal.setitimer(signal.ITIMER_REAL,60)
        try:
            with self.assertRaisesRegex(ValueError,'existing timer'):guarded()
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0],0)
            self.assertEqual(signal.getsignal(signal.SIGALRM),original_handler)
        finally:signal.setitimer(signal.ITIMER_REAL,0)
        with patch.object(pipeline.signal,'setitimer',side_effect=OSError('Synthetic timer failure')):
            with self.assertRaises(OSError):guarded()
        self.assertEqual(signal.getsignal(signal.SIGALRM),original_handler)

    def test_source_deadline_preserves_foreign_pending_alarm_and_drains_its_own(self):
        alarm={signal.SIGALRM};original_handler=signal.getsignal(signal.SIGALRM)
        mask=signal.pthread_sigmask(signal.SIG_BLOCK,alarm)
        try:
            os.kill(os.getpid(),signal.SIGALRM)
            with self.assertRaisesRegex(ValueError,'blocked or pending alarm'):
                with pipeline.infrastructure_deadline():raise AssertionError('Source began work')
            self.assertIn(signal.SIGALRM,signal.sigpending())
            self.assertEqual(signal.getsignal(signal.SIGALRM),original_handler)
        finally:
            while signal.SIGALRM in signal.sigpending():signal.sigwait(alarm)
            signal.pthread_sigmask(signal.SIG_SETMASK,mask)
        with pipeline.infrastructure_deadline():
            signal.pthread_sigmask(signal.SIG_BLOCK,alarm)
            os.kill(os.getpid(),signal.SIGALRM)
            self.assertIn(signal.SIGALRM,signal.sigpending())
        self.assertNotIn(signal.SIGALRM,signal.sigpending())
        self.assertEqual(signal.getsignal(signal.SIGALRM),original_handler)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK,set()),mask)

    def test_source_deadline_rejects_overdue_completion_and_restores_after_parser_error(self):
        original_handler=signal.getsignal(signal.SIGALRM)
        with patch.object(pipeline.time,'monotonic',side_effect=[0,91]):
            with self.assertRaisesRegex(ValueError,'source wall-time budget'):
                with pipeline.infrastructure_deadline():pass
        with self.assertRaisesRegex(ValueError,'Synthetic parse failure'):
            with pipeline.infrastructure_deadline():raise ValueError('Synthetic parse failure')
        self.assertEqual(signal.getsignal(signal.SIGALRM),original_handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))

    def test_source_deadline_at_cleanup_boundary_restores_before_rejecting(self):
        real_mask=signal.pthread_sigmask;blocks=[]
        original_handler=signal.getsignal(signal.SIGALRM)
        original_mask=real_mask(signal.SIG_BLOCK,set())
        def boundary(how,signals):
            if how==signal.SIG_BLOCK:
                blocks.append(True)
                if len(blocks)==2:raise pipeline._SourceDeadlineExpired()
            return real_mask(how,signals)
        with patch.object(pipeline.signal,'pthread_sigmask',side_effect=boundary):
            with self.assertRaisesRegex(ValueError,'source wall-time budget'):
                with pipeline.infrastructure_deadline():pass
        self.assertEqual(signal.getsignal(signal.SIGALRM),original_handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))
        self.assertEqual(real_mask(signal.SIG_BLOCK,set()),original_mask)

    def test_reconciliation_ceiling_preserves_prior_state_and_stops_collection(self):
        self.run_refresh(client([entry(1),entry(2)]),now='2026-10-14T06:00:00Z')
        before=(self.data/'collector-state.json').read_bytes()
        with patch.object(water,'EXACT_LIMIT',1):result=self.run_refresh(client([]),now=NOW)
        self.assertEqual(result['outcome'],'failed');self.assertEqual((self.data/'collector-state.json').read_bytes(),before)
        self.assertFalse(self.records())

    def test_partial_marker_is_bound_to_source_policy_complete_scan_and_exact_unresolved_ids(self):
        marker=dict(adapter='kg-bishkek-water',sourceId=SOURCE['id'],listingValidated=True,validatedResponses=3,unresolvedCorrections=1)
        valid=dict(sourceId=SOURCE['id'],status='unavailable',validatedPartial=marker,
            infrastructureLiveCaughtUp=True,infrastructureUnresolvedCorrectionIds=[2])
        self.assertTrue(refresh.validated_partial(valid,[POLICY]))
        for row,policies in [(dict(valid,sourceId='other'),[POLICY]),(valid,[]),
            (dict(valid,infrastructureLiveCaughtUp=False),[POLICY]),(dict(valid,infrastructureUnresolvedCorrectionIds=[]),[POLICY]),
            (dict(valid,infrastructureUnresolvedCorrectionIds=[True]),[POLICY]),
            (dict(valid,validatedPartial=dict(marker,validatedResponses=True)),[POLICY])]:
            self.assertFalse(refresh.validated_partial(row,policies))

    def test_initial_listing_conditionals_cannot_skip_pending_or_exact_corrections(self):
        previous=dict(url=water.endpoint(),etag='old',lastModified='old',lastSuccessAt=NOW)
        self.assertEqual(official.request_state(POLICY,previous),dict(url=water.endpoint(),lastSuccessAt=NOW))
        self.assertEqual(water.prepare_infrastructure_policy(dict(POLICY,url='https://bad.example/'),{},NOW)['url'],water.endpoint())

    def test_complete_http404_passthrough_is_exact_source_article_only(self):
        raw=b'<html><body>Complete missing notice response.</body></html>'
        detail=dict(POLICY,sourceId=SOURCE['id']+'-child',url=water.HOST+'/ru/post/1')
        cases=[(detail,404,detail['url'],True),(POLICY,404,POLICY['url'],False),
            (dict(detail,adapter='other'),404,detail['url'],False),
            (dict(detail,sourceId='other'),404,detail['url'],False),
            (detail,404,water.HOST+'/ru/post/2',False),(POLICY,304,POLICY['url'],False)]
        for policy,code,url,allowed in cases:
            error=pipeline.urllib.error.HTTPError(url,code,'Synthetic status',{},io.BytesIO(raw))
            with patch.object(pipeline.urllib.request,'build_opener',return_value=Mock(open=Mock(side_effect=error))):
                result,body=pipeline.fetch(policy,dict(lastSuccessAt=NOW),NOW)
            error.close()
            self.assertEqual(body,raw if allowed else None)
            if allowed:
                self.assertEqual(result['httpStatus'],404);self.assertEqual(result['sha256'],water.sha(raw))
            else:self.assertEqual(result['status'],'unavailable')

    def test_actual_fetch_error_path_withholds_missing_article_and_retains_evidence(self):
        self.run_refresh();old=copy.deepcopy(next(iter(self.records().values())))
        real_fetch=pipeline.fetch;ordinary=client([])
        def fetched(policy,previous,now,fixture):
            if policy['url']!=water.HOST+'/ru/post/1':return ordinary(policy,previous,now,fixture)
            error=pipeline.urllib.error.HTTPError(policy['url'],404,'Not found',{},io.BytesIO(b'<html>Gone</html>'))
            with patch.object(pipeline.urllib.request,'build_opener',return_value=Mock(open=Mock(side_effect=error))):
                return real_fetch(policy,previous,now)
        result=self.run_refresh(fetched,now='2026-10-15T07:00:00Z')
        self.assertEqual(result['outcome'],'complete',result['errors'])
        row=self.records()[old['id']]
        self.assertEqual((row['state'],row['evidenceStatus']),('expired','unverified'))
        self.assertEqual(row['reports'][1],old['reports'][0]);self.assertEqual(row['occurredAt'],old['occurredAt'])


if __name__=='__main__':unittest.main()
