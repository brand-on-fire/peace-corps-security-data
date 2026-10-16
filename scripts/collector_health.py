"""Source freshness and observed collection timing, independent of job success.

Only reviewed collection policies define expected checks. A recent successful
HTTP/adapter check is not proof that the publisher has reported every event.
The bounded timing ledger is diagnostic; incident evidence stays in the archive.
"""
import datetime as dt
import math

UTC = dt.timezone.utc
CHECK_GRACE_SECONDS = 300
HISTORY_DAYS = 7
MAX_HISTORY_ROWS = 2048


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Health timestamp requires a timezone')
    return result.astimezone(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def source_health(policies, coverage, attempts, now):
    """Inspect all configured sources, including ones not due in this run."""
    if now.tzinfo is None:
        raise ValueError('Health evaluation requires a timezone')
    states = {row['sourceId']: dict(row) for row in coverage}
    for row in attempts:
        states[row['sourceId']] = dict(states.get(row['sourceId'], {}), **row)
    rows, seen = [], set()
    for policy in sorted(policies, key=lambda row: row['sourceId']):
        sid, cadence = policy['sourceId'], policy['cadenceMinutes']
        if sid in seen or isinstance(cadence, bool) or not isinstance(cadence, (int, float)) or not math.isfinite(cadence) or cadence < 15:
            raise ValueError('Invalid or duplicated source freshness policy')
        seen.add(sid)
        state = states.get(sid, {})
        last_attempt, last_success = state.get('lastAttemptAt'), state.get('lastSuccessAt')
        attempt_age = int((now-timestamp(last_attempt)).total_seconds()) if last_attempt else None
        success_age = int((now-timestamp(last_success)).total_seconds()) if last_success else None
        maximum_age = int(cadence*60) + CHECK_GRACE_SECONDS
        clock_error = any(age is not None and age < 0 for age in (attempt_age, success_age))
        freshness = ('clock-error' if clock_error else 'never-checked' if success_age is None
                     else 'overdue' if success_age > maximum_age else 'fresh')
        rows.append(dict(sourceId=sid, cadenceMinutes=cadence, graceSeconds=CHECK_GRACE_SECONDS,
                         lastAttemptAt=last_attempt, lastSuccessAt=last_success,
                         attemptAgeSeconds=attempt_age, successfulCheckAgeSeconds=success_age,
                         fetchStatus=state.get('status', 'pending'), freshness=freshness,
                         attemptOverdue=attempt_age is None or attempt_age > maximum_age,
                         message=state.get('message')))
    counts = dict(configured=len(rows), fresh=sum(row['freshness']=='fresh' for row in rows),
                  overdue=sum(row['freshness']=='overdue' for row in rows),
                  neverChecked=sum(row['freshness']=='never-checked' for row in rows),
                  clockErrors=sum(row['freshness']=='clock-error' for row in rows),
                  unavailable=sum(row['fetchStatus']=='unavailable' for row in rows),
                  overdueAttempts=sum(row['attemptOverdue'] for row in rows))
    problems = any(row['freshness'] != 'fresh' or row['fetchStatus'] != 'healthy' for row in rows)
    return dict(evaluatedAt=stamp(now), state='unconfigured' if not rows else 'degraded' if problems else 'healthy',
                meaning='Freshness measures successful source checks, not complete event reporting.',
                counts=counts, sources=rows)


def collection_timing(previous, current, now):
    """Retain seven days plus the preceding checkpoint; never infer missing runs."""
    prior = previous.get('collectionTiming', {})
    rows = list(prior.get('observations', []))
    if not rows and previous.get('generatedAt'):
        rows.append(dict(at=previous['generatedAt'], outcome=previous.get('outcome', 'unknown'),
                         run=previous.get('run'), sourceCounts=previous.get('sourceCounts')))
    row = dict(at=current['generatedAt'], outcome=current['outcome'], run=current.get('run'),
               sourceCounts=current.get('sourceCounts'),
               sourceHealthCounts=current.get('sourceHealth', {}).get('counts'))
    by_time = {item['at']:item for item in rows}
    by_time[row['at']] = row
    rows = sorted(by_time.values(), key=lambda item: timestamp(item['at']))
    if any(timestamp(item['at']) > now for item in rows):
        raise ValueError('Collection observation is in the future')
    cutoff = now-dt.timedelta(days=HISTORY_DAYS)
    before = [item for item in rows if timestamp(item['at']) < cutoff]
    rows = before[-1:] + [item for item in rows if timestamp(item['at']) >= cutoff]
    if len(rows) > MAX_HISTORY_ROWS:
        raise ValueError('Diagnostic collection history exceeds its explicit bound')
    dates = [timestamp(item['at']) for item in rows]
    gaps = [(b-a).total_seconds() for a, b in zip(dates, dates[1:])]
    first = prior.get('firstObservedAt', rows[0]['at'])
    return dict(firstObservedAt=first, windowStart=stamp(cutoff), windowEnd=stamp(now),
                retentionDays=HISTORY_DAYS, includesPriorCheckpoint=bool(before),
                observations=rows, observationCount=len(rows),
                previousCollectionGapSeconds=gaps[-1] if gaps else None,
                maximumObservedGapSeconds=max(gaps) if gaps else None,
                gapsOver20Minutes=sum(gap > 1200 for gap in gaps),
                futureScheduleGuarantee=False,
                sevenDayElapsed=(now-timestamp(first)).total_seconds() >= HISTORY_DAYS*86400)
