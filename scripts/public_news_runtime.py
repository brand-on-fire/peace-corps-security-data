#!/usr/bin/env python3
"""Opt-in, source-specific English security reports; no model or paid service.

This is intentionally not a general keyword-to-incident converter. Complete public
WordPress bodies, a reviewed source identity, an event statement, a local place
and an explicit event-day or honestly labeled publication anchor are required. The whole
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
        places=('Lusaka', 'Chililabombwe', 'Chingola', 'Solwezi', 'Kitwe', 'Kifubwa River', 'Kafue River', 'Nangoma')),
    'news-gy-demerara-waves': dict(host='demerarawaves.com', country='GY', timezone='America/Guyana',
        places=('Demerara', 'East Canje', 'East Coast Demerara', 'West Coast Demerara', 'Linden')),
    'gov-ke-kenya-broadcasting-corporation': dict(host='www.kbc.co.ke', country='KE', timezone='Africa/Nairobi',
        places=('Nairobi', 'Kisumu', 'Nyahururu', 'Garissa', 'Mandera', 'Kericho', 'Naivasha')),
}
# Category needs evidence in both the title and the primary event statement.
# Gunfire alone is never organized conflict. Routine weather is not admitted.
CATEGORY = {
    'conflict': r'\b(?:al[ -]?shabaab|armed conflict|armed clashes|terror(?:ist)? attack|militants?|insurgents?|shelling|airstrikes?)\b',
    'unrest': r'\b(?:riots?|unrest|violent protests?|roadblocks?|political violence|riot and arson)\b',
    'transport': r'\b(?:highway|road|bridge|ferry|railway|train|bus|plane|aircraft|helicopter|airport|road traffic|crash|collision|derailment|emergency landing)\b',
    'infrastructure': r'\b(?:blackouts?|power outage|power disruptions?|water (?:outage|supply disruption)|service interruption|electricity.{0,25}(?:cut|disruption))\b',
    'other': r'\b(?:pollutes|pollution|acid (?:spill|tanker)|chemical (?:spill|leak)|toxic (?:spill|leak)|gas leak|poisoned (?:water|fish)|water contamination)\b',
    'crime': r'\b(?:murder|fatal (?:police )?shooting|shooting incident|gunfire|shot dead|shot and (?:killed|injured)|stabbed to death|robbery|armed men rob|robbed|abduction|kidnapping|assault)\b',
    'flood': r'\b(?:flood(?:s|ing)?|rising waters)\b',
}
TRANSPORT_IMPACT = r'\b(?:closed|closes|closure|collapsed|collapse|sinkhole|blocked|killed|dead|fatal|injured|derail(?:ed|ment)|crash-landed|emergency landing)\b'
EVENT_VERB = r'\b(?:occurred|happened|began|hit|erupted|killed|shot|stabbed|claimed the life|crash-landed|emergency landing|robbed|robbery|murder|shooting|clashes|assault|closed|closes|closure|collapsed|spill(?:ed|ing)?|outage|blackout|interruption|disruption|attack|injured|protests?|riot|evacuated|displaced)\b'
FLOOD_IMPACT = r'\b(?:evacuated|displaced|stranded|marooned|homeless|houses? collapsed)\b'
HOLD_TITLE = r'\b(?:opinion|analysis|editorial|anniversary|statistics|decline|reduction|historic low|sentenced|conviction|court|charged|charges|extradition|suspect|alleged(?:ly)?|may |could |forecast|planned maintenance|scheduled maintenance)\b'
HOLD_TEXT = r'\b(?:correction|retraction|retracted|withdrawn|unconfirmed|unverified|conflicting accounts|contrary to|false report|false that|not true|refuted|refutes|no evidence that|did not (?:occur|happen|result in)|disputed|disputes|denied (?:the|that)|allegations? of|reports? (?:were|was) false)\b'
NON_EVENT = r'\b(?:simulation|simulated|rumou?rs?|hoax|fabricated|tomorrow|mock (?:attack|incident)|training exercise|emergency drill|fictional|movie|film|novel|planned maintenance|scheduled maintenance|will (?:occur|happen|take place|start|begin|be closed|be blocked)|(?:did|does|has|have|had) not (?:actually )?(?:occur|happen|take place)|didn[’\x27]t (?:occur|happen)|next (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)|no (?:reported |actual |fatal )?(?:murder|shooting|attack|robbery|outage|blackout)|(?:never|not) (?:occurred|happened)|denies|denied)\b'
NEGATED_HARM = r'\b(?:(?:was|were|has|have|had) not (?:been )?|never (?:been )?)(?:stabbed|shot|killed|robbed|evacuated|displaced)\b'
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
    for name in ('conflict','unrest','other','crime','infrastructure','transport','flood'):
        pattern=CATEGORY[name]
        title_matches=re.search(pattern,title,re.I) or (name=='crime' and re.search(r'\bkilled\b',title,re.I)
            and re.search(r'\b(?:stabbed to death|shot dead|shot and killed)\b',body,re.I))
        if title_matches and re.search(pattern,body,re.I):
            if name == 'transport' and not re.search(TRANSPORT_IMPACT,title+' '+body,re.I): continue
            if name == 'flood' and not re.search(FLOOD_IMPACT,body,re.I): continue
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
    relative_days={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7}
    for match in re.finditer(r'\b(one|two|three|four|five|six|seven|[1-7]) days? ago\b',text,re.I):
        days=relative_days.get(match[1].lower(),int(match[1]) if match[1].isdigit() else 0)
        matches.append(local-dt.timedelta(days=days))
    return matches[0] if matches and len(set(matches))==1 else None


def publication_anchor(statement, body, category, source_id):
    """Reviewed actual-event forms when occurrence time is genuinely unstated.

    This anchor says when a publisher reported the event, never when it happened.
    Dates elsewhere, retrospective context and uncertain/future verbs stay held.
    Only categories/source structures demonstrated by complete retained articles
    are enabled; a category keyword or the publisher's date alone is insufficient.
    """
    if re.search(r'\b(?:'+MONTH_PATTERN+'|'+DAY_PATTERN+r'|yesterday|today|tomorrow|ago|previous|historical|anniversary|cold case|last year|last month|last week|20\d{2})\b',body,re.I):
        return False
    if re.search(r'\b(?:may|might|could|would|will|reportedly|allegedly|suspected|possible|planned|scheduled|expected)\b',statement,re.I):
        return False
    if source_id=='gov-ke-kenya-broadcasting-corporation' and category=='transport':
        return bool(re.match(r'^(?:a|the) helicopter has crash-landed at\b',statement,re.I)
            and re.search(r'\bKenya Red Cross\b',body)
            and re.search(r'\b(?:all persons on board have been taken to hospital for treatment|serious injuries)\b',body,re.I))
    if source_id=='news-zm-lusaka-times' and category=='transport':
        return bool(re.match(r'^(?:Two|The two) pilots aboard (?:[^.!?]+ )?aircraft survived an emergency landing after the plane developed a fault\b',statement,re.I)
            and re.search(r'\bconfirmed that both pilots sustained head injuries\b',body,re.I))
    return False


def investigated_shooting(source_id, title, body):
    """A police investigation plus physical evidence is not proof of guilt."""
    return (source_id=='news-bz-breaking-belize-news'
        and bool(re.match(r'^Police are investigating a shooting incident in Belize City after\b',title,re.I))
        and not re.search(HOLD_TITLE,re.sub(r'\ballegedly\b','',title,flags=re.I),re.I)
        and bool(re.search(r'\bPolice are investigating a shooting incident in Belize City\b',body,re.I))
        and bool(re.search(r'\bPolice recovered (?:one|a|\d+) expended 9mm shell casing from the scene\b',body,re.I)))


def headline_event_place(source_id, title, statement):
    """Join an explicit event locality with the same reported road, not an office."""
    if (source_id=='news-bz-breaking-belize-news'
            and re.fullmatch(r'Armed men rob Orange Walk shop on Palmar Road[.!]?',title,re.I)
            and re.search(r'\ban armed robbery at a shop on Palmar Road\b',statement,re.I)):
        return ('Palmar Road, Orange Walk',None)
    return None


# These two complete canonical representations were reviewed in full. This is
# finite reviewed intake, not a classifier for future health/conflict articles.
REVIEWED_SECURITY_ACCOUNTS = {
    'https://www.kbc.co.ke/kenya-confirms-second-ebola-case/': dict(
        contentHash='65e6e1d56549bb72d4cbbb8d6af52b13ef9aab56c0b72013aa87e43f334a9904',
        category='health',statement='A confirmed Ebola case was reported in Kenya.',
        summary='KBC, citing Kenya’s Ministry of Health, reports a second laboratory-confirmed Ebola case linked to the first case. The ministry reports no evidence of wider community transmission and no travel, trade or public-gathering restrictions.',
        metadata=dict(admissionProfile='reviewed-kbc-linked-ebola-case-v1',reportedSymptomOnsetDate='2026-10-13',
            healthAuthority='Kenya Ministry of Health',transmissionQualifier='No evidence of transmission in the wider community, according to the ministry')),
    'https://www.kbc.co.ke/kenya-elite-forces-foil-attack-by-al-shabaab-in-garissa/': dict(
        contentHash='23c25358c3f50f15693269a87e15b64018d396231d3726075124267fb260a4b3',
        category='conflict',statement='An attempted attack and recovery of explosive devices was reported in Garissa County.',
        summary='KBC, citing the National Counter Terrorism Centre, reports that officers recovered assembled explosive devices after intercepting an attempted attack in Garissa County. The reported Al-Shabaab affiliation is suspected, not independently verified.',
        metadata=dict(admissionProfile='reviewed-kbc-local-ied-recovery-v1',allegationsRetained=True,
            attribution='National Counter Terrorism Centre, as reported by KBC',actorAffiliation='suspected Al-Shabaab')),
}


def reviewed_security_account(source_id, item, representation_hash):
    """Project only a reviewed complete representation; changed text is held.

    The hash includes the full accepted text, headline, URL, GUID and source
    clocks. A later matching phrase cannot override changed/contradictory text.
    """
    if source_id!='gov-ke-kenya-broadcasting-corporation':return None
    review=REVIEWED_SECURITY_ACCOUNTS.get(item['url'])
    if not review or representation_hash!=review['contentHash']:return None
    result=copy.deepcopy(review)
    result['metadata']['reviewedRepresentationHash']=review['contentHash']
    result['metadata']['admissionBasis']='Explicit complete-representation review; changes require review'
    text=item['description']
    if review['category']=='conflict':
        # This background exception is bound to the full reviewed representation.
        background='Attacks along the Kenya-Somalia border are frequent, mostly targeting patrol cars and civilians'
        if not text.endswith(background):return None
        text=text[:-len(background)].rstrip()
    result['foreignScreenText']=text
    return result


def place_evidence(text, profile, areas, country):
    """Require a place in an event statement; do not geocode a publisher's nation."""
    # Reporting offices do not establish the incident location. Remove only a
    # reporting preamble before the event predicate; the original full text is
    # still retained and inspected by the admission checks.
    event=re.search(EVENT_VERB,text,re.I)
    reporting=re.search(r'\b(?:said|reported|confirmed|announced|stated|are investigating|is investigating)\b',text,re.I)
    if reporting and (not event or reporting.start()<event.start()):
        text=text[reporting.end():]
    # A victim's residence before an assault does not locate the assault.
    harm=re.search(r'\b(?:stabbed to death|shot dead|shot and killed)\b',text,re.I)
    if harm:text=text[harm.end():]
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
        pinned_review=REVIEWED_SECURITY_ACCOUNTS.get(url) if source['id']=='gov-ke-kenya-broadcasting-corporation' else None
        reviewed=reviewed_security_account(source['id'],item,digest(item))
        category=pinned_review['category'] if pinned_review else category_for(title,body)
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
                # This is an observed withdrawal of our automatic assertion, not
                # a claim that the publisher advanced its CMS clock. Otherwise
                # the pipeline's stale-positive guard discards this safe hold.
                row['updatedAt']=max(instant(old['updatedAt']),current).isoformat(timespec='seconds').replace('+00:00','Z')
                row['summary']='The publisher changed this report; the updated account requires review.'
                row['sourceMetadata'].update(reviewRequired=True,reviewReason=reason,latestRepresentationHash=digest(item))
                report_id=iid+'-update-'+digest(item)[:12]
                if not any(report['id']==report_id for report in row['reports']):
                    row['reports'].append(dict(id=report_id,incidentId=iid,sourceId=source['id'],
                        title=title,url=url,language=source['language'],publishedAt=item['publishedAt'],retrievedAt=now,
                        excerpt='',contentHash=digest(item),independentGroup=source.get('independentGroup',source['id']),
                        sourceMetadata=dict(sourceModifiedAt=item.get('modifiedAt',item['publishedAt']),holdObservedAt=now)))
                rows.append(row)
        if item.get('representationFormat')!='wordpress-full-content-v1':hold('complete-source-body-required');continue
        parts=urllib.parse.urlsplit(url)
        if parts.scheme!='https' or parts.hostname!=profile['host'] or parts.username or parts.password:
            raise ValueError('Automatic news article identity changed')
        published=instant(item['publishedAt']);modified=instant(item.get('modifiedAt',item['publishedAt']))
        if published>current+dt.timedelta(minutes=5) or modified>current+dt.timedelta(minutes=5) or published<current-dt.timedelta(days=366):
            hold('publication-outside-supported-window');continue
        if pinned_review and not reviewed:hold('reviewed-complete-representation-changed');continue
        if not category:hold('changed-account-no-longer-supports-event');continue
        investigation=investigated_shooting(source['id'],title,body)
        if re.search(HOLD_TITLE,title,re.I) and not investigation:hold('non-event-or-allegation-headline');continue
        if re.search(NON_EVENT+'|'+NEGATED_HARM,title+' '+body,re.I):hold('planned-simulated-or-denied-event');continue
        if re.search(HOLD_TEXT,title+' '+body,re.I):hold('uncertain-or-corrected-account');continue
        foreign_text=reviewed['foreignScreenText'] if reviewed else body
        foreign_country=any(c['code']!=code and re.search(r'\b(?:in|across|near)\s+'+re.escape(c['name'])+r'(?!\w)',foreign_text,re.I) for c in countries.values())
        if foreign_country or re.search(FOREIGN,title+' '+foreign_text,re.I):hold('foreign-or-cross-border-context-requires-review');continue
        if category=='conflict' and not re.search(r'\b(?:armed|militants?|terrorists?|forces|soldiers|fighters|insurgents?)\b',body,re.I):
            hold('organized-violence-evidence-required');continue
        if category=='other' and not re.search(r'\b(?:acid|toxic|chemical|contaminat\w*|poison\w*|gas leak)\b',body,re.I):
            hold('physical-security-impact-required');continue
        statements=sentences(body,title)
        candidates=[];conflicting_day=False
        if reviewed:
            location=place_evidence(reviewed['statement'],profile,areas,countries[code])
            if (reviewed['category']=='conflict' and
                    place_evidence(body,profile,areas,countries[code])!=location):
                hold('multiple-event-localities-require-review');continue
            if location:candidates.append((published.astimezone(ZoneInfo(profile['timezone'])).date(),location,'publication'))
        explicit_event_days=[event_day(s,published,profile['timezone']) for s in statements
            if re.search(r'\bthe incident (?:happened|occurred)\b',s,re.I)]
        explicit_event_days=[day for day in explicit_event_days if day is not None]
        # Primary event statements are the opening three sentences. All text above
        # has still been examined for conflicts/corrections/foreign context.
        for index,statement in enumerate([] if reviewed else statements[:3]):
            if not re.search(CATEGORY[category],statement,re.I) or not re.search(EVENT_VERB,statement,re.I):continue
            if re.search(r'\b(?:allegedly|reportedly|suspected|unconfirmed)\b',statement,re.I) and not investigation:continue
            if category=='transport' and not re.search(TRANSPORT_IMPACT,statement,re.I):continue
            if category=='flood' and (not re.search(FLOOD_IMPACT,statement,re.I)
                    or re.search(r'\b(?:not|no|never|without|may|might|could|would|will|expected|planned)\b',statement,re.I)):continue
            if re.search(r'\b(?:not|no)\s+(?:\w+\s+){0,2}(?:shooting|attack|outage|closure|injur|disruption)',statement,re.I):continue
            location=place_evidence(statement,profile,areas,countries[code])
            if location is None:location=headline_event_place(source['id'],title,statement)
            if not location:continue
            when=event_day(statement,published,profile['timezone'])
            if when is None and index+1<len(statements):
                # The immediately following source sentence can specify the time of
                # the same reported event, but not a court hearing or another event.
                follow=statements[index+1]
                if re.search(r'\b(?:incident|occurred|happened|at about|around|at approximately|according to police)\b',follow,re.I):
                    when=event_day(statement+' '+follow,published,profile['timezone'])
            if when is None:
                if len(set(explicit_event_days))==1:when=explicit_event_days[0]
            if when is not None and any(day!=when for day in explicit_event_days):
                conflicting_day=True;break
            basis='explicit-source-event-day'
            if when is None and category in ('transport','infrastructure') and re.search(r'\b(?:is|remains|has been)\s+(?:temporarily\s+)?(?:closed|blocked)|\befforts are ongoing.{0,80}restore',statement,re.I):
                when=published.astimezone(ZoneInfo(profile['timezone'])).date();basis='current-condition-reported-at-publication'
            if when is None and publication_anchor(statement,body,category,source['id']):
                when=published.astimezone(ZoneInfo(profile['timezone'])).date();basis='publication'
            if when is None:continue
            if when<current.astimezone(ZoneInfo(profile['timezone'])).date()-dt.timedelta(days=366):continue
            candidates.append((when,location,basis))
        if conflicting_day:hold('conflicting-event-days-require-review');continue
        if not candidates:hold('primary-event-location-and-date-required');continue
        identities={(when,place[0],basis) for when,place,basis in candidates}
        if len(identities)!=1:hold('multiple-event-accounts-require-review');continue
        when,(place,area),basis=candidates[0]
        occurred=(published.isoformat(timespec='seconds').replace('+00:00','Z') if basis in ('current-condition-reported-at-publication','publication')
                  else when.isoformat()+'T00:00:00Z')
        summary=reviewed['summary'] if reviewed else f'{source["name"]} reports: {title}'
        if investigation:
            summary=(f'{source["name"]} reports that police are investigating gunfire in Belize City '
                'and recovered a spent shell casing. Allegations about who fired remain unverified.')
        provider=dict(parser=PARSER,recordId=item['guid'],eventTimeBasis=basis,
            eventDatePrecision='publication' if basis in ('current-condition-reported-at-publication','publication') else 'day',
            reportedLocalTimeZone=profile['timezone'],reportedPlace=place,
            evidenceScope='Source-attributed primary event account; not independently corroborated',
            representationFormat=item['representationFormat'],completeAcceptedTextSha256=hashlib.sha256(body.encode()).hexdigest(),
            languageProcessing='Original English source; no translation or generative model',reviewRequired=False)
        if basis=='publication':
            provider.update(eventDateKnown=False,eventDatePrecision='unknown')
            summary+=(' The source does not specify the confirmation date.' if category=='health' else ' The source does not specify the event date.')
        if reviewed:provider.update(reviewed['metadata'])
        if investigation:provider['allegationsRetained']=True
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
