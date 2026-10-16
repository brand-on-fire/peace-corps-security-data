"""Conservative Fiji Roads factual intake; no network except the injected fetcher.

Complete provider text is analyzed in memory. Public records contain original
factual metadata and links, never copies of the provider's articles or media.
"""
import copy
import datetime as dt
import json
import re
import urllib.parse
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from public_official import base, digest, evidence, iso, moment, sha

ADAPTERS = {'fj-fra'}
SOURCE_ID = 'gov-fj-fra-media'
API = 'https://www.fijiroads.org/index.php/wp-json/wp/v2/posts'
ZONE = ZoneInfo('Pacific/Fiji')
PAGE_SIZE = 20
LIVE_PAGES = 5
BASELINE_PAGES = 2
EXACT_LIMIT = 20
MAX_RESPONSE = 20_000_000
MAX_BATCH = 60_000_000
MAX_REQUESTS = 40
MONTHS = {name: number for number, name in enumerate(
    ('January February March April May June July August September October November December').split(), 1)}
DATE = r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),? (?P<day>\d{1,2})(?:st|nd|rd|th)? (?P<month>[A-Za-z]+)(?: (?P<year>20\d{2}))?'
TIME = r'\d{1,2}:\d{2}\s*(?:am|pm)'


def endpoint(order='modified', page=1):
    return API+'?'+urllib.parse.urlencode(dict(categories=26, orderby=order,
        order='desc', per_page=PAGE_SIZE, page=page))


def prepare_transport_policy(policy, previous, now):
    if policy.get('adapter') != 'fj-fra':
        return dict(policy)
    return dict(policy, url=endpoint())


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.hidden = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag == 'a':
            self.links.extend(v for k, v in attrs if k == 'href' and v)
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden-1)
        if tag in ('p', 'div', 'li'):
            self.parts.append('\n')

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def plain(value):
    if not isinstance(value, str):
        raise ValueError('FRA rendered text is missing')
    parser = Text()
    parser.feed(value)
    parser.close()
    return re.sub(r'\s+', ' ', ''.join(parser.parts)).strip(), parser.links


def gmt(value):
    # WordPress *_gmt is UTC, despite its lack of a serialized suffix.
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?', value):
        raise ValueError('FRA UTC date malformed')
    return iso(value+'Z')


def post(value, now):
    if not isinstance(value, dict) or type(value.get('id')) is not int or value['id'] <= 0:
        raise ValueError('FRA post identity malformed')
    published, modified = gmt(value.get('date_gmt')), gmt(value.get('modified_gmt'))
    if published > modified or modified > now:
        raise ValueError('FRA publication/modification dates conflict')
    address = value.get('link', '')
    parsed = urllib.parse.urlsplit(address)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.fijiroads.org'
            or parsed.query or parsed.fragment or not re.fullmatch(r'/index\.php/\d{4}/\d{2}/\d{2}/[a-z0-9-]+/', parsed.path)):
        raise ValueError('FRA canonical URL outside reviewed provider')
    content = value.get('content', {})
    if not isinstance(content, dict) or type(content.get('protected')) is not bool:
        raise ValueError('FRA complete body status missing')
    body, links = plain(content.get('rendered'))
    title, _ = plain(value.get('title', {}).get('rendered'))
    if not title or value.get('type') != 'post':
        raise ValueError('FRA post type/title malformed')
    # Full factual representation, excluding unrelated volatile WordPress links.
    representation = {key: value.get(key) for key in
        ('id', 'date_gmt', 'modified_gmt', 'status', 'type', 'link', 'title', 'content', 'categories')}
    return dict(id=value['id'], published=published, modified=modified, url=address,
        body=body, title=title, links=links, available=value.get('status') == 'publish' and not content['protected'],
        representation=representation, hash=digest(representation))


def local_day(value, default_year=None):
    match = re.fullmatch(DATE, value.strip(), re.I)
    if not match:
        raise ValueError('FRA interval date requires an explicit calendar date')
    parts = match.groupdict()
    month = MONTHS.get(parts['month'].title())
    year = int(parts['year']) if parts['year'] else default_year
    if not month or not year:
        raise ValueError('FRA interval month/year unresolved')
    day = dt.date(year, month, int(parts['day']))
    if day.strftime('%A').casefold() != parts['weekday'].casefold():
        raise ValueError('FRA interval weekday disagrees with calendar')
    return day


