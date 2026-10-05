#!/usr/bin/env python3
"""Public-data-only collector. Standard library; no secrets or model/API clients.

Run in a bundle produced by prepare_public_data_repo.py. --fixtures replays complete
saved responses offline. RSS discovery is deliberately separate from incidents.
"""
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
import gzip
import json
import math
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

UTC = dt.timezone.utc
INDEX_FIELDS = ('id', 'title', 'countryCode', 'admin1Code', 'admin1Name', 'admin2Code',
                'admin2Name', 'category', 'severity', 'evidenceStatus', 'state',
                'occurredAt', 'updatedAt', 'firstSeenAt', 'historical', 'region',
                'location', 'sourceIds', 'aiStatus')
PART_LIMIT = 12_000_000
MAX_RESPONSE = 20_000_000
KEYWORDS = {
    'earthquake': r'\b(earthquakes?|sismos?|terremotos?|séisme)\b',
    'flood': r'\b(flood(?:s|ed|ing)?|inundaci[oó]n(?:es)?|inondation)\b',
    'weather': r'\b(cyclone|typhoon|hurricane|hurac[aá]n|drought|storm|wildfire)\b',
    'volcano': r'\b(volcan(?:o|ic)|volc[aá]n|eruption|erupci[oó]n)\b',
    'conflict': r'\b(armed conflict|clashes|airstrike|shelling|gunfire|bombing)\b',
    'unrest': r'\b(riot|curfew|unrest|couvre-feu|toque de queda)\b',
    'health': r'\b(cholera|chol[eé]ra|dengue|outbreak|epidemic|epidemia|medicine.{0,20}shortage)\b',
    'transport': r'\b(road.{0,25}(closed|blocked)|bridge.{0,25}collaps|landslide|derailment)\b',
    'crime': r'\b(kidnap|abduct|shooting|homicide|secuestro|asesinato)\b',
    'infrastructure': r'\b(blackout|power outage|water shortage|food insecurity)\b',
}


def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def publication_revision(value):
    # Use JSON/number semantics identical to the browser's parser and stringify.
    program = "const fs=require('node:fs'),crypto=require('node:crypto');function sort(x){return Array.isArray(x)?x.map(sort):x&&typeof x==='object'?Object.fromEntries(Object.keys(x).sort().map(k=>[k,sort(x[k])])):x}const v=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(crypto.createHash('sha256').update(JSON.stringify(sort(v))).digest('hex'))"
    return subprocess.run(['node','-e',program], input=encode(value), check=True, capture_output=True).stdout.decode()


def load(path, default=None):
    return json.loads(path.read_bytes()) if path.exists() else default


def save(path, value):
    content = encode(value)
    if path.exists() and path.read_bytes() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_bytes(content)
    temp.replace(path)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def date(value):
    try:
        value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        value = parsedate_to_datetime(value)
    if value.tzinfo is None:
        raise ValueError('Undated/ambiguous source time')
    return stamp(value)


def milliseconds(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Invalid provider timestamp')
    return stamp(dt.datetime.fromtimestamp(value / 1000, UTC))


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value):
    parser = PlainText()
    parser.feed(value)
    return html.unescape(' '.join(parser.parts)).strip()


