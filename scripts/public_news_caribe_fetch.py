#!/usr/bin/env python3
"""Bounded, inactive Caribe Actual collector; caller owns atomic state publication."""
import copy
import datetime as dt
import hashlib
import json
import re

from public_news_caribe import (HOST, SOURCE_ID, PARSER, collect_caribe,
                                instant, parse_posts, sha)

ADAPTERS = {'cr-caribe'}
LATEST_URL = 'https://'+HOST+'/wp-json/wp/v2/posts?per_page=100&orderby=modified&order=desc'
MAX_REQUESTS = 4
MAX_RESPONSE_BYTES = 8_000_000
MAX_TOTAL_BYTES = 12_000_000
MAX_RECHECKS = 2500
MAX_QUARANTINE = 256
RECHECK_HOURS = 6
STATE_VERSION = 1


def endpoint(identity):
    if not isinstance(identity, str) or not re.fullmatch(r'[1-9]\d{0,9}', identity):
        raise ValueError('Unreviewed numeric Caribe post identity')
    return 'https://'+HOST+'/wp-json/wp/v2/posts/'+identity


def incident_id(identity):
    endpoint(identity)
    return 'news-'+hashlib.sha256((SOURCE_ID+'|'+identity).encode()).hexdigest()[:20]


def prepare_caribe_policy(policy, previous, now):
    return dict(policy)


def validate_source_policy(source, policy, now, countries):
    if (source.get('id')!=SOURCE_ID or source.get('enabled') is not True or policy.get('enabled') is False
            or source.get('feedUrl')!=LATEST_URL or source.get('kind')!='rss'
            or source.get('access')!='public-feed' or policy.get('sourceId')!=SOURCE_ID
            or policy.get('adapter') not in ADAPTERS or policy.get('mode')!='official'
            or policy.get('url')!=LATEST_URL):
        raise ValueError('Caribe collector is disabled or has an unreviewed source/policy')
    collect_caribe(source,b'[]',now,countries)
    if countries.get('CR',{}).get('status')!='active':
        raise ValueError('Caribe collector requires verified active Costa Rica')


def request_state(policy, previous):
    # The full listing and old exact-ID corrections have separate clocks.
    # A listing 304 cannot certify either against retained complete evidence.
    return {k:v for k,v in previous.items() if k not in {'etag','lastModified'}}


def clock(value, now):
    if not isinstance(value, str) or instant(value)>instant(now):
        raise ValueError('Invalid Caribe retained observation time')
    return value


class DetailUnavailable(ValueError):
    pass


class Requests:
    def __init__(self, policy, fetcher, now, fixtures):
        self.policy=policy;self.fetcher=fetcher;self.now=now;self.fixtures=fixtures
        self.receipts=[];self.bytes=0

    def accept(self, raw, url, result):
        if len(self.receipts)>=MAX_REQUESTS:
            raise ValueError('Caribe request budget exceeded')
        status=result.get('httpStatus')
        receipt=dict(url=url,httpStatus=status,retrievedAt=self.now,
            bytes=len(raw) if isinstance(raw,bytes) else 0,
            sha256=hashlib.sha256(raw).hexdigest() if isinstance(raw,bytes) else None)
        if result.get('transportTiming') is not None:
            receipt['transportTiming']=copy.deepcopy(result['transportTiming'])
        self.receipts.append(receipt);self.bytes+=receipt['bytes']
        if self.bytes>MAX_TOTAL_BYTES:
            raise ValueError('Caribe complete-response aggregate byte budget exceeded')
        if result.get('finalUrl',url)!=url:
            raise DetailUnavailable('Exact article endpoint redirected; identity requires review')
        if not isinstance(raw,bytes):
            raise DetailUnavailable('Complete article response unavailable')
        if len(raw)>MAX_RESPONSE_BYTES:
            raise DetailUnavailable('Complete article exceeds individual response budget')
        return receipt

    def get(self, identity):
        url=endpoint(identity)
        if len(self.receipts)>=MAX_REQUESTS:
            raise ValueError('Caribe request budget exhausted before request')
        result,raw=self.fetcher(dict(self.policy,sourceId=SOURCE_ID+'-'+identity,url=url),{},self.now,self.fixtures)
        return raw,self.accept(raw,url,result)


