#!/usr/bin/env python3
"""Public-data-only collector. Standard library; no secrets or model/API clients.

Run in a bundle produced by prepare_public_data_repo.py. --fixtures replays complete
saved responses offline. RSS discovery is deliberately separate from incidents.
"""
import argparse
import concurrent.futures
import contextlib
import copy
import datetime as dt
import hashlib
import html
import gzip
import io
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from public_weather_relevance import (SCOPE as WEATHER_SCOPE, decision as weather_decision,
    annotate as annotate_weather, exclusion_index as weather_exclusion_index,
    preserve_significant_history)

UTC = dt.timezone.utc
INDEX_FIELDS = ('id', 'title', 'countryCode', 'admin1Code', 'admin1Name', 'admin2Code',
                'admin2Name', 'category', 'severity', 'evidenceStatus', 'state',
                'occurredAt', 'updatedAt', 'firstSeenAt', 'historical', 'region',
                'location', 'sourceIds', 'aiStatus')
PART_LIMIT = 12_000_000
MAX_RESPONSE = 20_000_000
MAX_SNAPSHOT = 10_000_000
MAX_HISTORY_INDEX = 25_000_000
INFRASTRUCTURE_DEADLINE_SECONDS = 90
CARIBE_DEADLINE_SECONDS = 90


class _SourceDeadlineExpired(BaseException):
    """Escape request/parser Exception handlers before rejecting the source batch."""


@contextlib.contextmanager
def source_deadline(seconds, label):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 480:
        raise ValueError('Source wall-time budget is invalid')
    if label not in ('Municipality', 'WordPress history', 'Caribe news'):
        raise ValueError('Unreviewed source deadline label')
    # The public collector runs on the Ubuntu main thread. Refuse unsupported
    # execution before requesting the source, rather than silently losing its cap.
    if (threading.current_thread() is not threading.main_thread()
            or not all(hasattr(signal, name) for name in ('SIGALRM', 'ITIMER_REAL', 'setitimer', 'getitimer',
                'pthread_sigmask', 'sigpending', 'sigwait', 'SIG_BLOCK', 'SIG_SETMASK'))):
        raise ValueError(f'{label} wall-time isolation requires a POSIX main thread')
    alarm = {signal.SIGALRM}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, alarm)
    owned = False
    def expired(signum, frame):
        raise _SourceDeadlineExpired()
    try:
        try:
            if signal.SIGALRM in mask or signal.SIGALRM in signal.sigpending():
                raise ValueError(f'{label} wall-time isolation cannot use a blocked or pending alarm')
            if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
                raise ValueError(f'{label} wall-time isolation cannot replace an existing timer')
            handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, expired)
            owned = True
            started = time.monotonic()
            signal.setitimer(signal.ITIMER_REAL, seconds)
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            yield
            if time.monotonic()-started >= seconds:
                raise _SourceDeadlineExpired()
        finally:
            try:
                signal.pthread_sigmask(signal.SIG_BLOCK, alarm)
            finally:
                # Even expiry at the cleanup mask call must restore ownership.
                try:
                    if owned:
                        try:
                            signal.setitimer(signal.ITIMER_REAL, 0)
                            while signal.SIGALRM in signal.sigpending():
                                signal.sigwait(alarm)
                        finally:
                            signal.signal(signal.SIGALRM, handler)
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    except _SourceDeadlineExpired:
        raise ValueError(f'{label} exceeded its source wall-time budget; prior checkpoint retained') from None


@contextlib.contextmanager
def infrastructure_deadline():
    with source_deadline(INFRASTRUCTURE_DEADLINE_SECONDS, 'Municipality'):
        yield