def parse_feed(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('Feed entity declarations are not permitted')
    root = ET.fromstring(raw)
    if root.tag.split('}')[-1] not in ('rss', 'feed', 'RDF'):
        raise ValueError('Not an RSS/Atom feed')
    rows = []
    for node in (element for element in root.iter() if element.tag.split('}')[-1] == 'item'):
        def text(name):
            child = node.find(name)
            if child is None and not name.startswith('{'):
                child = next((c for c in node if c.tag.split('}')[-1] == name), None)
            return ''.join(child.itertext()).strip() if child is not None else ''
        rows.append(dict(title=text('title'), url=text('link'), publishedAt=text('pubDate'),
                         guid=text('guid'), description=plain(text('description') or
                         text('{http://purl.org/rss/1.0/modules/content/}encoded'))))
    for node in root.findall('{http://www.w3.org/2005/Atom}entry'):
        def atom(name):
            child = node.find('{http://www.w3.org/2005/Atom}' + name)
            return ''.join(child.itertext()).strip() if child is not None else ''
        links = node.findall('{http://www.w3.org/2005/Atom}link')
        link = next((e for e in links if e.get('rel', 'alternate') == 'alternate'), None)
        rows.append(dict(title=atom('title'), url=link.get('href', '') if link is not None else '',
                         publishedAt=atom('published') or atom('updated'), guid=atom('id'),
                         description=plain(atom('summary') or atom('content'))))
    return rows


def safe_url(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == 'https' and bool(parts.hostname) and not parts.username and not parts.password


class ReviewedSourceRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hostname):
        super().__init__()
        self.hostname = hostname

    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not safe_url(newurl) or urllib.parse.urlsplit(newurl).hostname != self.hostname:
            raise ValueError('Redirect rejected before follow: source hostname/HTTPS identity changed')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch(policy, previous, now, fixture=None):
    """Read the entire response or fail; never parse/publish a truncated response."""
    result = dict(sourceId=policy['sourceId'], lastAttemptAt=now, url=policy['url'])
    try:
        if not safe_url(policy['url']):
            raise ValueError('Only anonymous HTTPS sources are permitted')
        if fixture:
            path = fixture / (policy['sourceId'] + '.response')
            if not path.exists():
                raise ValueError('No offline fixture supplied')
            raw = path.read_bytes()
            status, final_url, etag, modified = 200, policy['url'], None, None
            encoding = ''
        else:
            headers = {'User-Agent': 'PublicPolicyHazardMonitor/1.0 (public RSS and official hazard feeds)'}
            if previous.get('etag'):
                headers['If-None-Match'] = previous['etag']
            if previous.get('lastModified'):
                headers['If-Modified-Since'] = previous['lastModified']
            request = urllib.request.Request(policy['url'], headers=headers)
            try:
                opener = urllib.request.build_opener(ReviewedSourceRedirect(urllib.parse.urlsplit(policy['url']).hostname))
                with opener.open(request, timeout=30) as response:
                    raw = response.read()
                    status, final_url = response.status, response.url
                    etag, modified = response.headers.get('ETag'), response.headers.get('Last-Modified')
                    encoding = response.headers.get('Content-Encoding','').lower()
            except urllib.error.HTTPError as error:
                if error.code != 304:
                    raise
                if not previous.get('lastSuccessAt'):
                    raise ValueError('304 without a prior validated representation')
                result.update(status='healthy', httpStatus=304, lastSuccessAt=now,
                              unchanged=True, message='Source confirms its prior validated response is unchanged.')
                return result, None
        if not safe_url(final_url):
            raise ValueError('Redirect did not remain anonymous HTTPS')
        if urllib.parse.urlsplit(final_url).hostname != urllib.parse.urlsplit(policy['url']).hostname:
            raise ValueError('Feed changed hostname; requires an explicit source-identity review')
        if len(raw) > MAX_RESPONSE:
            raise ValueError('Complete response exceeds 20MB policy; entire response rejected')
        wire_bytes = len(raw)
        if encoding == 'gzip':
            raw = gzip.decompress(raw)
        elif encoding == 'deflate':
            raw = zlib.decompress(raw)
        elif encoding not in ('','identity'):
            raise ValueError('Unsupported HTTP content encoding; no partial parsing')
        if len(raw) > MAX_RESPONSE:
            raise ValueError('Complete decoded response exceeds 20MB policy; entire response rejected')
        result.update(httpStatus=status, finalUrl=final_url, bytes=len(raw),
                      wireBytes=wire_bytes, contentEncoding=encoding,
                      sha256=hashlib.sha256(raw).hexdigest(), etag=etag, lastModified=modified)
        return result, raw
    except Exception as error:
        result.update(status='unavailable', lastSuccessAt=previous.get('lastSuccessAt'),
                      message=str(error))
        return result, None


def in_ring(ring, lon, lat):
    if len(ring) < 3:
        return False
    unwrapped = [list(ring[0][:2])]
    for point in ring[1:]:
        x, y = point[:2]
        while x - unwrapped[-1][0] > 180:
            x -= 360
        while x - unwrapped[-1][0] < -180:
            x += 360
        unwrapped.append([x, y])
    center = sum(p[0] for p in unwrapped) / len(unwrapped)
    lon += round((center - lon) / 360) * 360
    inside = False
    for a, b in zip(unwrapped, unwrapped[1:] + unwrapped[:1]):
        cross = (lon-a[0])*(b[1]-a[1])-(lat-a[1])*(b[0]-a[0])
        if abs(cross) < 1e-12 and min(a[0],b[0]) <= lon <= max(a[0],b[0]) and min(a[1],b[1]) <= lat <= max(a[1],b[1]):
            return None  # Boundary points are uncertain, never assigned by tie-break.
        if (a[1] > lat) != (b[1] > lat) and lon < (b[0]-a[0])*(lat-a[1])/(b[1]-a[1])+a[0]:
            inside = not inside
    return inside


def in_polygon(rings, lon, lat):
    return bool(rings) and in_ring(rings[0], lon, lat) is True and all(in_ring(r, lon, lat) is False for r in rings[1:])


class Geography:
    def __init__(self, root, countries):
        self.root, self.countries = root, countries
        self.bounds = load(root/'config/geo/index.json')
        self.country_cache, self.admin_cache = {}, {}

    def country(self, lon, lat):
        matches = []
        for entry in self.bounds:
            code = entry['code']
            if code not in self.countries:
                continue
            if not any(lo <= x <= hi and bottom <= lat <= top
                       for lo, hi, bottom, top in entry['bounds'] for x in (lon, lon-360, lon+360)):
                continue
            if code not in self.country_cache:
                self.country_cache[code] = load(self.root/f'config/geo/{code}.json')
            if any(in_polygon(p['rings'], lon, lat) for p in self.country_cache[code]):
                matches.append(code)
        return matches[0] if len(matches) == 1 else None

    def administrative(self, incident):
        if incident['location']['precision'] != 'point':
            return
        code, location = incident['countryCode'], incident['location']
        assignment = dict(method='strict_point_containment', confidence='spatial_inference_only',
                          geometryResolution='provider_simplified', levels={},
                          note='Occurrence point only; not an impact footprint or Volunteer exposure assessment.')
        matches = {}
        for level in ('ADM1', 'ADM2'):
            key = code+'-'+level
            path = self.root/f'config/map-areas/{key}.geojson'
            if not path.exists():
                assignment['levels'][level] = dict(status='unavailable')
                continue
            if key not in self.admin_cache:
                self.admin_cache[key] = load(path)
            layer = self.admin_cache[key]
            found = []
            for feature in layer['features']:
                geom = feature['geometry']
                polygons = [geom['coordinates']] if geom['type'] == 'Polygon' else geom['coordinates']
                if any(in_polygon(p, location['lon'], location['lat']) for p in polygons):
                    found.append(feature['properties'])
            assignment['levels'][level] = dict(status='matched' if len(found) == 1 else
                                               ('ambiguous' if found else 'unmatched'),
                                               candidateIds=[p['id'] for p in found],
                                               source=layer.get('source'))
            if len(found) == 1:
                matches[level] = found[0]
        if 'ADM2' in matches and ('ADM1' not in matches or matches['ADM2']['parentId'] != matches['ADM1']['id']):
            assignment['levels']['ADM2'].update(status='ambiguous', reason='inconsistent_or_unmatched_parent')
            del matches['ADM2']
        for level, place in matches.items():
            prefix = 'admin'+level[-1]
            incident[prefix+'Id'] = incident[prefix+'Code'] = place['id']
            incident[prefix+'Name'] = place['name']
        incident['adminAssignment'] = assignment


def make_incident(source, iid, title, summary, country, category, occurred, updated, now,
                  location, url, provider, raw, severity='unknown', published=None):
    return dict(id=iid, title=title, summary=summary, countryCode=country['code'], region=country['region'],
                category=category, severity=severity, evidenceStatus='official', state='reported',
                occurredAt=occurred, updatedAt=updated, firstSeenAt=now, location=location,
                sourceIds=[source['id']], historical=False, aiStatus='not-requested', sourceMetadata=provider,
                reports=[dict(id=iid+'-report', incidentId=iid, sourceId=source['id'], title=title, url=url,
                              language=source['language'], publishedAt=published or updated, retrievedAt=now,
                              excerpt=summary, contentHash=digest(raw), independentGroup=source['id'])])


def official_incidents(source, raw, now, countries, geo, iso3):
    rows = []
    if source['kind'] == 'usgs':
        document = json.loads(raw)
        if document.get('type') != 'FeatureCollection' or document['metadata']['count'] != len(document['features']):
            raise ValueError('USGS feature count/schema mismatch')
        for feature in document['features']:
            p = feature['properties']
            if p.get('type') != 'earthquake':
                continue
            coordinates = feature['geometry']['coordinates']
            if len(coordinates) < 3 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in coordinates[:3]):
                raise ValueError('USGS invalid coordinates; response rejected')
            lon, lat, depth = coordinates[:3]
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError('USGS coordinates out of range')
            code = geo.country(lon, lat)
            if not code:
                continue
            pager = p.get('alert')
            summary = f'USGS recorded magnitude {p["mag"]} at depth {depth:g} km. '
            summary += f'Provider PAGER alert: {pager}. ' if pager else 'No PAGER impact classification provided. '
            summary += 'Epicenter is inside the country outline; local effects and Volunteer exposure are not assessed.'
            rows.append(make_incident(source, 'usgs-'+feature['id'], p['title'], summary, countries[code],
                        'earthquake', milliseconds(p['time']), milliseconds(p['updated']), now,
                        dict(precision='point', lon=lon, lat=lat), p['url'],
                        dict(recordId=feature['id'], magnitude=p['mag'], depthKm=depth, pagerAlert=pager,
                             providerReviewStatus=p.get('status')), feature,
                        {'red':'severe', 'orange':'moderate', 'yellow':'moderate'}.get(pager, 'unknown')))
    else:
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('Feed entity declarations are not permitted')
        root = ET.fromstring(raw)
        if root.tag.split('}')[-1] != 'rss':
            raise ValueError('GDACS did not return RSS')
        ns = {'g':'http://www.gdacs.org', 'geo':'http://www.w3.org/2003/01/geo/wgs84_pos#'}
        for element in root.findall('.//item'):
            def get(name):
                return element.findtext(name, default='', namespaces=ns)
            code = iso3.get(get('g:iso3'))
            if code not in countries:
                continue
            kind, event, color = get('g:eventtype'), get('g:eventid'), get('g:alertlevel')
            if not kind or not event:
                raise ValueError('GDACS event identity missing')
            location = dict(precision='country')
            try:
                lon, lat = float(get('geo:Point/geo:long')), float(get('geo:Point/geo:lat'))
                if math.isfinite(lon) and math.isfinite(lat) and geo.country(lon, lat) == code:
                    location = dict(precision='point', lon=lon, lat=lat)
            except ValueError:
                pass
            summary = plain(get('description')) + ' GDACS provider alert: '+color+'. Local effects require review.'
            rows.append(make_incident(source, f'gdacs-{kind}-{event}-{code}', get('title'), summary,
                        countries[code], {'EQ':'earthquake', 'FL':'flood', 'TC':'weather', 'VO':'volcano',
                        'DR':'weather', 'WF':'weather'}.get(kind, 'other'), date(get('g:fromdate')),
                        date(get('g:datemodified') or get('pubDate')), now, location, get('link'),
                        dict(eventType=kind, eventId=event, episodeId=get('g:episodeid'), providerAlert=color,
                             providerIsCurrent=get('g:iscurrent'), providerEnd=get('g:todate')),
                        ET.tostring(element, encoding='unicode'),
                        {'Red':'severe', 'Orange':'moderate'}.get(color, 'unknown'), date(get('pubDate'))))
    return rows


