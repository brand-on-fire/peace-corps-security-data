#!/usr/bin/env python3
"""Small factual adapters for Nepal BIPAD and named-country CDC outbreak notices.

Only the injected fetcher performs network I/O. Complete responses are validated
before callers commit anything. No models, credentials, paid services or writes.
"""
import copy
import datetime as dt
import email.utils
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET

from public_official import base, digest, evidence, iso, moment, sha

ADAPTERS = {'np-bipad', 'cdc-notices'}
BIPAD = 'https://bipadportal.gov.np/api/v1/'
CDC = 'https://wwwnc.cdc.gov/travel/rss/notices.xml'
MAX_PAGES = 10
MAX_REQUESTS = 120
MAX_BYTES = 60_000_000
# IDs/labels verified against the complete agency hazard dictionary on 2026-10-15.
HAZARDS = {
    1: ('Aircraft Accident', 'transport'), 3: ('Avalanche', 'other'),
    4: ('Boat Capsize', 'transport'), 5: ('Bridge Collapse', 'transport'),
    7: ('Drowning', 'other'), 8: ('Earthquake', 'earthquake'),
    9: ('Epidemic', 'health'), 10: ('Fire', 'other'), 11: ('Flood', 'flood'),
    12: ('Forest Fire', 'other'), 15: ('Helicopter Crash', 'transport'),
    17: ('Landslide', 'other'), 26: ('Glacial lake outburst', 'flood'),
    28: ('Inundation', 'flood'), 30: ('Volcanic eruption', 'volcano'),
    31: ('Industrial disaster', 'infrastructure'), 32: ('Mine disaster', 'other'),
    33: ('Pandemics', 'health'), 34: ('Road accident', 'transport'),
    37: ('Environmental pollution', 'other'), 39: ('Food poisoning', 'health'),
    40: ('Gas explosion', 'infrastructure'), 41: ('Leakage (chemical)', 'other'),
    42: ('Leakage (radiation)', 'other'), 43: ('Leakage (toxic gas)', 'other'),
    46: ('Water Accident', 'transport'),
}
LOSS_FIELDS = (
    'peopleDeathCount', 'peopleMissingCount', 'peopleInjuredCount',
    'peopleAffectedCount', 'familyAffectedCount', 'familyRelocatedCount',
    'familyEvacuatedCount', 'infrastructureDestroyedHouseCount',
    'infrastructureAffectedHouseCount', 'infrastructureDestroyedRoadCount',
    'infrastructureAffectedRoadCount', 'infrastructureDestroyedBridgeCount',
    'infrastructureAffectedBridgeCount', 'infrastructureDestroyedElectricityCount',
    'infrastructureAffectedElectricityCount',
)


def normalized(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                           if not unicodedata.combining(c)).split())


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError('Provider identity must be a positive integer')
    return value


def when(value, now):
    parsed = iso(value)
    if moment(parsed) > moment(now):
        raise ValueError('Provider timestamp is in the future')
    return parsed


def prepare_security_policy(policy, previous, now):
    p = dict(policy)
    if p.get('adapter') == 'np-bipad':
        current = moment(now)
        last = moment(previous['lastSuccessAt']) if previous.get('lastSuccessAt') else None
        since = last - dt.timedelta(days=1) if last else current - dt.timedelta(days=2)
        since = max(since, current-dt.timedelta(days=7))
        p['url'] = BIPAD+'incident/?'+urllib.parse.urlencode({
            'reported_on__gt': iso(since), 'ordering': '-reported_on', 'limit': 100})
    elif p.get('adapter') == 'cdc-notices':
        p['url'] = CDC
    return p


