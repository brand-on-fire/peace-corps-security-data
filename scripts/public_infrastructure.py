"""Inactive Bishkek drinking-water notice adapter; injected anonymous GET only.

Complete HTML is analyzed locally. Published records contain factual intervals,
source headlines and named service areas, not copies of articles or media.
"""
import copy
import datetime as dt
import re
import urllib.parse
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from public_official import base, digest, evidence, iso, moment, sha

ADAPTERS = {'kg-bishkek-water'}
SOURCE_ID = 'gov-kg-bishkek-water'
HOST = 'https://www.bishkek.gov.kg'
ZONE = ZoneInfo('Asia/Bishkek')
SEARCHES = ('воды', 'водоснабжен')
PAGE_SIZE = 12
LIVE_PAGES = 5
EXACT_LIMIT = 20
MAX_REQUESTS = 100
MAX_RESPONSE = 2_000_000
MAX_BATCH = 8_000_000
MONTHS = {name: i for i, pair in enumerate(
    ('январь января', 'февраль февраля', 'март марта', 'апрель апреля', 'май мая', 'июнь июня',
     'июль июля', 'август августа', 'сентябрь сентября', 'октябрь октября', 'ноябрь ноября', 'декабрь декабря'), 1)
    for name in pair.split()}
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


def endpoint(page=1, search=None):
    if search is not None and search not in SEARCHES:
        raise ValueError('Unreviewed municipal search')
    return HOST+('/search?'+urllib.parse.urlencode({'q': search, 'page': page})
                 if search else '/ru/post'+('?page='+str(page) if page > 1 else ''))


def prepare_infrastructure_policy(policy, previous, now):
    return dict(policy, url=endpoint()) if policy.get('adapter') in ADAPTERS else dict(policy)


class Node:
    def __init__(self, tag='', attrs=None):
        self.tag, self.attrs, self.children, self.closed = tag, dict(attrs or []), [], False

    def has(self, value):
        return value in self.attrs.get('class', '').split()

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()

    def text(self):
        if self.tag in {'script', 'style', 'noscript'}:
            return ''
        value = ''.join(c if isinstance(c, str) else c.text() for c in self.children)
        return ('\n'+value+'\n') if self.tag in {'p','div','br','li','h5','h6'} else value


class Document(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        if not isinstance(raw, bytes):
            raise ValueError('Complete municipality HTML unavailable')
        self.root = Node()
        self.stack = [self.root]
        self.feed(raw.decode('utf-8', errors='strict'))
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)
        else:
            node.closed = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack[i].closed = True
                self.stack = self.stack[:i]
                break

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def compact(value):
    return re.sub(r'\s+', ' ', value).strip()


def one(nodes, label):
    nodes = list(nodes)
    if len(nodes) != 1 or not nodes[0].closed:
        raise ValueError('Municipality '+label+' missing, ambiguous or incomplete')
    return nodes[0]


def published_day(text):
    match = re.fullmatch(r'(\d{1,2})[ -]([А-Яа-яёЁ]+) (20\d{2})(?:-ж\.)?', compact(text))
    if not match or match[2].casefold() not in MONTHS:
        raise ValueError('Municipality calendar publication date unavailable')
    day = dt.date(int(match[3]), MONTHS[match[2].casefold()], int(match[1]))
    return day.isoformat(), iso(dt.datetime.combine(day, dt.time(), ZONE))


def listing(raw, now, search=None):
    root = Document(raw).root
    if search:
        # The search presents Kyrgyz and Russian result columns separately;
        # global ordering across those columns is not meaningful.
        groups = [n for n in root.nodes() if n.has('col-sm-6') and any(
            c.has('section-title-in') and compact(c.text()) == 'Посты' for c in n.nodes())]
        group = one(groups, 'Russian search result column')
        cards = [n for n in group.nodes() if n.tag == 'a' and n.has('media')]
    else:
        group = one((n for n in root.nodes() if n.has('newservice-news')), 'news listing')
        cards = [n for n in group.nodes() if n.has('card')]
    rows = []
    for card in cards:
        if not card.closed:
            raise ValueError('Municipality listing card incomplete')
        if search:
            address = card.attrs.get('href', '')
            title = one((n for n in card.nodes() if n.tag == 'h5'), 'search title').text()
            date = one((n for n in card.nodes() if n.tag == 'span'), 'search date').text()
        else:
            title_node = one((n for n in card.nodes() if n.has('card-title')), 'listing title')
            link = one((n for n in title_node.nodes() if n.tag == 'a'), 'listing article link')
            address, title = link.attrs.get('href', ''), link.text()
            date = one((n for n in card.nodes() if n.has('card-body-date')), 'listing date').text()
        match = re.fullmatch(r'/(?:ru|ky)/post/([1-9]\d*)', address)
        if not match or not compact(title):
            raise ValueError('Municipality listing identity malformed')
        day, published = published_day(date)
        if published > now:
            raise ValueError('Municipality future publication date')
        rows.append(dict(id=int(match[1]), title=compact(title), publishedDate=day,
                         published=published, url=HOST+'/ru/post/'+match[1]))
    dates = [r['published'] for r in rows]
    if len(rows) > PAGE_SIZE or len({r['id'] for r in rows}) != len(rows) or dates != sorted(dates, reverse=True):
        raise ValueError('Municipality listing order/count/identities inconsistent')
    return rows