def owned_records(records, reviews):
    manual={r['url'] for r in reviews if r.get('sourceId')==SOURCE_ID}
    owned={};blocked=set()
    for row in records.values():
        metadata=row.get('sourceMetadata',{})
        if SOURCE_ID not in row.get('sourceIds',[]):continue
        identity=str(metadata.get('recordId',''))
        if row.get('sourceIds')!=[SOURCE_ID] or metadata.get('parser')!=PARSER:
            # A reviewer may own the same stable post ID while using a different
            # parser. Match only an explicit numeric provenance ID, never a URL.
            if re.fullmatch(r'[1-9]\d{0,9}',identity):blocked.add(identity)
            continue
        if (row.get('id')!=incident_id(identity) or row.get('countryCode')!='CR'
                or not row.get('reports') or any(r.get('sourceId')!=SOURCE_ID for r in row['reports'])):
            raise ValueError('Retained Caribe automatic record identity mismatch')
        if any(r.get('url') in manual for r in row['reports']):blocked.add(identity);continue
        if identity in owned:raise ValueError('Duplicate retained Caribe post identity')
        owned[identity]=row
    for identity in blocked:owned.pop(identity,None)
    if len(owned)>MAX_RECHECKS:raise ValueError('Caribe retained recheck ceiling requires review')
    return owned,blocked,manual


def prior_state(previous, owned, now):
    checks=copy.deepcopy(previous.get('caribeRechecks',{}))
    quarantine=copy.deepcopy(previous.get('caribeQuarantine',{}))
    if (not isinstance(checks,dict) or len(checks)>MAX_RECHECKS+100 or not isinstance(quarantine,dict)
            or len(quarantine)>MAX_QUARANTINE or previous.get('caribeVersion',STATE_VERSION)!=STATE_VERSION):
        raise ValueError('Invalid Caribe bounded recheck state')
    for identity,entry in checks.items():
        endpoint(identity)
        if not isinstance(entry,dict) or entry.get('url')!=endpoint(identity):raise ValueError('Unbound recheck entry')
        clock(entry['lastAttemptAt'],now)
        if entry.get('lastCheckedAt'):clock(entry['lastCheckedAt'],now)
        if entry.get('contentHash') is not None and not re.fullmatch('[0-9a-f]{64}',entry['contentHash']):raise ValueError('Invalid retained representation hash')
    for identity,entry in quarantine.items():
        if (not isinstance(entry,dict) or entry.get('id')!=identity or entry.get('url')!=endpoint(identity)
                or not isinstance(entry.get('reason'),str) or not entry['reason']
                or type(entry.get('attempts')) is not int or not 1<=entry['attempts']<=1000000
                or type(entry.get('bytes')) is not int or not 0<=entry['bytes']<=20_000_000
                or entry.get('httpStatus') is not None and (type(entry['httpStatus']) is not int or not 100<=entry['httpStatus']<=599)):
            raise ValueError('Invalid bound Caribe quarantine entry')
        for name in ('firstFailedAt','lastAttemptAt'):clock(entry[name],now)
        if not instant(entry['lastAttemptAt'])<=instant(entry['nextRetryAt'])<=instant(entry['lastAttemptAt'])+dt.timedelta(hours=24):raise ValueError('Invalid retry interval')
        if entry.get('responseHash') is not None and not re.fullmatch('[0-9a-f]{64}',entry['responseHash']):raise ValueError('Invalid quarantine response hash')
        if entry['bytes'] and entry.get('responseHash') is None:raise ValueError('Quarantine bytes lack complete hash')
    # Reviewer ownership/inactive retained identities no longer belong to this
    # automatic scheduler; original records are untouched.
    return ({i:v for i,v in checks.items() if i in owned},
            {i:v for i,v in quarantine.items() if i in owned})


def confirmed_missing(raw, receipt):
    if receipt['httpStatus']!=404:return False
    try:value=json.loads(raw)
    except (ValueError,TypeError):return False
    return (isinstance(value,dict) and value.get('code')=='rest_post_invalid_id'
        and isinstance(value.get('data'),dict) and type(value['data'].get('status')) is int
        and value['data']['status']==404 and isinstance(value.get('message'),str))