class CompleteRequests:
    def __init__(self, policy, fetcher, now, fixtures, sleeper, clock):
        self.policy, self.fetcher, self.now, self.fixtures = policy, fetcher, now, fixtures
        self.receipts, self.total = [], 0
        self.sleeper, self.clock = sleeper, clock
        # Initial RSS fetch has just completed. Conservatively wait a full delay
        # before the first child even if another source was processed meanwhile.
        self.last_request = clock()

    def accept(self, raw, address):
        if not isinstance(raw, bytes):
            raise ValueError('Complete provider response is missing')
        self.total += len(raw)
        if len(raw) > 20_000_000 or self.total > MAX_BYTES:
            raise ValueError('Complete source response budget exceeded')
        receipt = {'url': address, 'retrievedAt': self.now, 'bytes': len(raw), 'sha256': sha(raw)}
        self.receipts.append(receipt)
        return raw

    def get(self, address, key):
        if len(self.receipts) >= MAX_REQUESTS:
            raise ValueError('Complete source request ceiling reached')
        parsed = urllib.parse.urlsplit(address)
        if parsed.scheme != 'https' or parsed.netloc != urllib.parse.urlsplit(self.policy['url']).netloc:
            raise ValueError('Child request left reviewed anonymous HTTPS provider')
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError('Child request contains unsupported URL components')
        if self.policy['adapter'] == 'cdc-notices' and not self.fixtures:
            delay = max(0, 20-(self.clock()-self.last_request))
            if delay:
                self.sleeper(delay)
        child = dict(self.policy, sourceId=self.policy['sourceId']+'-'+key, url=address)
        state, raw = self.fetcher(child, {}, self.now, self.fixtures)
        self.last_request = self.clock()
        if state.get('httpStatus') != 200 or raw is None:
            raise ValueError('Complete child response unavailable: '+state.get('message', str(state.get('httpStatus'))))
        return self.accept(raw, address)


def merge_report(row, old, now):
    """Stable replay, earliest CDC publication anchor and explicit reversions."""
    if not old:
        return row
    new = row['reports'][0]
    cdc = row['sourceMetadata'].get('provider') == 'CDC'
    if cdc:
        old_pub = old.get('sourceMetadata', {}).get('providerPublicationTime', old['occurredAt'])
        new_pub = row['sourceMetadata']['providerPublicationTime']
        if new_pub < old_pub:
            return copy.deepcopy(old)
        row['occurredAt'] = min(old['occurredAt'], row['occurredAt'])
        row['sourceMetadata']['firstPublicationTime'] = row['occurredAt']
    elif row['updatedAt'] < old['updatedAt']:
        return copy.deepcopy(old)
    latest_same = old['reports'][0]['contentHash'] == new['contentHash']
    same_listing = old.get('sourceMetadata', {}).get('providerActiveNotice') == row['sourceMetadata'].get('providerActiveNotice')
    if latest_same and (not cdc or same_listing):
        return copy.deepcopy(old)
    row['firstSeenAt'] = old['firstSeenAt']
    # A -> B -> A must restore A as current without duplicating its report ID.
    row['reports'] = [new] + [copy.deepcopy(r) for r in old['reports'] if r['id'] != new['id']]
    if (cdc and new_pub == old_pub) or row['updatedAt'] <= old['updatedAt']:
        row['updatedAt'] = now
        row['sourceMetadata']['updateTimeBasis'] = 'observed_changed_representation'
    return row


def withhold_previous(source, old, now, title, address, published, obj, receipt, reason,
                      provider_modified=None, clear_location=False, extra_metadata=None):
    """Keep history but end a prior automatic assertion unsupported by new data."""
    row = copy.deepcopy(old)
    row.update(state='expired',evidenceStatus='unverified',severity='unknown',updatedAt=provider_modified or now)
    row['sourceMetadata'].update(automaticEvidenceState=reason,changedRepresentationObservedAt=now)
    row['sourceMetadata'].update(extra_metadata or {})
    if clear_location:
        row['location'] = {'precision':'country'}
        for key in ('admin1Id','admin1Code','admin1Name','admin2Id','admin2Code','admin2Name','adminAssignment'):
            row.pop(key,None)
    row['reports'] = [evidence(source,row,title,address,published,now,
        'Changed source record; earlier details retained in the report history.',obj,receipt,str(old['id']))]
    return merge_report(row,old,now)


def discovery(source, title, address, published, now, content, reason):
    return dict(id='security-discovery-'+digest([source['id'], address])[:24],
                sourceId=source['id'], title=title, url=address, publishedAt=published,
                retrievedAt=now, firstSeenAt=now, language=source.get('language', 'en'),
                sourceCountryCodes=source['countryCodes'], mentionedCountryCodes=[],
                categoryCandidates=['health'] if source.get('_adapter') == 'cdc-notices' else [],
                status='needs-review', locationStatus='unverified', contentHash=digest(content), note=reason)