def local_time(day, value):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})\s*(am|pm)', value, re.I)
    hour, minute = int(match[1]), int(match[2])
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise ValueError('FRA interval time invalid')
    hour = hour % 12 + (12 if match[3].lower() == 'pm' else 0)
    return dt.datetime.combine(day, dt.time(hour, minute), ZONE)


def interval(body):
    # Reviewed complete textual interval forms, never CMS expiry.
    date = DATE.replace('?P<weekday>', '?:').replace('?P<day>', '?:').replace('?P<month>', '?:').replace('?P<year>', '?:')
    spanning = re.findall(r'(?:Closure will be in place|The bridge will be closed|will be temporarily closed) from ('+TIME+r') (?:on )?('+date+r') to ('+TIME+r') (?:on )?('+date+r')(?:\.| to facilitate urgent deck repair works due to accelerated deterioration\.)', body, re.I)
    single = re.findall(r'The road will be closed on ('+date+r'), from ('+TIME+r') to ('+TIME+r')\.', body, re.I)
    if len(spanning)+len(single) != 1:
        raise ValueError('FRA exact single closure interval not established')
    if spanning:
        start_time, start_day, end_time, end_day = spanning[0]
        end = local_day(end_day)
        start = local_day(start_day, end.year)
    else:
        day, start_time, end_time = single[0]
        start = end = local_day(day)
    begins, ends = local_time(start, start_time), local_time(end, end_time)
    if not dt.timedelta(0) < ends-begins <= dt.timedelta(hours=48):
        raise ValueError('FRA closure interval is reversed, recurring or outside reviewed duration')
    return iso(begins), iso(ends)


def closure(item):
    body = item['body']
    if not item['available'] or not body:
        return None, 'source_unavailable'
    if re.search(r'\b(cancelled|canceled|postponed|deferred|not (?:be )?closed|remain open|remains open|nightly|daily|every night|weather permitting|tentative)\b', body, re.I):
        return None, 'uncertain_or_changed_closure'
    name = r"[A-Z][A-Za-z0-9 '’()-]*?"
    bridge = re.findall(r'public that ('+name+r' Bridge) along ('+name+r' Road) in ('+name+r') will be closed temporarily to carry out emergency deck replacement works\.', body)
    road = re.findall(r'public that ('+name+r' Road) in ('+name+r') will be temporarily closed to allow for the replacement of a dislocated and broken culvert along the road\.', body)
    deterioration = re.findall(r'public that the ('+name+r' Bridge(?: \d+)?) along ('+name+r' Road),?(?: off ('+name+r' Road),)? will be temporarily closed to allow for (?:emergency )?repair and improvement works following accelerated deterioration\.', body)
    urgent = re.findall(r'public that ('+name+r' Bridge(?: \d+)?) along ('+name+r' Road) in ('+name+r') will be temporarily closed from [^.]+ to facilitate urgent deck repair works due to accelerated deterioration\.', body)
    planned = re.findall(r'public that ('+name+r' Bridge(?: \d+)?) along ('+name+r' Road) off ('+name+r' Road) will be temporarily closed to carry out repair and improvement works\.', body)
    if len(bridge)+len(road)+len(deterioration)+len(urgent)+len(planned) != 1:
        return None, 'unreviewed_closure_description'
    if road and not re.search(r'During the culvert replacement works, vehicle access will be prohibited,', body):
        return None, 'full_vehicle_prohibition_not_established'
    if deterioration and 'emergency repair and improvement' not in body and not re.search(r'access over the bridge will be prohibited', body, re.I):
        return None, 'full_vehicle_prohibition_not_established'
    if re.search(r'\b(partial closure|single lane|one lane|weight restriction|may be closed|might be closed)\b', body, re.I):
        return None, 'partial_or_uncertain_restriction'
    context_kind = 'locality'
    if bridge or urgent:
        asset, route, place = (bridge or urgent)[0]
        reported = ', '.join((asset, route, place))
        cause = 'urgent deck repair after accelerated deterioration' if urgent else 'emergency bridge deck replacement'
    elif deterioration or planned:
        asset, route, off = (deterioration or planned)[0]
        place = 'off '+off if off else route
        reported = ', '.join((asset, route)+(('off '+off,) if off else ()))
        cause = 'bridge repair after accelerated deterioration' if deterioration else 'scheduled bridge repair and improvement works'
        context_kind = 'road_reference'
    else:
        asset, place = road[0]
        route, reported, cause = asset, ', '.join((asset, place)), 'broken or dislocated culvert'
    if asset.casefold() not in item['title'].casefold() or context_kind == 'locality' and place.casefold() not in item['title'].casefold():
        return None, 'title_and_body_asset_disagree'
    try:
        begins, ends = interval(body)
    except ValueError as error:
        return None, str(error)
    return dict(asset=asset, road=route, locality=place, geographicContextKind=context_kind, reportedPlace=reported,
        assetKey=digest([asset.casefold(), route.casefold(), place.casefold()]),
        effectiveFrom=begins, effectiveUntil=ends, cause=cause,
        worksWeatherDependent=bool(re.search(r'works are weather[- ]dependent', body, re.I)),
        diversionAvailable=bool(re.search(r'(?:a bypass will be provided|An alternative route will be available)', body)),
        noDiversion=bool(re.search(r'no alternative diversion route available', body, re.I)),
        pedestrianBypass=bool(re.search(r'a pedestrian bypass will be provided', body, re.I)),
        pedestrianAccessMaintained=bool(re.search(r'pedestrian access will be maintained throughout the repair works', body, re.I))), None


