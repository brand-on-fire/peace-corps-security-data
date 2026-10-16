#!/usr/bin/env python3
"""Opt-in, source-specific English security reports; no model or paid service.

This is intentionally not a general keyword-to-incident converter. Complete public
WordPress bodies, a reviewed source identity, an event statement, a local place
and an explicit event-day/current-restriction anchor are all required. The whole
accepted text is inspected; uncertain/foreign/corrected material stays in review.
"""
import copy
import datetime as dt
import hashlib
import re
import urllib.parse
from zoneinfo import ZoneInfo

PARSER = 'reviewed-security-news-v1'
PROFILES = {
    'gov-jm-jcf': dict(host='jcf.gov.jm', country='JM', timezone='America/Jamaica',
        places=('Kingston', 'Montego Bay', 'Denham Town'), officialJurisdiction=True),
    'news-bz-breaking-belize-news': dict(host='www.breakingbelizenews.com', country='BZ', timezone='America/Belize',
        places=('Belize City', 'Orange Walk', 'Hummingbird Highway', 'George Price Highway', 'Philip Goldson Highway', 'Ladyville', 'Belmopan')),
    'news-zm-lusaka-times': dict(host='www.lusakatimes.com', country='ZM', timezone='Africa/Lusaka',
        places=('Lusaka', 'Chililabombwe', 'Chingola', 'Solwezi', 'Kitwe', 'Kifubwa River', 'Kafue River')),
    'news-gy-demerara-waves': dict(host='demerarawaves.com', country='GY', timezone='America/Guyana',
        places=('Demerara', 'East Canje', 'East Coast Demerara', 'West Coast Demerara')),
    'gov-ke-kenya-broadcasting-corporation': dict(host='www.kbc.co.ke', country='KE', timezone='Africa/Nairobi',
        places=('Nairobi', 'Kisumu', 'Nyahururu', 'Garissa', 'Mandera')),
}
# Category needs evidence in both the title and the primary event statement.
# Gunfire alone is never organized conflict. Routine weather is not admitted.
CATEGORY = {
    'conflict': r'\b(?:al[ -]?shabaab|armed conflict|armed clashes|terror(?:ist)? attack|militants?|insurgents?|shelling|airstrikes?)\b',
    'unrest': r'\b(?:riots?|unrest|violent protests?|roadblocks?|political violence|riot and arson)\b',
    'transport': r'\b(?:highway|road|bridge|ferry|railway|train|bus|plane|aircraft|airport|road traffic|crash|collision|derailment|emergency landing)\b',
    'infrastructure': r'\b(?:blackouts?|power outage|power disruptions?|water (?:outage|supply disruption)|service interruption|electricity.{0,25}(?:cut|disruption))\b',
    'other': r'\b(?:pollutes|pollution|acid (?:spill|tanker)|chemical (?:spill|leak)|toxic (?:spill|leak)|gas leak|poisoned (?:water|fish)|water contamination)\b',
    'crime': r'\b(?:murder|fatal (?:police )?shooting|shooting incident|gunfire|shot dead|shot and (?:killed|injured)|robbery|armed men rob|robbed|abduction|kidnapping|assault)\b',
}
TRANSPORT_IMPACT = r'\b(?:closed|closes|closure|collapsed|collapse|sinkhole|blocked|killed|dead|fatal|injured|derail(?:ed|ment)|emergency landing)\b'
EVENT_VERB = r'\b(?:occurred|happened|began|hit|erupted|killed|shot|robbed|robbery|murder|shooting|clashes|assault|closed|closes|closure|collapsed|spill(?:ed|ing)?|outage|blackout|interruption|disruption|attack|injured|protests?|riot)\b'
HOLD_TITLE = r'\b(?:opinion|analysis|editorial|anniversary|statistics|decline|reduction|historic low|sentenced|conviction|court|charged|charges|extradition|suspect|alleged(?:ly)?|may |could |forecast|planned maintenance|scheduled maintenance)\b'
HOLD_TEXT = r'\b(?:correction|retraction|retracted|withdrawn|unconfirmed|unverified|conflicting accounts|contrary to|false report|did not (?:occur|happen|result in)|disputed|disputes|denied (?:the|that)|allegations? of|reports? (?:were|was) false)\b'
NON_EVENT = r'\b(?:simulation|simulated|rumou?rs?|hoax|fabricated|tomorrow|mock (?:attack|incident)|training exercise|emergency drill|fictional|movie|film|novel|planned maintenance|scheduled maintenance|will (?:occur|happen|take place|start|begin|be closed|be blocked)|(?:did|does|has|have|had) not (?:actually )?(?:occur|happen|take place)|didn[’\x27]t (?:occur|happen)|next (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)|no (?:reported |actual |fatal )?(?:murder|shooting|attack|robbery|outage|blackout)|(?:never|not) (?:occurred|happened)|denies|denied)\b'
FOREIGN = r'\b(?:International News|in (?:France|Cuba|Mexico|Somalia|Ethiopia|the United States|the United Kingdom|South Africa|Japan|Ukraine|Russia|Israel|Gaza|Iran)|Kenya[–-]Somalia border|Kenyan border)\b'
MONTHS = {name:i for i,name in enumerate(('January','February','March','April','May','June','July','August','September','October','November','December'),1)}
MONTH_PATTERN = '|'.join(MONTHS)
WEEKDAYS = {name:i for i,name in enumerate(('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'))}
DAY_PATTERN = '|'.join(WEEKDAYS)
EXPLICIT_DATE = re.compile(r'\b(?:(?P<weekday>'+DAY_PATTERN+r'),?\s+)?(?P<month>'+MONTH_PATTERN+r')\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{2}))?\b', re.I)