def article(raw, identity):
    root = Document(raw).root
    section = one((n for n in root.nodes() if n.tag == 'section' and n.has('article') and n.has('post')), 'article section')
    title = one((n for n in section.nodes() if n.has('article-title')), 'article title')
    body = one((n for n in section.nodes() if n.tag == 'article' and n.attrs.get('id') == 'lightgallery'), 'complete article body')
    canonical = [n.attrs.get('content') for n in root.nodes() if n.tag == 'meta' and n.attrs.get('property') == 'og:url']
    if canonical != ['http://www.bishkek.gov.kg/ru/post/'+str(identity)] and canonical != [HOST+'/ru/post/'+str(identity)]:
        raise ValueError('Municipality article identity or language mismatch')
    body_text = re.sub(r'(?<=\.)\s*(?=Район отключения)', '\n', body.text())
    lines = [compact(line) for line in body_text.splitlines() if compact(line)]
    links = sorted({urllib.parse.urljoin(HOST,n.attrs['href']) for n in body.nodes()
        if n.tag == 'a' and n.attrs.get('href') and re.fullmatch(
            re.escape(HOST)+r'/(?:ru|ky)/post/[1-9]\d*', urllib.parse.urljoin(HOST,n.attrs['href']))})
    representation = dict(id=identity, language='ru', title=compact(title.text()), lines=lines, links=links)
    if not representation['title']:
        raise ValueError('Municipality article title empty')
    return dict(**representation, body='\n'.join(lines), representation=representation, hash=digest(representation))


def candidate(title):
    return bool(re.search(r'\b(?:вод[ауы]|водоснабж\w*)\b', title, re.I)
                and re.search(r'отключ|подач|перебо|не будет|восстанов|перенос|огранич|отмен', title, re.I))


def area_identity(text):
    # Only orthographic/list-label equivalence. Keep street names, numbers,
    # direction/side qualifiers and their order; never fuzzy-match geography.
    labels = {'ул','улица','улицы','улицами','улиц','пр','проспект','проспекта','ж','м',
        'жилой','жилые','массив','массивы','линия','линией','а','также','и'}
    tokens = re.findall(r'[а-яa-z0-9]+',text.casefold().replace('ё','е'))
    return tuple(t for t in tokens if t not in labels)


