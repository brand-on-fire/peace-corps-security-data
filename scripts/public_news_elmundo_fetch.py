"""Bounded inactive public-HTML intake for Diario El Mundo; injected free fetches.

All requested listing/detail/correction representations validate before returning
state. Parent commits records and returned state together after its size/geo gates.
No social platform requests; quoted police links are provenance only.
"""
import copy
import datetime as dt
import hashlib
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from public_news_elmundo import SOURCE_ID,PARSER,HOST,MAX_BYTES,ZONE,MONTHS,canonical,collect_elmundo,norm,parse_article
from public_news_runtime import instant

ADAPTERS={'sv-elmundo'}
LISTING=f'https://{HOST}/nacionales'
PAGE_SIZE=12
MAX_PAGES=3
NEW_LIMIT=10
RECHECK_LIMIT=6
MAX_REQUESTS=MAX_PAGES+NEW_LIMIT+RECHECK_LIMIT
MAX_BATCH_BYTES=12_000_000
MAX_PENDING=240
MAX_CHECKED=2000
MAX_QUARANTINE=240
MAX_ARCHIVE_PAGES=2000
ARCHIVE_RESERVED=2
WEEKDAYS={name:i for i,name in enumerate(('lunes','martes','miercoles','jueves','viernes','sabado','domingo'))}


def endpoint(page=1):return LISTING if page==1 else LISTING+f'?page={page}'


def prepare_elmundo_policy(policy,previous,now):
    return dict(policy,url=LISTING) if policy.get('adapter')=='sv-elmundo' else dict(policy)


def request_state(policy,previous):
    # An unchanged listing cannot skip article/correction work.
    return {k:v for k,v in previous.items() if k not in ('etag','lastModified')} if policy.get('adapter')=='sv-elmundo' else dict(previous)