def instant(value):
    result = dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None: raise ValueError('News timestamp lacks timezone')
    return result


def sentences(text, title):
    # Remove a repeated article heading, not any article content from analysis.
    if text.casefold().startswith(title.casefold()): text = text[len(title):].lstrip(' :–-')
    text = re.sub(r'\bSt\.', 'St', text)
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+(?=[A-Z“])',text) if s.strip()]


def category_for(title, body):
    for name in ('conflict','unrest','other','crime','infrastructure','transport'):
        pattern=CATEGORY[name]
        if re.search(pattern,title,re.I) and re.search(pattern,body,re.I):
            if name == 'transport' and not re.search(TRANSPORT_IMPACT,title+' '+body,re.I): continue
            return name
    return None


def event_day(text, published, zone):
    """Resolve explicit source days only; never replace a missing day with now."""
    if re.search(NON_EVENT,text,re.I):return None
    local = published.astimezone(ZoneInfo(zone)).date()
    matches, covered = [], []
    for match in EXPLICIT_DATE.finditer(text):
        month = MONTHS[match['month'].title()]
        years = [int(match['year'])] if match['year'] else [local.year-1,local.year]
        possible = []
        for year in years:
            try: value = dt.date(year,month,int(match['day']))
            except ValueError: continue
            if value > local or (local-value).days > 366: continue
            if match['weekday'] and value.weekday()!=WEEKDAYS[match['weekday'].title()]: continue
            possible.append(value)
        # A yearless date resolves to its most recent occurrence; a contradictory
        # weekday cannot be "repaired" by silently choosing a different day.
        if not possible: return None
        value=max(possible)
        if not match['year'] and (local-value).days > 31: return None
        matches.append(value);covered.append(match.span())
    for match in re.finditer(r'\b('+DAY_PATTERN+r')\b',text,re.I):
        if any(a<=match.start()<b for a,b in covered): continue
        matches.append(local-dt.timedelta(days=(local.weekday()-WEEKDAYS[match[0].title()])%7))
    if re.search(r'\byesterday\b',text,re.I): matches.append(local-dt.timedelta(days=1))
    if re.search(r'\b(?:today|this morning|this afternoon|this evening)\b',text,re.I): matches.append(local)
    return matches[0] if matches and len(set(matches))==1 else None