def correction(item):
    if 'Бишкекводоканал' not in item['body']:
        return None
    changed = [line for line in item['lines'] if re.search(r'перенос[еи]|переносится',line,re.I)]
    if len(changed) != 1:
        return None
    line = changed[0]
    # Never strip a correction sentence before checking whether its claim is
    # negated or conditional. In particular, the deferred reason span must not
    # consume "не" immediately before "переносится".
    if (re.search(r'\b(?:не|нет)\b',line,re.I) or re.search(
            r'возможн|предполож|при условии|\bесли\b|\bможет\b|\bмогут\b|планируется|обсужда|'
            r'отмен|аннулир|отозв|опроверг|недействител|не соответствует действительности|не подтвержд',item['body'],re.I)):
        return None
    moved = re.search(r'о переносе планового отключения питьевой воды с (\d{1,2}) на (\d{1,2}) ([а-яё]+) (20\d{2}) года\.',line,re.I)
    deferred = re.search(r'ранее запланированное отключение питьевой воды на (\d{1,2}) ([а-яё]+) (20\d{2}) года .+ переносится на неопределенный срок\.',line,re.I)
    if bool(moved) == bool(deferred):
        return None
    try:
        if moved:
            old_day,new_day,month,year=moved.groups()
            original=dt.date(int(year),MONTHS[month.casefold()],int(old_day))
            replacement=dt.date(int(year),MONTHS[month.casefold()],int(new_day))
            if not dt.timedelta(0) < replacement-original <= dt.timedelta(days=14):
                return None
        else:
            day,month,year=deferred.groups()
            original=dt.date(int(year),MONTHS[month.casefold()],int(day));replacement=None
    except (ValueError,KeyError):
        return None
    normalized=[]
    for value in item['lines']:
        if value == line:
            continue
        value=re.sub(r'^Запланированный район отключения','Район отключения',value,flags=re.I)
        value=re.sub(r'^Ограничение водоснабжения затронет территорию в границах ','Район отключения: ',value,flags=re.I)
        if replacement:
            value=re.sub(r'^'+str(replacement.day)+' '+re.escape(month)+r' с ',
                str(replacement.day)+' '+month+' '+str(replacement.year)+' года с ',value,flags=re.I)
        normalized.append(value)
    areas=[]
    for i,value in enumerate(normalized):
        if re.match(r'Район,? отключения,?(?: ограничен(?:ный)? улицами)?\s*:',value,re.I):
            area=[]
            for part in normalized[i:]:
                if area and re.match(r'(?:Отключение |Временное |Отметим|МП |Получать)',part,re.I):break
                area.append(part)
            areas.append(re.sub(r'^Район,? отключения,?(?: ограничен(?:ный)? улицами)?\s*:\s*','', '\n'.join(area),flags=re.I))
    if len(areas) != 1 or len(area_identity(areas[0])) < 4:
        return None
    facts=None
    if replacement:
        revised=dict(item,lines=normalized,body='\n'.join(normalized))
        facts,_=outage(revised)
        if not facts or moment(facts['effectiveFrom']).astimezone(ZONE).date()!=replacement:
            return None
        facts['dateYearBasis']='explicit_rescheduling_statement_in_same_complete_notice'
    return dict(originalDate=original.isoformat(),replacementDate=replacement.isoformat() if replacement else None,
                serviceAreaOriginal=areas[0],areaIdentity=area_identity(areas[0]),replacementFacts=facts)