def news_discovery(source, items, now, countries):
    rows = []
    for item in items:
        if not item['title'] or not safe_url(item['url']):
            continue
        text = item['title']+' '+item['description']
        categories = [key for key, pattern in KEYWORDS.items() if re.search(pattern, text, re.I)]
        if not categories:
            continue
        try:
            published = date(item['publishedAt'])
        except (ValueError, TypeError, OverflowError):
            published = None
        # Literal country mentions are candidates, never an event-location assertion.
        mentions = [c['code'] for c in countries.values() if re.search(r'(?<!\w)'+re.escape(c['name'])+r'(?!\w)', text, re.I)]
        rows.append(dict(id='discovery-'+hashlib.sha256((source['id']+'|'+item['url']).encode()).hexdigest()[:24],
                         sourceId=source['id'], title=item['title'], url=item['url'], publishedAt=published,
                         retrievedAt=now, contentHash=digest(item), language=source['language'],
                         sourceCountryCodes=source['countryCodes'], mentionedCountryCodes=mentions,
                         categoryCandidates=categories, status='needs-review', locationStatus='unverified',
                         note='Keyword discovery only. May describe foreign, old, hypothetical or non-incident news. '
                              'Publisher country and publication time are not event location or event time.'))
    return rows


def reviewed_news_incidents(source, items, reviews, now, countries, areas):
    """Only a complete matching approved source representation can be promoted."""
    rows = []
    for review in reviews:
        if review['sourceId'] != source['id'] or review['decision'] != 'include':
            continue
        matching = [i for i in items if i['url'] == review['url'] and digest(i) == review['approvedItemHash']]
        if len(matching) != 1 or review['countryCode'] not in countries:
            continue
        item, code = matching[0], review['countryCode']
        if review['locationPrecision'] not in ('area','country') or not review['summary']:
            raise ValueError('News approval lacks a supported reviewed location/summary')
        published = date(item['publishedAt'])
        row = make_incident(source, 'news-'+hashlib.sha256(item['url'].encode()).hexdigest()[:20],
                            item['title'], review['summary'], countries[code], review['category'],
                            date(review.get('occurredAt', published)), published, now,
                            dict(precision=review['locationPrecision']), item['url'],
                            dict(recordId=item['guid'], eventTimeBasis=review['eventTimeBasis'],
                                 evidenceScope=review['evidenceScope'], reviewedAt=review['reviewedAt'],
                                 reviewMethod='Explicit source-representation approval; no automated model used'), item)
        row['evidenceStatus'] = 'reported'
        row['region'] = review.get('region', countries[code]['region'])
        for level in (1,2):
            name = review.get(f'admin{level}Name')
            if not name:
                continue
            normalize = lambda s: ''.join(c for c in s.casefold() if c.isalnum())
            matching_areas = [a for a in areas if a['countryCode']==code and a['level']==f'ADM{level}' and normalize(a['name'])==normalize(name)]
            if len(matching_areas) != 1:
                raise ValueError('Reviewed administrative name is missing or ambiguous')
            area = matching_areas[0]
            row[f'admin{level}Id'] = row[f'admin{level}Code'] = area['id']
            row[f'admin{level}Name'] = area['name']
            if level == 2 and area['parentId'] != row.get('admin1Code'):
                raise ValueError('Reviewed administrative hierarchy conflicts')
        row['adminAssignment'] = dict(method='explicit_source_place_names', confidence='source_reported_area', pointInferred=False)
        rows.append(row)
    return rows


