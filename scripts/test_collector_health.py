"""Offline freshness regression tests; no requests, credentials, or collector run."""
import datetime as dt
import unittest

from collector_health import collection_timing, source_health

NOW = dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc)
POLICIES = [dict(sourceId='urgent', cadenceMinutes=15), dict(sourceId='news', cadenceMinutes=360)]


def state(sid, when, status='healthy'):
    return dict(sourceId=sid, status=status, lastAttemptAt=when, lastSuccessAt=when)


class HealthTests(unittest.TestCase):
    def test_non_due_failed_news_is_not_hidden_by_successful_urgent_feed(self):
        coverage = [state('urgent', '2026-10-15T11:00:00Z'),
                    state('news', '2026-10-15T07:00:00Z', 'unavailable')]
        result = source_health(POLICIES, coverage, [state('urgent', '2026-10-15T12:00:00Z')], NOW)
        self.assertEqual(result['state'], 'degraded')
        self.assertEqual(result['counts']['unavailable'], 1)
        self.assertEqual(result['counts']['fresh'], 2)

    def test_old_healthy_status_is_overdue_without_new_failure(self):
        result = source_health(POLICIES, [state('urgent', '2026-10-15T11:39:59Z'),
                                         state('news', '2026-10-15T05:54:59Z')], [], NOW)
        self.assertEqual(result['counts']['overdue'], 2)
        self.assertEqual(result['counts']['overdueAttempts'], 2)
        self.assertEqual(result['state'], 'degraded')

    def test_exact_grace_boundary_is_fresh(self):
        result = source_health(POLICIES, [state('urgent', '2026-10-15T11:40:00Z'),
                                         state('news', '2026-10-15T05:55:00Z')], [], NOW)
        self.assertEqual(result['state'], 'healthy')

    def test_reference_only_source_does_not_create_false_failure(self):
        result = source_health([POLICIES[0]], [state('urgent', '2026-10-15T12:00:00Z'),
                                              dict(sourceId='reference', status='disabled')], [], NOW)
        self.assertEqual(result['counts']['configured'], 1)
        self.assertEqual(result['state'], 'healthy')

    def test_never_checked_and_future_checks_fail_closed(self):
        result = source_health(POLICIES, [state('urgent', '2026-10-15T12:00:01Z')], [], NOW)
        self.assertEqual(result['counts']['neverChecked'], 1)
        self.assertEqual(result['counts']['clockErrors'], 1)
        self.assertEqual(result['state'], 'degraded')

    def test_empty_policies_are_not_a_healthy_coverage_claim(self):
        self.assertEqual(source_health([], [], [], NOW)['state'], 'unconfigured')

    def test_bad_policy_or_timestamp_is_rejected(self):
        for cadence in [True, 0, float('nan'), float('inf'), '15']:
            with self.subTest(cadence=cadence), self.assertRaises(ValueError):
                source_health([dict(sourceId='one', cadenceMinutes=cadence)], [], [], NOW)
        with self.assertRaises(ValueError):
            source_health([POLICIES[0], POLICIES[0]], [], [], NOW)
        with self.assertRaisesRegex(ValueError, 'timezone'):
            source_health(POLICIES, [state('urgent', '2026-10-15T12:00:00')], [], NOW)

    def test_successful_check_has_no_incident_count_requirement(self):
        # A quiet feed remains healthy; absence of articles is not a source outage.
        checks = [dict(state(p['sourceId'], '2026-10-15T12:00:00Z'), acceptedCount=0) for p in POLICIES]
        self.assertEqual(source_health(POLICIES, checks, [], NOW)['state'], 'healthy')

    def test_full_source_message_is_preserved(self):
        message = 'x'*10000
        check = dict(state('urgent', '2026-10-15T12:00:00Z'), message=message)
        self.assertEqual(source_health([POLICIES[0]], [check], [], NOW)['sources'][0]['message'], message)


class TimingTests(unittest.TestCase):
    def test_bootstraps_prior_status_and_measures_actual_gap(self):
        previous = dict(generatedAt='2026-10-15T04:00:00Z', outcome='degraded')
        current = dict(generatedAt='2026-10-15T12:00:00Z', outcome='complete', run=dict(event='schedule', id='2'))
        result = collection_timing(previous, current, NOW)
        self.assertEqual(result['previousCollectionGapSeconds'], 8*3600)
        self.assertEqual(result['gapsOver20Minutes'], 1)
        self.assertFalse(result['sevenDayElapsed'])
        self.assertFalse(result['futureScheduleGuarantee'])

    def test_repeated_cycles_in_one_run_have_separate_observations(self):
        old = dict(at='2026-10-15T11:45:00Z', outcome='degraded', run=dict(event='schedule', id='1'))
        current = dict(generatedAt='2026-10-15T12:00:00Z', outcome='degraded', run=old['run'])
        previous = dict(collectionTiming=dict(firstObservedAt=old['at'], observations=[old]))
        result = collection_timing(previous, current, NOW)
        self.assertEqual(result['observationCount'], 2)
        self.assertEqual(result['previousCollectionGapSeconds'], 900)

    def test_retains_preceding_checkpoint_and_all_in_window_without_catchup(self):
        rows = [dict(at=at, outcome='complete') for at in
                ['2026-10-01T12:00:00Z', '2026-10-07T12:00:00Z', '2026-10-08T12:00:00Z']]
        previous = dict(collectionTiming=dict(firstObservedAt=rows[0]['at'], observations=rows))
        result = collection_timing(previous, dict(generatedAt='2026-10-15T12:00:00Z', outcome='failed'), NOW)
        self.assertEqual([r['at'] for r in result['observations']],
                         ['2026-10-07T12:00:00Z', '2026-10-08T12:00:00Z', '2026-10-15T12:00:00Z'])
        self.assertTrue(result['sevenDayElapsed'])
        self.assertEqual(result['maximumObservedGapSeconds'], 7*86400)
        self.assertFalse(result['futureScheduleGuarantee'])

    def test_future_history_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'future'):
            collection_timing(dict(generatedAt='2026-10-16T12:00:00Z'),
                              dict(generatedAt='2026-10-15T12:00:00Z', outcome='complete'), NOW)


if __name__ == '__main__':
    unittest.main()
