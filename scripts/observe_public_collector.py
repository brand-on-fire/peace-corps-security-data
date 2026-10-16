#!/usr/bin/env python3
"""Anonymous, read-only observation; offline replay is the default operation.

This does not run the collector, install a scheduler, or claim browser acceptance.
Complete public responses are retained, gzip-compressed, and hash checked on replay.
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import urllib.parse
import urllib.request

REPOSITORY = 'brand-on-fire/peace-corps-security-data'
API = 'https://api.github.com/repos/' + REPOSITORY
RAW = 'https://raw.githubusercontent.com/' + REPOSITORY
WORKFLOW = 'refresh-data.yml'
FIXED_COMMIT = 'fff24c542a53a448fda8173ebe5c9893eb9bf0bd'
MAX_BODY = 12 * 1024 * 1024
DAY = 86400
SHA = re.compile(r'^[0-9a-f]{40}$')


def stamp(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def date(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp lacks timezone')
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def safe_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.username or p.password or p.port or p.fragment:
        raise ValueError('Not an approved anonymous public URL')
    prefix = '/repos/' + REPOSITORY + '/' if p.hostname == 'api.github.com' else '/' + REPOSITORY + '/'
    if p.hostname not in ('api.github.com', 'raw.githubusercontent.com') or not (p.path + '/').startswith(prefix):
        raise ValueError('URL leaves the exact public repository')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Unexpected public evidence redirect')


def fetch(url):
    safe_url(url)
    request = urllib.request.Request(url, headers={
        'User-Agent': 'PeaceCorpsPublicCollectorObservation/1.0',
        'Accept': 'application/vnd.github+json' if url.startswith(API) else 'application/json',
        'Accept-Encoding': 'identity', 'X-GitHub-Api-Version': '2022-11-28',
    })
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError('Complete evidence body exceeds 12 MiB; no prefix was parsed')
        length = response.headers.get('Content-Length')
        if length is not None and len(raw) != int(length):
            raise ValueError('Incomplete evidence response')
        if response.headers.get('Content-Encoding', 'identity') != 'identity':
            raise ValueError('Unexpected encoded response')
        return raw, {k.lower(): v for k, v in response.headers.items()}


def read_body(root, receipt):
    sha = receipt['sha256']
    if not re.fullmatch('[0-9a-f]{64}', sha):
        raise ValueError('Invalid evidence digest')
    with gzip.open(root/'bodies'/f'{sha}.gz', 'rb') as file:
        raw = file.read(MAX_BODY + 1)
    if len(raw) != receipt['bytes'] or len(raw) > MAX_BODY or digest(raw) != sha:
        raise ValueError('Evidence body is missing, corrupt, or incomplete')
    return raw


def read_ledger(root):
    path = root/'observations.jsonl'
    rows, prior = [], None
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        row = json.loads(line)
        claimed = row.pop('sha256')
        if row.get('previous') != prior or digest(canonical(row)) != claimed:
            raise ValueError('Observation ledger hash chain mismatch')
        for receipt in row['evidence'].values():
            read_body(root, receipt)
        row['sha256'] = claimed
        rows.append(row)
        prior = claimed
    return rows


def append(root, row):
    previous = read_ledger(root)
    row = dict(row, previous=previous[-1]['sha256'] if previous else None)
    row['sha256'] = digest(canonical(row))
    root.mkdir(parents=True, exist_ok=True)
    with (root/'observations.jsonl').open('a') as file:
        file.write(canonical(row).decode() + '\n')
    return row


class Recorder:
    def __init__(self, root, fetcher=fetch):
        self.root, self.fetcher, self.evidence = root, fetcher, {}
        self.cache = {r['url']: r for row in read_ledger(root) for r in row['evidence'].values()}

    def get(self, key, url, immutable=False, as_json=True):
        safe_url(url)
        if immutable and url in self.cache:
            receipt = self.cache[url]
            raw = read_body(self.root, receipt)
        else:
            raw, headers = self.fetcher(url)
            if len(raw) > MAX_BODY:
                raise ValueError('Evidence exceeds whole-body limit')
            receipt = dict(url=url, retrievedAt=stamp(dt.datetime.now(dt.timezone.utc)),
                           status=200, bytes=len(raw), sha256=digest(raw), headers=headers)
            path = self.root/'bodies'/f'{receipt["sha256"]}.gz'
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(gzip.compress(raw, mtime=0))
        self.evidence[key] = receipt
        return json.loads(raw) if as_json else raw.decode()


def next_page(headers, original):
    found = re.findall(r'<([^>]+)>;\s*rel="next"', headers.get('link', ''))
    if len(found) > 1:
        raise ValueError('Ambiguous pagination')
    if not found:
        return None
    url = found[0]
    safe_url(url)
    a, b = urllib.parse.urlsplit(original), urllib.parse.urlsplit(url)
    q1, q2 = urllib.parse.parse_qs(a.query), urllib.parse.parse_qs(b.query)
    q1.pop('page', None)
    page = q2.pop('page', [])
    if a.netloc != b.netloc or a.path != b.path or q1 != q2 or len(page) != 1 or not page[0].isdigit():
        raise ValueError('Pagination changed the evidence query')
    return url


def paginate(recorder, key, url, field=None):
    """Read every page; reject GitHub's filtered 1,000-result search ceiling."""
    original, seen, rows, total, pages = url, set(), [], None, 0
    while url:
        if url in seen or pages >= 20:
            raise ValueError('Pagination cycle or unsupported complete-query size')
        seen.add(url)
        data = recorder.get(f'{key}/{pages}', url)
        headers = recorder.evidence[f'{key}/{pages}']['headers']
        pages += 1
        if field:
            if type(data.get('total_count')) is not int or data['total_count'] > 1000:
                raise ValueError('GitHub query exceeds complete 1,000-result window; narrow the date window')
            if total is not None and total != data['total_count']:
                raise ValueError('Run listing changed during pagination; retry the observation')
            total = data['total_count']
            data = data[field]
        if not isinstance(data, list):
            raise ValueError('Malformed paginated response')
        rows.extend(data)
        url = next_page(headers, original)
    identities = [row['id'] if field else row['sha'] for row in rows]
    if len(set(identities)) != len(identities) or (total is not None and total != len(rows)):
        raise ValueError('Incomplete or duplicated paginated result')
    return rows