def outage(item):
    """Accept an affirmative drinking-water interruption and one dated interval."""
    body = item['body']
    if not candidate(item['title']) or 'Бишкекводоканал' not in body:
        return None, 'not_a_reviewed_drinking_water_notice'
    if re.search(r'горяч\w* вод|поливн\w* вод', item['title'], re.I):
        return None, 'hot_or_irrigation_water_outside_profile'
    if re.search(r'отмен|перенос|перенес|не будет прекращена|не будет отключ|возможн|предполож|при условии|ежеднев|еженедель|каждый', body, re.I):
        return None, 'cancelled_rescheduled_or_uncertain_notice'
    clauses = [line for line in item['lines'] if re.search(r'будет (?:временно )?прекращена подача питьевой воды', line, re.I)]
    if len(clauses) != 1:
        return None, 'affirmative_single_drinking_water_interruption_missing'
    clause = clauses[0]
    date = r'(\d{1,2}) ([а-яё]+) (20\d{2}) года'
    time = r'(\d{1,2}):(\d\d)'
    spanning = re.findall(r'\bС '+time+' '+date+' до '+time+' '+date, clause, re.I)
    shared = re.findall(r'\b(\d{1,2}) ([а-яё]+) с '+time+r' до (\d{1,2}) ([а-яё]+) '+time+r' (20\d{2}) года', clause, re.I)
    dates = re.findall(r'(?<!\d)'+date, clause, re.I)
    times = re.findall(r'\bс '+time+r'(?: часов)? до '+time+r'(?: часов)?\b', clause, re.I)
    if len(spanning)+len(shared) > 1 or not spanning and not shared and (len(dates) != 1 or len(times) != 1):
        return None, 'explicit_single_calendar_day_and_interval_required'
    try:
        if spanning:
            h1,m1,d1,mo1,y1,h2,m2,d2,mo2,y2 = spanning[0]
        elif shared:
            d1,mo1,h1,m1,d2,mo2,h2,m2,y1 = shared[0]
            y2 = y1
        else:
            d1,mo1,y1 = dates[0]
            h1,m1,h2,m2 = times[0]
            end_date = re.findall(r'до '+time+r'(?: часов)? (\d{1,2}) ([а-яё]+)(?!\w)', clause, re.I)
            if len(end_date) > 1:
                raise ValueError('ambiguous end date')
            d2,mo2 = end_date[0][2:] if end_date else (d1,mo1)
            y2 = y1
        start_day = dt.date(int(y1),MONTHS[mo1.casefold()],int(d1))
        end_day = dt.date(int(y2),MONTHS[mo2.casefold()],int(d2))
        begins = dt.datetime.combine(start_day,dt.time(int(h1),int(m1)),ZONE)
        # The publisher also writes 24:00: exactly the end of that calendar day.
        end_of_day = int(h2) == 24 and int(m2) == 0
        ends = dt.datetime.combine(end_day,dt.time(0 if end_of_day else int(h2),int(m2)),ZONE)
        if end_of_day:
            ends += dt.timedelta(days=1)
        if int(h2) == int(m2) == 0 and start_day == end_day and begins > ends:
            ends += dt.timedelta(days=1)  # explicitly announced midnight endpoint
        if not dt.timedelta(0) < ends-begins <= dt.timedelta(hours=24):
            raise ValueError('interval')
    except (ValueError, KeyError):
        return None, 'invalid_or_ambiguous_interval'
    # Preserve the complete named area block; never convert street bounds into
    # coordinates or claim that the entire municipality loses water.
    area_prefix = r'Район,? отключения,?(?: ограничен(?:ный)? улицами)?\s*:'
    starts = [i for i, line in enumerate(item['lines']) if re.match(area_prefix, line, re.I)]
    direct = [line.removeprefix('Отключение водоснабжения затронет ') for line in item['lines']
              if line.startswith('Отключение водоснабжения затронет ') and line.endswith('.')]
    if len(direct) == 1 and not starts:
        area = direct[0]
        starts = []
    elif len(starts) != 1 or direct:
        return None, 'explicit_service_area_block_missing'
    else:
        area = []
        for line in item['lines'][starts[0]:]:
            if area and re.match(r'(?:Временное (?:отключение|прекращение)|Отключение (?:водоснабжения |)?связано|МП |Уважаемые|Дорогие)', line, re.I):
                break
            area.append(line)
        area = re.sub('^'+area_prefix+r'\s*', '', '\n'.join(area), flags=re.I).strip()
    if not area or len(area) > 6000 or re.search(r'приносит извинения|приложение|запастись', area, re.I):
        return None, 'service_area_boundary_ambiguous'
    return dict(effectiveFrom=iso(begins), effectiveUntil=iso(ends),
                serviceAreaOriginal=area, serviceAreaLanguage='ru',
                sourceTimezone='Asia/Bishkek', eventTimeBasis='announced_service_interruption_interval',
                sourceStartDate=start_day.isoformat(), sourceEndDate=ends.date().isoformat(),
                areaKey=digest(compact(area).casefold())), None


class Requests:
    def __init__(self, policy, fetcher, now, fixtures):
        self.policy, self.fetcher, self.now, self.fixtures = policy, fetcher, now, fixtures
        self.total, self.receipts, self.cache = 0, [], {}

    def accept(self, raw, url, status=200):
        if not isinstance(raw, bytes):
            raise ValueError('Municipality complete response unavailable')
        self.total += len(raw)
        if len(raw) > MAX_RESPONSE or self.total > MAX_BATCH or len(self.receipts) >= MAX_REQUESTS:
            raise ValueError('Municipality complete response/request budget exceeded')
        receipt = dict(url=url, retrievedAt=self.now, httpStatus=status, bytes=len(raw), sha256=sha(raw))
        self.receipts.append(receipt)
        self.cache[url] = raw, receipt
        return raw, receipt

    def get(self, url):
        if url in self.cache:
            return self.cache[url]
        if len(self.receipts) >= MAX_REQUESTS:
            raise ValueError('Municipality complete response/request budget exceeded')
        allowed = {endpoint(i) for i in range(1, LIVE_PAGES+1)}
        parsed = urllib.parse.urlsplit(url)
        if url not in allowed and not re.fullmatch(re.escape(HOST)+r'/ru/post/[1-9]\d*', url):
            q = urllib.parse.parse_qs(parsed.query)
            if (parsed.scheme != 'https' or parsed.netloc != 'www.bishkek.gov.kg' or parsed.path != '/search'
                    or parsed.fragment or set(q) != {'q', 'page'} or q['q'] not in [[x] for x in SEARCHES]
                    or len(q['page']) != 1 or not re.fullmatch(r'[1-9]\d*', q['page'][0])):
                raise ValueError('Municipality request outside reviewed public paths')
        child = dict(self.policy, url=url, sourceId=SOURCE_ID+'-'+sha(url.encode())[:20])
        result, raw = self.fetcher(child, {}, self.now, self.fixtures)
        status = result.get('httpStatus')
        if status not in (200, 404) or raw is None or status == 404 and not re.fullmatch(re.escape(HOST)+r'/ru/post/[1-9]\d*', url):
            raise ValueError('Municipality complete response unavailable: '+result.get('message', str(status)))
        return self.accept(raw, url, status)


