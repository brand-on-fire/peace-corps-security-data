#!/usr/bin/env python3
"""Bounded collection window on the existing public standard GitHub runner.

No self-dispatch, external scheduler, cache, artifact, or paid runtime is used.
Scheduled runs collect repeatedly instead of requiring a new runner each time.
The next runner still depends on best-effort GitHub scheduling.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

from public_data_refresh import REPOSITORY, provenance

INTERVAL_SECONDS = 900
MAX_WINDOW_SECONDS = 285*60
AUDIT_TIMEOUT = 120
REFRESH_TIMEOUT = 540
PUBLISH_TIMEOUT = 120
CYCLE_RESERVE = AUDIT_TIMEOUT+REFRESH_TIMEOUT+PUBLISH_TIMEOUT


def process(command, root, environment, timeout):
    # Child output goes to the runner's full log, never to a truncated parser.
    return subprocess.run(command, cwd=root, env=environment, timeout=timeout, check=False).returncode


def checkout_head(root):
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root,
                            check=True, capture_output=True, text=True, timeout=30)
    value = result.stdout.strip()
    if len(value) != 40 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid checkout revision')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Unexpected repository-head redirect')


def current_remote_head():
    # Anonymous read of the exact public repository; no persistent credential.
    request = urllib.request.Request('https://api.github.com/repos/'+REPOSITORY+'/git/ref/heads/main',
                                     headers={'User-Agent':'PeaceCorpsCollector/1.0',
                                              'Accept':'application/vnd.github+json',
                                              'Accept-Encoding':'identity',
                                              'X-GitHub-Api-Version':'2022-11-28'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(1024*1024+1)
        length = response.headers.get('Content-Length')
        if len(raw) > 1024*1024 or (length and len(raw) != int(length)):
            raise ValueError('Repository-head response incomplete or oversized')
        if response.headers.get('Content-Encoding', 'identity') != 'identity':
            raise ValueError('Unexpected repository-head encoding')
        payload = json.loads(raw)
    value = payload.get('object', {}).get('sha', '')
    if payload.get('ref') != 'refs/heads/main' or len(value) != 40 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid public main revision')
    return value


def run_window(root, window_seconds=0, environment=None, execute=process,
               local_head=checkout_head, remote_head=current_remote_head,
               monotonic=time.monotonic, sleep=time.sleep,
               utcnow=lambda: dt.datetime.now(dt.timezone.utc), emit=print):
    environment = dict(os.environ if environment is None else environment)
    # Long windows are admitted only in the exact reviewed workflow context.
    if environment.get('GITHUB_ACTIONS') != 'true' or environment.get('GITHUB_REF_NAME') != 'main':
        raise ValueError('Collection windows run only in the audited public main workflow')
    provenance(environment, utcnow().isoformat())
    if environment.get('RUNNER_ENVIRONMENT') != 'github-hosted' or environment.get('RUNNER_OS') != 'Linux':
        raise ValueError('Collection window requires the reviewed hosted Linux runner')
    if window_seconds not in (0, MAX_WINDOW_SECONDS):
        raise ValueError('Collection window is outside the reviewed duration')
    if window_seconds and environment.get('GITHUB_EVENT_NAME') not in ('schedule', 'push'):
        raise ValueError('Only scheduled runs or reviewed main pushes may use a repeated collection window')
    environment['COLLECTOR_CODE_SHA'] = local_head(root)
    start, cycles, unhealthy = monotonic(), 0, False
    deadline = start+window_seconds if window_seconds else start+CYCLE_RESERVE
    def bounded(command, context, limit):
        remaining = deadline-monotonic()
        if remaining <= 0:
            raise TimeoutError('Collection window reached its hard deadline')
        return execute(command, root, context, min(limit, remaining))
    while True:
        if cycles and monotonic()+CYCLE_RESERVE > deadline:
            break
        # Every publication advances local HEAD. A different remote HEAD means a
        # human/new source release changed main; stop rather than run stale code,
        # rebase unreviewed data, or overwrite it. The next run reads fresh main.
        if remote_head() != local_head(root):
            emit('::notice title=Collector handoff::Public main changed; collection window stopped before the next cycle.')
            return dict(outcome='handoff', cycles=cycles, unhealthyCyclesObserved=unhealthy)
        audit = bounded(['python3','scripts/public_data_publish.py','--audit'], environment, AUDIT_TIMEOUT)
        if audit:
            raise RuntimeError('Public repository/storage audit failed; collection window stopped')
        cycle_environment = dict(environment, COLLECTOR_CYCLE=str(cycles+1))
        status_path = root/'docs/data/refresh-status.json'
        before = status_path.read_bytes() if status_path.exists() else None
        refresh_result = bounded(['python3','scripts/public_data_refresh.py'], cycle_environment, REFRESH_TIMEOUT)
        # A rejected or killed wrapper must not publish stale success metadata.
        after = status_path.read_bytes()
        status = json.loads(after)
        run = status.get('run', {})
        if (refresh_result not in (0, 1) or after == before or
                run.get('id') != environment['GITHUB_RUN_ID'] or
                run.get('cycle') != cycles+1 or run.get('collectorHeadSha') != environment['COLLECTOR_CODE_SHA'] or
                status.get('outcome') not in ('complete','degraded','failed')):
            raise RuntimeError('Collector did not produce a valid new cycle diagnostic; publication stopped')
        publish_result = bounded(['python3','scripts/public_data_publish.py','--publish'], environment, PUBLISH_TIMEOUT)
        if publish_result:
            raise RuntimeError('Publication failed; collection window stopped without retry or overwrite')
        cycles += 1
        if status['outcome'] != 'complete':
            unhealthy = True
            counts = status.get('sourceHealth', {}).get('counts', {})
            emit('::warning title=Source collection health::Cycle '+str(cycles)+' is '+status['outcome']+
                 '; unavailable='+str(counts.get('unavailable', 'unknown'))+
                 ', overdue='+str(counts.get('overdue', 'unknown'))+
                 '. Complete source diagnostics are in docs/data/refresh-status.json.')
        if not window_seconds:
            break
        # Monotonic cadence skips missed slots: no burst of catch-up requests.
        target = start+(int((monotonic()-start)//INTERVAL_SECONDS)+1)*INTERVAL_SECONDS
        if target+CYCLE_RESERVE > deadline:
            break
        while monotonic() < target:
            sleep(min(60, target-monotonic()))
    return dict(outcome='degraded' if unhealthy else 'complete', cycles=cycles,
                unhealthyCyclesObserved=unhealthy, windowSeconds=window_seconds,
                futureScheduleGuarantee=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scheduled-window', action='store_true',
                        help='Use the bounded window for a natural schedule or reviewed main code/config push')
    args = parser.parse_args()
    try:
        result = run_window(Path.cwd(), MAX_WINDOW_SECONDS if args.scheduled_window else 0)
        print(json.dumps(result))
        # Data is already published; a green workflow must not conceal outages.
        return 1 if result['unhealthyCyclesObserved'] else 0
    except Exception as error:
        print('::error title=Public collection window::Stopped ('+type(error).__name__+'); last published data retained.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