def reopening(item, old, now):
    """Only full all-traffic reopening, explicitly linked to one known episode.

    The actual Vunidilo reopening keeps weight restrictions and is deliberately
    not a resolution. Unlinked/new-asset and anticipated reopenings need review.
    """
    meta = old['sourceMetadata']
    if meta.get('geographicContextKind', 'locality') != 'locality':
        return None
    asset = re.escape(meta['asset'])
    road, place = re.escape(meta['road']), re.escape(meta['locality'])
    identity = asset+(r' along '+road if meta['asset'] != meta['road'] else '')+r' in '+place
    date = DATE.replace('?P<weekday>', '?:').replace('?P<day>', '?:').replace('?P<month>', '?:').replace('?P<year>', '?:')
    matches = re.findall(identity+r' (?:has reopened|was reopened|reopened) to all traffic on ('+date+r') at ('+TIME+r')\.', item['body'], re.I)
    if (not item['available'] or len(matches) != 1 or re.search(
            r'\b(expected|planned|will reopen|weather permitting|weight restrictions?|partial|remain closed|remains closed|not reopened|not open)\b', item['body'], re.I)):
        return None
    known_ids = meta.get('recordIds', [meta['recordId']])
    links = {r['url'] for r in old['reports']}
    if item['id'] not in known_ids and not links.intersection(item['links']):
        return None
    try:
        reopened = iso(local_time(local_day(matches[0][0]), matches[0][1]))
    except ValueError:
        return None
    if not meta['effectiveFrom'] <= reopened <= now or moment(reopened) > moment(meta['effectiveUntil'])+dt.timedelta(days=7):
        return None
    return reopened


class Requests:
    def __init__(self, policy, fetcher, now, fixtures):
        self.policy, self.fetcher, self.now, self.fixtures = policy, fetcher, now, fixtures
        self.receipts, self.total, self.cache = [], 0, {}

    def accept(self, raw, url, status=200):
        if not isinstance(raw, bytes):
            raise ValueError('FRA complete response unavailable')
        self.total += len(raw)
        if len(raw) > MAX_RESPONSE or self.total > MAX_BATCH or len(self.receipts) >= MAX_REQUESTS:
            raise ValueError('FRA complete response/request budget exceeded')
        receipt = dict(url=url, retrievedAt=self.now, httpStatus=status, bytes=len(raw), sha256=sha(raw))
        self.receipts.append(receipt)
        value = json.loads(raw)
        self.cache[url] = value, receipt
        return value, receipt

    def get(self, url):
        if url in self.cache:
            return self.cache[url]
        if not (url.startswith(API+'?') or re.fullmatch(re.escape(API)+r'/[1-9]\d*', url)):
            raise ValueError('FRA request outside reviewed endpoint')
        child = dict(self.policy, url=url, sourceId=SOURCE_ID+'-'+sha(url.encode())[:24])
        result, raw = self.fetcher(child, {}, self.now, self.fixtures)
        status = result.get('httpStatus')
        if status not in (200, 404) or raw is None:
            raise ValueError('FRA complete child response unavailable')
        if status == 404 and '?' in url:
            raise ValueError('FRA page unavailable')
        return self.accept(raw, url, status)