def read_archive(data, countries):
    rows = {}
    for code in countries:
        root = load(data/f'history/{code}.json', [])
        if isinstance(root, dict):
            if root.get('format') != 'country-parts':
                raise ValueError('Unknown archive format')
            root = [row for name in root['parts'] for row in load(data/'history'/Path(name).name)]
        for row in root:
            if row['id'] in rows:
                raise ValueError('Duplicate archive identity')
            rows[row['id']] = row
    return rows


def write_archive(data, records, countries):
    index, manifest = [], dict(format='explicit-field-projection-with-complete-country-details',
                             recordCount=len(records), projectionFields=list(INDEX_FIELDS)+['reports','reportCount'],
                             omittedFromIndex=['summary','sourceMetadata','revisions','adminAssignment'], countries=[])
    ordered = sorted(records.values(), key=lambda row:(row['occurredAt'],row['id']), reverse=True)
    for row in ordered:
        compact = {key:row[key] for key in INDEX_FIELDS if key in row}
        compact.update(reports=[], reportCount=len(row['reports']))
        index.append(compact)
    for code in countries:
        rows = [r for r in ordered if r['countryCode'] == code]
        groups, group, size = [], [], 3
        for row in rows:
            length = len(encode(row))
            if length+3 > PART_LIMIT:
                raise ValueError('One complete incident exceeds 12MB; no partial write allowed')
            if group and size+length > PART_LIMIT:
                groups.append(group)
                group, size = [], 3
            group.append(row)
            size += length
        groups.append(group)
        parts = []
        if len(groups) == 1:
            save(data/f'history/{code}.json', rows)
        else:
            for number, group in enumerate(groups, 1):
                name = f'{code}-{number:03d}.json'
                save(data/'history'/name, group)
                parts.append(name)
            save(data/f'history/{code}.json', dict(format='country-parts', countryCode=code,
                                                 recordCount=len(rows), parts=parts))
        manifest['countries'].append(dict(code=code, path=f'history/{code}.json', recordCount=len(rows), parts=parts))
    save(data/'history.json', index)
    save(data/'history-manifest.json', manifest)