def np_pages(initial, request):
    document = json.loads(initial)
    seen, items, prior_time = set(), [], None
    initial_url = urllib.parse.urlsplit(request.policy['url'])
    initial_params = dict(urllib.parse.parse_qsl(initial_url.query))
    if set(initial_params) != {'reported_on__gt','ordering','limit'} or initial_params['ordering'] != '-reported_on' or initial_params['limit'] != '100':
        raise ValueError('BIPAD request does not use the reviewed reporting window/order/page size')
    offset = 0
    for page in range(MAX_PAGES):
        current = document.get('results')
        if not isinstance(current, list) or len(current) > 100:
            raise ValueError('BIPAD incident page schema/size invalid')
        if not current:
            return items
        for item in current:
            iid = positive_id(item.get('id'))
            reported = when(item.get('reportedOn'), request.now)
            if iid in seen or (prior_time is not None and reported > prior_time):
                raise ValueError('BIPAD duplicate/order changed across complete pages')
            if reported <= iso(initial_params['reported_on__gt']):
                raise ValueError('BIPAD returned an incident outside requested reporting window')
            seen.add(iid)
            prior_time = reported
            items.append((item, request.receipts[-1]))
        # Provider count and next are invalid sentinels. Probe one further page
        # even after a short page, terminating only on the verified empty page.
        offset += 100
        params = dict(initial_params, offset=str(offset))
        address = BIPAD+'incident/?'+urllib.parse.urlencode(params)
        document = json.loads(request.get(address, 'page-'+str(page+2)))
    raise ValueError('BIPAD page ceiling reached; no partial batch')


def np_modified(request, previous):
    current = moment(request.now)
    last = moment(previous['lastSuccessAt']) if previous.get('lastSuccessAt') else current-dt.timedelta(days=1)
    cutoff = max(last-dt.timedelta(days=1),current-dt.timedelta(days=7))
    seen, items, prior_time = set(), [], None
    for page in range(MAX_PAGES):
        params = {'ordering':'-modified_on','limit':100}
        if page: params['offset'] = page*100
        raw = request.get(BIPAD+'incident/?'+urllib.parse.urlencode(params),'modified-page-'+str(page+1))
        document = json.loads(raw)
        current_items = document.get('results')
        if not isinstance(current_items,list) or len(current_items)>100:
            raise ValueError('BIPAD modification page schema/size invalid')
        if not current_items:
            return items, iso(cutoff)
        reached = False
        for item in current_items:
            iid = positive_id(item.get('id'))
            modified = when(item.get('modifiedOn'),request.now)
            if iid in seen or prior_time is not None and modified>prior_time:
                raise ValueError('BIPAD modification ordering/identity changed')
            seen.add(iid);prior_time=modified
            if moment(modified) <= cutoff:
                reached = True
            else:
                items.append((item,request.receipts[-1]))
        if reached:
            return items, iso(cutoff)
    raise ValueError('BIPAD modification lookback ceiling reached; no partial batch')


def np_loss(iid, request):
    raw = request.get(BIPAD+'loss/'+str(positive_id(iid))+'/', 'loss-'+str(iid))
    value = json.loads(raw)
    if value.get('id') != iid:
        raise ValueError('BIPAD loss identity mismatch')
    counts = {}
    for key in LOSS_FIELDS:
        n = value.get(key)
        if type(n) is not int or n < 0:
            raise ValueError('BIPAD loss count invalid or missing')
        counts[key] = n
    modified = when(value['modifiedOn'], request.now)
    return value, counts, modified, request.receipts[-1]


def np_impact(hazard, counts):
    # A single ordinary accident, drowning or food-poisoning complaint is not an
    # automatic regional security event. Mass casualties/evacuations qualify.
    serious = counts['peopleDeathCount']+counts['peopleMissingCount'] > 0 or counts['peopleInjuredCount'] >= 3
    access = any(counts[k] for k in LOSS_FIELDS if 'Road' in k or 'Bridge' in k or 'Electricity' in k)
    displaced = counts['familyEvacuatedCount']+counts['familyRelocatedCount'] > 0
    if hazard in {7, 34, 39, 46}:
        return serious or access or displaced
    return serious or access or displaced or counts['peopleInjuredCount'] > 0 or counts['familyAffectedCount'] > 0 or counts['peopleAffectedCount'] > 0 or counts['infrastructureDestroyedHouseCount'] > 0 or counts['infrastructureAffectedHouseCount'] > 0