def page(requests, url, now, field, previous=None):
    values, receipt = requests.get(url)
    if not isinstance(values, list) or len(values) > PAGE_SIZE:
        raise ValueError('FRA list response malformed')
    rows = [post(v, now) for v in values]
    dates = [row[field] for row in rows]
    if dates != sorted(dates, reverse=True) or previous and dates and previous < dates[0]:
        raise ValueError('FRA page ordering inconsistent')
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('FRA duplicate post in page')
    return [(row, receipt) for row in rows]


def discovery(item, now, reason, receipt):
    return dict(id='fra-discovery-'+str(item['id']), sourceId=SOURCE_ID,
        title='Road authority notice requires review', url=item['url'], publishedAt=item['published'],
        retrievedAt=now, firstSeenAt=now, language='en', sourceCountryCodes=['FJ'],
        mentionedCountryCodes=['FJ'], categoryCandidates=['transport'], status='needs-review',
        locationStatus='unverified', contentHash=item['hash'], note=reason,
        sourceMetadata=dict(provider='Fiji Roads Authority', recordId=item['id'],
            providerModifiedAt=item['modified'], rawResponseSha256=receipt['sha256']))


def merge(row, old, now):
    if not old:
        return row
    row['firstSeenAt'] = old['firstSeenAt']
    row['occurredAt'] = old['occurredAt']
    latest = row['reports'][0]
    if (old['reports'][0]['contentHash'] == latest['contentHash'] and row['state'] == old['state']
            and row['evidenceStatus'] == old['evidenceStatus']):
        return copy.deepcopy(old)
    row['reports'] = [latest]+[copy.deepcopy(r) for r in old['reports'] if r['id'] != latest['id']]
    if row['updatedAt'] <= old['updatedAt']:
        row['updatedAt'] = now
        row['sourceMetadata']['updateTimeBasis'] = 'observed_changed_representation'
    return row


def source_report(source, row, item, now, summary, receipt):
    title = item.get('title') or 'Fiji Roads Authority post unavailable'
    report = evidence(source, row, title, item['url'], item['published'], now,
        summary, item['representation'], receipt, str(item['id']))
    if not item.get('missing'):
        report['sourceMetadata']['providerModifiedAt'] = item['modified']
    return report


def retain_lifecycle_evidence(source, item, old, now, summary, receipt):
    """Add earlier episode evidence without undoing a later lifecycle update."""
    latest = next((r for r in old['reports']
        if r.get('sourceMetadata', {}).get('externalId') == str(item['id'])), None)
    if latest and latest['contentHash'] == item['hash']:
        return copy.deepcopy(old)
    row = copy.deepcopy(old)
    report = source_report(source, row, item, now, summary, receipt)
    row['reports'] = [report]+[r for r in row['reports'] if r['id'] != report['id']]
    row['sourceMetadata']['recordIds'] = sorted(set(row['sourceMetadata'].get('recordIds', [])+[item['id']]))
    row['sourceMetadata']['additionalEvidenceObservedAt'] = now
    row['updatedAt'] = max(row['updatedAt'], item['modified'], now)
    return row