def run(root, now, fixtures=None):
    now_text = stamp(now)
    data = root/'docs/data'
    all_countries = {c['code']:c for c in load(root/'config/countries.json')}
    countries = {k:c for k,c in all_countries.items() if c['status']=='active' and c['reviewDue'] >= now.date().isoformat()}
    sources = {s['id']:s for s in load(root/'config/sources.json')}
    policies = load(root/'config/collection-policy.json')['sources']
    states = load(data/'collector-state.json', {})
    snapshot = load(data/'snapshot.json')
    baseline_coverage = {c['sourceId']:c for c in snapshot['coverage']}
    for policy in policies:
        sid = policy['sourceId']
        if sid not in states:
            states[sid] = dict(sourceId=sid, lastSuccessAt=baseline_coverage.get(sid,{}).get('lastSuccessAt'))
    records = read_archive(data, all_countries)
    discoveries = {r['id']:r for r in load(data/'discovery.json', [])}
    geo = Geography(root, countries)
    iso3 = load(root/'config/country-iso3.json')
    reviews = load(root/'config/news-reviews.json', [])
    areas = load(root/'config/admin-places.json', [])
    due = [p for p in policies if p['sourceId'] in sources and
           (not states.get(p['sourceId'], {}).get('lastAttemptAt') or
            (now-dt.datetime.fromisoformat(states[p['sourceId']]['lastAttemptAt'].replace('Z','+00:00'))).total_seconds() >= p['cadenceMinutes']*60)]
    changes, source_results = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda p:fetch(p, states.get(p['sourceId'], {}), now_text, fixtures), due))
    for policy, (result, raw) in zip(due, responses):
        sid = policy['sourceId']
        previous = states.get(sid, {})
        source = sources[sid]
        try:
            if raw is not None:
                if policy['mode'] == 'official':
                    incoming = official_incidents(source, raw, now_text, countries, geo, iso3)
                    accepted = len(incoming)
                    message = f'Complete official feed parsed; {accepted} records matched reviewed active-country boundaries/codes.'
                    pending_discoveries = []
                else:
                    items = parse_feed(raw)
                    pending_discoveries = news_discovery(source, items, now_text, countries)
                    incoming = reviewed_news_incidents(source, items, reviews, now_text, countries, areas)
                    accepted = len(incoming)
                    message = f'Complete feed parsed ({len(items)} items); {len(pending_discoveries)} keyword discoveries; {accepted} unchanged source representations match explicit incident approvals.'
                # Stage all source updates before committing any: a malformed item or
                # geographic assignment cannot leave a partially accepted response.
                pending_records = {}
                for row in incoming:
                        old = records.get(row['id'])
                        if old and old['reports'][0]['contentHash'] == row['reports'][0]['contentHash']:
                            continue
                        if old:
                            row['firstSeenAt'] = old['firstSeenAt']
                            row['revisions'] = old.get('revisions', []) + [dict(at=now_text,
                                note='Refreshed source representation; prior archived report retained.', previousReports=old['reports'])]
                        if old and old['location'] == row['location']:
                            for key in ('admin1Id','admin1Code','admin1Name','admin2Id','admin2Code','admin2Name','adminAssignment'):
                                if key in old:
                                    row[key] = old[key]
                        if 'adminAssignment' not in row:
                            geo.administrative(row)
                        pending_records[row['id']] = row
                for row in pending_discoveries:
                        old = discoveries.get(row['id'])
                        row['firstSeenAt'] = old.get('firstSeenAt', old['retrievedAt']) if old else now_text
                        if old and old['contentHash'] != row['contentHash']:
                            row['revisions'] = old.get('revisions', []) + [dict(at=now_text, previous=old['contentHash'])]
                        elif old and 'revisions' in old:
                            row['revisions'] = old['revisions']
                records.update(pending_records)
                changes.extend(pending_records)
                discoveries.update({r['id']:r for r in pending_discoveries})
                result.update(status='healthy', lastSuccessAt=now_text, acceptedCount=accepted, message=message)
        except Exception as error:
            result.update(status='unavailable', lastSuccessAt=previous.get('lastSuccessAt'),
                          message='Source response rejected; prior archive retained: '+str(error))
        states[sid] = dict(previous, **result)
        if result.get('status') != 'healthy':
            # A failed payload's ETag must not suppress a necessary full retry.
            states[sid]['etag'], states[sid]['lastModified'] = previous.get('etag'), previous.get('lastModified')
        source_results.append(result)
    for row in records.values():
        row['historical'] = dt.datetime.fromisoformat(row['occurredAt'].replace('Z','+00:00')) < now-dt.timedelta(days=30)
        if row['historical'] and row['state'] == 'reported':
            row['state'] = 'expired'
    snapshot['generatedAt'] = now_text
    snapshot['mode'] = 'fixture' if fixtures else 'live'
    snapshot['schemaVersion'] = 1
    snapshot['archiveTotal'] = snapshot['history']['recordCount'] = len(records)
    snapshot['incidents'] = sorted([r for r in records.values() if not r['historical'] and r['countryCode'] in countries],
                                   key=lambda r:r['occurredAt'], reverse=True)
    snapshot['history']['to'] = now_text
    snapshot['collector'] = dict(kind='public-standard-runner', requestedOfficialCadenceMinutes=15,
                                  requestedNewsCadenceMinutes=360, latestRunAt=now_text,
                                  activeReviewedCountries=len(countries), discoveryCount=len(discoveries),
                                  newsPromotion='review-required', scheduleGuarantee=False)
    coverage = {c['sourceId']:c for c in snapshot['coverage']}
    for sid, state in states.items():
        coverage[sid] = {k:state.get(k) for k in ('sourceId','lastAttemptAt','lastSuccessAt','status','message')}
    snapshot['coverage'] = list(coverage.values())
    snapshot['countries'] = list(countries.values())
    snapshot['sources'] = list(sources.values())
    snapshot.pop('publication', None)
    snapshot['publication'] = dict(format='peace-corps-public-data-v1', generatedAt=now_text,
                                  revision=publication_revision(snapshot), activeCountryCodes=sorted(countries))
    write_archive(data, records, all_countries)
    save(data/'snapshot.json', snapshot)
    save(data/'discovery.json', sorted(discoveries.values(), key=lambda r:r['retrievedAt'], reverse=True))
    save(data/'collector-state.json', states)
    audit = dict(generatedAt=now_text, requested=len(due), changedIncidents=changes,
                 archiveCount=len(records), currentCount=len(snapshot['incidents']), discoveryCount=len(discoveries),
                 activeReviewedCountries=len(countries), sources=source_results)
    save(data/'refresh-status.json', audit)
    return audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--fixtures', type=Path)
    args = parser.parse_args()
    audit = run(args.root.resolve(), dt.datetime.now(UTC), args.fixtures)
    print(json.dumps({k:v for k,v in audit.items() if k != 'sources'}))