def discovery(item, entry, now, reason, receipt):
    return dict(id='bishkek-water-discovery-'+str(entry['id']), sourceId=SOURCE_ID,
        title=item['title'], url=entry['url'], publishedAt=entry['published'], retrievedAt=now,
        firstSeenAt=now, language='ru', sourceCountryCodes=['KG'], mentionedCountryCodes=['KG'],
        categoryCandidates=['infrastructure'], status='needs-review', locationStatus='unverified',
        contentHash=item['hash'], note=reason,
        sourceMetadata=dict(recordId=entry['id'], rawResponseSha256=receipt['sha256'],
                            publicationDate=entry['publishedDate'], publicationDatePrecision='day'))


def report(source, row, item, entry, now, summary, receipt):
    value = evidence(source, row, item['title'], entry['url'], entry['published'], now,
                     summary, item['representation'], receipt, str(entry['id']))
    value['language'] = 'ru'
    value['sourceMetadata'].update(publicationDate=entry['publishedDate'], publicationDatePrecision='day',
        publicationTimeBasis='start_of_published_local_calendar_day', sourceTimezone='Asia/Bishkek',
        summaryLanguage='en', providerModificationTimeAvailable=False)
    return value


def record(source, item, entry, facts, old, now, receipt, reason=None):
    if old and old['sourceMetadata'].get('supersededBy') and item.get('id') == old['sourceMetadata']['recordId']:
        latest=next((r for r in old['reports'] if r['sourceMetadata']['externalId']==str(item['id'])),None)
        if latest and latest['contentHash']==item['hash']:
            return copy.deepcopy(old)
        facts=None;reason='original_notice_changed_after_rescheduling_requires_review'
    if old and (not facts or facts['effectiveFrom'] != old['occurredAt']
                or facts['areaKey'] != old['sourceMetadata']['areaKey']):
        row = copy.deepcopy(old)
        row.update(state='expired', evidenceStatus='unverified', updatedAt=now,
                   summary='The source no longer supports the previously announced water-interruption schedule.')
        row['sourceMetadata']['automaticEvidenceState'] = reason or 'changed_time_or_area_requires_review'
    else:
        if not facts:
            raise ValueError('New water incident requires supported facts')
        start, end = moment(facts['effectiveFrom']).astimezone(ZONE), moment(facts['effectiveUntil']).astimezone(ZONE)
        summary = ('Bishkek municipality announced a drinking-water interruption from '+start.strftime('%d %B %Y %H:%M')
                   +' to '+end.strftime('%d %B %Y %H:%M')+' (Bishkek time). Affected area: '+facts['serviceAreaOriginal'])
        metadata = dict(provider='Bishkek municipality', recordId=entry['id'], listing=entry,
            service='drinking_water', eventStatusBasis='announced_schedule_not_independently_observed',
            providerModificationTimeAvailable=False, publicationDatePrecision='day',
            geographyBasis='named_service_area_without_validated_polygon', **facts)
        row = base(source, old['id'] if old else 'bishkek-water-'+str(entry['id'])+'-KG', 'KG',
            'Scheduled drinking-water interruption — Bishkek', summary, facts['effectiveFrom'],
            max(entry['published'], facts['effectiveFrom']), now, 'infrastructure', {'precision':'country'}, metadata)
        row['region'] = 'Bishkek'
        if now >= facts['effectiveUntil']:
            row['state'] = 'expired'
            row['updatedAt'] = max(row['updatedAt'], facts['effectiveUntil'])
    latest = report(source, row, item, entry, now, row['summary'], receipt)
    row['reports'] = [latest]
    if old:
        if (latest['contentHash'] == old['reports'][0]['contentHash'] and row['state'] == old['state']
                and row['evidenceStatus'] == old['evidenceStatus']):
            return copy.deepcopy(old)
        row['firstSeenAt'], row['occurredAt'] = old['firstSeenAt'], old['occurredAt']
        row['reports'] += [copy.deepcopy(r) for r in old['reports'] if r['id'] != latest['id']]
        row['updatedAt'] = max(row['updatedAt'], old['updatedAt'], now)
        row['sourceMetadata']['updateTimeBasis'] = 'observed_changed_representation_or_schedule_expiry'
    return row


