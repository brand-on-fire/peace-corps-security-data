"""Collection-window scheduling and failure tests use only a fake clock/process."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

import public_collection_loop as loop


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'docs/data').mkdir(parents=True)
        self.environment = dict(GITHUB_ACTIONS='true', GITHUB_REF_NAME='main',
            GITHUB_REPOSITORY=loop.REPOSITORY, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1',
            GITHUB_SHA='a'*40, GITHUB_EVENT_NAME='schedule', RUNNER_ENVIRONMENT='github-hosted', RUNNER_OS='Linux')
        self.clock, self.head, self.calls, self.waits, self.messages = 0.0, 'b'*40, [], [], []
        self.outcomes, self.fail_audit, self.fail_publish, self.change_remote, self.corrupt_cycle = {}, False, False, False, False

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, command, root, environment, timeout):
        self.calls.append((self.clock, command, timeout))
        self.assertGreater(timeout, 0)
        self.clock += 10
        if command[-1]=='--audit':
            return int(self.fail_audit)
        if command[-1]=='--publish':
            self.head = format(int(self.head, 16)+1, '040x')
            return int(self.fail_publish)
        cycle = int(environment['COLLECTOR_CYCLE'])
        result = self.outcomes.get(cycle, 'complete')
        status = dict(generatedAt=str(self.clock), outcome=result,
            run=dict(id=environment['GITHUB_RUN_ID'], cycle=cycle if not self.corrupt_cycle else cycle+1,
                     collectorHeadSha=environment['COLLECTOR_CODE_SHA']), sourceHealth=dict(counts=dict(unavailable=1)))
        (self.root/'docs/data/refresh-status.json').write_text(json.dumps(status))
        return 1 if result=='failed' else 0

    def sleep(self, seconds):
        self.assertLessEqual(seconds, 60)
        self.waits.append(seconds)
        self.clock += seconds

    def run_window(self, seconds=loop.MAX_WINDOW_SECONDS, execute=None):
        return loop.run_window(self.root, seconds, self.environment, execute or self.execute,
            local_head=lambda root:self.head,
            remote_head=lambda: 'c'*40 if self.change_remote else self.head,
            monotonic=lambda:self.clock, sleep=self.sleep,
            utcnow=lambda:dt.datetime(2026,10,15,tzinfo=dt.timezone.utc), emit=self.messages.append)

    def test_window_collects_at_quarter_hour_intervals_with_hard_bound(self):
        result = self.run_window()
        starts = [at for at, command, limit in self.calls if command[-1]=='--audit']
        self.assertEqual(starts, [i*900 for i in range(19)])
        self.assertEqual(result['cycles'], 19)
        self.assertLess(self.clock, loop.MAX_WINDOW_SECONDS)
        self.assertEqual(result['outcome'], 'complete')
        self.assertFalse(result['futureScheduleGuarantee'])

    def test_source_failure_is_published_and_next_cycles_continue(self):
        self.outcomes = {1:'degraded', 2:'failed'}
        result = self.run_window()
        self.assertEqual(result['cycles'], 19)
        self.assertEqual(sum(cmd[-1]=='--publish' for _,cmd,_ in self.calls), 19)
        self.assertEqual(result['outcome'], 'degraded')
        self.assertEqual(len(self.messages), 2)

    def test_manual_runs_are_single_pass_only(self):
        self.environment['GITHUB_EVENT_NAME']='workflow_dispatch'
        result = self.run_window(0)
        self.assertEqual(result['cycles'], 1)
        self.assertEqual(self.waits, [])
        with self.assertRaisesRegex(ValueError, 'scheduled runs or reviewed main pushes'):
            self.run_window()

    def test_reviewed_main_code_push_can_start_a_bounded_window(self):
        self.environment['GITHUB_EVENT_NAME']='push'
        result = self.run_window()
        self.assertEqual(result['cycles'], 19)
        self.assertEqual(result['windowSeconds'], loop.MAX_WINDOW_SECONDS)
        # GitHub workflow gates this trigger to the reviewed code/config paths;
        # ordinary data publication commits do not recursively trigger it.
        self.environment['GITHUB_REF_NAME']='feature'
        with self.assertRaises(ValueError):
            self.run_window()

    def test_audit_failure_prevents_collection_and_publish(self):
        self.fail_audit = True
        with self.assertRaisesRegex(RuntimeError, 'audit failed'):
            self.run_window()
        self.assertEqual(len(self.calls), 1)

    def test_remote_main_change_stops_without_collecting_or_merging(self):
        self.change_remote = True
        result = self.run_window()
        self.assertEqual(result['outcome'], 'handoff')
        self.assertEqual(result['cycles'], 0)
        self.assertEqual(self.calls, [])

    def test_remote_change_between_cycles_stops_next_one(self):
        def execute(*args):
            result = self.execute(*args)
            if args[0][-1]=='--publish':
                self.change_remote=True
            return result
        result = self.run_window(execute=execute)
        self.assertEqual(result['cycles'], 1)
        self.assertEqual(result['outcome'], 'handoff')

    def test_push_failure_stops_without_retry(self):
        self.fail_publish = True
        with self.assertRaisesRegex(RuntimeError, 'Publication failed'):
            self.run_window()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.waits, [])

    def test_incorrect_cycle_provenance_never_publishes(self):
        self.corrupt_cycle = True
        with self.assertRaisesRegex(RuntimeError, 'valid new cycle'):
            self.run_window()
        self.assertFalse(any(cmd[-1]=='--publish' for _,cmd,_ in self.calls))

    def test_deadline_exceeded_does_not_start_another_child(self):
        def execute(*args):
            result = self.execute(*args)
            self.clock = loop.CYCLE_RESERVE
            return result
        with self.assertRaises(TimeoutError):
            self.run_window(0, execute)
        self.assertEqual(len(self.calls), 1)

    def test_bad_runner_repo_branch_or_context_is_rejected_before_commands(self):
        for field, value in [('GITHUB_ACTIONS','false'), ('GITHUB_REPOSITORY','elsewhere/private'),
                             ('GITHUB_REF_NAME','branch'), ('RUNNER_ENVIRONMENT','self-hosted'), ('RUNNER_OS','Windows')]:
            old = self.environment[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.environment[field]=value
                self.run_window()
            self.environment[field]=old
        self.assertEqual(self.calls, [])

    def test_unreviewed_window_rejected(self):
        for seconds in [1, 3600, 21600, -1]:
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ValueError, 'duration'):
                self.run_window(seconds)


if __name__ == '__main__':
    unittest.main()
