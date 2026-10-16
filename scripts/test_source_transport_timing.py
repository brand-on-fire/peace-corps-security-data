"""Offline request-clock tests; mocked responses never invoke a provider."""
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_data_pipeline as pipeline
import public_data_publish as publisher
from collector_health import source_health


class Response(io.BytesIO):
    def __init__(self, raw=b'complete response', headers=None):
        super().__init__(raw)
        self.status = 200
        self.url = 'https://example.org/feed'
        self.headers = headers or {}


class SourceTransportTimingTests(unittest.TestCase):
    policy = dict(sourceId='reviewed-source', url='https://example.org/feed', cadenceMinutes=15)
    cycle = '2026-10-16T03:00:00Z'
    previous = dict(lastSuccessAt='2026-10-16T02:45:00Z', etag='reviewed-etag')

    def timed(self, response=None, error=None, wall=None, ticks=None, previous=None, policy=None):
        wall = wall or [dt.datetime(2026,10,16,3,0,5,123000,tzinfo=dt.timezone.utc),
                        dt.datetime(2026,10,16,3,0,8,456000,tzinfo=dt.timezone.utc)]
        ticks = ticks or [100_000_000_000, 103_333_000_000]
        opener = Mock()
        if isinstance(error,urllib.error.HTTPError):
            self.addCleanup(error.close)
        opener.open.side_effect = error
        opener.open.return_value = response or Response()
        with patch.object(pipeline.urllib.request,'build_opener',return_value=opener), \
             patch.object(pipeline.dt,'datetime') as clock, \
             patch.object(pipeline.time,'monotonic_ns',side_effect=ticks) as monotonic:
            clock.now.side_effect = wall
            result = pipeline.fetch(policy or self.policy,self.previous if previous is None else previous,self.cycle)
        self.assertEqual(monotonic.call_count,2)
        self.assertEqual(opener.open.call_count,1)
        return result, opener

    def test_complete_slow_wire_read_keeps_cycle_and_body_and_excludes_headers(self):
        response = Response(b'full source body', {'ETag':'new-etag','Last-Modified':'Thu, 16 Oct 2026 03:00:00 GMT',
                                                 'Set-Cookie':'not-public-diagnostic','X-Private':'not-public-diagnostic'})
        (result,raw),opener = self.timed(response)
        self.assertEqual(raw,b'full source body')
        self.assertEqual(result['lastAttemptAt'],self.cycle)
        self.assertEqual(result['etag'],'new-etag')
        self.assertEqual(result['bytes'],len(raw))
        self.assertEqual(result['transportTiming'],dict(mode='live-http',outcome='response-complete',durationMs=3333.0,
            requestStartedAt='2026-10-16T03:00:05.123Z',requestCompletedAt='2026-10-16T03:00:08.456Z'))
        self.assertNotIn('not-public-diagnostic',json.dumps(result))
        self.assertEqual(opener.open.call_args.kwargs,{'timeout':30})
        self.assertEqual(opener.open.call_args.args[0].get_header('If-none-match'),'reviewed-etag')

    def test_duration_uses_monotonic_clock_when_wall_clock_rolls_back(self):
        wall=[dt.datetime(2026,10,16,3,0,5,tzinfo=dt.timezone.utc),dt.datetime(2026,10,16,3,0,2,tzinfo=dt.timezone.utc)]
        (result,_),_=self.timed(wall=wall,ticks=[1_000_000_000,2_250_000_000])
        self.assertEqual(result['transportTiming']['durationMs'],1250)
        self.assertLess(result['transportTiming']['requestCompletedAt'],result['transportTiming']['requestStartedAt'])

    def test_elapsed_duration_includes_complete_body_read_until_eof(self):
        ticks=[1_000_000_000]
        response=Response(b'body')
        original_read=response.read
        def delayed_read(size):
            ticks[0]+=750_000_000
            return original_read(size)
        response.read=delayed_read
        with patch.object(pipeline.urllib.request,'build_opener') as build, \
             patch.object(pipeline.time,'monotonic_ns',side_effect=lambda:ticks[0]):
            build.return_value.open.return_value=response
            result,raw=pipeline.fetch(self.policy,self.previous,self.cycle)
        self.assertEqual(raw,b'body')
        self.assertEqual(result['transportTiming']['durationMs'],1500)

    def test_timeout_and_http_errors_keep_original_failure_behavior(self):
        for error in [TimeoutError('source read timed out'),urllib.error.HTTPError(self.policy['url'],403,'Forbidden',{},None)]:
            with self.subTest(error=type(error).__name__):
                (result,raw),_=self.timed(error=error)
                self.assertIsNone(raw)
                self.assertEqual(result['status'],'unavailable')
                self.assertEqual(result['lastSuccessAt'],self.previous['lastSuccessAt'])
                self.assertEqual(result['message'],str(error))
                self.assertEqual(result['transportTiming']['outcome'],'request-failed')
                self.assertEqual(result['transportTiming']['requestFailedAt'],'2026-10-16T03:00:08.456Z')
                self.assertNotIn('requestCompletedAt',result['transportTiming'])

    def test_mid_body_failure_never_returns_a_prefix(self):
        response=Response()
        response.read=Mock(side_effect=[b'partial',OSError('interrupted body')])
        (result,raw),_=self.timed(response)
        self.assertIsNone(raw)
        self.assertEqual(result['transportTiming']['outcome'],'request-failed')
        self.assertNotIn('sha256',result)
        self.assertEqual(result['message'],'interrupted body')

    def test_304_records_completed_transport_without_changing_prior_gate(self):
        for previous,accepted in [(self.previous,True),({},False)]:
            with self.subTest(accepted=accepted):
                error=urllib.error.HTTPError(self.policy['url'],304,'Not Modified',{},None)
                (result,raw),_=self.timed(error=error,previous=previous)
                self.assertIsNone(raw)
                self.assertEqual(result['transportTiming']['outcome'],'response-complete')
                self.assertEqual(result['status'],'healthy' if accepted else 'unavailable')
                if accepted:
                    self.assertTrue(result['unchanged'])
                    self.assertEqual(result['lastSuccessAt'],self.cycle)
                else:
                    self.assertEqual(result['message'],'304 without a prior validated representation')
        error=urllib.error.HTTPError(self.policy['url'],304,'Not Modified',{},io.BytesIO())
        (result,raw),_=self.timed(error=error,policy=dict(self.policy,adapter='kg-bishkek-water'))
        self.assertIsNone(raw)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['message'],str(error))
        self.assertEqual(result['transportTiming']['outcome'],'response-complete')

    def test_reviewed_water_404_reads_complete_body_and_stays_complete_transport(self):
        policy=dict(sourceId='gov-kg-bishkek-water-12345',url='https://www.bishkek.gov.kg/ru/post/12345',adapter='kg-bishkek-water')
        error=urllib.error.HTTPError(policy['url'],404,'Not Found',{},io.BytesIO(b'complete missing article page'))
        (result,raw),_=self.timed(error=error,policy=policy)
        self.assertEqual(raw,b'complete missing article page')
        self.assertEqual(result['httpStatus'],404)
        self.assertEqual(result['transportTiming']['outcome'],'response-complete')

    def test_decoding_rejection_is_distinct_from_completed_transport(self):
        (result,raw),_=self.timed(Response(b'not gzip',{'Content-Encoding':'gzip'}))
        self.assertIsNone(raw)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['transportTiming']['outcome'],'response-complete')

    def test_fixture_and_missing_fixture_never_claim_live_request_clocks(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(pipeline.urllib.request,'build_opener',side_effect=AssertionError('No network')), \
             patch.object(pipeline.time,'monotonic_ns',side_effect=AssertionError('No transport clock')):
            root=Path(tmp)
            (root/'reviewed-source.response').write_bytes(b'complete fixture')
            result,raw=pipeline.fetch(self.policy,self.previous,self.cycle,root)
            self.assertEqual(raw,b'complete fixture')
            self.assertEqual(result['transportTiming'],{'mode':'fixture-replay'})
            missing,raw=pipeline.fetch(dict(self.policy,sourceId='missing'),self.previous,self.cycle,root)
            self.assertIsNone(raw)
            self.assertEqual(missing['transportTiming'],{'mode':'fixture-replay'})
            self.assertEqual(missing['status'],'unavailable')

    def test_invalid_source_never_claims_an_attempted_http_request(self):
        with patch.object(pipeline.urllib.request,'build_opener',side_effect=AssertionError('No network')):
            result,raw=pipeline.fetch(dict(self.policy,url='http://example.org/feed'),self.previous,self.cycle)
        self.assertIsNone(raw)
        self.assertEqual(result['transportTiming'],{'mode':'not-requested'})

    def test_private_source_deadline_still_escapes(self):
        with self.assertRaises(pipeline._SourceDeadlineExpired):
            self.timed(error=pipeline._SourceDeadlineExpired())

    def test_additive_diagnostics_do_not_change_source_health(self):
        (result,_),_=self.timed()
        result.update(status='healthy',lastSuccessAt=self.cycle)
        baseline={k:v for k,v in result.items() if k!='transportTiming'}
        now=dt.datetime(2026,10,16,3,1,tzinfo=dt.timezone.utc)
        self.assertEqual(source_health([self.policy],[],[result],now),source_health([self.policy],[],[baseline],now))
        self.assertEqual(json.loads(json.dumps(result))['transportTiming'],result['transportTiming'])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'config').mkdir()
            (root/'docs/data').mkdir(parents=True)
            (root/'config/collection-policy.json').write_text(json.dumps({'sources':[dict(self.policy,mode='official')]}))
            (root/'docs/data/collector-state.json').write_text(json.dumps({self.policy['sourceId']:result}))
            (root/'docs/data/refresh-status.json').write_text(json.dumps({'sources':[result]}))
            self.assertTrue(publisher.audit(root,remote=False)['passed'])


if __name__=='__main__':
    unittest.main()