def inaccessible_record(old, receipt, now):
    """Inaccessible evidence is not an event resolution or publisher retraction."""
    metadata=old.get('sourceMetadata',{})
    if metadata.get('inaccessibleResponseHash')==receipt['sha256']:return None
    row=copy.deepcopy(old)
    row.update(evidenceStatus='unverified',state='expired',updatedAt=max(old['updatedAt'],now),
        summary='The publisher article is inaccessible; its earlier account requires review.',
        severity='unknown',location=dict(precision='country'))
    for key in ('admin1Id','admin2Id','adminAssignment'):row.pop(key,None)
    row['sourceMetadata'].update(reviewRequired=True,reviewReason='confirmed_source_post_inaccessible',
        inaccessibleResponseHash=receipt['sha256'],inaccessibleAt=now,
        inaccessibleMeaning='Source retrieval failure; not a resolution or retraction',
        inaccessibleReceipt=copy.deepcopy(receipt))
    original=old['reports'][0]
    report=dict(id=old['id']+'-inaccessible-'+receipt['sha256'][:12],incidentId=old['id'],sourceId=SOURCE_ID,
        title=old['title'],url=original['url'],language='es',publishedAt=original['publishedAt'],retrievedAt=now,
        excerpt='',contentHash=receipt['sha256'],independentGroup=original.get('independentGroup',SOURCE_ID),
        sourceMetadata=dict(representationFormat='caribe-wordpress-missing-post-v1',
            evidenceMeaning='Exact endpoint reports post inaccessible, not event resolution or retraction',
            retrievalReceipt=copy.deepcopy(receipt)))
    row['reports']=[report]+[r for r in row['reports'] if r['id']!=report['id']]
    return row


