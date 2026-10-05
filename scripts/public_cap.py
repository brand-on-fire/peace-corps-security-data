"""Bounded anonymous CAP intake; warnings are provider claims, never safety scores."""
import copy
import datetime as dt
import hashlib
import json
import math
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

NS = 'urn:oasis:names:tc:emergency:cap:1.2'
MAX_BULLETINS = 400
MAX_BYTES = 20_000_000


def text(node, name, default=''):
    return node.findtext('{*}'+name, default=default).strip()


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('CAP timestamp has no offset')
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def xml(raw):
    if len(raw) > MAX_BYTES or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('CAP response exceeds policy or contains entity declarations')
    return ET.fromstring(raw)


def message_key(sender, identifier, sent):
    return hashlib.sha256((sender+'|'+identifier+'|'+timestamp(sent)).encode()).hexdigest()


def feed_links(raw):
    root = xml(raw)
    if root.tag == '{'+NS+'}alert':
        return None
    if root.tag.split('}')[-1] not in ('rss', 'feed', 'RDF'):
        raise ValueError('CAP index is not RSS/Atom')
    links = []
    for node in root.iter():
        if node.tag.split('}')[-1] not in ('item', 'entry'):
            continue
        candidates = []
        for child in node:
            if child.tag.split('}')[-1] != 'link':
                continue
            url = child.get('href') or (child.text or '').strip()
            rel, mime = child.get('rel', 'alternate'), child.get('type', '')
            if url and rel in ('alternate', 'enclosure', 'related'):
                candidates.append((0 if 'cap+xml' in mime else 1, url))
        if not candidates:
            raise ValueError('CAP index item has no bulletin link')
        links.append(sorted(candidates)[0][1])
    unique = list(dict.fromkeys(links))
    if len(unique) > MAX_BULLETINS:
        raise ValueError('CAP feed exceeds bulletin bound; complete source deferred')
    return unique


