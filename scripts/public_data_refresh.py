#!/usr/bin/env python3
"""Transactional, deadline-bounded public collector with durable failure visibility.

Only complete validated publications replace the last good data directory. A failed
run updates refresh-status.json alone; the workflow publishes that diagnostic and
then reports failure. No caches, artifacts, external schedulers, or credentials.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import public_data_pipeline as pipeline
import public_data_publish as publisher
from collector_health import source_health, collection_timing

REPOSITORY = 'brand-on-fire/peace-corps-security-data'
DEADLINE_SECONDS = 480


def provenance(environment, now):
    if environment.get('GITHUB_ACTIONS') != 'true':
        return dict(event='local', startedAt=now)
    if environment.get('GITHUB_REPOSITORY') != REPOSITORY:
        raise ValueError('Unexpected repository context')
    run_id = environment.get('GITHUB_RUN_ID', '')
    attempt = environment.get('GITHUB_RUN_ATTEMPT', '')
    sha = environment.get('GITHUB_SHA', '')
    if not run_id.isdigit() or not attempt.isdigit() or len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
        raise ValueError('Invalid runner provenance')
    event = environment.get('GITHUB_EVENT_NAME')
    if event not in ('schedule', 'push', 'workflow_dispatch'):
        raise ValueError('Unapproved workflow event')
    result = dict(event=event, startedAt=now, id=run_id, attempt=int(attempt), headSha=sha,
                  url=f'https://github.com/{REPOSITORY}/actions/runs/{run_id}')
    collector_sha = environment.get('COLLECTOR_CODE_SHA')
    cycle = environment.get('COLLECTOR_CYCLE')
    if collector_sha is not None or cycle is not None:
        if (not collector_sha or len(collector_sha) != 40 or any(c not in '0123456789abcdef' for c in collector_sha)
                or not cycle or not cycle.isdigit() or not 1 <= int(cycle) <= 20):
            raise ValueError('Invalid collection-window provenance')
        result.update(collectorHeadSha=collector_sha, cycle=int(cycle))
    return result


def validate_candidate(root, baseline):
    """Check complete archive identity and browser revision before promotion."""
    publisher.audit(root, remote=False)
    data = root/'docs/data'
    snapshot = pipeline.load(data/'snapshot.json')
    previous_snapshot = pipeline.load(baseline/'snapshot.json')
    generated = dt.datetime.fromisoformat(snapshot['generatedAt'].replace('Z', '+00:00'))
    previous_generated = dt.datetime.fromisoformat(previous_snapshot['generatedAt'].replace('Z', '+00:00'))
    if generated < previous_generated:
        raise ValueError('Candidate publication timestamp moved backwards')
    if generated == previous_generated and snapshot.get('publication', {}).get('revision') != previous_snapshot.get('publication', {}).get('revision'):
        raise ValueError('Candidate changed content at the same publication timestamp')
    countries = {c['code']:c for c in pipeline.load(root/'config/countries.json')}
    records = pipeline.read_archive(data, countries)
    prior = pipeline.read_archive(baseline, countries)
    if not set(prior).issubset(records):
        raise ValueError('Candidate removed archived records')
    for identity, old in prior.items():
        row = records[identity]
        if row['firstSeenAt'] != old['firstSeenAt']:
            raise ValueError('Candidate changed the original first-seen time')
        if dt.datetime.fromisoformat(row['updatedAt'].replace('Z', '+00:00')) < dt.datetime.fromisoformat(old['updatedAt'].replace('Z', '+00:00')):
            raise ValueError('Candidate rolled back an incident timestamp')
        revisions = {pipeline.digest(r) for r in row.get('revisions', [])}
        if any(pipeline.digest(r) not in revisions for r in old.get('revisions', [])):
            raise ValueError('Candidate removed an original evidence revision')
        retained = row.get('reports', []) + [r for revision in row.get('revisions', []) for r in revision.get('previousReports', [])]
        hashes = {r['contentHash'] for r in retained}
        if any(r['contentHash'] not in hashes for r in old.get('reports', [])):
            raise ValueError('Candidate removed original source evidence')
    current = snapshot['incidents']
    codes = set(snapshot['publication']['activeCountryCodes'])
    generated_day = snapshot['generatedAt'][:10]
    expected_codes = {c['code'] for c in countries.values() if c['status']=='active' and c['reviewDue'] >= generated_day}
    if codes != expected_codes or {c['code'] for c in snapshot['countries']} != codes:
        raise ValueError('Candidate active-country scope mismatch')
    index = pipeline.load(data/'history.json')
    expected = {key for key,row in records.items() if row['countryCode'] in codes and pipeline.public_incident(row)}
    if len(index) != len(expected) or {r['id'] for r in index} != expected:
        raise ValueError('Candidate archive index is incomplete or duplicated')
    if snapshot['archiveTotal'] != len(expected) or snapshot['history']['recordCount'] != len(expected):
        raise ValueError('Candidate archive totals disagree')
    if len({r['id'] for r in current}) != len(current):
        raise ValueError('Candidate current incidents duplicated')
    for row in current:
        archived = records.get(row['id'])
        if (row['countryCode'] not in codes or row['historical'] or archived is None
                or not pipeline.public_incident(archived) or pipeline.public_snapshot_record(archived) != row):
            raise ValueError('Candidate current incident differs from complete archive')
    advisories = snapshot.get('advisoryRecords', [])
    if (not isinstance(advisories, list) or any(not isinstance(row, dict) for row in advisories)
            or len({row.get('id') for row in advisories}) != len(advisories)):
        raise ValueError('Candidate advisory records malformed or duplicated')
    expected_advisories = pipeline.advisory_snapshot_rows(
        records, codes, snapshot['history']['from'], snapshot['history'].get('to', snapshot['generatedAt']))
    if advisories != expected_advisories:
        raise ValueError('Candidate advisory records differ from complete archive projection')
    # Both projections must describe the same original incident, never two
    # competing representations or an extra record in archive totals.
    by_id = {row['id']:row for row in current}
    if any(row['id'] in by_id and by_id[row['id']] != row for row in advisories):
        raise ValueError('Candidate advisory and current incident disagree')
    unsigned = dict(snapshot)
    publication = unsigned.pop('publication')
    if publication['revision'] != pipeline.publication_revision(unsigned):
        raise ValueError('Candidate browser content revision mismatch')
    return snapshot


def promote(candidate, destination):
    """Same-filesystem directory swap; restore baseline if the second rename fails."""
    pending = destination.with_name('.data-next')
    backup = destination.with_name('.data-previous')
    if pending.exists() or backup.exists():
        raise ValueError('Unresolved prior transaction; refusing replacement')
    try:
        shutil.copytree(candidate, pending)
        destination.rename(backup)
        try:
            pending.rename(destination)
        except Exception:
            backup.rename(destination)
            raise
        shutil.rmtree(backup)
    finally:
        if pending.exists():
            shutil.rmtree(pending)


def refresh(root, now=None, fixtures=None, runner=subprocess.run, environment=None, clock=None):
    # A supplied start time keeps offline replay deterministic. Production reads
    # the wall clock again after the child, rather than judging new source checks
    # against a timestamp captured before a potentially large directory copy.
    if clock is None:
        clock = (lambda: now) if now is not None else lambda: dt.datetime.now(dt.timezone.utc)
    now = now or clock()
    now_text = pipeline.stamp(now)
    environment = dict(os.environ if environment is None else environment)
    data = root/'docs/data'
    previous = pipeline.load(data/'refresh-status.json', {})
    baseline = pipeline.load(data/'snapshot.json')
    status = dict(schemaVersion=1, generatedAt=now_text, outcome='failed',
                  publicationGeneratedAt=baseline['generatedAt'],
                  lastSuccessfulCollectionAt=previous.get('lastSuccessfulCollectionAt'),
                  scheduleGuarantee=False, sources=[], errors=[])
    def evaluation_time():
        # The independently timed child creates its own generation timestamp.
        # It is also the deterministic replay lower bound; source timestamps
        # later than that are still checked rather than silently corrected.
        generated = dt.datetime.fromisoformat(status['generatedAt'].replace('Z', '+00:00'))
        return max(now, clock(), generated)
    try:
        status['run'] = provenance(environment, now_text)
        status['lastScheduledRun'] = status['run'] if status['run']['event']=='schedule' else previous.get('lastScheduledRun')
        with tempfile.TemporaryDirectory(prefix='public-data-refresh-') as temporary:
            stage = Path(temporary)
            shutil.copytree(root/'config', stage/'config')
            shutil.copytree(data, stage/'docs/data')
            command = ['python3', str(root/'scripts/public_data_pipeline.py'), '--root', str(stage)]
            if fixtures:
                command.extend(['--fixtures', str(fixtures)])
            # The collector receives no ephemeral publication token. It needs only
            # public sources and local executables; preserve ordinary runtime PATH.
            collector_environment = {k:v for k,v in environment.items()
                                     if not any(term in k.upper() for term in ('TOKEN','SECRET','PASSWORD','CREDENTIAL','KEY'))}
            runner(command, env=collector_environment, check=True, capture_output=True,
                   timeout=DEADLINE_SECONDS)
            candidate = validate_candidate(stage, data)
            audit = pipeline.load(stage/'docs/data/refresh-status.json')
            status.update(audit)
            status['schemaVersion'] = 1
            evaluated_at = evaluation_time()
            status['completedAt'] = pipeline.stamp(evaluated_at)
            healthy = sum(s.get('status')=='healthy' for s in audit['sources'])
            failed = len(audit['sources'])-healthy
            status['sourceCounts'] = dict(attempted=len(audit['sources']), healthy=healthy, failed=failed)
            if audit['sources'] and not healthy:
                status['outcome'] = 'failed'
                status['errors'] = ['All due sources failed; the last validated publication was retained.']
            else:
                status['outcome'] = 'degraded' if failed else 'complete'
                status['publicationGeneratedAt'] = candidate['generatedAt']
                if healthy:
                    status['lastSuccessfulCollectionAt'] = candidate['generatedAt']
                status['sourceHealth'] = source_health(
                    pipeline.load(root/'config/collection-policy.json')['sources'],
                    candidate.get('coverage', []), status['sources'], evaluated_at)
                if status['sourceHealth']['state'] == 'degraded':
                    status['outcome'] = 'degraded'
                status['collectionTiming'] = collection_timing(previous, status, evaluated_at)
                pipeline.save(stage/'docs/data/refresh-status.json', status)
                promote(stage/'docs/data', data)
    except subprocess.TimeoutExpired:
        status['outcome'] = 'failed'
        status['errors'] = ['Collector exceeded the eight-minute deadline; the last validated publication was retained.']
    except Exception as error:
        # Public diagnostics never include machine paths, subprocess output, or
        # authentication context. Detailed source failures are already public URLs.
        status['outcome'] = 'failed'
        status['errors'] = [f'Collection or validation failed ({type(error).__name__}); the last validated publication was retained.']
    if status['outcome'] == 'failed':
        status['publicationGeneratedAt'] = baseline['generatedAt']
        status['lastSuccessfulCollectionAt'] = previous.get('lastSuccessfulCollectionAt')
        # Failure observations still diagnose sources not attempted in this run.
        # Do not re-date the retained snapshot or turn an old healthy check green.
        try:
            evaluated_at = evaluation_time()
            status['generatedAt'] = status['completedAt'] = pipeline.stamp(evaluated_at)
            status['sourceHealth'] = source_health(
                pipeline.load(root/'config/collection-policy.json')['sources'],
                baseline.get('coverage', []), status['sources'], evaluated_at)
            status['collectionTiming'] = collection_timing(previous, status, evaluated_at)
        except Exception as error:
            status['errors'].append(f'Health diagnostics unavailable ({type(error).__name__}).')
    pipeline.save(data/'refresh-status.json', status)
    summary = environment.get('GITHUB_STEP_SUMMARY')
    if summary:
        text = f"## Public collection: {status['outcome']}\n\n"
        text += f"Trigger: {status.get('run', {}).get('event', 'unknown')}. Last validated publication: {status['publicationGeneratedAt']}.\n\n"
        text += f"Sources attempted/healthy/failed: {status.get('sourceCounts', {})}.\n\n"
        text += f"All configured source checks: {status.get('sourceHealth', {}).get('counts', {})}.\n\n"
        gap = status.get('collectionTiming', {}).get('previousCollectionGapSeconds')
        text += f"Observed time since the previous collection: {gap} seconds.\n\n"
        text += '\n'.join(status['errors']) + '\n\nScheduled runs are best effort; this run does not establish a future delivery guarantee.\n'
        Path(summary).write_text(text)
    return status


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--fixtures', type=Path)
    args = parser.parse_args()
    status = refresh(args.root.resolve(), fixtures=args.fixtures)
    print(json.dumps({k:v for k,v in status.items() if k not in ('sources','changedIncidents')}))
    if status['outcome']=='failed':
        raise SystemExit(1)