def make_record(source, item, facts, old, now, receipt):
    iid = old['id'] if old else 'fra-post-'+str(item['id'])+'-FJ'
    summary = 'Fiji Roads Authority announced a full vehicle closure for '+facts['cause']+'.'
    if facts['noDiversion']:
        summary += ' No diversion route was available in the notice.'
    if facts['pedestrianBypass']:
        summary += ' The notice provides a pedestrian bypass.'
    if facts['pedestrianAccessMaintained']:
        summary += ' Pedestrian access was to be maintained.'
    if facts['diversionAvailable']:
        summary += ' A diversion or bypass was announced.'
    if old:
        meta = old['sourceMetadata']
        latest = next((r for r in old['reports']
            if r.get('sourceMetadata', {}).get('externalId') == str(item['id'])), None)
        if meta.get('reopenedAt') and facts['effectiveUntil'] != meta['effectiveUntil']:
            return end_assertion(source, item, old, now, receipt, 'closure_changed_after_reopening_requires_review')
        if meta.get('reopenedAt') or (meta.get('automaticEvidenceState') and
                (item['modified'] < meta['providerModifiedAt'] or latest and latest['contentHash'] == item['hash'])):
            return retain_lifecycle_evidence(source, item, old, now, summary, receipt)
    metadata = dict(provider='Fiji Roads Authority', recordId=item['id'],
        recordIds=sorted(set((old or {}).get('sourceMetadata', {}).get('recordIds', [])+[item['id']])),
        providerPublishedAt=item['published'], providerModifiedAt=item['modified'],
        eventTimeBasis='announced_closure_interval', sourceTimezone='Pacific/Fiji',
        fieldConditionIndependentlyVerified=False, **facts)
    row = base(source, iid, 'FJ', facts['asset']+' closure — '+facts['locality'], summary,
        facts['effectiveFrom'], item['modified'], now, 'transport', {'precision': 'country'}, metadata)
    row['region'] = facts['reportedPlace']
    row['state'] = 'expired' if now >= facts['effectiveUntil'] else 'reported'
    if row['state'] == 'expired':
        row['updatedAt'] = max(row['updatedAt'], facts['effectiveUntil'])
    row['reports'] = [source_report(source, row, item, now, summary, receipt)]
    return merge(row, old, now)


def end_assertion(source, item, old, now, receipt, reason, reopened=None):
    row = copy.deepcopy(old)
    row.update(state='resolved' if reopened else 'expired', evidenceStatus='official' if reopened else 'unverified',
               updatedAt=max(item['modified'], reopened or item['modified']))
    row['sourceMetadata'].update(providerModifiedAt=(old['sourceMetadata']['providerModifiedAt'] if item.get('missing') else item['modified']), automaticEvidenceState=reason,
        currentRepresentationObservedAt=now)
    if reopened:
        row['sourceMetadata']['recordIds'] = sorted(set(row['sourceMetadata'].get('recordIds', [])+[item['id']]))
        row['sourceMetadata']['reopenedAt'] = reopened
        row['summary'] = 'Fiji Roads Authority reported that '+row['sourceMetadata']['asset']+' reopened to all traffic.'
    else:
        row['summary'] = 'The changed source no longer supports the earlier automatic closure claim.'
    row['reports'] = [source_report(source, row, item, now, row['summary'], receipt)]
    return merge(row, old, now)