def place_evidence(text, profile, areas, country):
    """Require a place in an event statement; do not geocode a publisher's nation."""
    # Reporting offices do not establish the incident location. Remove only a
    # reporting preamble before the event predicate; the original full text is
    # still retained and inspected by the admission checks.
    event=re.search(EVENT_VERB,text,re.I)
    reporting=re.search(r'\b(?:said|reported|confirmed|announced|stated|are investigating|is investigating)\b',text,re.I)
    if reporting and (not event or reporting.start()<event.start()):
        text=text[reporting.end():]
    matches = []
    # Qualified administrative names may be assigned only when exactly one
    # canonical area matches, including parent hierarchy for a district.
    for area in areas:
        if area['countryCode']!=profile['country']: continue
        name=area['name'];pattern=re.escape(name)
        if name.startswith('Saint '): pattern=r'(?:Saint|St\.?)\s+'+re.escape(name[6:])
        qualified = pattern + r'\s+(?:County|Province|District|Parish)\b'
        if re.search(r'(?<!\w)'+qualified,text,re.I): matches.append((name,area))
    anchor_matches=[place for place in profile['places'] if re.search(r'(?<!\w)'+re.escape(place)+r'(?!\w)',text,re.I)]
    if matches:
        unique={area['id']: (name,area) for name,area in matches}
        if any(place.casefold() not in {name.casefold() for name,_ in matches} for place in anchor_matches):
            return (country['name'],None)
        if len(unique)==1: return next(iter(unique.values()))
        # A mixed-area statement cannot produce a fictitiously precise point.
        return (country['name'],None)
    for place in profile['places']:
        # A token in a headline or an official's job title is insufficient.
        if re.search(r'\b(?:in|at|across|near|on|of|parts of)\s+(?:the\s+)?(?:[^.!?;]{0,55}\s)?'+re.escape(place)+r'(?!\w)',text,re.I):
            return (place,None)
    if re.search(r'\b(?:in|across|throughout)\s+'+re.escape(country['name'])+r'(?!\w)',text,re.I):
        return (country['name'],None)
    return None


def news_window(items, previous, now):
    """Describe the bounded first page; preserve evidence of a missed interval."""
    modified=[instant(item.get('modifiedAt',item['publishedAt'])) for item in items]
    if any(a<b for a,b in zip(modified,modified[1:])):
        raise ValueError('WordPress modified-date order changed; collection window unverified')
    last=previous.get('lastSuccessAt')
    window=dict(itemCount=len(items),pageLimit=100,boundedInitialWindow=not bool(last),
                oldestModifiedAt=items[-1].get('modifiedAt',items[-1]['publishedAt']) if items else None,
                newestModifiedAt=items[0].get('modifiedAt',items[0]['publishedAt']) if items else None,
                previousSuccessfulCheck=last,checkedAt=now)
    result=dict(newsCollectionWindow=window)
    if previous.get('coverageGap'):result['coverageGap']=previous['coverageGap']
    if len(items)>=100 and last and modified[-1]>instant(last):
        start=last
        prior=previous.get('coverageGap')
        if prior and instant(prior['from'])<instant(start):start=prior['from']
        result['coverageGap']={'from':start,'through':window['oldestModifiedAt'],
            'reason':'Latest 100 modified articles do not reach the prior successful check; older changes may be missing.'}
    window['reachesPreviousSuccessfulCheck']=bool(last and (len(items)<100 or (modified and modified[-1]<=instant(last))))
    return result