def np_collect(source, initial, request, countries, geo, previous, records):
    rows, held = [], []
    items = np_pages(initial, request)
    modified_items, modified_cutoff = np_modified(request, previous)
    combined = {}
    for item, receipt in items+modified_items:
        old = combined.get(item['id'])
        if old and iso(old[0]['modifiedOn']) == iso(item['modifiedOn']) and digest(old[0]) != digest(item):
            raise ValueError('BIPAD same-version representations disagree')
        if old is None or iso(item['modifiedOn']) > iso(old[0]['modifiedOn']):
            combined[item['id']] = (item,receipt)
    items = list(combined.values())
    if 'NP' not in countries or 'NP' not in source['countryCodes']:
        return rows, held, {'fetchedIncidentCount': len(items), 'eligibleCountry': False}
    for item, receipt in items:
        iid = 'bipad-'+str(item['id'])+'-NP'
        old = records.get(iid)
        for flag in ('verified', 'approved'):
            if type(item.get(flag)) is not bool:
                raise ValueError('BIPAD verification flags missing or invalid')
        title = item.get('title')
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError('BIPAD incident title missing/oversized')
        occurred = when(item['incidentOn'], request.now)
        reported = when(item['reportedOn'], request.now)
        modified = when(item['modifiedOn'], request.now)
        if occurred > reported or modified < reported:
            raise ValueError('BIPAD event/report/modification chronology conflicts')
        if not isinstance(item.get('wards'), list) or any(type(w) is not int or w <= 0 for w in item['wards']):
            raise ValueError('BIPAD ward identifiers invalid')
        address = BIPAD+'incident/'+str(item['id'])+'/'
        if not item['verified'] or not item['approved']:
            if old:
                row = copy.deepcopy(old)
                row.update(state='cancelled', severity='unknown', updatedAt=modified,
                           summary='BIPAD no longer marks this record as both verified and approved.')
                row['sourceMetadata'].update(providerVerified=item['verified'], providerApproved=item['approved'],
                                             withdrawalMeaning='source_verification_withdrawn_not_event_disproved')
                row['reports'] = [evidence(source, row, title, address, reported, request.now, row['summary'], item, receipt, str(item['id']))]
                rows.append(merge_report(row, old, request.now))
            continue
        hazard = item.get('hazard')
        if type(hazard) is not int or hazard not in HAZARDS:
            if old:
                rows.append(withhold_previous(source,old,request.now,title,address,reported,item,receipt,
                    'changed_hazard_requires_review',provider_modified=modified,clear_location=True))
            continue
        if item.get('loss') is None:
            if old:
                rows.append(withhold_previous(source,old,request.now,title,address,reported,item,receipt,
                    'complete_impact_record_no_longer_supplied',provider_modified=modified,clear_location=True))
            held.append(discovery(source, title, address, reported, request.now, item, 'Verified incident has no complete impact record.'))
            continue
        loss, counts, loss_modified, loss_receipt = np_loss(item['loss'], request)
        impact = np_impact(hazard, counts)
        if not impact:
            if old:
                rows.append(withhold_previous(source,old,request.now,title,address,reported,
                    {'incident':item,'loss':loss},receipt,'current_impact_no_longer_meets_admission_rule',
                    provider_modified=max(modified,loss_modified),clear_location=True))
            continue
        location = {'precision': 'country'}
        if item.get('point') is not None:
            point = item['point']
            coordinates = point.get('coordinates') if isinstance(point, dict) else None
            if not isinstance(point, dict) or point.get('type') != 'Point' or not isinstance(coordinates, list) or len(coordinates) != 2 or any(type(n) not in (int, float) or not math.isfinite(n) for n in coordinates):
                raise ValueError('BIPAD occurrence point invalid')
            lon, lat = coordinates
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError('BIPAD occurrence coordinates out of range')
            if geo.country(lon, lat) != 'NP':
                if old:
                    rows.append(withhold_previous(source,old,request.now,title,address,reported,
                        {'incident':item,'loss':loss},receipt,'changed_point_has_conflicting_country',
                        provider_modified=max(modified,loss_modified),clear_location=True))
                held.append(discovery(source, title, address, reported, request.now, item, 'Provider occurrence point does not have unambiguous Nepal containment.'))
                continue
            location = {'precision': 'point', 'lon': lon, 'lat': lat}
        summary_parts = []
        for key, label in [('peopleDeathCount','reported deaths'), ('peopleMissingCount','reported missing'),
                           ('peopleInjuredCount','reported injuries'), ('familyAffectedCount','families affected'),
                           ('familyEvacuatedCount','families evacuated'), ('familyRelocatedCount','families relocated')]:
            if counts[key]:
                summary_parts.append(f'{counts[key]} {label}')
        for kind in ('Road','Bridge','Electricity'):
            for field, label in [('Destroyed','destroyed'),('Affected','affected')]:
                n = counts['infrastructure'+field+kind+'Count']
                if n:
                    summary_parts.append(f'{n} {kind.lower()} infrastructure records {label}')
        summary = 'BIPAD records '+HAZARDS[hazard][0].lower()+'.'
        if summary_parts:
            summary += ' '+ '; '.join(summary_parts)+'.'
        md = dict(provider='BIPAD', recordId=item['id'], providerHazardId=hazard,
                  providerVerified=True, providerApproved=True, providerWardIds=item['wards'],
                  providerReportedOn=item['reportedOn'], providerModifiedOn=item['modifiedOn'],
                  providerIncidentOn=item['incidentOn'], eventTimeBasis='provider_incident_date',
                  eventDatePrecision='day' if 'T00:00:00' in item['incidentOn'] else 'second',
                  providerLossId=item['loss'], reportedImpact=counts, impactGateCurrentlyMet=impact,
                  locationMeaning='reported_occurrence_point_not_impact_boundary',
                  lossResponseSha256=loss_receipt['sha256'])
        row = base(source, iid, 'NP', title.strip(), summary, occurred, max(modified, loss_modified),
                   request.now, HAZARDS[hazard][1], location, md)
        report = evidence(source, row, title.strip(), address, reported, request.now, summary,
                          {'incident':item, 'loss':loss}, receipt, str(item['id']))
        report['sourceMetadata'].update(lossResponseSha256=loss_receipt['sha256'], lossResponseUrl=loss_receipt['url'])
        row['reports'] = [report]
        rows.append(merge_report(row, old, request.now))
    state = {'fetchedIncidentCount':len(items), 'reportingWindow':dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.policy['url']).query)), 'completePagesValidated':True,'modificationLookbackFrom':modified_cutoff,'modificationRecordCount':len(modified_items),'modifiedFilterSupported':False}
    last = previous.get('lastSuccessAt')
    since = state['reportingWindow']['reported_on__gt']
    if last and moment(last) < moment(since):
        state['coverageGap'] = {'from':last, 'through':since, 'reason':'BIPAD outage exceeds bounded seven-day report catch-up.'}
    return rows, held, state