class CollectionDiagnostics:
    """Bounded metadata only; elapsed sums may overlap and are not wall time."""
    def __init__(self, clock=None, monotonic=None, detail_limit=1024, aggregate_limit=512):
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic_ns
        self.detail_limit = max(0, min(detail_limit, 1024))
        self.aggregate_limit = max(1, min(aggregate_limit, 512))
        self.lock = threading.Lock()
        self.requests, self.stages, self.request_groups, self.stage_groups = [], [], {}, {}
        self.request_count = self.stage_count = 0
        self.request_omitted = self.stage_omitted = 0
        self.started = self.start()

    def wall(self):
        return self.clock().isoformat(timespec='milliseconds').replace('+00:00', 'Z')

    def start(self):
        return self.wall(), self.monotonic()

    def elapsed(self, token):
        return dict(startedAt=token[0], endedAt=self.wall(),
                    durationMs=round(max(0, self.monotonic()-token[1])/1_000_000, 3))

    @staticmethod
    def identity(value):
        # Only reviewed source IDs are readable. Never copy arbitrary URLs,
        # paths, headers, exception text or an unbounded caller-supplied string.
        if isinstance(value, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', value):
            return value
        return 'sha256-'+hashlib.sha256(str(value).encode()).hexdigest()

    def group(self, groups, key):
        if key not in groups:
            if len(groups) >= self.aggregate_limit:
                key = ('aggregate-overflow',)
            groups.setdefault(key, dict(count=0, durationMsSum=0, durationMsMax=0,
                                       outcomes={}, modes={}, httpStatuses={}, transportCount=0,
                                       transportDurationMsSum=0, transportDurationMsMax=0))
        return groups[key]

    @staticmethod
    def add(group, row):
        group['count'] += 1
        group['durationMsSum'] = round(group['durationMsSum']+row['durationMs'], 3)
        group['durationMsMax'] = max(group['durationMsMax'], row['durationMs'])
        for field, plural in [('outcome', 'outcomes'), ('mode', 'modes'), ('httpStatus', 'httpStatuses')]:
            if field in row:
                key = str(row[field]); group[plural][key] = group[plural].get(key, 0)+1
        transport = row.get('transportTiming', {})
        if 'durationMs' in transport:
            group['transportCount'] += 1
            group['transportDurationMsSum'] = round(group['transportDurationMsSum']+transport['durationMs'], 3)
            group['transportDurationMsMax'] = max(group['transportDurationMsMax'], transport['durationMs'])

    def fetcher(self, parent, phase, fetcher):
        parent = self.identity(parent)
        if phase not in ('initial', 'child'):
            raise ValueError('Unknown diagnostic request phase')
        def measured(policy, previous, now, fixture=None):
            token = self.start(); result = None; outcome = 'returned'
            try:
                result, raw = fetcher(policy, previous, now, fixture)
                return result, raw
            except BaseException as error:
                outcome = 'raised' if isinstance(error, Exception) else 'interrupted'
                raise
            finally:
                row = dict(sourceId=parent, phase=phase, outcome=outcome, **self.elapsed(token))
                row['urlSha256'] = hashlib.sha256(str(policy.get('url', '')).encode()).hexdigest()
                transport = result.get('transportTiming', {}) if isinstance(result, dict) else {}
                row['mode'] = ('fixture-replay' if fixture else
                    transport.get('mode') if transport.get('mode') in ('live-http', 'not-requested') else 'unobserved')
                # Explicit projection: adding provider headers to fetch later
                # cannot accidentally expose them in timing diagnostics.
                timing = {}
                for key in ('requestStartedAt', 'requestCompletedAt', 'requestFailedAt'):
                    value = transport.get(key)
                    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z', value):
                        timing[key] = value
                duration = transport.get('durationMs')
                if type(duration) in (float, int) and math.isfinite(duration) and duration >= 0:
                    timing['durationMs'] = duration
                if transport.get('outcome') in ('response-complete', 'request-failed'):
                    timing['outcome'] = transport['outcome']
                if row['mode'] == 'live-http':
                    row['transportTiming'] = timing
                if isinstance(result, dict) and type(result.get('httpStatus')) is int and 100 <= result['httpStatus'] <= 599:
                    row['httpStatus'] = result['httpStatus']
                with self.lock:
                    self.request_count += 1
                    row['sequence'] = self.request_count
                    self.add(self.group(self.request_groups, (parent, phase)), row)
                    if len(self.requests) < self.detail_limit:
                        self.requests.append(row)
                    else:
                        self.request_omitted += 1
        return measured

    def end(self, name, token, source=None, outcome='complete', detail=True):
        row = dict(stage=name, outcome=outcome, **self.elapsed(token))
        if source is not None:
            row['sourceId'] = self.identity(source)
        with self.lock:
            self.stage_count += 1
            self.add(self.group(self.stage_groups, (name, row.get('sourceId'))), row)
            if detail and len(self.stages) < self.detail_limit:
                self.stages.append(row)
            else:
                self.stage_omitted += 1

    @contextlib.contextmanager
    def stage(self, name, source=None, detail=True):
        token = self.start(); outcome = 'complete'
        try:
            yield
        except BaseException as error:
            outcome = 'failed' if isinstance(error, Exception) else 'interrupted'
            raise
        finally:
            self.end(name, token, source, outcome, detail)

    def snapshot(self):
        elapsed = self.elapsed(self.started)
        with self.lock:
            def groups(values, labels):
                return [dict(identityOverflow=key==('aggregate-overflow',),
                    **({} if key==('aggregate-overflow',) else dict(zip(labels,key))), **value)
                    for key,value in values.items()]
            return copy.deepcopy(dict(schemaVersion=1, **elapsed,
                requestCalls=self.request_count, requestDetailsOmitted=self.request_omitted,
                stageCalls=self.stage_count, stageDetailsOmitted=self.stage_omitted,
                detailLimit=self.detail_limit, aggregateLimit=self.aggregate_limit,
                requests=self.requests, stages=self.stages,
                requestAggregates=groups(self.request_groups, ('sourceId','phase')),
                stageAggregates=groups(self.stage_groups, ('stage','sourceId'))))


KEYWORDS = {
    'earthquake': r'\b(earthquakes?|sismos?|terremotos?|séisme)\b',
    'flood': r'\b(flood(?:s|ed|ing)?|inundaci[oó]n(?:es)?|inondation)\b',
    'weather': r'\b(cyclone|typhoon|hurricane|hurac[aá]n|drought|storm|wildfire|sequ[ií]as?|tormentas?)\b',
    'volcano': r'\b(volcan(?:o|ic)|volc[aá]n|eruption|erupci[oó]n)\b',
    'conflict': r'\b(armed conflict|clashes|airstrike|shelling|gunfire|bombing)\b',
    'unrest': r'\b(riot|curfew|unrest|couvre-feu|toque de queda)\b',
    'health': r'\b(cholera|chol[eé]ra|dengue|outbreak|epidemic|epidemia|medicine.{0,30}shortages?)\b',
    'transport': (r'\b(road.{0,25}(closed|blocked)|bridge.{0,25}collaps(?:e|ed|es|ing)?|landslide|derailment|'
                  r'\b(?:road|highway|vehicles?|cars?|buses?|bus|lorr(?:y|ies)|trucks?|matatu)\b.{0,60}\b(?:crash(?:es|ed|ing)?|accidents?|collisions?)\b|'
                  r'\b(?:crash(?:es|ed|ing)?|accidents?|collisions?)\b.{0,60}\b(?:road|highway|vehicles?|cars?|buses?|bus|lorr(?:y|ies)|trucks?|matatu)\b|'
                  r'(?:accidentes?|siniestros?).{0,60}(?:tr[aá]nsito|viales?|transporte pesado)|'
                  r'deslizamientos?|derrumbes?|descarrilamientos?|d[eé]raillements?|'
                  r'(?:carreteras?|rutas?|v[ií]as?|puentes?|t[uú]neles?|aeropuertos?|vuelos?|trenes?)'
                  r'.{0,60}(?:cerrad[oa]s?|cierres?|bloquead[oa]s?|bloqueos?|colaps\w*|suspendid[oa]s?|cancelad[oa]s?)|'
                  r'(?:cierres?|bloqueos?|colaps\w*|suspensi[oó]n|cancelaci[oó]n).{0,60}'
                  r'(?:carreteras?|rutas?|v[ií]as?|puentes?|t[uú]neles?|aeropuertos?|vuelos?|trenes?)|'
                  r'(?:routes?|ponts?|tunnels?|a[eé]roports?|vols?|trains?).{0,60}'
                  r'(?:ferm[eé][es]*|coup[eé][es]*|bloqu[eé][es]*|effondr\w*|suspendu[es]*|annul[eé][es]*)|'
                  r'(?:fermetures?|blocages?|effondrements?|suspensions?|annulations?).{0,60}'
                  r'(?:routes?|ponts?|tunnels?|a[eé]roports?|vols?|trains?))\b'),
    # Discovery candidates only. These terms never establish an incident, its
    # date/location, an ongoing threat, or independent corroboration.
    'crime': (r'\b(kidnap(?:s|ped|ping)?|abduct(?:ed|ing|ion|ions)?|shootings?|homicides?|'
              r'phone[- ]snatch(?:ers?|ing)|snatch(?:ed|ing)\s+(?:a\s+)?phones?|'
              r'murder(?:s|ed)?|robber(?:y|ies)|thefts?|burglar(?:y|ies)|assault(?:s|ed)?|'
              r'secuestros?|asesinatos?|homicidios?|robos?|asaltos?|agresi[oó]n(?:es)?|'
              r'meurtres?|assassinats?|braquages?|cambriolages?|agressions?|enl[eè]vements?|'
              r'vols? (?:à main armée|avec violence))\b'),
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


def load_boundary_layer(path):
    """Lossless storage only: identical JSON coordinates and source metadata."""
    compressed = path.with_suffix(path.suffix + '.gz')
    if path.exists() and compressed.exists():
        raise ValueError('Ambiguous duplicate boundary representations')
    if path.exists():
        return load(path)
    if not compressed.exists():
        return None
    with gzip.open(compressed, 'rb') as stream:
        raw = stream.read(25_000_001)
    if len(raw) > 25_000_000:
        raise ValueError('Complete boundary layer exceeds 25MB')
    return json.loads(raw)


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
    # USGS timestamps are epoch milliseconds; rounding them to seconds can
    # make an unchanged provider record look older than the archived version.
    return dt.datetime.fromtimestamp(value / 1000, UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def source_due(policy, previous, now):
    """One attempt per UTC cadence slot, despite runner start-time jitter.

    Failed attempts consume their slot too. A one-minute minimum spacing avoids
    bursts across a slot boundary; missed slots never create catch-up work.
    """
    last_attempt = previous.get('lastAttemptAt')
    if not last_attempt:
        return True
    attempted = dt.datetime.fromisoformat(last_attempt.replace('Z', '+00:00'))
    elapsed = (now-attempted).total_seconds()
    cadence = policy['cadenceMinutes']*60
    return elapsed >= 60 and now.timestamp()//cadence > attempted.timestamp()//cadence


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


DISCOVERY_FORMATS = frozenset(('rss-atom', 'wordpress', 'google-news-sitemap'))


def parse_news_sitemap(raw, allowed_hosts):
    """Read complete publisher News metadata, without fetching article contents."""
    sitemap = 'http://www.sitemaps.org/schemas/sitemap/0.9'
    news_namespace = 'http://www.google.com/schemas/sitemap-news/0.9'
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('DTD and entity declarations are not allowed')
    root = ET.fromstring(raw)
    if root.tag != f'{{{sitemap}}}urlset':
        raise ValueError('Not a News sitemap urlset')
    rows, seen = [], set()
    for item in root:
        if item.tag != f'{{{sitemap}}}url':
            raise ValueError('Unexpected sitemap item')
        links = item.findall(f'{{{sitemap}}}loc')
        news = item.findall(f'{{{news_namespace}}}news')
        if len(links) != 1 or len(links[0]) or len(news) != 1:
            raise ValueError('Incomplete or ambiguous News sitemap item')
        url = (links[0].text or '').strip()
        def field(name):
            values = news[0].findall(f'{{{news_namespace}}}{name}')
            if len(values) != 1 or len(values[0]) or not values[0].text or not values[0].text.strip():
                raise ValueError('Missing or ambiguous News metadata: '+name)
            return values[0].text.strip()
        title, published = field('title'), field('publication_date')
        parts = urllib.parse.urlsplit(url)
        if (not safe_url(url) or parts.hostname not in allowed_hosts or parts.port not in (None, 443)
                or any(character.isspace() or ord(character) < 32 for character in url)):
            raise ValueError('Unexpected publisher URL')
        if dt.datetime.fromisoformat(published.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('Publication timestamp lacks timezone')
        if url in seen:
            raise ValueError('Duplicate publisher URL')
        seen.add(url)
        rows.append(dict(title=title, url=url, publishedAt=published, description=''))
    return rows


def parse_discovery_feed(raw, policy, source):
    format_name = policy.get('format', 'rss-atom')
    if format_name == 'rss-atom':
        return parse_feed(raw)
    if format_name == 'wordpress':
        from local_news_archive import parse_wordpress
        return parse_wordpress(raw, source)
    if format_name == 'google-news-sitemap':
        return parse_news_sitemap(raw, {urllib.parse.urlsplit(policy['url']).hostname})
    raise ValueError('Discovery format has no reviewed adapter')


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
    result = dict(sourceId=policy['sourceId'], lastAttemptAt=now, url=policy['url'],
                  transportTiming=dict(mode='fixture-replay' if fixture else 'not-requested'))
    wordpress_pagination = None
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
            opener = urllib.request.build_opener(ReviewedSourceRedirect(urllib.parse.urlsplit(policy['url']).hostname))
            started = time.monotonic_ns()
            timing = result['transportTiming'] = dict(mode='live-http',
                requestStartedAt=dt.datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z'))
            complete = False
            try:
                with opener.open(request, timeout=30) as response:
                    raw = read_complete(response)
                    status, final_url = response.status, response.url
                    etag, modified = response.headers.get('ETag'), response.headers.get('Last-Modified')
                    encoding = response.headers.get('Content-Encoding','').lower()
                    if policy.get('format') == 'wordpress':
                        counts = [response.headers.get(name) for name in ('X-WP-Total', 'X-WP-TotalPages')]
                        if all(isinstance(value, str) and re.fullmatch(r'(?:0|[1-9][0-9]{0,9})', value) for value in counts):
                            wordpress_pagination = dict(total=int(counts[0]), pages=int(counts[1]))
                complete = True
            except urllib.error.HTTPError as error:
                # A 304 has no representation body. Transport completed even
                # when the existing prior-representation gate rejects its use.
                if error.code == 304:
                    complete = True
                # Only this reviewed adapter interprets a missing exact article
                # as a changed representation. Read it whole under the normal
                # decoding/budget gates; listing errors retain the failed batch.
                water_article = (policy.get('adapter')=='kg-bishkek-water'
                    and policy['sourceId'].startswith('gov-kg-bishkek-water-')
                    and re.fullmatch(r'https://www\.bishkek\.gov\.kg/ru/post/[1-9]\d*',policy['url']))
                caribe_match = re.fullmatch(r'https://caribeactual\.com/wp-json/wp/v2/posts/([1-9][0-9]{0,9})',policy['url'])
                caribe_article = (policy.get('adapter')=='cr-caribe' and caribe_match
                    and policy['sourceId']=='news-cr-caribe-actual-'+caribe_match[1])
                if error.code == 404 and (water_article or caribe_article) and error.url == policy['url']:
                    with error:
                        raw = read_complete(error)
                    status, final_url = 404, error.url
                    etag, modified = error.headers.get('ETag'), error.headers.get('Last-Modified')
                    encoding = error.headers.get('Content-Encoding','').lower()
                    complete = True
                elif error.code != 304 or policy.get('adapter') in ('kg-bishkek-water','cr-caribe'):
                    raise
                else:
                    if not previous.get('lastSuccessAt'):
                        raise ValueError('304 without a prior validated representation')
                    result.update(status='healthy', httpStatus=304, lastSuccessAt=now,
                                  unchanged=True, message='Source confirms its prior validated response is unchanged.')
                    return result, None
            finally:
                # Never catch BaseException here: a source deadline must still
                # escape without returning or publishing a partial response.
                duration_ms = (time.monotonic_ns()-started)/1_000_000
                ended = dt.datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z')
                timing.update(outcome='response-complete' if complete else 'request-failed',
                              durationMs=round(duration_ms,3))
                timing['requestCompletedAt' if complete else 'requestFailedAt'] = ended
        if not safe_url(final_url):
            raise ValueError('Redirect did not remain anonymous HTTPS')
        if urllib.parse.urlsplit(final_url).hostname != urllib.parse.urlsplit(policy['url']).hostname:
            raise ValueError('Feed changed hostname; requires an explicit source-identity review')
        if len(raw) > MAX_RESPONSE:
            raise ValueError('Complete response exceeds 20MB policy; entire response rejected')
        wire_bytes = len(raw)
        if encoding == 'gzip':
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as decoded:
                raw = read_complete(decoded)
        elif encoding == 'deflate':
            decoder = zlib.decompressobj()
            raw = decoder.decompress(raw, MAX_RESPONSE+1)
            if len(raw) > MAX_RESPONSE or decoder.unconsumed_tail:
                raise ValueError('Decoded response exceeds 20MB policy; entire response rejected')
            if not decoder.eof:
                raise ValueError('Incomplete compressed response; entire response rejected')
        elif encoding not in ('','identity'):
            raise ValueError('Unsupported HTTP content encoding; no partial parsing')
        if len(raw) > MAX_RESPONSE:
            raise ValueError('Complete decoded response exceeds 20MB policy; entire response rejected')
        result.update(httpStatus=status, finalUrl=final_url, bytes=len(raw),
                      wireBytes=wire_bytes, contentEncoding=encoding,
                      sha256=hashlib.sha256(raw).hexdigest(), etag=etag, lastModified=modified)
        if wordpress_pagination is not None:
            result['wordpressPagination'] = wordpress_pagination
        return result, raw
    except Exception as error:
        result.update(status='unavailable', lastSuccessAt=previous.get('lastSuccessAt'),
                      message=str(error))
        return result, None


def read_complete(stream):
    """Bound memory while accepting only an entire response, never a prefix."""
    chunks, size = [], 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return b''.join(chunks)
        size += len(chunk)
        if size > MAX_RESPONSE:
            raise ValueError('Complete response exceeds 20MB policy; entire response rejected')
        chunks.append(chunk)


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
            if key not in self.admin_cache:
                self.admin_cache[key] = load_boundary_layer(path)
            layer = self.admin_cache[key]
            if layer is None:
                assignment['levels'][level] = dict(status='unavailable')
                continue
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
                              excerpt=summary, contentHash=digest(raw), independentGroup=source.get('independentGroup', source['id']))])


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
    elif source['kind'] == 'gdacs':
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
            representative = None
            try:
                lon, lat = float(get('geo:Point/geo:long')), float(get('geo:Point/geo:lat'))
                if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
                    representative = dict(lon=lon, lat=lat, precision='provider-representative-point',
                                          note='GDACS representative coordinate; not a verified occurrence point or impact footprint.')
            except ValueError:
                pass
            summary = plain(get('description')) + ' GDACS provider alert: '+color+'. Local effects require review.'
            rows.append(make_incident(source, f'gdacs-{kind}-{event}-{code}', get('title'), summary,
                        countries[code], {'EQ':'earthquake', 'FL':'flood', 'TC':'weather', 'VO':'volcano',
                        'DR':'weather', 'WF':'weather'}.get(kind, 'other'), date(get('g:fromdate')),
                        date(get('g:datemodified') or get('pubDate')), now, location, get('link'),
                        dict(eventType=kind, eventId=event, episodeId=get('g:episodeid'), providerAlert=color,
                             providerIsCurrent=get('g:iscurrent'), providerEnd=get('g:todate'),
                             representativePoint=representative),
                        ET.tostring(element, encoding='unicode'),
                        {'Red':'severe', 'Orange':'moderate'}.get(color, 'unknown'), date(get('pubDate'))))
    else:
        raise ValueError('Official source kind has no reviewed adapter')
    return rows


def news_discovery(source, items, now, countries):
    rows = []
    for item in items:
        if not item['title'] or not safe_url(item['url']):
            continue
        text = item['title']+' '+item['description']
        categories = [key for key, pattern in KEYWORDS.items() if re.search(pattern, text, re.I)]
        # Keep reviewed official color notices for review without inventing a hazard.
        if (not categories and source.get('tier') == 'official'
                and re.search(r'\balerta\s+(?:amarilla|roja|naranja|verde)\b', text, re.I)):
            categories = ['other']
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


def routine_weather_forecast(row):
    """Routine CAP forecasts are source observations, not monitor incidents."""
    return (row.get('category') == 'weather' and row.get('severity') != 'severe'
            and (row.get('sourceMetadata', {}).get('provider') == 'CAP 1.2'
                 or row.get('id', '').startswith('cap-')))


def public_incident(row):
    return weather_decision(row)['public']


def read_archive(data, countries):
    from public_archive_storage import load as load_retained
    rows = {}
    # Excluded prior observations remain available for evidence and CAP lifecycle
    # references, outside the application's incident index and country archives.
    for directory in ('history', 'retained-weather'):
        loader = load_retained if directory == 'retained-weather' else load
        for code in countries:
            root = loader(data/directory/f'{code}.json', [])
            if isinstance(root, dict):
                if root.get('format') != 'country-parts':
                    raise ValueError('Unknown archive format')
                root = [row for name in root['parts'] for row in loader(data/directory/Path(name).name)]
            for row in root:
                if row['id'] in rows:
                    raise ValueError('Duplicate archive identity')
                rows[row['id']] = row
    return rows


def public_snapshot_record(row):
    """Explicit overview projection; authoritative area details stay in the archive."""
    compact = copy.deepcopy(row)
    omitted = compact.get('location', {}).pop('geometry', None) is not None
    metadata = compact.get('sourceMetadata', {})
    if 'infoBlocks' in metadata:
        del metadata['infoBlocks']
        omitted = True
    lower_update = metadata.get('lowerRelevanceUpdate', {})
    if lower_update:
        omitted = lower_update.get('location', {}).pop('geometry', None) is not None or omitted
        update_metadata = lower_update.get('sourceMetadata', {})
        if 'infoBlocks' in update_metadata:
            del update_metadata['infoBlocks']
            update_metadata['fullDetailInCountryArchive'] = True
            omitted = True
    reports = list(compact.get('reports', []))
    for revision in compact.get('revisions', []):
        reports.extend(revision.get('previousReports', []))
    for report in reports:
        details = report.get('sourceMetadata', {})
        if 'infoBlocks' in details:
            del details['infoBlocks']
            details['fullDetailInCountryArchive'] = True
            omitted = True
    if omitted:
        compact.setdefault('sourceMetadata', {})['fullDetailInCountryArchive'] = True
    return compact


def current_snapshot_rows(records, countries):
    return [public_snapshot_record(row) for row in sorted(records.values(), key=lambda r:r['occurredAt'], reverse=True)
            if not row['historical'] and row['countryCode'] in countries and public_incident(row)]


def advisory_snapshot_rows(records, countries, history_from, history_to):
    """Complete projections of the reviewed CDC identities, including withdrawals.

    Listing status selects the UI's active subset, never archive membership. Keep
    terminal evidence here so a later publication cannot silently lose a notice.
    """
    from public_security_official import CDC_PROFILES
    start = dt.datetime.fromisoformat(history_from.replace('Z', '+00:00'))
    end = dt.datetime.fromisoformat(history_to.replace('Z', '+00:00'))
    if start.tzinfo is None or end.tzinfo is None or start > end:
        raise ValueError('Invalid advisory history interval')
    profiles = {'cdc-'+slug+'-'+profile[0]:(slug,profile[0]) for slug,profile in CDC_PROFILES.items()}
    rows = []
    for row in records.values():
        profile = profiles.get(row.get('id'))
        metadata = row.get('sourceMetadata', {})
        if (profile is None or row.get('countryCode') not in countries or row['countryCode'] != profile[1]
                or row.get('sourceIds') != ['cdc-travel-notices'] or row.get('category') != 'health'
                or row.get('severity') != 'unknown' or row.get('location', {}).get('precision') not in ('area','country')
                or metadata.get('provider') != 'CDC'
                or metadata.get('eventTimeBasis') != 'notice_publication_not_outbreak_onset'
                or metadata.get('firstPublicationTime') != row.get('occurredAt')
                or type(metadata.get('providerActiveNotice')) is not bool
                or type(metadata.get('providerListed')) is not bool):
            continue
        if metadata['providerActiveNotice'] and (not metadata['providerListed']
                or row.get('state') != 'reported' or row.get('evidenceStatus') != 'official'):
            continue
        citation = r'https://wwwnc\.cdc\.gov/travel/notices/level[1-3]/'+re.escape(profile[0])
        reports = row.get('reports', []) + [report for revision in row.get('revisions', [])
                                           for report in revision.get('previousReports', [])]
        if (not row.get('reports') or not any(report.get('publishedAt') == row['occurredAt'] for report in reports)
                or not all(
                report.get('sourceId') == 'cdc-travel-notices' and report.get('incidentId') == row['id']
                and re.fullmatch(citation, report.get('url', '')) for report in reports)):
            continue
        occurred = dt.datetime.fromisoformat(row['occurredAt'].replace('Z', '+00:00'))
        if occurred.tzinfo is None:
            raise ValueError('Advisory publication date requires a time zone')
        if start <= occurred <= end:
            rows.append(public_snapshot_record(row))
    return sorted(rows, key=lambda row:(row['occurredAt'],row['id']), reverse=True)


def write_archive(data, records, countries, public_country_codes=None, publication=None, *, compress_retained=False):
    from public_archive_storage import save as save_retained, compress_legacy_files
    if type(compress_retained) is not bool:
        raise ValueError('Retained archive compression requires an explicit boolean')
    # Retain complete previously collected country files, while exposing only
    # the currently admitted roster in the public index and manifest.
    public_codes = set(countries if public_country_codes is None else public_country_codes)
    ordered = sorted(records.values(), key=lambda row:(row['occurredAt'],row['id']), reverse=True)
    public_records = [row for row in ordered if row['countryCode'] in public_codes and public_incident(row)]
    index, manifest = [], dict(format='explicit-field-projection-with-complete-country-details',
                             recordCount=len(public_records), projectionFields=list(INDEX_FIELDS)+['reports','reportCount'],
                             omittedFromIndex=['summary','sourceMetadata','revisions','adminAssignment','location.geometry'], countries=[])
    for row in public_records:
        compact = {key:row[key] for key in INDEX_FIELDS if key in row}
        if 'location' in row:
            compact['location'] = {key:value for key,value in row['location'].items() if key != 'geometry'}
        compact.update(reports=[], reportCount=len(row['reports']))
        index.append(compact)
    if len(encode(index)) > MAX_HISTORY_INDEX:
        raise ValueError('Complete history overview exceeds 25MB; no partial index permitted')
    def write_country(directory, code, rows):
        def store(name, value):
            path = data/directory/name
            if directory == 'retained-weather':
                save_retained(path, encode(value), compressed=compress_retained)
            else:
                save(path, value)
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
            store(f'{code}.json', rows)
        else:
            if len(groups) > 999:
                raise ValueError('Country archive exceeds the bounded 999-part index')
            part_index = []
            for number, group in enumerate(groups, 1):
                name = f'{code}-{number:03d}.json'
                store(name, group)
                parts.append(name)
                if directory == 'history':
                    raw = (data/directory/name).read_bytes()
                    part_index.append(dict(path=name, recordCount=len(group), bytes=len(raw),
                                           sha256=hashlib.sha256(raw).hexdigest(), ids=[row['id'] for row in group]))
            store(f'{code}.json', dict(format='country-parts', countryCode=code,
                                     recordCount=len(rows), parts=parts))
            if directory == 'history' and publication is not None:
                ids = [identity for part in part_index for identity in part['ids']]
                if len(set(ids)) != len(rows) or ids != [row['id'] for row in rows]:
                    raise ValueError('Country part index lost or duplicated an incident')
                sidecar = dict(format='country-part-index-v1', countryCode=code,
                               publicationRevision=publication['revision'], recordCount=len(rows), parts=part_index)
                if len(encode(sidecar)) > 2*1024*1024:
                    raise ValueError('Complete country part index exceeds 2MiB; no partial write allowed')
                save(data/directory/f'{code}-index.json', sidecar)
        return parts
    for code in countries:
        rows = [r for r in ordered if r['countryCode'] == code and public_incident(r)]
        parts = write_country('history', code, rows)
        retained = [r for r in ordered if r['countryCode'] == code and not public_incident(r)]
        if retained or any((data/'retained-weather'/f'{code}{suffix}').exists() for suffix in ('.json','.json.gz')):
            write_country('retained-weather', code, retained)
        if code in public_codes:
            manifest['countries'].append(dict(code=code, path=f'history/{code}.json', recordCount=len(rows), parts=parts))
    save(data/'history.json', index)
    save(data/'history-manifest.json', manifest)
    if compress_retained:
        compress_legacy_files(data/'retained-weather')


def apply_archive_age(row, now):
    row['historical'] = dt.datetime.fromisoformat(row['occurredAt'].replace('Z', '+00:00')) < now-dt.timedelta(days=30)
    active_notice = ('cdc-travel-notices' in row.get('sourceIds', []) and
        row.get('sourceMetadata', {}).get('provider') == 'CDC' and
        row.get('sourceMetadata', {}).get('providerActiveNotice') is True)
    if row['historical'] and row['state'] == 'reported' and not active_notice:
        row['state'] = 'expired'


def collect_news_records(source, policy, items, now, countries, areas, reviews, records, *, history_window=None):
    """Shared complete-news admission rules for current collection and history."""
    from local_news_archive import jcf_curfew_incidents
    from public_news_runtime import automatic_news
    bounds = None
    if history_window is not None:
        from public_news_backfill import configuration, timestamp
        config = configuration(source, policy, now)
        if history_window != policy['wordpressHistory']:
            raise ValueError('News history interval differs from the reviewed policy')
        bounds = (timestamp(config['from_']), timestamp(config['through']))
    pending = news_discovery(source, items, now, countries)
    incoming = []
    if policy.get('format') != 'google-news-sitemap':
        incoming = reviewed_news_incidents(source, items, reviews, now, countries, areas)
        if bounds:
            # A review can legitimately describe an older event. It remains in
            # the normal collector, but cannot broaden an explicit archive job.
            incoming = [row for row in incoming
                if bounds[0] <= timestamp(row['occurredAt']) < bounds[1]
                and all(bounds[0] <= timestamp(report['publishedAt']) < bounds[1]
                        for report in row['reports'])]
        incoming.extend(jcf_curfew_incidents(source, items, now, countries, areas, history_window=history_window))
    automatic, decisions = automatic_news(source, policy, items, now, countries, areas, reviews, records,
                                           history_window=history_window)
    incoming.extend(automatic)
    decisions_by_url = {decision['url']:decision for decision in decisions}
    for discovery in pending:
        decision = decisions_by_url.get(discovery['url'])
        if decision:
            discovery.update(status=decision['status'], reviewReason=decision['reason'])
            if 'incidentId' in decision:
                discovery.update(incidentId=decision['incidentId'], locationStatus='source-context-validated',
                    note='Admitted by a reviewed complete-source event, place and date adapter.')
    stats = dict(accepted=sum(d['status']=='automatic-source-record' for d in decisions),
        held=sum(d['status']=='needs-review' for d in decisions),
        heldReasons={reason:sum(d['reason']==reason for d in decisions)
            for reason in sorted({d['reason'] for d in decisions if d['status']=='needs-review'})})
    return incoming, pending, stats


def stage_source_records(incoming, pending_discoveries, records, discoveries, now, geo, diagnostics, sid):
    """Apply identical revision/geography/size gates before any source commit."""
    now_text = stamp(now)
    pending_records = {}
    for row in incoming:
            # Compare the state that would actually be published.
            # Replaying an old reported event must not manufacture an
            # expired -> reported -> expired revision on every fetch.
            apply_archive_age(row, now)
            old = records.get(row['id'])
            if old and dt.datetime.fromisoformat(row['updatedAt'].replace('Z', '+00:00')) < dt.datetime.fromisoformat(old['updatedAt'].replace('Z', '+00:00')):
                # A delayed provider response must not replace newer facts.
                # Adapters merge genuinely new evidence into the current
                # record before reaching this transaction.
                continue
            row = preserve_significant_history(row, old)
            annotate_weather(row)
            if (old and [(r['id'],r['contentHash'],r.get('independentGroup')) for r in old['reports']] == [(r['id'],r['contentHash'],r.get('independentGroup')) for r in row['reports']]
                    and old['occurredAt'] == row['occurredAt']
                    and old['updatedAt'] == row['updatedAt']
                    and old['state'] == row['state']
                    and old['location'] == row['location']
                    and old.get('sourceMetadata') == row.get('sourceMetadata')):
                continue
            if old:
                row['firstSeenAt'] = old['firstSeenAt']
                row['revisions'] = old.get('revisions', []) + [dict(at=now_text,
                    note='Refreshed source representation; prior archived report retained.', previousReports=old['reports'])]
            if (old and row['location'].get('precision') == 'point' and old['location'] == row['location']
                    and old.get('adminAssignment', {}).get('method') == 'strict_point_containment'
                    and 'adminAssignment' not in row):
                for key in ('admin1Id','admin1Code','admin1Name','admin2Id','admin2Code','admin2Name','adminAssignment'):
                    if key in old:
                        row[key] = old[key]
            if 'adminAssignment' not in row:
                with diagnostics.stage('geographic-assignment', sid, detail=False):
                    geo.administrative(row)
            # Fail this source transaction before admission, so one
            # oversized complete record cannot block other feeds.
            if len(encode(row))+3 > PART_LIMIT:
                raise ValueError('Complete incident exceeds 12MB; source deferred without truncation: '+row['id'])
            pending_records[row['id']] = row
    for row in pending_discoveries:
            old = discoveries.get(row['id'])
            row['firstSeenAt'] = old.get('firstSeenAt', old['retrievedAt']) if old else now_text
            if old and old['contentHash'] != row['contentHash']:
                row['revisions'] = old.get('revisions', []) + [dict(at=now_text, previous=old['contentHash'])]
            elif old and 'revisions' in old:
                row['revisions'] = old['revisions']
    return pending_records, pending_discoveries


def run(root, now, fixtures=None, diagnostics=None):
    diagnostics = diagnostics or CollectionDiagnostics()
    startup_started = diagnostics.start()
    from public_cap import collect_cap, collect_cap_batch, apply_lifecycle, expire_warnings
    from public_official import ADAPTERS, prepare_official_policy, request_state, collect_official
    from public_security_official import (ADAPTERS as SECURITY_ADAPTERS,
        prepare_security_policy, collect_security_official)
    from public_transport import (ADAPTERS as TRANSPORT_ADAPTERS,
        prepare_transport_policy, collect_transport)
    from public_infrastructure import (ADAPTERS as INFRASTRUCTURE_ADAPTERS,
        SOURCE_ID as INFRASTRUCTURE_SOURCE_ID, prepare_infrastructure_policy, collect_infrastructure)
    from public_news_elmundo_fetch import (ADAPTERS as ELMUNDO_ADAPTERS,
        prepare_elmundo_policy, collect_elmundo_source)
    from public_news_caribe_fetch import (ADAPTERS as CARIBE_ADAPTERS, SOURCE_ID as CARIBE_SOURCE_ID,
        LATEST_URL as CARIBE_URL, collect_caribe_source, validate_source_policy as validate_caribe_policy,
        request_state as caribe_request_state)
    from local_news_archive import expire_curfews
    from public_news_runtime import automatic_news, news_window
    from public_archive_storage import compression_enabled
    now_text = stamp(now)
    data = root/'docs/data'
    all_countries = {c['code']:c for c in load(root/'config/countries.json')}
    countries = {k:c for k,c in all_countries.items() if c['status']=='active' and c['reviewDue'] >= now.date().isoformat()}
    sources = {s['id']:s for s in load(root/'config/sources.json')}
    configuration = load(root/'config/collection-policy.json')
    policies = configuration['sources']
    compress_retained = compression_enabled(configuration)
    states = load(data/'collector-state.json', {})
    snapshot = load(data/'snapshot.json')
    baseline_coverage = {c['sourceId']:c for c in snapshot['coverage']}
    for policy in policies:
        sid = policy['sourceId']
        if sid not in states:
            states[sid] = dict(sourceId=sid, lastSuccessAt=baseline_coverage.get(sid,{}).get('lastSuccessAt'))
    # An all-invalid cycle retains the publication, but its failure audit may
    # contain newly discovered exact URLs which must not vanish with the index.
    prior_audit = load(data/'refresh-status.json', {})
    prior_cap_observations = [dict(sourceId=sid, capQuarantine=queue)
        for sid,queue in prior_audit.get('retainedCapQuarantines', {}).items()]
    for observation in prior_cap_observations + prior_audit.get('sources', []):
        sid = observation.get('sourceId')
        if sid not in states or sid not in sources or sources[sid].get('kind') != 'cap' or not observation.get('capQuarantine'):
            continue
        retained = states[sid]
        queue = retained.get('capQuarantine', []) + observation['capQuarantine']
        retained['capQuarantine'] = list({q['url']:q for q in queue}.values())
    diagnostics.end('startup-config', startup_started)
    archive_started = diagnostics.start()
    records = read_archive(data, all_countries)
    for row in records.values():
        annotate_weather(row)
    diagnostics.end('archive-read-and-weather', archive_started)
    geography_started = diagnostics.start()
    discoveries = {r['id']:r for r in load(data/'discovery.json', [])}
    geo = Geography(root, countries)
    iso3 = load(root/'config/country-iso3.json')
    reviews = load(root/'config/news-reviews.json', [])
    try:
        areas = load(root/'config/admin-places.json', [])
    except (OSError, ValueError):
        areas = []
    if not isinstance(areas, list):
        areas = []
    due = [p for p in policies if p['sourceId'] in sources and
           (p.get('adapter') not in INFRASTRUCTURE_ADAPTERS | CARIBE_ADAPTERS
            or sources[p['sourceId']].get('enabled') is True and p.get('enabled') is not False) and
           source_due(p, states.get(p['sourceId'], {}), now)]
    due = [prepare_official_policy(p, states.get(p['sourceId'], {}), now_text)
           if p.get('adapter') in ADAPTERS else
           prepare_security_policy(p, states.get(p['sourceId'], {}), now_text)
           if p.get('adapter') in SECURITY_ADAPTERS else
           prepare_transport_policy(p, states.get(p['sourceId'], {}), now_text)
           if p.get('adapter') in TRANSPORT_ADAPTERS else
           prepare_infrastructure_policy(p, states.get(p['sourceId'], {}), now_text)
           if p.get('adapter') in INFRASTRUCTURE_ADAPTERS else
           prepare_elmundo_policy(p, states.get(p['sourceId'], {}), now_text)
           if p.get('adapter') in ELMUNDO_ADAPTERS else p for p in due]
    diagnostics.end('geography-and-policy-setup', geography_started)
    changes, source_results = [], []
    def prefetch(policy):
        if policy.get('adapter') in INFRASTRUCTURE_ADAPTERS | CARIBE_ADAPTERS:
            # Its initial request and child requests share one interruptible
            # source budget on the main thread, outside this worker pool.
            return dict(sourceId=policy['sourceId'],lastAttemptAt=now_text,url=policy['url']), None
        prior = states.get(policy['sourceId'], {})
        requested = request_state(policy, prior)
        if sources[policy['sourceId']].get('kind') == 'cap' and prior.get('capQuarantine'):
            requested = {k:v for k,v in requested.items() if k not in ('etag','lastModified')}
        return diagnostics.fetcher(policy['sourceId'], 'initial', fetch)(policy, requested, now_text, fixtures)
    with diagnostics.stage('initial-prefetch'):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(prefetch, due))
    for policy, (result, raw) in zip(due, responses):
        sid = policy['sourceId']
        source_started = diagnostics.start()
        child_fetch = diagnostics.fetcher(sid, 'child', fetch)
        previous = states.get(sid, {})
        source = sources[sid]
        transport_state = {}
        article_health = None
        infrastructure_unresolved = []
        cap_batch = None
        cap_policy = (source.get('kind') == 'cap' and source.get('tier') == 'official'
            and source.get('enabled') is True and source.get('feedUrl') == policy.get('url')
            and policy.get('mode') == 'official' and sid.startswith('cap-')
            and policy.get('format') in (None, 'jamaica-cap-atom')
            and policy.get('capVersion') in (None, '1.2') and safe_url(policy.get('url', '')))
        if source.get('kind') == 'cap' and previous.get('capQuarantine'):
            result['capQuarantine'] = copy.deepcopy(previous['capQuarantine'])
        try:
            group = policy.get('independentGroup', sid)
            if not isinstance(group, str) or group not in sources:
                raise ValueError('Source independence group is not a reviewed registry identity')
            # Collection policy is reviewed configuration, never publisher input.
            source = dict(source, independentGroup=group)
            if source.get('kind') == 'cap' and previous.get('capQuarantine') and not cap_policy:
                raise ValueError('CAP quarantine requires its reviewed source and collection policy')
            if source.get('kind') == 'cap' and previous.get('capQuarantine') and raw is None:
                # Even an unsolicited 304 cannot confirm queued child documents.
                raise ValueError('CAP quarantine requires complete index and bulletin retries')
            if policy.get('adapter') in INFRASTRUCTURE_ADAPTERS:
                if policy['mode'] != 'official' or not policy.get('historyFrom'):
                    raise ValueError('Municipality collection requires official mode and a fixed historyFrom boundary')
                with infrastructure_deadline():
                    result, raw = diagnostics.fetcher(sid, 'initial', fetch)(policy, request_state(policy, previous), now_text, fixtures)
                    infrastructure_collected = (collect_infrastructure(source, policy, raw, now_text, countries,
                        previous, records, child_fetch, fixtures, history_from=snapshot['history']['from'])
                        if raw is not None else None)
            if policy.get('adapter') in CARIBE_ADAPTERS:
                validate_caribe_policy(source,policy,now_text,countries)
                with source_deadline(CARIBE_DEADLINE_SECONDS, 'Caribe news'):
                    result, raw = diagnostics.fetcher(sid, 'initial', fetch)(policy,
                        caribe_request_state(policy, previous), now_text, fixtures)
                    if raw is not None and (result.get('httpStatus')!=200 or result.get('finalUrl')!=CARIBE_URL):
                        raise ValueError('Caribe listing did not return its complete exact HTTP 200 representation')
                    caribe_collected = (collect_caribe_source(source, policy, raw, now_text, countries,
                        previous, records, child_fetch, fixtures, reviews=reviews,
                        history_from=max(snapshot['history']['from'],policy.get('historyFrom',snapshot['history']['from'])))
                        if raw is not None else None)
            if policy['mode'] == 'discovery' and policy.get('format', 'rss-atom') not in DISCOVERY_FORMATS:
                # Reject unknown formats even for an unchanged HTTP response.
                raise ValueError('Discovery format has no reviewed adapter')
            if policy['mode'] == 'discovery' and policy.get('newsAdapter'):
                automatic_news(source, policy, [], now_text, countries, areas)
            if raw is not None:
                if policy['mode'] == 'official':
                    pending_discoveries = []
                    official_receipts = None
                    if policy.get('adapter') in ADAPTERS | SECURITY_ADAPTERS | TRANSPORT_ADAPTERS | ELMUNDO_ADAPTERS | INFRASTRUCTURE_ADAPTERS | CARIBE_ADAPTERS:
                        if policy.get('adapter') in INFRASTRUCTURE_ADAPTERS and not policy.get('historyFrom'):
                            raise ValueError('Municipality collection requires a fixed historyFrom boundary')
                        elmundo_layers = None
                        if policy.get('adapter') in ELMUNDO_ADAPTERS:
                            try:
                                elmundo_layers = {level:load_boundary_layer(root/f'config/map-areas/SV-{level}.geojson')
                                                  for level in ('ADM1', 'ADM2')}
                            except (OSError, ValueError, EOFError, zlib.error):
                                pass  # Valid source evidence remains country-only.
                        collected = (collect_elmundo_source(source, policy, raw, now_text, countries,
                            previous, records, child_fetch, fixtures,
                            history_from=max(snapshot['history']['from'], policy.get('historyFrom', snapshot['history']['from'])),
                            reviews=reviews,areas=areas,boundary_layers=elmundo_layers)
                            if policy.get('adapter') in ELMUNDO_ADAPTERS else
                            infrastructure_collected
                            if policy.get('adapter') in INFRASTRUCTURE_ADAPTERS else
                            caribe_collected
                            if policy.get('adapter') in CARIBE_ADAPTERS else
                            collect_transport(source, policy, raw, now_text, countries,
                            previous, records, child_fetch, fixtures,
                            history_from=max(snapshot['history']['from'], policy.get('historyFrom', snapshot['history']['from'])))
                            if policy.get('adapter') in TRANSPORT_ADAPTERS else
                            collect_security_official(source, policy, raw, now_text, countries, geo,
                            previous, records, child_fetch, fixtures, areas=areas, history_from=snapshot['history']['from'])
                            if policy.get('adapter') in SECURITY_ADAPTERS else
                            collect_official(source, policy, raw, now_text, countries, geo,
                            previous, records, child_fetch, fixtures, initial_http_status=result.get('httpStatus', 200)))
                        incoming = collected['incidents']
                        pending_discoveries = collected['discoveries']
                        if policy.get('adapter') in TRANSPORT_ADAPTERS | ELMUNDO_ADAPTERS | INFRASTRUCTURE_ADAPTERS | CARIBE_ADAPTERS:
                            # A failed record/size gate must not advance a baseline
                            # page or exact-ID cursor in an otherwise healthy run.
                            transport_state = collected['state']
                        else:
                            result.update(collected['state'])
                        official_receipts = result['evidenceReceipts'] = collected['evidence']
                        if policy.get('adapter') in ELMUNDO_ADAPTERS | CARIBE_ADAPTERS:
                            article_health = dict(status=collected['sourceStatus'],message=collected['message'])
                        if policy.get('adapter') in INFRASTRUCTURE_ADAPTERS:
                            infrastructure_unresolved = transport_state['infrastructureUnresolvedCorrectionIds']
                    elif source['kind'] == 'cap':
                        if cap_policy:
                            cap_batch = collect_cap_batch(source, policy, raw, now_text, countries, areas,
                                child_fetch, fixtures, previous_quarantined=previous.get('capQuarantine', []))
                            messages = cap_batch['messages']
                        else:
                            messages = collect_cap(source, policy, raw, now_text, countries, areas, child_fetch, fixtures)
                        incoming = apply_lifecycle(messages, records, now_text)
                    else:
                        incoming = official_incidents(source, raw, now_text, countries, geo, iso3)
                    accepted = sum(public_incident(row) for row in incoming)
                    result['routineWeatherExcludedCount'] = len(incoming)-accepted
                    message = f'Complete official feed parsed; {accepted} records matched reviewed active-country boundaries/codes.'
                    if official_receipts is not None:
                        message += f' {len(official_receipts)} complete source responses validated.'
                    gap = result.get('coverageGap') or previous.get('coverageGap')
                    if gap:
                        result['coverageGap'] = gap
                        message += f" Missing interval {gap['from']} through {gap['through']}: {gap['reason']}."
                elif policy['mode'] == 'discovery':
                    items = parse_discovery_feed(raw, policy, source)
                    incoming, pending_discoveries, automatic_stats = collect_news_records(
                        source, policy, items, now_text, countries, areas, reviews, records)
                    if policy.get('newsAdapter'):
                        result.update(news_window(items, previous, now_text))
                    result['automaticNews'] = automatic_stats
                    accepted = len(incoming)
                    message = f'Complete feed parsed ({len(items)} items); {len(pending_discoveries)} discoveries; {accepted} reviewed or source-profile-validated incident records.'
                    if result.get('coverageGap'):
                        message += ' Possible missed article interval: '+result['coverageGap']['reason']
                else:
                    raise ValueError('Collection mode has no reviewed adapter')
                # Stage all source updates before committing any: a malformed item or
                # geographic assignment cannot leave a partially accepted response.
                pending_records, pending_discoveries = stage_source_records(
                    incoming, pending_discoveries, records, discoveries, now, geo, diagnostics, sid)
                records.update(pending_records)
                result.update(transport_state)
                changes.extend(pending_records)
                discoveries.update({r['id']:r for r in pending_discoveries})
                result.update(status='healthy', lastSuccessAt=now_text, acceptedCount=accepted, message=message)
                if cap_batch is not None:
                    result['capQuarantine'] = cap_batch['quarantined']
                    result['capValidatedMessages'] = len(cap_batch['messages'])
                    if cap_batch['status'] == 'partial':
                        result.update(status='unavailable',lastSuccessAt=previous.get('lastSuccessAt'),
                            message=f"CAP batch incomplete: {len(cap_batch['messages'])} valid messages; {len(cap_batch['quarantined'])} bulletins require review.")
                        if cap_batch['messages']:
                            result.update(lastPartialSuccessAt=now_text,validatedPartial=dict(
                                adapter='cap-1.2',sourceId=sid,capVersion='1.2',listingValidated=True,
                                indexUrl=cap_batch['indexUrl'],indexContentHash=cap_batch['indexContentHash'],
                                bulletinHosts=sorted(set(policy.get('bulletinHosts', [urllib.parse.urlsplit(policy['url']).hostname]))),
                                validatedMessages=len(cap_batch['messages']),quarantinedBulletins=len(cap_batch['quarantined'])))
                if article_health:
                    result.update(article_health)
                    if article_health['status'] != 'healthy':
                        result['lastSuccessAt'] = previous.get('lastSuccessAt')
                        # Constructed only after a complete listing, independently
                        # validated articles, and all canonical record gates.
                        if policy.get('adapter') in CARIBE_ADAPTERS:
                            result.update(lastPartialSuccessAt=now_text,validatedPartial=dict(
                                adapter='cr-caribe',sourceId=sid,listingValidated=True,initialUrl=CARIBE_URL,
                                listingContentHash=transport_state['caribeListingContentHash'],
                                validatedRepresentations=transport_state['caribeValidatedRepresentations'],
                                failedRechecks=transport_state['caribeFailedRechecks'],
                                pendingRechecks=transport_state['caribePendingRechecks']))
                        else:
                            result['validatedPartial'] = dict(adapter='sv-elmundo',sourceId=sid,
                                listingValidated=True,checkedArticles=len(pending_discoveries),
                                failedArticles=len(transport_state['elmundoQuarantine']),
                                queuedArticles=len(transport_state['elmundoPending']))
                if policy.get('adapter') in INFRASTRUCTURE_ADAPTERS:
                    result['message'] = (f'Water notices checked; {accepted} records; '
                        f"{len(transport_state['infrastructurePending'])} upcoming; "
                        f'{len(infrastructure_unresolved)} corrections require review. Keyword baseline '
                        +('complete.' if transport_state['infrastructureBaseline']['complete'] else 'incomplete.'))
                    if infrastructure_unresolved:
                        result.update(status='unavailable',lastSuccessAt=previous.get('lastSuccessAt'),
                            lastPartialSuccessAt=now_text,validatedPartial=dict(adapter='kg-bishkek-water',
                                sourceId=sid,listingValidated=True,validatedResponses=len(official_receipts),
                                unresolvedCorrections=len(infrastructure_unresolved)))
        except Exception as error:
            result.update(status='unavailable', lastSuccessAt=previous.get('lastSuccessAt'),
                          message='Source response rejected; prior archive retained: '+str(error))
        finally:
            diagnostics.end('source-processing', source_started, sid, result.get('status', 'unknown'))
        states[sid] = dict(previous, **result)
        if 'validatedPartial' not in result:
            states[sid].pop('validatedPartial',None)
        if result.get('status') != 'healthy':
            # A failed payload's ETag must not suppress a necessary full retry.
            states[sid]['etag'], states[sid]['lastModified'] = previous.get('etag'), previous.get('lastModified')
        source_results.append(result)
    projection_started = diagnostics.start()
    prior_states = {key:row['state'] for key,row in records.items()}
    expire_warnings(records, now_text)
    expire_curfews(records, now_text)
    for row in records.values():
        apply_archive_age(row, now)
    changes = list(dict.fromkeys(changes + [key for key,row in records.items() if row['state'] != prior_states[key]]))
    snapshot['generatedAt'] = now_text
    snapshot['mode'] = 'fixture' if fixtures else 'live'
    snapshot['schemaVersion'] = 1
    snapshot['archiveTotal'] = snapshot['history']['recordCount'] = sum(row['countryCode'] in countries and public_incident(row) for row in records.values())
    snapshot['incidents'] = current_snapshot_rows(records, countries)
    snapshot['history']['to'] = now_text
    snapshot['advisoryRecords'] = advisory_snapshot_rows(records, countries, snapshot['history']['from'], now_text)
    snapshot['collector'] = dict(kind='public-standard-runner', requestedOfficialCadenceMinutes=15,
                                  requestedNewsCadenceMinutes=360, latestRunAt=now_text,
                                  activeReviewedCountries=len(countries), discoveryCount=len(discoveries),
                                  newsPromotion='review-required', scheduleGuarantee=False,
                                  incidentScope=WEATHER_SCOPE,
                                  weatherExclusions=weather_exclusion_index(records, countries, snapshot['history']['from']))
    coverage = {c['sourceId']:c for c in snapshot['coverage']}
    for sid, state in states.items():
        coverage[sid] = {k:state.get(k) for k in ('sourceId','lastAttemptAt','lastSuccessAt','status','message')}
    admitted_sources = {policy['sourceId'] for policy in policies if
        policy.get('adapter') not in INFRASTRUCTURE_ADAPTERS | CARIBE_ADAPTERS
        or sources.get(policy['sourceId'],{}).get('enabled') is True and policy.get('enabled') is not False}
    for sid in sources:
        coverage.setdefault(sid, dict(sourceId=sid, lastAttemptAt=None, lastSuccessAt=None,
            status='pending' if sid in admitted_sources else 'disabled',
            message='No collector attempt recorded.' if sid in admitted_sources else
                    'Reference only; no automatic collection policy is enabled.'))
        if sid == INFRASTRUCTURE_SOURCE_ID and sources[sid].get('enabled') is not True:
            coverage[sid].update(status='disabled',message='Automatic drinking-water collection is disabled.')
        if sid == CARIBE_SOURCE_ID and sid not in admitted_sources:
            coverage[sid].update(status='disabled',message='Automatic Caribe Actual collection is disabled.')
    snapshot['coverage'] = list(coverage.values())
    snapshot['countries'] = list(countries.values())
    snapshot['sources'] = list(sources.values())
    diagnostics.end('lifecycle-and-projection', projection_started)
    canonical_started = diagnostics.start()
    snapshot.pop('publication', None)
    snapshot['publication'] = dict(format='peace-corps-public-data-v1', generatedAt=now_text,
                                  revision=publication_revision(snapshot), activeCountryCodes=sorted(countries))
    if len(encode(snapshot)) > MAX_SNAPSHOT:
        raise ValueError('Complete current overview exceeds 10MB; no partial snapshot permitted')
    diagnostics.end('canonical-revision', canonical_started)
    with diagnostics.stage('archive-write'):
        write_archive(data, records, all_countries, countries, snapshot['publication'],
                      compress_retained=compress_retained)
    write_started = diagnostics.start()
    save(data/'snapshot.json', snapshot)
    # A tiny change hint lets readers poll without downloading the full map data.
    # Readers still validate the complete snapshot before accepting any change.
    save(data/'publication.json', dict(schemaVersion=1, generatedAt=now_text,
                                     revision=snapshot['publication']['revision']))
    save(data/'discovery.json', sorted(discoveries.values(), key=lambda r:r['retrievedAt'], reverse=True))
    save(data/'collector-state.json', states)
    diagnostics.end('snapshot-and-state-write', write_started)
    audit = dict(generatedAt=now_text, requested=len(due), changedIncidents=changes,
                 archiveCount=len(records), publicArchiveCount=snapshot['archiveTotal'],
                 currentCount=len(snapshot['incidents']), discoveryCount=len(discoveries),
                 activeReviewedCountries=len(countries), sources=source_results,
                 diagnostics=diagnostics.snapshot())
    save(data/'refresh-status.json', audit)
    return audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--fixtures', type=Path)
    args = parser.parse_args()
    audit = run(args.root.resolve(), dt.datetime.now(UTC), args.fixtures)
    print(json.dumps({k:v for k,v in audit.items() if k != 'sources'}))