def geometry(info):
    polygons, circles, descriptions = [], [], []
    for area in info.findall('{*}area'):
        descriptions.append(text(area, 'areaDesc'))
        for shape in area.findall('{*}polygon'):
            ring = []
            for pair in (shape.text or '').split():
                coords = pair.split(',')
                if len(coords) != 2:
                    raise ValueError('CAP polygon coordinate malformed')
                lat, lon = map(float, coords)
                if not math.isfinite(lat+lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                    raise ValueError('CAP polygon coordinate out of range')
                ring.append([lon, lat])
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError('CAP polygon is not a closed ring')
            polygons.append([ring])
        for shape in area.findall('{*}circle'):
            point, radius = (shape.text or '').split()
            lat, lon = map(float, point.split(',')); radius = float(radius)
            if not all(math.isfinite(v) for v in (lat, lon, radius)) or not -90 <= lat <= 90 or not -180 <= lon <= 180 or radius < 0:
                raise ValueError('CAP circle malformed')
            circles.append(dict(center=[lon, lat], radiusKm=radius))
    return polygons, circles, descriptions


def parse_alert(source, raw, url, now, countries, areas):
    root = xml(raw)
    if root.tag != '{'+NS+'}alert':
        raise ValueError('Linked bulletin is not CAP 1.2')
    # Do not put exercise, draft, restricted or private alerts on the public map.
    if text(root, 'status') != 'Actual' or text(root, 'scope') != 'Public':
        return None
    identifier, sender, sent = text(root, 'identifier'), text(root, 'sender'), timestamp(text(root, 'sent'))
    kind = text(root, 'msgType')
    if not identifier or not sender or kind not in ('Alert', 'Update', 'Cancel', 'Ack', 'Error'):
        raise ValueError('CAP identity/type missing or unsupported')
    if kind in ('Ack', 'Error'):
        return None
    refs = []
    for reference in text(root, 'references').split():
        parts = reference.split(',')
        if len(parts) != 3:
            raise ValueError('CAP reference malformed')
        refs.append(message_key(*parts))
    key = message_key(sender, identifier, sent)
    control = dict(key=key, references=refs, kind=kind, sent=sent, url=url, sourceId=source['id'], contentHash=hashlib.sha256(raw).hexdigest())
    infos = root.findall('{*}info')
    if not infos:
        if kind != 'Cancel':
            raise ValueError('CAP warning has no information block')
        return dict(control=control, record=None)
    codes = source['countryCodes']
    if len(codes) != 1:
        raise ValueError('CAP source must have one reviewed national jurisdiction')
    code = codes[0]
    if code not in countries:
        return None
    # Retain every language block in provenance; display provider English if present.
    info = next((i for i in infos if text(i, 'language', 'en-US').lower().startswith('en')), infos[0])
    event = text(info, 'event'); headline = text(info, 'headline') or event
    if not event:
        raise ValueError('CAP event missing')
    expires = timestamp(text(info, 'expires')) if text(info, 'expires') else None
    onset = text(info, 'onset') or text(info, 'effective') or sent
    onset = timestamp(onset)
    polygons, circles, descriptions = geometry(info)
    if expires and expires < onset:
        raise ValueError('CAP expiry precedes effective/onset time')
    category = 'weather' if 'Met' in [c.text for c in info.findall('{*}category')] else 'other'
    for candidate, pattern in [('flood', r'flood|inundaci[oó]n|inondation'), ('earthquake', r'earthquake|terremoto|sismo|séisme'), ('volcano', r'volcan|erupci[oó]n|eruption'), ('health', r'outbreak|epidemic|cholera|dengue')]:
        if re.search(pattern, event, re.I):
            category = candidate; break
    iid = 'cap-'+source['id']+'-'+key[:24]
    summary = text(info, 'description') or headline
    location = dict(precision='area' if descriptions or polygons or circles else 'country')
    if polygons:
        location['geometry'] = dict(type='MultiPolygon', coordinates=polygons)
    language = text(info, 'language', 'en-US')
    metadata = dict(provider='CAP 1.2', recordId=identifier, sender=sender, messageType=kind,
                    capMessageKeys=[key], capReferences=refs, sent=sent, effective=onset, expires=expires,
                    eventTimeBasis='provider-warning-issued', warningNotConfirmedImpact=True,
                    urgency=text(info, 'urgency'), certainty=text(info, 'certainty'),
                    providerSeverity=text(info, 'severity'), areaDescriptions=descriptions, circles=circles,
                    originalLanguage=language,
                    infoBlocks=[ET.tostring(i, encoding='unicode') for i in infos])
    record = dict(id=iid, title=headline, summary=summary, countryCode=code,
                  region=' / '.join(descriptions) or countries[code]['name'], category=category,
                  severity={'Extreme':'severe','Severe':'severe','Moderate':'moderate','Minor':'minor'}.get(text(info,'severity'),'unknown'),
                  evidenceStatus='official', state='cancelled' if kind=='Cancel' else ('expired' if expires and expires <= now else 'reported'),
                  occurredAt=sent, updatedAt=sent, firstSeenAt=now, historical=False, aiStatus='not-requested',
                  sourceIds=[source['id']], location=location, sourceMetadata=metadata,
                  reports=[dict(id=iid+'-report',incidentId=iid,sourceId=source['id'],title=headline,url=url,
                                language=language,publishedAt=sent,retrievedAt=now,excerpt=summary,
                                contentHash=control['contentHash'],independentGroup=source['id'])])
    # Only exact unique issuer place names can supply one administrative label.
    normal = lambda s: ''.join(c for c in s.casefold() if c.isalnum())
    named = {normal(d) for d in descriptions if d}
    matches = {level:[a for a in areas if a['countryCode']==code and a['level']==level and normal(a['name']) in named] for level in ('ADM1','ADM2')}
    metadata['namedAdministrativeMatches'] = {level:[a['id'] for a in rows] for level,rows in matches.items()}
    if len(matches['ADM1']) == 1 and len(named) == 1:
        area = matches['ADM1'][0];record.update(admin1Code=area['id'],admin1Name=area['name'])
    if len(matches['ADM2']) == 1 and len(named) == 1:
        area=matches['ADM2'][0];parent=next((a for a in areas if a['id']==area.get('parentId') and a['level']=='ADM1' and a['countryCode']==code),None)
        if parent:
            record.update(admin1Code=parent['id'],admin1Name=parent['name'],admin2Code=area['id'],admin2Name=area['name'])
    return dict(control=control, record=record)


def collect_cap(source, policy, raw, now, countries, areas, fetcher, fixtures=None):
    """Fetch all linked bulletins or reject this source atomically. No partial feed."""
    links = feed_links(raw)
    if links is None:
        return [v for v in [parse_alert(source,raw,policy['url'],now,countries,areas)] if v]
    messages, total = [], len(raw)
    allowed = set(policy.get('bulletinHosts', [urllib.parse.urlsplit(policy['url']).hostname]))
    for url in links:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in allowed or parsed.username or parsed.password:
            raise ValueError('CAP bulletin URL is outside reviewed HTTPS hosts')
    # Small parallel batches bound duration; every response is fully parsed.
    import concurrent.futures
    deadline = time.monotonic()+90
    def one(url):
        if time.monotonic() > deadline:
            raise ValueError('CAP source exceeded 90-second collection deadline; entire source deferred')
        child = dict(sourceId=source['id']+'-'+hashlib.sha256(url.encode()).hexdigest()[:24],url=url)
        result, content = fetcher(child,{},now,fixtures)
        if content is None:
            raise ValueError('CAP bulletin fetch failed: '+str(result.get('message')))
        return url,content
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for offset in range(0,len(links),4):
            # At most four complete responses are resident; stop scheduling after
            # a budget failure, and never publish an accepted prefix of a feed.
            for url,content in pool.map(one,links[offset:offset+4]):
                total += len(content)
                if total > MAX_BYTES:
                    raise ValueError('CAP complete source exceeds byte budget; deferred without publication')
                parsed = parse_alert(source,content,url,now,countries,areas)
                if parsed:
                    messages.append(parsed)
    return messages


def apply_lifecycle(messages, records, now):
    """Collapse explicit reference chains; expiry/cancellation never means resolved."""
    working = {}
    lookup = {}
    for row in records.values():
        for key in row.get('sourceMetadata',{}).get('capMessageKeys',[]):
            lookup[(row['sourceIds'][0],key)] = row['id']
    for message in sorted(messages,key=lambda m:m['control']['sent']):
        c = message['control']; own=(c['sourceId'],c['key'])
        targets = {lookup[(c['sourceId'],k)] for k in c['references'] if (c['sourceId'],k) in lookup}
        if own in lookup:
            previous = working.get(lookup[own], records.get(lookup[own]))
            if previous and any(r['contentHash']==c['contentHash'] for r in previous['reports']):
                continue
            targets.add(lookup[own])
        if c['kind']=='Cancel':
            for iid in targets:
                old=working.get(iid,records.get(iid))
                if not old or c['sent'] < old['updatedAt']:
                    continue
                row=copy.deepcopy(old);row['state']='cancelled';row['updatedAt']=c['sent']
                meta=row['sourceMetadata'];meta['capMessageKeys']=list(dict.fromkeys(meta.get('capMessageKeys',[])+[c['key']]))
                meta['cancelledAt']=c['sent'];meta['cancellationUrl']=c['url']
                row['reports']=[dict(id=iid+'-cancel-'+c['key'][:12],incidentId=iid,sourceId=c['sourceId'],title='Warning cancelled',url=c['url'],language='und',publishedAt=c['sent'],retrievedAt=now,excerpt='The issuing authority cancelled its referenced warning.',contentHash=c['contentHash'],independentGroup=c['sourceId'])]+row['reports']
                working[iid]=row;lookup[own]=iid
            continue
        row=copy.deepcopy(message['record'])
        if row is None:
            continue
        if len(targets)==1:
            iid=next(iter(targets));old=working.get(iid,records.get(iid))
            if c['sent'] < old['updatedAt']:
                continue
            row['id']=iid;row['firstSeenAt']=old['firstSeenAt']
            for report in row['reports']:report['incidentId']=iid
            row['sourceMetadata']['capMessageKeys']=list(dict.fromkeys(old.get('sourceMetadata',{}).get('capMessageKeys',[])+[c['key']]))
        elif len(targets)>1:
            # A replacement can explicitly supersede several warnings; retain each.
            for iid in targets:
                old=working.get(iid,records.get(iid))
                if c['sent']>=old['updatedAt']:
                    prior=copy.deepcopy(old);prior['state']='expired';prior['sourceMetadata']['supersededBy']=row['id'];working[iid]=prior
        working[row['id']]=row;lookup[own]=row['id']
    return list(working.values())


def expire_warnings(records, now):
    for row in records.values():
        meta=row.get('sourceMetadata',{})
        if meta.get('provider')=='CAP 1.2' and meta.get('expires') and meta['expires']<=now and row['state'] not in ('cancelled','resolved','expired'):
            row['state']='expired'