class CDCContent(HTMLParser):
    """Read the entire document; use only the notice's first Key points list."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = self.list_depth = self.li_depth = 0
        self.heading = None
        self.heading_parts, self.parts, self.points, self.notice_text = [], [], [], []
        self.ready = False
        self.notices = self.head_depth = self.title_depth = self.skip_depth = 0
        self.title_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script','style','template'): self.skip_depth += 1
        if tag == 'head': self.head_depth += 1
        if tag == 'title' and self.head_depth: self.title_depth += 1
        if tag == 'div':
            if self.depth: self.depth += 1
            elif 'notice' in attrs.get('class', '').split():
                self.depth = 1
                self.notices += 1
        if not self.depth: return
        if tag == 'h3': self.heading, self.heading_parts = tag, []
        if tag in ('ul','ol') and (self.ready or self.list_depth):
            self.list_depth += 1
            self.ready = False
        if tag == 'li' and self.list_depth:
            if self.li_depth == 0: self.parts = []
            self.li_depth += 1

    def handle_endtag(self, tag):
        if tag in ('script','style','template') and self.skip_depth: self.skip_depth -= 1
        if tag == 'title' and self.title_depth: self.title_depth -= 1
        if tag == 'head' and self.head_depth: self.head_depth -= 1
        if not self.depth: return
        if tag == self.heading:
            self.ready = normalized(' '.join(self.heading_parts)) == 'key points'
            self.heading = None
        if tag == 'li' and self.li_depth:
            self.li_depth -= 1
            if not self.li_depth:
                self.points.append(' '.join(' '.join(self.parts).split()))
        if tag in ('ul','ol') and self.list_depth: self.list_depth -= 1
        if tag == 'div': self.depth -= 1

    def handle_data(self, text):
        if self.skip_depth: return
        if self.title_depth: self.title_parts.append(text)
        if self.depth:
            self.notice_text.append(text)
            if self.heading: self.heading_parts.append(text)
            if self.li_depth: self.parts.append(text)


def cdc_country(title, countries):
    match = re.fullmatch(r'Level ([1-4]) - (.+) in (.+)', title.strip())
    if not match: return None
    level, disease, label = match.groups()
    matches = [code for code,c in countries.items() if normalized(c['name']) == normalized(label)]
    if len(matches) != 1 or any(word in normalized(disease).split() for word in ('global','worldwide')):
        return None
    return matches[0], int(level), disease, title.strip().split(' - ',1)[1]


CDC_PROFILES = {
    'zika-indonesia': ('ID','Zika', r'cases of zika (?:are being|have been) reported in returning travelers from bali, indonesia(?: \(see map\))?\.'),
    'hepatitis-a-costa-rica': ('CR','Hepatitis A', r'there is an outbreak of hepatitis a(?: \(hav\))? in costa rica\. most cases have been reported in san jose province(?: \(see map\))?\.'),
    'yellow-fever-colombia': ('CO','Yellow Fever', r'an increased number of cases of yellow fever have been reported in colombia\.'),
    'chikungunya-costa-rica': ('CR','Chikungunya', r'there is an outbreak of chikungunya in northwest costa rica(?: \(see map\))?\.'),
    'rmsf-mexico': ('MX','Rocky Mountain Spotted Fever', r'illness and death due to rocky mountain spotted fever \(rmsf\) have been reported in the northern region of mexico along the u\.s\. border, particularly in baja california, sonora, chihuahua, coahuila, and nuevo leon\.'),
}


def cdc_detail(raw, expected_title, profile):
    parser = CDCContent()
    parser.feed(raw.decode('utf-8-sig'))
    parser.close()
    title = ' '.join(' '.join(parser.title_parts).split())
    if parser.notices != 1 or parser.depth or not parser.points or len(parser.points)>50:
        raise ValueError('CDC complete Key points structure changed')
    if not normalized(title).startswith(normalized(expected_title)+' - level '):
        raise ValueError('CDC detail title disagrees with RSS identity')
    matches = [p for p in parser.points if re.fullmatch(profile[2],normalized(p))]
    statement = matches[0] if len(matches)==1 else None
    return statement,sha(' '.join(parser.notice_text).encode())


def cdc_areas(statement, code, countries, areas):
    text = normalized(statement)
    matches = []
    for area in areas:
        if area.get('countryCode') != code or area.get('level') != 'ADM1':
            continue
        label = normalized(area['name'])
        # The country Mexico must never assign its same-named state.
        if label == normalized(countries[code]['name']):
            continue
        labels = [label]
        # Costa Rica's reviewed GIS prefixes each name with Provincia; the CDC
        # specifically calls San José a province. Do not apply generic aliases.
        if code == 'CR' and label.startswith('provincia '):
            labels.append(label.removeprefix('provincia '))
        for candidate in labels:
            for found in re.finditer(r'(?<!\w)'+re.escape(candidate)+r'(?!\w)', text):
                if code == 'CR' and not re.search(r'(?<!\w)'+re.escape(candidate)+r' province\b|\bprovince of '+re.escape(candidate)+r'(?!\w)', text):
                    continue
                matches.append((area,found.start(),found.end()))
    # A longer explicit area (e.g. Baja California Sur) must not also assign
    # a shorter, distinct area whose name is embedded in it.
    kept = [a for a,left,right in matches if not any(
        other['id'] != a['id'] and ol <= left and right <= oright and oright-ol > right-left
        for other,ol,oright in matches)]
    return {a['id']:a for a in kept}


def cdc_collect(source, initial, request, countries, records, areas):
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)', initial, re.I):
        raise ValueError('CDC RSS DTD/entity declarations are not permitted')
    root = ET.fromstring(initial)
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('CDC RSS channel missing')
    items = root.findall('./channel/item')
    if not items or len(items) > 200:
        raise ValueError('CDC complete notice list empty/oversized')
    rows, held, seen, listed = [], [], set(), {}
    for item in items:
        title = (item.findtext('title') or '').strip()
        address = (item.findtext('link') or '').strip()
        p = urllib.parse.urlsplit(address)
        if p.scheme != 'https' or p.netloc != 'wwwnc.cdc.gov' or not re.fullmatch(r'/travel/notices/level[1-4]/[a-z0-9-]+', p.path) or p.query or p.fragment:
            raise ValueError('CDC citation left reviewed notice paths')
        if address in seen or not title or len(title) > 500:
            raise ValueError('CDC duplicate/invalid notice identity')
        seen.add(address)
        published = when(email.utils.parsedate_to_datetime(item.findtext('pubDate') or ''), request.now)
        listed[address] = (title,published,ET.tostring(item).decode())
        parsed = cdc_country(title, countries)
        if not parsed:
            held.append(discovery(source,title,address,published,request.now,ET.tostring(item).decode(),
                                  'Notice requires explicit affected-country review; global country lists are not reliable automatic location evidence.'))
            continue
        code, level, disease, detail_title = parsed
        if code not in source['countryCodes']: continue
        if p.path.split('/')[3] != 'level'+str(level):
            raise ValueError('CDC title and URL notice levels disagree')
        slug = p.path.rsplit('/',1)[1]
        profile = CDC_PROFILES.get(slug)
        if profile is None or profile[0] != code or normalized(profile[1]) != normalized(disease):
            held.append(discovery(source,title,address,published,request.now,ET.tostring(item).decode(),
                                  'Named-country notice does not yet have a reviewed factual-statement profile.'))
            continue
        detail = request.get(address, 'notice-'+slug)
        statement, text_hash = cdc_detail(detail, detail_title, profile)
        if not statement:
            old = records.get('cdc-'+slug+'-'+code)
            if old:
                rows.append(withhold_previous(source,old,request.now,detail_title,address,published,
                    {'noticeUrl':address,'publication':published,'completeNoticeTextSha256':text_hash,'evidenceState':'changed_detail_requires_review'},
                    request.receipts[-1],'changed_detail_requires_review',clear_location=True,
                    extra_metadata=dict(providerActiveNotice=False,providerListed=True,providerPublicationTime=published,
                        noticeListingState='listed_but_prior_factual_statement_no_longer_supported',
                        noticeListResponseSha256=request.receipts[0]['sha256'],completeNoticeTextSha256=text_hash)))
            held.append(discovery(source,title,address,published,request.now,sha(detail),
                                  'Complete CDC Key points no longer match a reviewed affirmative current disease/country statement.'))
            continue
        unique = cdc_areas(statement, code, countries, areas)
        iid = 'cdc-'+p.path.rsplit('/',1)[1]+'-'+code
        metadata = dict(provider='CDC', providerNoticeLevel=level, providerActiveNotice=True, providerListed=True,
                        providerPublicationTime=published, firstPublicationTime=published, eventTimeBasis='notice_publication_not_outbreak_onset',
                        exactEventTimeUnknown=True, countryEvidence='exact_notice_title_and_factual_key_point',
                        completeNoticeTextSha256=text_hash, affectedAdmin1Names=[a['name'] for a in unique.values()],
                        sourceAvailableWithoutCharge=True, sourceAttribution='CDC',
                        noticeListResponseSha256=request.receipts[0]['sha256'])
        row = base(source,iid,code,detail_title,statement,published,published,request.now,'health',{'precision':'country'},metadata)
        if len(unique) == 1:
            area = next(iter(unique.values()))
            row.update(admin1Id=area['id'],admin1Code=area['id'],admin1Name=area['name'],
                       adminAssignment=dict(method='explicit_source_place_names',confidence='source_reported_area',pointInferred=False))
            row['location'] = {'precision':'area'}
        row['reports'] = [evidence(source,row,detail_title,address,published,request.now,statement,
                                   {'noticeUrl':address,'publication':published,'title':title,'level':level,'factualStatement':statement,'completeNoticeTextSha256':text_hash},
                                   request.receipts[-1],p.path.rsplit('/',1)[1])]
        rows.append(merge_report(row,records.get(iid),request.now))
    accepted = len(rows)
    updated_ids = {row['id'] for row in rows}
    for old in records.values():
        metadata = old.get('sourceMetadata', {})
        if source['id'] not in old.get('sourceIds', []) or metadata.get('provider') != 'CDC' or old['countryCode'] not in countries:
            continue
        addresses = {r['url'] for r in old['reports'] if r['sourceId'] == source['id']}
        current_urls = addresses.intersection(seen)
        if current_urls and old['id'] not in updated_ids and metadata.get('providerActiveNotice') is True:
            address = sorted(current_urls)[0]
            title,published,representation = listed[address]
            rows.append(withhold_previous(source,old,request.now,title,address,published,
                {'noticeUrl':address,'publication':published,'rssItem':representation,'evidenceState':'changed_notice_identity_requires_review'},
                request.receipts[0],'changed_notice_identity_requires_review',clear_location=True,
                extra_metadata=dict(providerActiveNotice=False,providerListed=True,providerPublicationTime=published,
                    noticeListingState='listed_but_identity_requires_review',noticeListResponseSha256=request.receipts[0]['sha256'])))
        if old['id'] not in updated_ids and addresses.isdisjoint(seen) and metadata.get('providerActiveNotice') is True:
            row = copy.deepcopy(old)
            row.update(state='expired',updatedAt=request.now)
            row['sourceMetadata'].update(providerActiveNotice=False,noticeListingState='no_longer_listed_not_confirmed_resolved',
                                         noticeAbsenceObservedAt=request.now,noticeListResponseSha256=request.receipts[0]['sha256'])
            rows.append(row)
    return rows, held, {'noticeCount':len(items),'explicitCountryNoticeCount':accepted,'globalNoticesRequireReview':True,'unreviewedNoticeProfilesRequireReview':True,
                        'detailHostMinimumIntervalSeconds':20,'activeNoticeUrls':sorted(seen)}


def collect_security_official(source, policy, raw, now, countries, geo, previous,
                              records, fetcher, fixtures=None, areas=None,
                              sleeper=time.sleep, clock=time.monotonic, history_from=None):
    """Same result contract as collect_official; pass reviewed admin areas for CDC."""
    if policy.get('adapter') not in ADAPTERS:
        raise ValueError('Unknown security official adapter')
    expected = BIPAD+'incident/' if policy['adapter']=='np-bipad' else CDC
    parsed = urllib.parse.urlsplit(policy['url'])
    if urllib.parse.urlunsplit(parsed._replace(query='')) != expected or parsed.username or parsed.password:
        raise ValueError('Initial URL is outside reviewed official endpoint')
    source = dict(source,_countries=countries,_adapter=policy['adapter'])
    request = CompleteRequests(policy,fetcher,now,fixtures,sleeper,clock)
    request.accept(raw,policy['url'])
    if policy['adapter'] == 'np-bipad':
        incoming, discoveries, state = np_collect(source,raw,request,countries,geo,previous,records)
    else:
        incoming, discoveries, state = cdc_collect(source,raw,request,countries,records,areas or [])
    if history_from is not None:
        earliest = iso(history_from+'T00:00:00Z' if re.fullmatch(r'\d{4}-\d{2}-\d{2}',history_from) else history_from)
        admitted = []
        for row in incoming:
            if row['id'] not in records and row['occurredAt'] < earliest:
                report = row['reports'][0]
                discoveries.append(discovery(source,row['title'],report['url'],report['publishedAt'],now,
                    report['contentHash'],'Reported occurrence predates the published historical coverage window.'))
            else:
                admitted.append(row)
        state['historyWindowFrom'] = earliest
        state['outsideHistoryWindowCount'] = len(incoming)-len(admitted)
        incoming = admitted
    state.update(responseCount=len(request.receipts),completeResponseBytes=request.total,
                 responseHashes=[r['sha256'] for r in request.receipts])
    return {'incidents':incoming,'discoveries':discoveries,'state':state,'evidence':request.receipts}