def collect(root, now=None, fetcher=fetch, baseline_commit=FIXED_COMMIT):
    if not isinstance(baseline_commit, str) or not SHA.fullmatch(baseline_commit):
        raise ValueError('Observation baseline must be an exact commit SHA')
    now = now or dt.datetime.now(dt.timezone.utc)
    recorder = Recorder(root, fetcher)
    row = dict(schemaVersion=1, observedAt=stamp(now), windowStart=stamp(now-dt.timedelta(days=8)),
               windowEnd=stamp(now), fixedCommit=baseline_commit, evidence=recorder.evidence)
    try:
        repo = recorder.get('repository', API)
        if repo.get('full_name') != REPOSITORY or repo.get('private') is not False or repo.get('default_branch') != 'main':
            raise ValueError('Repository identity/public/default-branch gate failed')
        main = recorder.get('main', API+'/commits/main')
        sha = main['sha']
        if not SHA.fullmatch(sha):
            raise ValueError('Invalid main commit')
        recorder.get('workflow', API+'/actions/workflows/'+WORKFLOW)
        recorder.get('workflowText', RAW+'/'+sha+'/.github/workflows/'+WORKFLOW, immutable=True, as_json=False)
        created = row['windowStart']+'..'+row['windowEnd']
        runs = paginate(recorder, 'runs', API+'/actions/workflows/'+WORKFLOW+'/runs?'+urllib.parse.urlencode(
            dict(created=created, per_page=100)), 'workflow_runs')
        # GitHub's commit path query has complete pagination but no total_count.
        commits = paginate(recorder, 'statusCommits', API+'/commits?'+urllib.parse.urlencode(
            dict(path='docs/data/refresh-status.json', since=row['windowStart'], until=row['windowEnd'], per_page=100)))
        collector_heads = set()
        for item in commits:
            if not SHA.fullmatch(item['sha']):
                raise ValueError('Invalid status commit')
            status = recorder.get('status/'+item['sha'], RAW+'/'+item['sha']+'/docs/data/refresh-status.json', immutable=True)
            if status.get('run', {}).get('collectorHeadSha'):
                collector_heads.add(status['run']['collectorHeadSha'])
        latest_status = recorder.get('latestStatus', RAW+'/'+sha+'/docs/data/refresh-status.json', immutable=True)
        if latest_status.get('run', {}).get('collectorHeadSha'):
            collector_heads.add(latest_status['run']['collectorHeadSha'])
        recorder.get('latestSnapshot', RAW+'/'+sha+'/docs/data/snapshot.json', immutable=True)
        recorder.get('fixedCommit', API+'/commits/'+baseline_commit, immutable=True)
        heads = {run['head_sha'] for run in runs if run['event'] == 'schedule'} | {sha} | collector_heads
        for head in sorted(heads):
            if not SHA.fullmatch(head):
                raise ValueError('Invalid run head')
            recorder.get('ancestry/'+head, API+'/compare/'+baseline_commit+'...'+head, immutable=True)
        row['outcome'] = 'complete'
        # Validate coherence before declaring the observation complete.
        summarize_observation(root, row)
    except Exception as error:
        row['outcome'] = 'failed'
        row['error'] = f'{type(error).__name__}: {error}'
    return append(root, row)