class Listing(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.current=None;self.rows=[];self.stack=[];self.target=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='article':
            if self.current is not None:raise ValueError('Nested listing article')
            self.current={'url':None,'title':[],'date':[]}
        classes=a.get('class','').split()
        inherited=self.stack[-1][1] if self.stack else None
        field='title' if 'article-title' in classes else 'date' if 'article-date' in classes else inherited
        if self.current is not None and tag=='a' and field=='title':
            if self.current['url'] is not None:raise ValueError('Multiple article identities')
            self.current['url']=a.get('href')
        if tag not in ('img','source','br','meta','link','input','hr'):self.stack.append((tag,field))
    def handle_endtag(self,tag):
        if tag=='article' and self.current is not None:self.rows.append(self.current);self.current=None
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:self.stack=self.stack[:i];break
    def handle_data(self,value):
        if self.current is not None and self.stack and self.stack[-1][1] in ('title','date'):
            self.current[self.stack[-1][1]].append(value)


def listing_date(text):
    text=norm(' '.join(text.split())).replace('.','')
    pattern=r'('+ '|'.join(WEEKDAYS)+r') (\d{1,2}), ('+'|'.join(MONTHS)+r') (20\d{2}) - (\d{1,2}):(\d{2}) ([ap])\s*m'
    m=re.fullmatch(pattern,text)
    if not m:raise ValueError('Listing date format changed')
    day=dt.date(int(m[4]),MONTHS[m[3]],int(m[2]));hour=int(m[5]);minute=int(m[6])
    if day.weekday()!=WEEKDAYS[m[1]] or not 1<=hour<=12 or not 0<=minute<=59:raise ValueError('Listing calendar/clock mismatch')
    hour=hour%12+(12 if m[7]=='p' else 0)
    return dt.datetime.combine(day,dt.time(hour,minute),ZONE).astimezone(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')


def parse_listing(raw,now):
    if not isinstance(raw,bytes) or not raw or len(raw)>MAX_BYTES:raise ValueError('Complete listing missing or oversized')
    p=Listing();p.feed(raw.decode('utf-8',errors='strict'));p.close()
    if p.current is not None or not 1<=len(p.rows)<=PAGE_SIZE:raise ValueError('Listing count/structure changed')
    result=[]
    for row in p.rows:
        url=canonical(row['url']);title=' '.join(''.join(row['title']).split());date=listing_date(''.join(row['date']))
        if not title or instant(date)>instant(now)+dt.timedelta(minutes=5):raise ValueError('Listing title or future clock invalid')
        result.append(dict(url=url,title=title,publishedAt=date))
    if len({r['url'] for r in result})!=len(result):raise ValueError('Duplicate listing article')
    if any(result[i]['publishedAt']<result[i+1]['publishedAt'] for i in range(len(result)-1)):raise ValueError('Listing not newest first')
    return result


class DetailUnavailable(ValueError):
    """An individual complete detail could not be verified; keep old evidence."""


class ArchivePageUnavailable(ValueError):
    """Historical page error, independent of the validated live listing."""


class Requests:
    def __init__(self,policy,fetcher,now,fixtures):
        self.policy=policy;self.fetcher=fetcher;self.now=now;self.fixtures=fixtures;self.bytes=0;self.receipts=[]
    def accept(self,raw,url,result):
        size=len(raw) if isinstance(raw,bytes) else 0
        self.bytes+=size
        if self.bytes>MAX_BATCH_BYTES or len(self.receipts)>=MAX_REQUESTS:
            raise ValueError('El Mundo complete request/byte budget exceeded')
        status=result.get('httpStatus',200 if raw is not None else None)
        receipt=dict(url=url,retrievedAt=self.now,httpStatus=status,bytes=size,
            sha256=hashlib.sha256(raw).hexdigest() if isinstance(raw,bytes) else None)
        self.receipts.append(receipt)
        if not isinstance(raw,bytes):raise DetailUnavailable('Complete detail unavailable; '+result.get('message','HTTP '+str(status)))
        if status!=200:raise DetailUnavailable('Detail response not HTTP200: '+str(status))
        if result.get('finalUrl',url)!=url:raise DetailUnavailable('Redirected detail requires review')
        if size>MAX_BYTES:raise DetailUnavailable('Complete individual detail exceeds 2MB; held without parsing')
        return raw
    def get(self,url):
        match=re.fullmatch(re.escape(LISTING)+r'\?page=([1-9]\d*)',url)
        if match:
            if int(match[1])>MAX_ARCHIVE_PAGES:raise ValueError('El Mundo archive page ceiling exceeded')
        elif url!=LISTING:canonical(url)
        result,raw=self.fetcher(dict(self.policy,url=url),{},self.now,self.fixtures)
        return self.accept(raw,url,result)


def archive_progress(policy,previous,listed,pages,requests,now,checked):
    """One observed publisher-listing chain, never a claim of all crime coverage.

    A URL plus its publication date anchors each continuation. Mutable numeric
    page offsets alone cannot advance it. Missing anchors leave an explicit gap;
    live collection and accepted-article correction checks can still proceed.
    """
    days=policy.get('archiveBackfillDays')
    if days is None:return None
    if type(days) is not int or days!=365:raise ValueError('Only the reviewed 365-day archive bound is supported')
    state=copy.deepcopy(previous.get('elmundoArchive'))
    if state is None:
        lower=(instant(now)-dt.timedelta(days=days)).isoformat(timespec='seconds').replace('+00:00','Z')
        state=dict(version=1,from_=lower,to=now,startedAt=now,status='scanning',anchor=None,
            page=1,pending=[],pages=[],checkedArticles=0,gaps=[],reachedLowerBound=False,
            listingScanComplete=False,allYearBackfill=False)
        state['from']=state.pop('from_')
        candidates=listed
        page=max(pages)
        for number,batch in pages.items():
            receipt=next(r for r in requests.receipts if r['url']==endpoint(number))
            state['pages'].append(dict(page=number,sha256=receipt['sha256'],retrievedAt=now,
                first=batch[0],last=batch[-1],count=len(batch)))
    else:
        if state.get('version')!=1 or state.get('to')>now:raise ValueError('Invalid El Mundo archive checkpoint')
        if state.get('retryAt') and instant(state['retryAt'])>instant(now):return state
        if state['status']=='blocked' or state['reachedLowerBound'] or any(r['url'] not in previous.get('elmundoQuarantine',{}) for r in state['pending']):return state
        if len(state['pending'])>MAX_PENDING-PAGE_SIZE:return state
        anchor=state['anchor'];page=state['page'];candidates=[]
        while len(requests.receipts)<MAX_PAGES:
            batch=pages.get(page)
            if batch is None:
                batch=archive_page(requests,page,now);pages[page]=batch
            positions=[i for i,row in enumerate(batch) if row['url']==anchor['url']]
            if positions:
                position=positions[0]
                if batch[position]['publishedAt']!=anchor['publishedAt']:
                    reason='archive_anchor_publication_changed';break
                candidates=batch[position+1:]
                if not candidates:
                    if len(batch)<PAGE_SIZE:
                        reason='publisher_listing_ended_before_requested_year';break
                    page+=1
                    if page>MAX_ARCHIVE_PAGES:
                        reason='archive_page_ceiling_before_requested_year';break
                    if page not in pages and len(requests.receipts)>=MAX_PAGES:return state
                    following=pages.get(page)
                    if following is None:following=archive_page(requests,page,now);pages[page]=following
                    if following[0]['publishedAt']>anchor['publishedAt'] or {r['url'] for r in following}.intersection(r['url'] for r in batch):
                        reason='publisher_listing_shifted_across_anchor';break
                    candidates=following;batch=following
                receipt=next(r for r in requests.receipts if r['url']==endpoint(page))
                if len(state['pages'])>=MAX_ARCHIVE_PAGES:
                    reason='archive_page_ledger_ceiling';break
                state['pages'].append(dict(page=page,sha256=receipt['sha256'],retrievedAt=now,
                    after=anchor,first=candidates[0],last=candidates[-1],count=len(candidates)))
                state['status']='scanning'
                state.pop('retryAt',None)
                for failure in state.get('pageFailures',[]):
                    if 'resolvedAt' not in failure and failure['response']['url'] in {endpoint(p) for p in pages}:failure['resolvedAt']=now
                for gap in state['gaps']:
                    if 'resolvedAt' not in gap:gap['resolvedAt']=now
                break
            # Offset drift can be recovered only by finding the exact URL/date.
            if anchor['publishedAt']>batch[0]['publishedAt'] and page>1:page-=1
            elif anchor['publishedAt']<batch[-1]['publishedAt'] and page<MAX_ARCHIVE_PAGES:page+=1
            else:
                reason='archive_anchor_missing_or_ambiguous';break
            state['page']=page;state['status']='seeking-anchor'
            if not state['gaps'] or 'resolvedAt' in state['gaps'][-1]:
                state['gaps'].append(dict(reason='archive_anchor_offset_changed',anchor=anchor,detectedAt=now))
            if len(state['gaps'])>64:raise ValueError('El Mundo archive gap ceiling requires review')
        else:return state
        if not candidates:
            state['status']='blocked';state['gaps'].append(dict(reason=reason,anchor=anchor,detectedAt=now,page=page));return state
    pending={r['url']:r for r in state['pending']}
    for row in candidates:
        if row['publishedAt']>=state['from'] and row['url'] not in checked:pending[row['url']]=row
    state['pending']=list(pending.values())
    state['reachedLowerBound']=candidates[-1]['publishedAt']<state['from']
    state['anchor']=dict(url=candidates[-1]['url'],publishedAt=candidates[-1]['publishedAt'])
    state['page']=page
    return state


def archive_page(requests,page,now):
    try:raw=requests.get(endpoint(page))
    except DetailUnavailable as error:raise ArchivePageUnavailable(str(error)) from error
    # Aggregate request/byte limit ValueErrors deliberately remain fatal.
    try:return parse_listing(raw,now)
    except (ValueError,UnicodeError) as error:raise ArchivePageUnavailable('Complete historical listing rejected: '+str(error)) from error


def archive_failure(previous,requests,now,error):
    state=copy.deepcopy(previous['elmundoArchive'])
    failures=state.setdefault('pageFailures',[]);receipt=requests.receipts[-1]
    same=next((row for row in failures if row['response']['url']==receipt['url'] and
        row['response']['sha256']==receipt['sha256'] and row['reason']==str(error)),None)
    if same:same.update(lastAttemptAt=now,attempts=same['attempts']+1,response=receipt);same.pop('resolvedAt',None)
    else:failures.append(dict(firstFailedAt=now,lastAttemptAt=now,attempts=1,reason=str(error),response=receipt))
    if len(failures)>64:raise ValueError('Historical page failure ledger requires review')
    state.update(status='retrying-page',listingScanComplete=False,
        retryAt=(instant(now)+dt.timedelta(hours=1)).isoformat(timespec='seconds').replace('+00:00','Z'))
    return state


def collect_elmundo_source(source,policy,raw,now,countries,previous,records,fetcher,fixtures=None,history_from=None,reviews=(),areas=(),boundary_layers=None):
    if (policy.get('adapter')!='sv-elmundo' or policy.get('sourceId')!=SOURCE_ID or policy.get('url')!=LISTING or
        source.get('id')!=SOURCE_ID):raise ValueError('Unreviewed El Mundo source/policy')
    current=instant(now)
    if countries.get('SV',{}).get('status')!='active':
        raise ValueError('El Salvador is outside the reviewed active-country scope; no articles collected')
    requests=Requests(policy,fetcher,now,fixtures)
    requests.accept(raw,LISTING,{'httpStatus':200})
    recent=set(previous.get('elmundoRecentUrls',[]));listed=[];overlap=False;prior=None;pages={}
    for page in range(1,MAX_PAGES+1):
        batch=parse_listing(raw if page==1 else requests.get(endpoint(page)),now)
        pages[page]=batch
        if prior and batch[0]['publishedAt']>prior:raise ValueError('Listing page order changed during collection')
        prior=batch[-1]['publishedAt']
        if {r['url'] for r in listed}.intersection(r['url'] for r in batch):raise ValueError('Listing shifted/duplicated between pages')
        listed.extend(batch)
        overlap=bool(recent.intersection(r['url'] for r in batch))
        if overlap or len(batch)<PAGE_SIZE:break
    gaps=copy.deepcopy(previous.get('elmundoCoverageGaps',[]))
    if recent and not overlap and len(listed)==PAGE_SIZE*MAX_PAGES:
        gap=dict(from_=previous.get('lastSuccessAt'),to=now,oldestVisiblePublication=listed[-1]['publishedAt'],reason='bounded_listing_window_no_prior_overlap')
        gap['from']=gap.pop('from_')
        if gap not in gaps:gaps.append(gap)
    if len(gaps)>64:raise ValueError('El Mundo unresolved coverage-gap ledger requires review')
    checked=dict(previous.get('elmundoChecked',{}))
    quarantine=copy.deepcopy(previous.get('elmundoQuarantine',{}))
    try:archive=archive_progress(policy,previous,listed,dict(pages),requests,now,checked)
    except ArchivePageUnavailable as error:archive=archive_failure(previous,requests,now,error)
    retry_due=lambda url:url not in quarantine or instant(quarantine[url]['nextRetryAt'])<=current
    pending={r['url']:r for r in previous.get('elmundoPending',[])}
    for item in listed:
        if item['url'] not in checked and instant(item['publishedAt'])>=current-dt.timedelta(days=366):pending[item['url']]=item
    if len(pending)>MAX_PENDING:raise ValueError('El Mundo pending discovery ceiling exceeded')
    live=sorted((row for row in pending.values() if retry_due(row['url'])),key=lambda x:(x['publishedAt'],x['url']),reverse=True)
    older=sorted((row for row in archive['pending'] if row['url'] not in pending and retry_due(row['url'])),key=lambda x:(x['publishedAt'],x['url']),reverse=True) if archive else []
    # Reserve two archive slots only when older queued work exists. Eight live
    # article slots and all six correction slots remain available under load.
    selected=live[:NEW_LIMIT-(ARCHIVE_RESERVED if older else 0)]
    live_count=len(selected)
    selected+=older[:NEW_LIMIT-len(selected)]
    selected+=live[live_count:live_count+NEW_LIMIT-len(selected)]
    accepted_urls={q['url'] for r in records.values() if r.get('sourceMetadata',{}).get('parser')==PARSER
        for q in r.get('reports',[]) if q.get('sourceId')==SOURCE_ID}
    # Accepted articles have independent rotating rechecks after they leave the
    # current listing. Failed details retain evidence in an explicit quarantine;
    # they cannot indefinitely block all other newly published articles.
    due=[url for url in accepted_urls if url not in {x['url'] for x in selected}
         and retry_due(url) and (url in quarantine or url not in checked or instant(checked[url]['checkedAt'])<=current-dt.timedelta(hours=6))]
    due.sort(key=lambda url:(checked.get(url,{}).get('checkedAt',''),url))
    # Remaining slots revisit recent unsupported pages too; a previously held
    # article can acquire a complete factual police account after publication.
    held=[url for url,v in checked.items() if url not in accepted_urls and url not in {x['url'] for x in selected}
          and retry_due(url) and (url in quarantine or instant(v['publishedAt'])>=current-dt.timedelta(days=14))
          and (url in quarantine or instant(v['checkedAt'])<=current-dt.timedelta(hours=6))]
    held.sort(key=lambda url:(checked[url]['checkedAt'],url))
    urls=[x['url'] for x in selected]+(due+held)[:RECHECK_LIMIT]
    documents=[]
    for url in urls:
        try:
            body=requests.get(url)
        except DetailUnavailable as error:
            failure=str(error)
        else:
            try:parse_article(body,url,now)
            except (ValueError,KeyError,TypeError) as error:failure='Complete detail validation failed: '+str(error)
            else:
                documents.append(dict(url=url,raw=body));quarantine.pop(url,None);continue
        prior_failure=quarantine.get(url,{})
        attempts=prior_failure.get('attempts',0)+1
        quarantine[url]=dict(firstFailedAt=prior_failure.get('firstFailedAt',now),lastAttemptAt=now,
            attempts=attempts,nextRetryAt=(current+dt.timedelta(hours=min(24,2**min(attempts-1,5)))).isoformat(timespec='seconds').replace('+00:00','Z'),
            lastSuccessAt=checked.get(url,{}).get('checkedAt'),reason=failure,
            sourceResponse=requests.receipts[-1])
    if len(quarantine)>MAX_QUARANTINE:raise ValueError('El Mundo quarantined detail ceiling requires review')
    result=collect_elmundo(source,documents,now,countries,existing=records,reviews=reviews,areas=areas,boundary_layers=boundary_layers)
    incoming=result['incidents']
    if history_from:
        lower=instant(history_from+'T00:00:00Z' if len(history_from)==10 else history_from)
        outside={r['id'] for r in incoming if r['id'] not in records and instant(r['occurredAt'])<lower}
        incoming=[r for r in incoming if r['id'] not in outside]
        for decision in result['decisions']:
            if decision.get('incidentId') in outside:
                decision.pop('incidentId',None)
                decision.update(status='needs-review',reason='outside-configured-history-window')
    # Only now are all requested complete representations known to validate.
    # Integration must still defer this state until canonical publication gates.
    decisions={r['url']:r for r in result['decisions']}
    published={r['url']:r['publishedAt'] for r in listed}
    for document in documents:
        url=document['url'];decision=decisions[url]
        published[url]=decision['publishedAt']
        checked[url]=dict(checkedAt=now,publishedAt=published[url],contentHash=decision['contentHash'])
        pending.pop(url,None)
    if archive:
        completed={d['url'] for d in documents}
        archive['checkedArticles']+=sum(r['url'] in completed for r in archive['pending'])
        archive['pending']=[r for r in archive['pending'] if r['url'] not in completed]
        archive['listingScanComplete']=bool(archive['reachedLowerBound'] and not archive['pending'] and not pending and
            not any('resolvedAt' not in gap for gap in archive['gaps']) and not gaps and
            not any('resolvedAt' not in failure for failure in archive.get('pageFailures',[])))
        if archive['listingScanComplete']:archive['status']='observed-listing-scan-complete'
    # Keep accepted history checkpoints. Expiring unsupported bookkeeping never
    # deletes reports/history; those URLs remain discoverable if they reappear.
    accepted_urls.update(q['url'] for r in incoming for q in r['reports'] if q['sourceId']==SOURCE_ID)
    checked={url:v for url,v in checked.items() if url in accepted_urls or url in quarantine or instant(v['publishedAt'])>=current-dt.timedelta(days=14)}
    if len(checked)>MAX_CHECKED:raise ValueError('El Mundo correction checkpoint ceiling exceeded')
    discoveries=[dict(id='discovery-'+hashlib.sha256((SOURCE_ID+'|'+d['url']).encode()).hexdigest()[:20],
        sourceId=SOURCE_ID,url=d['url'],title=d['title'],
        publishedAt=published[d['url']],retrievedAt=now,firstSeenAt=now,language='es',sourceCountryCodes=['SV'],
        mentionedCountryCodes=['SV'] if d.get('incidentId') else [],categoryCandidates=['crime'] if d.get('incidentId') else [],
        locationStatus='source-context-validated' if d.get('incidentId') else 'unverified',
        contentHash=d['contentHash'],status=d['status'],note=d['reason'],
        **({'incidentId':d['incidentId']} if d.get('incidentId') else {})) for d in result['decisions']]
    state=dict(elmundoParser=PARSER,elmundoChecked=checked,elmundoPending=sorted(pending.values(),key=lambda x:(x['publishedAt'],x['url']),reverse=True),
        elmundoRecentUrls=[x['url'] for x in listed],elmundoCoverageGaps=gaps,elmundoQuarantine=quarantine,
        elmundoWindow=dict(firstCheck=previous.get('elmundoWindow',{}).get('firstCheck',now),newestPublication=listed[0]['publishedAt'],
            oldestVisiblePublication=listed[-1]['publishedAt'],pages=page,priorListingOverlap=overlap,
            initialBoundedWindow=not bool(recent),allYearBackfill=False),
        elmundoCorrectionScope='Six-hour rotating complete-article rechecks; failed details retry after 1–24 hours',
        lastPartialSuccessAt=now)
    if archive:state['elmundoArchive']=archive
    # A historical scan in progress is distinct from failed requests or live
    # backlog. Its own completion/gap fields never imply complete news coverage.
    partial=bool(quarantine or pending)
    message=f'{len(documents)} complete articles checked; {len(incoming)} source-supported crime record updates.'
    if partial:message+=f' Incomplete checks: {len(quarantine)} failed article URLs; {len(pending)} queued articles. Prior complete-source success time retained.'
    if gaps:message+=f' {len(gaps)} unresolved listing interval gaps.'
    if archive:message+=f" Archive: {archive['status']}; {len(archive['pending'])} queued; fixed lower publication bound {archive['from']}."
    return dict(incidents=incoming,discoveries=discoveries,state=state,evidence=requests.receipts,
        sourceStatus='unavailable' if partial else 'healthy',message=message)