def collect_infrastructure(source, policy, raw, now, countries, previous, records, fetcher,
                           fixtures=None, history_from=None):
    """Atomic bounded collection. Caller must commit state only after archive gates.

    Publication-order listings cannot prove all backdated insertions or changes.
    Pending/current notices are reconciled every run; five older known records
    rotate per run. A paused/backlogged recent scan fails without advancing state.
    """
    if source['id'] != SOURCE_ID or source.get('countryCodes') != ['KG'] or policy.get('adapter') not in ADAPTERS or policy.get('url') != endpoint():
        raise ValueError('Municipality source identity/jurisdiction mismatch')
    now = iso(now)
    if 'KG' not in countries:
        return dict(incidents=[], discoveries=[], state={'activeJurisdiction':False}, evidence=[])
    source = dict(source, _countries=countries)
    req = Requests(policy, fetcher, now, fixtures)
    req.accept(raw, endpoint())
    prior = moment(previous['lastSuccessAt']) if previous.get('lastSuccessAt') else moment(now)-dt.timedelta(days=7)
    # A fully scanned listing remains a valid overlap checkpoint while a queued
    # correction keeps the separate full-success/health clock unchanged.
    if previous.get('infrastructureLiveCaughtUp') is True and previous.get('infrastructureLiveThrough') is not None:
        try:
            scanned = moment(previous['infrastructureLiveThrough'])
            if scanned.utcoffset() is None:
                raise ValueError('Municipality scan watermark needs an explicit timezone')
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError('Municipality scan watermark is invalid') from error
        if scanned > moment(now):
            raise ValueError('Municipality scan watermark is in the future')
        prior = max(prior, scanned)
    since = iso(prior-dt.timedelta(days=2))
    entries, seen, tail, caught = {}, set(), None, False

    def page(address, search=None):
        body, receipt = req.get(address)
        return listing(body, now, search), receipt

    for number in range(1, LIVE_PAGES+1):
        rows, _ = page(endpoint(number))
        if tail and rows and rows[0]['published'] > tail:
            raise ValueError('Municipality live page ordering changed')
        for entry in rows:
            if entry['id'] in seen:
                raise ValueError('Municipality duplicate across live pages')
            seen.add(entry['id'])
            if entry['published'] >= since and candidate(entry['title']):
                entries[entry['id']] = entry
        tail = rows[-1]['published'] if rows else tail
        if len(rows) < PAGE_SIZE or tail < since:
            caught = True
            break
    if not caught:
        raise ValueError('Municipality live backlog exceeds page limit; prior watermark retained')

    floor = history_from or (moment(now)-dt.timedelta(days=365)).date().isoformat()
    if re.fullmatch(r'\d{4}-\d\d-\d\d', floor):
        floor += 'T00:00:00Z'
    floor = iso(floor)
    source_floor=policy.get('historyFrom') or (moment(now)-dt.timedelta(days=365)).date().isoformat()
    if re.fullmatch(r'\d{4}-\d\d-\d\d',source_floor):source_floor+='T00:00:00Z'
    baseline_floor=max(floor,iso(source_floor))
    baseline = copy.deepcopy(previous.get('infrastructureBaseline') or
        dict(fromDate=baseline_floor, searchIndex=0, nextPage=1, complete=False))
    if baseline.get('fromDate') != baseline_floor:
        raise ValueError('Municipality baseline boundary changed; explicit reset required')
    if not baseline['complete']:
        query = SEARCHES[baseline['searchIndex']]
        number = baseline['nextPage']
        anchor_ids, tail = set(), None
        if number > 1:
            overlap, _ = page(endpoint(number-1, query), query)
            if [[r['id'], r['published']] for r in overlap] != baseline.get('anchor'):
                number = 1
                baseline['resetForChangedAnchor'] = True
            else:
                tail = overlap[-1]['published'] if overlap else None
                anchor_ids = {r['id'] for r in overlap}
        rows, _ = page(endpoint(number, query), query)
        if tail and rows and rows[0]['published'] > tail or anchor_ids.intersection(r['id'] for r in rows):
            raise ValueError('Municipality baseline page ordering or identity changed')
        for entry in rows:
            if entry['published'] >= baseline_floor and candidate(entry['title']):
                if entry['id'] in entries and entry != entries[entry['id']]:
                    raise ValueError('Municipality listing representation changed within batch')
                entries[entry['id']] = entry
        baseline.update(nextPage=number+1, anchor=[[r['id'],r['published']] for r in rows])
        if len(rows) < PAGE_SIZE or rows[-1]['published'] < baseline_floor:
            baseline.update(searchIndex=baseline['searchIndex']+1, nextPage=1, anchor=[])
            if baseline['searchIndex'] == len(SEARCHES):
                baseline.update(complete=True, completedAt=now)

    known = {r['sourceMetadata']['recordId']:r for r in records.values() if r.get('sourceIds') == [SOURCE_ID]}
    accepted_correction_ids = {r['sourceMetadata']['supersededBy']['id'] for r in known.values()
                               if r['sourceMetadata'].get('supersededBy')}
    pending = {e['id']:e for e in previous.get('infrastructurePending', [])}
    correction_queue={e['id']:e for e in previous.get('infrastructureCorrectionQueue',[])}
    pending_relations=copy.deepcopy(previous.get('infrastructurePendingCorrections',[]))
    relation_entries={e['id']:e for relation in pending_relations for e in (relation['original'],relation['correction'])}
    exact = set(pending) | set(correction_queue) | set(relation_entries) | {identity for identity,row in known.items() if row['state'] == 'reported'}
    if len(exact) > EXACT_LIMIT:
        raise ValueError('Municipality current/pending reconciliation limit reached')
    old_ids = sorted(set(known)-exact)
    cursor = previous.get('infrastructureRecheckCursor', 0)
    for offset in range(min(5, len(old_ids), EXACT_LIMIT-len(exact))):
        exact.add(old_ids[(cursor+offset) % len(old_ids)])
    for identity in exact:
        entries.setdefault(identity, pending.get(identity) or correction_queue.get(identity) or relation_entries.get(identity)
                           or known[identity]['sourceMetadata']['listing'])

    for relation in pending_relations:
        for name in ('original','correction'):
            entries.setdefault(relation[name]['id'],relation[name])
    for identity in list(entries):
        linked=known.get(identity,{}).get('sourceMetadata',{}).get('supersededBy')
        if linked:entries.setdefault(linked['id'],linked)

    incoming, discoveries, parsed, corrections, all_facts = [], [], {}, {}, {}
    for identity, entry in sorted(entries.items()):
        body, receipt = req.get(entry['url'])
        old = known.get(identity)
        if old and entry['publishedDate'] != old['sourceMetadata']['listing']['publishedDate']:
            raise ValueError('Municipality original publication date changed; review required')
        if receipt['httpStatus'] == 404:
            pending.pop(identity, None)
            if not old and identity not in accepted_correction_ids:
                raise ValueError('Municipality listed/pending article disappeared; prior batch retained')
            item = dict(id=identity, title='Bishkek municipal notice unavailable', body='', lines=[],
                representation={'id':identity,'httpStatus':404,'responseHash':receipt['sha256']})
            item['hash'] = digest(item['representation'])
            parsed[identity]=(item,entry,receipt)
            if old:
                incoming.append(record(source,item,entry,None,old,now,receipt,'source_article_removed'))
            continue
        item = article(body, identity)
        parsed[identity]=(item,entry,receipt)
        adjustment=correction(item)
        if adjustment:corrections[identity]=adjustment
        facts, reason = outage(item)
        if adjustment and adjustment['replacementFacts']:
            facts,reason=adjustment['replacementFacts'],None
        if facts:all_facts[identity]=facts
        if not facts:
            pending.pop(identity, None)
            discoveries.append(discovery(item,entry,now,reason,receipt))
            if old:
                incoming.append(record(source,item,entry,None,old,now,receipt,reason))
            continue
        if facts['effectiveFrom'] < floor:
            discoveries.append(discovery(item,entry,now,'outside_published_history_window',receipt))
            continue
        if facts['effectiveFrom'] > now:
            pending[identity] = entry
            discoveries.append(discovery(item,entry,now,'upcoming_announced_interruption',receipt))
            if old:
                incoming.append(record(source,item,entry,None,old,now,receipt,'rescheduled_future_interval'))
            continue
        pending.pop(identity, None)
        incoming.append(record(source,item,entry,facts,old,now,receipt))
    output={r['sourceMetadata']['recordId']:r for r in incoming}
    unresolved=set()
    relationships=[]
    accepted_associations={}
    for identity,(item,entry,receipt) in parsed.items():
        if not re.search(r'перенос|перенес|отмен',item['body'],re.I):continue
        adjustment=corrections.get(identity)
        if not adjustment:
            unresolved.add(identity);continue
        candidates=[]
        for original in set(known)|set(all_facts):
            if original==identity:continue
            facts=all_facts.get(original) or known[original]['sourceMetadata']
            day=moment(facts['effectiveFrom']).astimezone(ZONE).date().isoformat()
            linked=any(url.rsplit('/',1)[-1]==str(original) for url in item.get('links',[]))
            equal=area_identity(facts['serviceAreaOriginal'])==adjustment['areaIdentity']
            if day==adjustment['originalDate'] and (linked or equal):candidates.append(original)
        if len(candidates)!=1:
            unresolved.add(identity);continue
        original=candidates[0]
        old=output.get(original) or known.get(original)
        if not old:
            if all_facts[original]['effectiveFrom'] >= floor:
                relationships.append(dict(original=entries[original],correction=entry))
            continue  # retain both exact notices in the pending reconciliation queue
        if (old['evidenceStatus']=='unverified'
                and old['sourceMetadata'].get('automaticEvidenceState')!='correction_no_longer_supported'):
            unresolved.add(identity);continue
        row=copy.deepcopy(old)
        row.update(state='cancelled',evidenceStatus='official',updatedAt=max(old['updatedAt'],entry['published']),
            summary='Bishkek municipality withdrew this announced water-interruption date.'+
                    (' The replacement date is '+adjustment['replacementDate']+'.' if adjustment['replacementDate'] else ' A replacement date was not announced.'))
        row['sourceMetadata'].update(supersededBy=entry,correctionDate=adjustment['originalDate'],
            replacementDate=adjustment['replacementDate'],correctionAssociation='explicit_original_date_and_unique_area_or_link')
        row['sourceMetadata'].pop('automaticEvidenceState',None)
        latest=report(source,row,item,entry,now,row['summary'],receipt)
        if old['state']=='cancelled' and old['reports'][0]['contentHash']==latest['contentHash']:
            row=copy.deepcopy(old)
        else:
            row['reports']=[latest]+[r for r in old['reports'] if r['id']!=latest['id']]
            row['updatedAt']=max(row['updatedAt'],now)
            row['sourceMetadata']['updateTimeBasis']='observed_source_correction'
        output[original]=row
        accepted_associations[identity]=original
    # A previously accepted correction cannot silently disappear or change into
    # unrelated wording while leaving its old cancellation certified as current.
    for original,old in known.items():
        linked=old['sourceMetadata'].get('supersededBy')
        if linked and linked['id'] in parsed and accepted_associations.get(linked['id']) != original:
            item,entry,receipt=parsed[linked['id']]
            current=output.get(original) or old
            # A missing correction must not discard a simultaneous change to its
            # original notice, nor make that separate failure auto-recoverable.
            reason=(current['sourceMetadata'].get('automaticEvidenceState')
                    if current['evidenceStatus']=='unverified' else None)
            row=record(source,item,entry,None,current,now,receipt,reason or 'correction_no_longer_supported')
            output[original]=row;unresolved.add(linked['id'])
    if len(pending) > EXACT_LIMIT:
        raise ValueError('Municipality upcoming interruption limit reached')
    if len(unresolved)>EXACT_LIMIT:
        raise ValueError('Municipality unresolved correction queue limit reached')
    return dict(incidents=list(output.values()), discoveries=discoveries, evidence=req.receipts,
        state=dict(infrastructureBaseline=baseline, infrastructureLiveFrom=since,
            infrastructureLiveThrough=now, infrastructureLiveCaughtUp=True,
            infrastructurePending=sorted(pending.values(),key=lambda e:e['id']),
            infrastructurePendingCorrections=relationships,
            infrastructureUnresolvedCorrectionIds=sorted(unresolved),
            infrastructureCorrectionQueue=[parsed[i][1] for i in sorted(unresolved)],
            infrastructureRecheckCursor=(cursor+min(5,len(old_ids))) % max(1,len(old_ids)),
            infrastructureScope='Russian_text_drinking_water_notices; keyword_baseline_not_exhaustive',
            infrastructureGeography='country_only_named_service_area_no_validated_polygon'))