def collect_caribe_source(source,policy,raw,now,countries,previous,records,fetcher,fixtures=None,
                          history_from=None,reviews=()):
    """Return staged records/state only. No input is mutated or committed here.

    Complete latest100 plus at most3 old accepted exact post IDs. The bounded
    feed is not a one-year scan or a promise that all intervening edits were seen.
    """
    validate_source_policy(source,policy,now,countries)
    req=Requests(policy,fetcher,now,fixtures)
    req.accept(raw,LATEST_URL,dict(httpStatus=200))
    items=parse_posts(raw,now);raw_rows=json.loads(raw)
    if any(items[i]['modifiedAt']<items[i+1]['modifiedAt'] for i in range(len(items)-1)):
        raise ValueError('Complete latest-post ordering changed')
    owned,blocked,manual=owned_records(records,reviews)
    checks,quarantine=prior_state(previous,owned,now)
    current=instant(now);fresh={item['guid']:item for item in items}
    rows={str(row['id']):row for row in raw_rows if str(row['id']) not in blocked and row['link'] not in manual}
    for identity,item in fresh.items():
        if identity in blocked or item['url'] in manual:continue
        checks[identity]=dict(url=endpoint(identity),lastAttemptAt=now,lastCheckedAt=now,contentHash=item['contentHash'])
        quarantine.pop(identity,None)
    due=[i for i in owned if i not in fresh and (i not in quarantine or instant(quarantine[i]['nextRetryAt'])<=current)
        and (i not in checks or i in quarantine or instant(checks[i]['lastCheckedAt'])<=current-dt.timedelta(hours=RECHECK_HOURS))]
    due.sort(key=lambda i:(checks.get(i,{}).get('lastAttemptAt',''),int(i)))
    missing=[]
    for identity in due[:MAX_REQUESTS-1]:
        old=owned[identity];receipt=None
        try:
            body,receipt=req.get(identity)
            if confirmed_missing(body,receipt):
                changed=inaccessible_record(old,receipt,now)
                if changed:missing.append(changed)
                raise DetailUnavailable('Confirmed WordPress post inaccessible; prior account requires review')
            if receipt['httpStatus']!=200:raise DetailUnavailable('Exact post HTTP response requires retry')
            try:
                document=json.loads(body)
                if not isinstance(document,dict) or document.get('id')!=int(identity):raise ValueError('Wrong exact post identity')
                item=parse_posts(json.dumps([document],ensure_ascii=False).encode(),now)[0]
            except (ValueError,TypeError,KeyError) as error:
                raise DetailUnavailable('Complete exact post rejected: '+str(error)) from error
            rows[identity]=document;fresh[identity]=item
            checks[identity]=dict(url=endpoint(identity),lastAttemptAt=now,lastCheckedAt=now,contentHash=item['contentHash'])
            quarantine.pop(identity,None)
            continue
        except DetailUnavailable as error:
            receipt=receipt or req.receipts[-1]
            prior=quarantine.get(identity,{})
            attempts=min(prior.get('attempts',0)+1,1000000)
            quarantine[identity]=dict(id=identity,url=endpoint(identity),reason=str(error),
                firstFailedAt=prior.get('firstFailedAt',now),lastAttemptAt=now,attempts=attempts,
                nextRetryAt=(current+dt.timedelta(hours=min(24,2**min(attempts-1,5)))).isoformat(timespec='seconds').replace('+00:00','Z'),
                httpStatus=receipt['httpStatus'],bytes=receipt['bytes'],responseHash=receipt['sha256'])
            check=checks.setdefault(identity,dict(url=endpoint(identity),lastCheckedAt=old['firstSeenAt']))
            check['lastAttemptAt']=now
    # parse_posts deliberately caps one list at100; each response stays complete,
    # and exact-ID documents are separate validated representations, not a sliced
    # 103-row pseudo-response. Outputs are staged in a local copy until all pass.
    staged=dict(records);incoming={r['id']:r for r in missing};decisions=[]
    for identity,document in rows.items():
        result=collect_caribe(source,json.dumps([document],ensure_ascii=False).encode(),now,countries,
            existing=staged,reviews=reviews)
        for row in result['incidents']:
            if row['id'] not in records and history_from and instant(row['occurredAt'])<instant(history_from if 'T' in history_from else history_from+'T00:00:00Z'):
                continue
            staged[row['id']]=row;incoming[row['id']]=row
        decisions.extend(result['decisions'])
    current_owned,_,_=owned_records(staged,reviews)
    keep=set(current_owned)
    checks={i:v for i,v in checks.items() if i in keep}
    quarantine={i:v for i,v in quarantine.items() if i in keep}
    if len(quarantine)>MAX_QUARANTINE:raise ValueError('Caribe quarantine ceiling requires review')
    pending=[i for i in current_owned if i not in quarantine and
        (i not in checks or instant(checks[i]['lastCheckedAt'])<=current-dt.timedelta(hours=RECHECK_HOURS))]
    gaps=copy.deepcopy(previous.get('caribeCoverageGaps',[]))
    if not isinstance(gaps,list) or len(gaps)>128:raise ValueError('Caribe coverage-gap ledger requires review')
    prior_through=previous.get('caribeListingCheckedAt') or previous.get('lastSuccessAt')
    if prior_through:clock(prior_through,now)
    oldest=items[-1]['modifiedAt'] if items else None
    complete=bool(prior_through and len(items)<100 or prior_through and oldest and oldest<=prior_through)
    if len(items)==100 and (not prior_through or oldest>prior_through):
        gap=dict(reason='bounded_initial_latest100' if not prior_through else 'latest100_excludes_prior_interval',
            **{'from':prior_through,'through':now},oldestModifiedAt=oldest,listingContentHash=req.receipts[0]['sha256'])
        if not gaps or gaps[-1]['listingContentHash']!=gap['listingContentHash']:gaps.append(gap)
    if len(gaps)>128:raise ValueError('Caribe coverage-gap ledger ceiling requires review')
    state=dict(caribeVersion=STATE_VERSION,caribeRechecks=checks,caribeQuarantine=quarantine,
        caribeListingCheckedAt=now,caribeListingContentHash=req.receipts[0]['sha256'],caribeListingValidated=True,
        caribeValidatedRepresentations=len(fresh),caribeFailedRechecks=len(quarantine),caribePendingRechecks=len(pending),
        caribeOldestPendingCheckAt=min((checks.get(i,{}).get('lastCheckedAt',current_owned[i]['firstSeenAt']) for i in pending),default=None),
        caribeCurrentIntervalComplete=complete,caribeCoverageGaps=gaps,caribeAllYearBackfill=False)
    discoveries=[]
    for decision in decisions:
        item=fresh.get(decision.get('guid'))
        if item is None:continue  # Explicit review ownership has no new automatic decision.
        discoveries.append(dict(id='caribe-discovery-'+sha(decision['url'])[:20],sourceId=SOURCE_ID,
            url=decision['url'],title=item['title'],publishedAt=item['publishedAt'],retrievedAt=now,
            contentHash=item['contentHash'],language='es',sourceCountryCodes=['CR'],mentionedCountryCodes=[],
            categoryCandidates=['crime'] if decision.get('incidentId') else [],status=decision['status'],locationStatus='source-context-validated'
            if decision.get('incidentId') else 'unverified',reviewReason=decision.get('reason'),
            incidentId=decision.get('incidentId')))
    partial=bool(quarantine or pending)
    message=f'Caribe Actual: {len(items)} complete latest posts; {len(req.receipts)-1} exact rechecks; {len(incoming)} record changes.'
    if partial:message+=f' {len(quarantine)} failed and {len(pending)} pending rechecks; prior complete-source success retained.'
    return dict(incidents=list(incoming.values()),discoveries=discoveries,state=state,evidence=req.receipts,
        sourceStatus='unavailable' if partial else 'healthy',message=message)