def automatic_news(source, policy, items, now, countries, areas, reviews=(), existing=None):
    """Return admitted records and auditable held decisions, without fetching.

    Opt-in is reviewed configuration, never a publisher-supplied field. Existing
    explicit reviews (including exclusions) own their URL even if the feed changes.
    """
    from public_data_pipeline import digest, make_incident
    if policy.get('newsAdapter') is None: return [], []
    if policy['newsAdapter'] != PARSER: raise ValueError('Unreviewed automatic news adapter')
    profile=PROFILES.get(source['id'])
    endpoint=urllib.parse.urlsplit(policy['url'])
    origin=urllib.parse.urlsplit(source['url'])
    query=urllib.parse.parse_qs(endpoint.query)
    if (not profile or query!={'per_page':['100'],'orderby':['modified'],'order':['desc']} or policy.get('format')!='wordpress' or source.get('language','').split('-')[0]!='en'
            or endpoint.scheme!='https' or endpoint.hostname!=profile['host']
            or endpoint.path!='/wp-json/wp/v2/posts' or endpoint.username or endpoint.password
            or origin.hostname!=profile['host'] or profile['country'] not in source['countryCodes']):
        raise ValueError('Automatic news source, language or complete representation is not reviewed')
    code=profile['country']
    if code not in countries:return [], []
    current=instant(now);rows=[];decisions=[]
    manual_urls={r['url'] for r in reviews if r['sourceId']==source['id']}
    known=existing or {};seen_urls=set()
    for item in items:
        url=item['url'];title=item['title'];body=item['description']
        if url in seen_urls:raise ValueError('Duplicate automatic news article URL')
        seen_urls.add(url)
        if url in manual_urls:continue
        category=category_for(title,body)
        decision=dict(url=url,contentHash=digest(item),status='needs-review',reason=None)
        iid='news-'+hashlib.sha256(url.encode()).hexdigest()[:20]
        if iid in known and known[iid].get('sourceMetadata',{}).get('parser')!=PARSER:continue
        if not category and not (known.get(iid,{}).get('sourceMetadata',{}).get('parser')==PARSER):continue
        def hold(reason):
            decision['reason']=reason;decisions.append(decision)
            old=known.get(iid)
            if (old and old.get('sourceMetadata',{}).get('parser')==PARSER
                    and old.get('sourceMetadata',{}).get('latestRepresentationHash',old['reports'][-1]['contentHash'])!=digest(item)):
                # Do not silently preserve a now unsupported automatic assertion.
                row=copy.deepcopy(old);row['evidenceStatus']='unverified';row['state']='expired'
                row['updatedAt']=item.get('modifiedAt',item['publishedAt'])
                row['summary']='The publisher changed this report; the updated account requires review.'
                row['sourceMetadata'].update(reviewRequired=True,reviewReason=reason,latestRepresentationHash=digest(item))
                report_id=iid+'-update-'+digest(item)[:12]
                if not any(report['id']==report_id for report in row['reports']):
                    row['reports'].append(dict(id=report_id,incidentId=iid,sourceId=source['id'],
                        title=title,url=url,language=source['language'],publishedAt=item['publishedAt'],retrievedAt=now,
                        excerpt='',contentHash=digest(item),independentGroup=source.get('independentGroup',source['id'])))
                rows.append(row)
        if item.get('representationFormat')!='wordpress-full-content-v1':hold('complete-source-body-required');continue
        parts=urllib.parse.urlsplit(url)
        if parts.scheme!='https' or parts.hostname!=profile['host'] or parts.username or parts.password:
            raise ValueError('Automatic news article identity changed')
        published=instant(item['publishedAt']);modified=instant(item.get('modifiedAt',item['publishedAt']))
        if published>current+dt.timedelta(minutes=5) or modified>current+dt.timedelta(minutes=5) or published<current-dt.timedelta(days=366):
            hold('publication-outside-supported-window');continue
        if not category:hold('changed-account-no-longer-supports-event');continue
        if re.search(HOLD_TITLE,title,re.I):hold('non-event-or-allegation-headline');continue
        if re.search(NON_EVENT,title+' '+body,re.I):hold('planned-simulated-or-denied-event');continue
        if re.search(HOLD_TEXT,title+' '+body,re.I):hold('uncertain-or-corrected-account');continue
        foreign_country=any(c['code']!=code and re.search(r'\b(?:in|across|near)\s+'+re.escape(c['name'])+r'(?!\w)',body,re.I) for c in countries.values())
        if foreign_country or re.search(FOREIGN,title+' '+body,re.I):hold('foreign-or-cross-border-context-requires-review');continue
        if category=='conflict' and not re.search(r'\b(?:armed|militants?|terrorists?|forces|soldiers|fighters|insurgents?)\b',body,re.I):
            hold('organized-violence-evidence-required');continue
        if category=='other' and not re.search(r'\b(?:acid|toxic|chemical|contaminat\w*|poison\w*|gas leak)\b',body,re.I):
            hold('physical-security-impact-required');continue
        statements=sentences(body,title)
        candidates=[]
        # Primary event statements are the opening three sentences. All text above
        # has still been examined for conflicts/corrections/foreign context.
        for index,statement in enumerate(statements[:3]):
            if not re.search(CATEGORY[category],statement,re.I) or not re.search(EVENT_VERB,statement,re.I):continue
            if category=='transport' and not re.search(TRANSPORT_IMPACT,statement,re.I):continue
            if re.search(r'\b(?:not|no)\s+(?:\w+\s+){0,2}(?:shooting|attack|outage|closure|injur|disruption)',statement,re.I):continue
            location=place_evidence(statement,profile,areas,countries[code])
            if not location:continue
            when=event_day(statement,published,profile['timezone'])
            if when is None and index+1<len(statements):
                # The immediately following source sentence can specify the time of
                # the same reported event, but not a court hearing or another event.
                follow=statements[index+1]
                if re.search(r'\b(?:incident|occurred|happened|at about|around|at approximately|according to police)\b',follow,re.I):
                    when=event_day(statement+' '+follow,published,profile['timezone'])
            if when is None:
                anchors=[event_day(s,published,profile['timezone']) for s in statements
                         if re.search(r'\bthe incident (?:happened|occurred)\b',s,re.I)]
                anchors=[day for day in anchors if day is not None]
                if len(anchors)==1: when=anchors[0]
            basis='explicit-source-event-day'
            if when is None and category in ('transport','infrastructure') and re.search(r'\b(?:is|remains|has been)\s+(?:temporarily\s+)?(?:closed|blocked)|\befforts are ongoing.{0,80}restore',statement,re.I):
                when=published.astimezone(ZoneInfo(profile['timezone'])).date();basis='current-condition-reported-at-publication'
            if when is None:continue
            if when<current.astimezone(ZoneInfo(profile['timezone'])).date()-dt.timedelta(days=366):continue
            candidates.append((when,location,basis))
        if not candidates:hold('primary-event-location-and-date-required');continue
        identities={(when,place[0],basis) for when,place,basis in candidates}
        if len(identities)!=1:hold('multiple-event-accounts-require-review');continue
        when,(place,area),basis=candidates[0]
        occurred=(published.isoformat(timespec='seconds').replace('+00:00','Z') if basis=='current-condition-reported-at-publication'
                  else when.isoformat()+'T00:00:00Z')
        summary=f'{source["name"]} reports: {title}'
        provider=dict(parser=PARSER,recordId=item['guid'],eventTimeBasis=basis,
            eventDatePrecision='publication' if basis=='current-condition-reported-at-publication' else 'day',
            reportedLocalTimeZone=profile['timezone'],reportedPlace=place,
            evidenceScope='Source-attributed primary event account; not independently corroborated',
            representationFormat=item['representationFormat'],completeAcceptedTextSha256=hashlib.sha256(body.encode()).hexdigest(),
            languageProcessing='Original English source; no translation or generative model',reviewRequired=False)
        row=make_incident(source,iid,title,summary,countries[code],category,occurred,item.get('modifiedAt',item['publishedAt']),now,
            dict(precision='area' if area else 'country'),url,provider,item,published=item['publishedAt'])
        row['evidenceStatus']='official' if source.get('tier')=='official' and profile.get('officialJurisdiction') else 'reported'
        row['region']=place;row['reports'][0]['excerpt']=''
        if area:
            row['adminAssignment']=dict(method='explicit_source_place_names', confidence='source_reported_area',pointInferred=False)
            row['admin1Code']=row['admin1Id']=area['id'];row['admin1Name']=area['name']
            if area['level']=='ADM2':
                parent=next((a for a in areas if a['id']==area['parentId'] and a['countryCode']==code and a['level']=='ADM1'),None)
                if not parent:hold('administrative-parent-unverified');continue
                row.update(admin2Code=area['id'],admin2Id=area['id'],admin2Name=area['name'],
                           admin1Code=parent['id'],admin1Id=parent['id'],admin1Name=parent['name'])
        rows.append(row);decision.update(status='automatic-source-record',reason='source-profile-event-place-date-passed',incidentId=iid);decisions.append(decision)
    return rows,decisions