def collect_transport(source, policy, raw, now, countries, previous, records, fetcher,
                      fixtures=None, history_from=None):
    """Return an atomic source batch; caller commits only after normal gates.

    Live reconciliation and historical pagination have separate checkpoints.
    An unavailable page fails the whole batch; baseline progress is never inferred
    from the current-feed watermark. Partial baseline coverage is explicit.
    """
    if source['id'] != SOURCE_ID or source.get('countryCodes') != ['FJ'] or policy.get('adapter') != 'fj-fra':
        raise ValueError('FRA source identity/jurisdiction mismatch')
    if policy['url'] != endpoint():
        raise ValueError('FRA listing URL differs from reviewed endpoint')
    now = iso(now)
    if 'FJ' not in countries:
        return dict(incidents=[], discoveries=[], state={'activeJurisdiction': False}, evidence=[])
    source = dict(source, _countries=countries)
    requests = Requests(policy, fetcher, now, fixtures)
    requests.accept(raw, policy['url'])
    last = moment(previous['lastSuccessAt']) if previous.get('lastSuccessAt') else moment(now)
    since = iso(last-dt.timedelta(hours=48))
    items, seen, tail, caught = {}, set(), None, False
    for number in range(1, LIVE_PAGES+1):
        batch = page(requests, endpoint(page=number), now, 'modified', tail)
        for item, receipt in batch:
            if item['id'] in seen:
                raise ValueError('FRA duplicate across live pages')
            seen.add(item['id'])
            if item['modified'] >= since:
                items[item['id']] = item, receipt
        tail = batch[-1][0]['modified'] if batch else tail
        if len(batch) < PAGE_SIZE or tail < since:
            caught = True
            break
    if not caught:
        raise ValueError('FRA live reconciliation backlog exceeds page ceiling; watermark retained')

    baseline = copy.deepcopy(previous.get('transportBaseline') or {})
    minimum = history_from or iso(moment(now)-dt.timedelta(days=365))
    if re.fullmatch(r'\d{4}-\d\d-\d\d', minimum):
        minimum += 'T00:00:00Z'
    baseline.setdefault('from', iso(minimum))
    baseline.setdefault('complete', False)
    baseline.setdefault('nextPage', 1)
    reset = False
    if not baseline['complete']:
        number = baseline['nextPage']
        tail, baseline_seen = None, set()
        if number > 1:
            overlap = page(requests, endpoint('date', number-1), now, 'published')
            anchor = [[item['id'], item['published']] for item, _ in overlap]
            if anchor != baseline.get('anchor'):
                number, reset = 1, True
            else:
                tail = overlap[-1][0]['published'] if overlap else None
                baseline_seen = {item['id'] for item, _ in overlap}
        for _ in range(BASELINE_PAGES):
            batch = page(requests, endpoint('date', number), now, 'published', tail)
            for item, receipt in batch:
                if item['id'] in baseline_seen:
                    raise ValueError('FRA duplicate across baseline pages')
                baseline_seen.add(item['id'])
                if item['published'] >= baseline['from']:
                    existing = items.get(item['id'])
                    if existing and existing[0]['hash'] != item['hash']:
                        raise ValueError('FRA representation changed during paginated collection')
                    items[item['id']] = item, receipt
            tail = batch[-1][0]['published'] if batch else tail
            baseline.update(nextPage=number+1, anchor=[[item['id'], item['published']] for item, _ in batch])
            if len(batch) < PAGE_SIZE or tail < baseline['from']:
                baseline.update(complete=True, completedAt=now)
                break
            number += 1
    baseline['resetForChangedAnchor'] = reset

    known = {r['id']: r for r in records.values() if r.get('sourceIds') == [SOURCE_ID]}
    by_post = {}
    for row in known.values():
        for identity in row['sourceMetadata'].get('recordIds', [row['sourceMetadata']['recordId']]):
            if identity in by_post and by_post[identity]['id'] != row['id']:
                raise ValueError('FRA post belongs to conflicting archived episodes')
            by_post[identity] = row
    pending = set(previous.get('transportPendingPostIds', []))
    active = {identity for identity, row in by_post.items() if row['state'] == 'reported'}
    if len(pending | active) > EXACT_LIMIT:
        raise ValueError('FRA pending/current exact-ID reconciliation ceiling reached')
    exact = sorted(pending | active)
    old_ids = sorted(set(by_post)-set(exact))
    cursor = previous.get('transportRecheckCursor', 0)
    for offset in range(min(5, len(old_ids), EXACT_LIMIT-len(exact))):
        exact.append(old_ids[(cursor+offset) % len(old_ids)])
    for identity in exact:
        value, receipt = requests.get(API+'/'+str(identity))
        if receipt['httpStatus'] == 404:
            if not isinstance(value, dict) or value.get('code') != 'rest_post_invalid_id':
                raise ValueError('FRA missing-post response not established')
            old = by_post.get(identity)
            pending.discard(identity)
            if old:
                items[identity] = dict(id=identity, url=next(r['url'] for r in old['reports']
                    if r.get('sourceMetadata', {}).get('externalId') == str(identity)), published=old['sourceMetadata']['providerPublishedAt'],
                    modified=now, body='', links=[], available=False, representation=value,
                    hash=digest(value), missing=True), receipt
            continue
        item = post(value, now)
        if item['id'] != identity:
            raise ValueError('FRA exact-ID response identity mismatch')
        items[identity] = item, receipt

    incoming, discoveries = {}, []
    for item, receipt in sorted(items.values(), key=lambda pair:(pair[0]['modified'], pair[0]['id'])):
        old = by_post.get(item['id'])
        facts, reason = closure(item)
        if old is None:
            candidates = []
            for candidate in list(incoming.values())+list(known.values()):
                meta = candidate['sourceMetadata']
                exact_duplicate = facts and all(facts[key] == meta[key] for key in ('assetKey','effectiveFrom','effectiveUntil'))
                linked_reopening = reopening(item, candidate, now)
                linked_extension = (facts and facts['assetKey'] == meta['assetKey']
                    and facts['effectiveFrom'] == meta['effectiveFrom']
                    and {r['url'] for r in candidate['reports']}.intersection(item['links']))
                if exact_duplicate or linked_reopening or linked_extension:
                    candidates.append(candidate)
            candidates = {r['id']: incoming.get(r['id'],r) for r in candidates}
            if len(candidates) > 1:
                discoveries.append(discovery(item, now, 'Ambiguous linked closure episode', receipt))
                continue
            old = next(iter(candidates.values()), None)
        if old:
            old = incoming.get(old['id'], old)
            # One episode may have several publisher posts. An older newly
            # discovered post is not a rollback of a different post's clock.
            prior_clocks = [r['sourceMetadata']['providerModifiedAt'] for r in old['reports']
                if r.get('sourceMetadata', {}).get('externalId') == str(item['id'])
                and r.get('sourceMetadata', {}).get('providerModifiedAt')]
            if not prior_clocks and old['sourceMetadata']['recordId'] == item['id']:
                prior_clocks = [old['sourceMetadata']['providerModifiedAt']]
            if not item.get('missing') and prior_clocks and item['modified'] < max(prior_clocks):
                # A stale cache or provider rollback cannot certify the old
                # assertion as freshly supported, even if its body still parses.
                raise ValueError('FRA provider modification moved backwards; prior evidence retained for review')
        reopened = reopening(item, old, now) if old else None
        if old and reopened:
            row = end_assertion(source, item, old, now, receipt, 'explicit_all_traffic_reopening', reopened)
        elif facts and old and facts['assetKey'] != old['sourceMetadata']['assetKey']:
            row = end_assertion(source, item, old, now, receipt, 'changed_asset_requires_review')
            discoveries.append(discovery(item, now, 'Changed asset requires review', receipt))
        elif facts and old and facts['effectiveFrom'] != old['sourceMetadata']['effectiveFrom']:
            row = end_assertion(source, item, old, now, receipt, 'changed_episode_requires_review')
            discoveries.append(discovery(item, now, 'Changed closure episode requires review', receipt))
        elif facts and facts['effectiveFrom'] > now:
            pending.add(item['id'])
            discoveries.append(discovery(item, now, 'Upcoming announced closure', receipt))
            if old:
                row = end_assertion(source, item, old, now, receipt, 'rescheduled_future_interval')
            else:
                continue
        elif facts:
            pending.discard(item['id'])
            if facts['effectiveFrom'] < (history_from or baseline['from']):
                discoveries.append(discovery(item, now, 'Closure is outside the published history window', receipt))
                continue
            row = make_record(source, item, facts, old, now, receipt)
        elif old:
            pending.discard(item['id'])
            row = end_assertion(source, item, old, now, receipt, reason)
            discoveries.append(discovery(item, now, reason, receipt))
        else:
            pending.discard(item['id'])
            if re.search(r'\b(closed|closure|reopen|reopened|prohibited|broken|emergency)\b', item['body']+' '+item.get('title', ''), re.I):
                discoveries.append(discovery(item, now, reason, receipt))
            continue
        incoming[row['id']] = row
        for identity in row['sourceMetadata'].get('recordIds', []):
            by_post[identity] = row
    if len(pending) > EXACT_LIMIT:
        raise ValueError('FRA upcoming closure reconciliation ceiling reached')
    return dict(incidents=list(incoming.values()), discoveries=discoveries,
        state=dict(transportLiveFrom=since, transportLiveThrough=now, transportLiveCaughtUp=True,
            transportBaseline=baseline, transportPendingPostIds=sorted(pending),
            transportRecheckCursor=(cursor+min(5, len(old_ids))) % max(1, len(old_ids)),
            transportGeography='country_only_no_reviewed_asset_crosswalk',
            transportScope='category_26_reviewed_full_closure_templates'), evidence=requests.receipts)