def gap_stats(times):
    dates = sorted(set(map(date, times)))
    gaps = [(b-a).total_seconds() for a, b in zip(dates, dates[1:])]
    ordered = sorted(gaps)
    return dict(count=len(dates), intervals=len(gaps),
                maxSeconds=max(gaps) if gaps else None,
                medianSeconds=(ordered[(len(ordered)-1)//2]+ordered[len(ordered)//2])/2 if gaps else None,
                p95Seconds=ordered[math.ceil(.95*len(ordered))-1] if gaps else None,
                over15Minutes=sum(x > 900 for x in gaps))


def summarize_observation(root, row):
    evidence = row['evidence']
    def payload(key):
        return json.loads(read_body(root, evidence[key]))
    runs = [run for key in evidence if key.startswith('runs/') for run in payload(key)['workflow_runs']]
    if len({r['id'] for r in runs}) != len(runs):
        raise ValueError('Duplicate run IDs in complete observation')
    def fixed(head):
        proof = payload('ancestry/'+head)
        return proof.get('status') in ('ahead', 'identical') and proof.get('behind_by') == 0
    schedules = sorted((r for r in runs if r['event'] == 'schedule'), key=lambda r:r['created_at'])
    run_map = {str(r['id']):r for r in runs}
    publications = {}
    for key in evidence:
        if not key.startswith('status/') and key != 'latestStatus':
            continue
        status = payload(key)
        run = status.get('run', {})
        actual = run_map.get(str(run.get('id')))
        # A successful workflow without matching published run provenance is not
        # proof of a natural publication; failed status retains an old timestamp.
        if not actual or actual['event'] != 'schedule' or run.get('event') != 'schedule':
            continue
        cycle = run.get('cycle')
        code_sha = run.get('collectorHeadSha')
        window_cycle = (type(cycle) is int and 1 <= cycle <= 20 and isinstance(code_sha, str)
                        and SHA.fullmatch(code_sha))
        execution_observed = (actual['status']=='completed' and actual['conclusion']=='success')
        if window_cycle:
            # Source degradation deliberately makes the final workflow non-green.
            # A committed, validated cycle is still publication evidence while
            # later cycles run or after a later cancellation/failure.
            execution_observed = (actual['status']=='in_progress' or
                (actual['status']=='completed' and actual['conclusion'] in ('success','failure','cancelled','timed_out')))
        if (actual['head_sha'] != run.get('headSha') or actual.get('run_attempt', 1) != run.get('attempt')
                or not execution_observed):
            continue
        if (status.get('outcome') not in ('complete', 'degraded')
                or status.get('publicationGeneratedAt') != status.get('generatedAt')
                or not any(source.get('status') == 'healthy' for source in status.get('sources', []))):
            continue
        time = status['publicationGeneratedAt']
        upper_bound = row['observedAt'] if actual['status']=='in_progress' else actual['updated_at']
        if date(time) < date(actual['created_at']) or date(time) > date(upper_bound):
            raise ValueError('Publication timestamp falls outside its verified run')
        identity = (str(actual['id']), run.get('attempt'), cycle if window_cycle else 0, time)
        publication = dict(runId=actual['id'], generatedAt=time, cycle=cycle if window_cycle else None,
            collectorHeadSha=code_sha if window_cycle else actual['head_sha'],
            commit=key.removeprefix('status/') if key.startswith('status/') else payload('main')['sha'],
            fixedCode=fixed(code_sha if window_cycle else actual['head_sha']), outcome=status['outcome'])
        if identity not in publications or key.startswith('status/'):
            publications[identity] = publication
    status, snapshot = payload('latestStatus'), payload('latestSnapshot')
    if (snapshot['generatedAt'] != snapshot['publication']['generatedAt']
            or snapshot['generatedAt'] != status['publicationGeneratedAt']
            or not re.fullmatch('[0-9a-f]{64}', snapshot['publication']['revision'])):
        raise ValueError('Pinned latest publication and refresh status disagree')
    date(snapshot['generatedAt'])
    registry = {source['id']:source for source in snapshot['sources']}
    # A later official-only run must not hide a six-hour RSS source outage.
    # Current snapshot coverage retains each source's latest result across runs.
    failed = [dict({k:s.get(k) for k in ('sourceId','status','lastAttemptAt','lastSuccessAt','message')},
                   url=registry[s['sourceId']].get('feedUrl',registry[s['sourceId']]['url']),
                   cadenceMinutes=registry[s['sourceId']]['cadenceMinutes'])
              for s in snapshot['coverage'] if s.get('status') not in ('healthy','disabled')]
    workflow_text = read_body(root,evidence['workflowText']).decode() if 'workflowText' in evidence else ''
    by_run = {r['id']: sorted((p for p in publications.values() if p['runId']==r['id']),
                             key=lambda p:p['generatedAt']) for r in schedules}
    schedule_rows = [dict(id=r['id'], event=r['event'], createdAt=r['created_at'], updatedAt=r['updated_at'],
                          status=r['status'], conclusion=r['conclusion'], headSha=r['head_sha'],
                          fixedCode=any(p['fixedCode'] for p in by_run[r['id']]) or fixed(r['head_sha']),
                          publication=by_run[r['id']][-1] if by_run[r['id']] else None,
                          publications=by_run[r['id']]) for r in schedules]
    fixed_rows = [r for r in schedule_rows if r['fixedCode']]
    return dict(observedAt=row['observedAt'], mainCommit=payload('main')['sha'],
        completeWindow=dict(start=row['windowStart'],end=row['windowEnd']),
        workflowState=payload('workflow')['state'], configuredCron=re.findall(r"cron:\s*['\"]([^'\"]+)['\"]",workflow_text), totalRuns=len(runs),
        triggerCounts={event:sum(r['event']==event for r in runs) for event in sorted({r['event'] for r in runs})},
        scheduleRuns=schedule_rows, scheduleCreationGaps=gap_stats([r['createdAt'] for r in schedule_rows]),
        fixedScheduleCreationGaps=gap_stats([r['createdAt'] for r in fixed_rows]),
        naturalPublicationGaps=gap_stats([r['generatedAt'] for r in publications.values()]),
        fixedNaturalPublicationGaps=gap_stats([r['generatedAt'] for r in publications.values() if r['fixedCode']]),
        latestPublication=dict(generatedAt=snapshot['generatedAt'], revision=snapshot['publication']['revision'],
            currentCount=len(snapshot['incidents']), archiveCount=snapshot['archiveTotal'],
            sourceCounts=status.get('sourceCounts'), outcome=status['outcome'], run=status.get('run'), failedSources=failed,
            retainedCoverageCounts={state:sum(x.get('status')==state for x in snapshot['coverage'])
                                    for state in sorted({x.get('status') for x in snapshot['coverage']})},
            metadataCoherent=True, browserSchemaAcceptance='not assessed by this observer'),
        fixedCommitTime=payload('fixedCommit')['commit']['committer']['date'])


def report(root):
    rows = read_ledger(root)
    successful = [r for r in rows if r['outcome'] == 'complete']
    if not successful:
        return dict(observations=len(rows), latestObservation=rows[-1] if rows else None,
                    sevenDayObservationComplete=False, quarterHourDeliveryDemonstrated=False,
                    reasons=['No complete evidence observation.'])
    latest = summarize_observation(root, successful[-1])
    now, start = date(rows[-1]['observedAt']), date(rows[-1]['observedAt'])-dt.timedelta(days=7)
    # A quiet week or sparse observations is not a passed week. Require an
    # independent observation at least hourly, and every verified natural run
    # to have matching successful publication evidence across the whole window.
    observation_times = [date(r['observedAt']) for r in successful if date(r['observedAt']) >= start]
    prior = [date(r['observedAt']) for r in successful if date(r['observedAt']) < start]
    observation_bounds = ([max(prior)] if prior else [start]) + observation_times + [now]
    max_observation_gap = max((b-a).total_seconds() for a,b in zip(observation_bounds,observation_bounds[1:]))
    elapsed = (now-date(successful[0]['observedAt'])).total_seconds()
    reasons = []
    if elapsed < 7*DAY:
        reasons.append('Seven days of independent observations have not elapsed.')
    if date(latest['fixedCommitTime']) > start:
        reasons.append('The corrected collector has not existed for this entire seven-day window.')
    if rows[-1]['outcome'] != 'complete':
        reasons.append('The latest observation failed; the last complete evidence is retained.')
    if max_observation_gap > 3600:
        reasons.append('Independent endpoint observations have gaps longer than one hour.')
    if date(latest['completeWindow']['start']) > start or date(latest['completeWindow']['end']) < now:
        reasons.append('Complete provider run history does not cover the current seven-day window.')
    schedules = [r for r in latest['scheduleRuns'] if r['fixedCode'] and
                 (start <= date(r['createdAt']) <= now or
                  any(start <= date(p['generatedAt']) <= now for p in r['publications']))]
    unproved = [r['id'] for r in schedules if not r['publication']]
    pub_times = sorted({date(p['generatedAt']) for r in schedules for p in r['publications']
                        if p['fixedCode'] and start <= date(p['generatedAt']) <= now})
    boundaries = [start] + pub_times + [now]
    max_delivery_gap = max((b-a).total_seconds() for a,b in zip(boundaries,boundaries[1:]))
    if not schedules or unproved:
        reasons.append('Some or all fixed-code natural runs lack matching successful publication evidence.')
    if max_delivery_gap > 900:
        reasons.append('The seven-day natural-publication window contains a gap longer than 15 minutes.')
    return dict(observations=len(rows), failedObservations=sum(r['outcome']!='complete' for r in rows),
                latestObservationAt=rows[-1]['observedAt'], latestComplete=latest,
                sevenDayObservationComplete=elapsed >= 7*DAY and max_observation_gap <= 3600 and rows[-1]['outcome']=='complete',
                quarterHourDeliveryDemonstrated=not reasons, reasons=reasons,
                observationAgeSeconds=elapsed, maxObservationGapSeconds=max_observation_gap,
                maxSevenDayNaturalPublicationGapSeconds=max_delivery_gap, unprovedNaturalRunIds=unproved,
                futureScheduleGuarantee=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parents[1]/'artifacts/collector-observation')
    parser.add_argument('--collect', action='store_true', help='Perform one bounded anonymous public read; no collector execution')
    parser.add_argument('--baseline-commit', default=FIXED_COMMIT,
                        help='Exact collector release commit whose seven-day operation is being observed')
    args = parser.parse_args()
    if args.collect:
        collect(args.directory.resolve(), baseline_commit=args.baseline_commit)
    result = report(args.directory.resolve())
    args.directory.mkdir(parents=True, exist_ok=True)
    (args.directory/'report.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))
    if args.collect and read_ledger(args.directory)[-1]['outcome'] != 'complete':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
