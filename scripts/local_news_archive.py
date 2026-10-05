#!/usr/bin/env python3
"""Complete public WordPress representations and a narrow official curfew adapter.

No keyword rule promotes ordinary news. Crime reports require explicit, hashed
review. Only JCF's dated, bounded 48-hour curfew template is auto-admitted.
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

UTC = dt.timezone.utc
JAMAICA = dt.timezone(dt.timedelta(hours=-5))
SOURCE_ID = 'gov-jm-jcf'
API = 'https://jcf.gov.jm/wp-json/wp/v2/posts'
MAX_BYTES = 20_000_000

class Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ('script','style'): self.hidden=max(0,self.hidden-1)
    def handle_data(self, value):
        if not self.hidden:self.parts.append(value)

def plain(value):
    parser=Text();parser.feed(value);parser.close()
    return re.sub(r'\s+',' ',html.unescape(' '.join(parser.parts))).strip()

def encode(value):return (json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n').encode()
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def stamp(value):return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00','Z')

def parse_wordpress(raw, source):
    """Parse the complete response, rejecting truncation, hidden content and redirects."""
    if len(raw)>MAX_BYTES:raise ValueError('Complete WordPress response exceeds20MB')
    document=json.loads(raw)
    if not isinstance(document,list):raise ValueError('WordPress response is not an article array')
    expected=urllib.parse.urlsplit(source['url']).hostname
    rows=[];ids=set()
    for post in document:
        if not isinstance(post,dict) or type(post.get('id')) is not int or post['id'] in ids:
            raise ValueError('Missing/duplicate WordPress article identity')
        ids.add(post['id'])
        link=urllib.parse.urlsplit(post.get('link',''))
        if link.scheme!='https' or link.hostname!=expected or link.username or link.password:
            raise ValueError('WordPress article source identity changed')
        if post.get('status')!='publish' or post.get('type')!='post':
            raise ValueError('WordPress article is not a public post')
        body=post.get('content',{})
        if body.get('protected') is not False or not isinstance(body.get('rendered'),str):
            raise ValueError('Article full content is protected or unavailable')
        if not isinstance(post.get('title',{}).get('rendered'),str):
            raise ValueError('Article title unavailable')
        published=post.get('date_gmt','');modified=post.get('modified_gmt','')
        for value in (published,modified):
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}',value):
                raise ValueError('WordPress GMT timestamp unavailable')
            dt.datetime.fromisoformat(value)
        text=plain(body['rendered'])
        if not text:raise ValueError('Article complete accepted text empty')
        rows.append(dict(title=plain(post['title']['rendered']),url=post['link'],
                         publishedAt=published+'Z',guid=str(post['id']),description=text,
                         modifiedAt=modified+'Z',representationFormat='wordpress-full-content-v1'))
    return rows

MONTHS={name:i for i,name in enumerate(('January','February','March','April','May','June','July','August','September','October','November','December'),1)}
WEEKDAYS={name:i for i,name in enumerate(('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'))}
DATE=r'(?P<hour>\d{1,2})\s*[:.]\s*(?P<minute>\d{2})\s*(?P<meridiem>[ap])\.?\s*m\.?\s*,?\s*(?:on\s+)?(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(?P<year>20\d{2}))?'

def curfew_time(match, published):
    values=match.groupdict();year=int(values['year']) if values['year'] else published.year
    candidates=[]
    for candidate_year in ([year] if values['year'] else [year-1,year,year+1]):
        hour=int(values['hour']);minute=int(values['minute'])
        if not 1<=hour<=12 or not 0<=minute<60:raise ValueError('Invalid curfew clock time')
        hour=hour%12+(12 if values['meridiem'].lower()=='p' else 0)
        try:value=dt.datetime(candidate_year,MONTHS[values['month'].title()],int(values['day']),hour,minute,tzinfo=JAMAICA)
        except ValueError:continue
        if value.weekday()==WEEKDAYS[values['weekday'].title()] and abs((value-published).total_seconds())<=90*86400:
            candidates.append(value)
    if len(candidates)!=1:raise ValueError('Curfew date/year/weekday is ambiguous or contradictory')
    return candidates[0]

def jcf_curfew_incidents(source,items,now,countries,areas):
    if source['id']!=SOURCE_ID or source.get('url')!='https://jcf.gov.jm/' or 'JM' not in countries:return []
    rows=[]
    for item in items:
        if not re.search(r'\b48\s*[- ]\s*hour curfews?\b',item['title'],re.I):continue
        text=item['description']
        if re.search(r'\b(?:cancelled|canceled|revoked|lifted|rescinded|correction|withdrawn)\b',item['title']+' '+text,re.I):continue
        # This source-specific declaration is materially different from a keyword
        # headline. Require the enacted restriction, both dates, and all four edges.
        starts=list(re.finditer(r'\b(?:curfews?\s+(?:began|will began|will begin|will commence|commenced|takes effect|continues|will continue)|beginning)\s+(?:at|from)\s+'+DATE,text,re.I))
        ends=list(re.finditer(r'\b(?:remain in effect until|end at|ends at)\s+'+DATE,text,re.I))
        if len(starts)!=1 or len(ends)!=1 or not all(re.search(r'\b'+side+r'\s*[:–-]',text,re.I) for side in ('North','East','South','West')):continue
        published=dt.datetime.fromisoformat(item['publishedAt'].replace('Z','+00:00'))
        try:start,end=curfew_time(starts[0],published),curfew_time(ends[0],published)
        except ValueError:continue
        if (end-start).total_seconds()!=48*3600:continue
        current=dt.datetime.fromisoformat(now.replace('Z','+00:00'))
        if start<current-dt.timedelta(days=366) or start>current+dt.timedelta(days=7):continue
        iid='jcf-curfew-'+item['guid']
        summary=(f'Jamaica Constabulary Force announced a 48-hour curfew from {start:%d %B %Y %H:%M} '
                 f'to {end:%d %B %Y %H:%M}, Jamaica time, for the limited areas described in its notice. '
                 'The linked notice supplies verbal boundaries; no digital impact polygon or parish-wide restriction is inferred.')
        expired=end<=dt.datetime.fromisoformat(now.replace('Z','+00:00'))
        rows.append(dict(id=iid,title=item['title'],summary=summary,countryCode='JM',region=item['title'],
            category='unrest',severity='unknown',evidenceStatus='official',state='expired' if expired else 'reported',
            occurredAt=item['publishedAt'] if start>current else stamp(start),updatedAt=item['modifiedAt'],firstSeenAt=now,
            location=dict(precision='area'),sourceIds=[SOURCE_ID],historical=start<dt.datetime.fromisoformat(now.replace('Z','+00:00'))-dt.timedelta(days=30),aiStatus='not-requested',
            sourceMetadata=dict(recordId=item['guid'],eventTimeBasis='report-publication' if start>current else 'official-notice-start',eventDatePrecision='minute',
                providerStart=stamp(start),providerEnd=stamp(end),reportedLocalTimeZone='America/Jamaica',timeZoneBasis='Notice local clock in Jamaica; UTC-05:00',
                geographicPrecisionNote='Official policing division/community boundary text; no exact coordinates or administrative assignment inferred.',
                parser='jcf-48h-curfew-v1',acceptedTextSha256=hashlib.sha256(text.encode()).hexdigest(),
                expirationBasis='Official notice end time; does not establish resolution of underlying security conditions'),
            reports=[dict(id=iid+'-report',incidentId=iid,sourceId=SOURCE_ID,title=item['title'],url=item['url'],language='en',
                publishedAt=item['publishedAt'],retrievedAt=now,excerpt='',contentHash=digest(item),independentGroup=SOURCE_ID,
                sourceMetadata=dict(representationFormat=item['representationFormat'],completeAcceptedTextSha256=hashlib.sha256(text.encode()).hexdigest()))]))
    return rows

def expire_curfews(records, now):
    """Apply the stated end time even when this run got304 or the source failed."""
    current=dt.datetime.fromisoformat(now.replace('Z','+00:00')) if isinstance(now,str) else now
    changed=[]
    for row in records.values() if isinstance(records,dict) else records:
        meta=row.get('sourceMetadata',{})
        if meta.get('parser')!='jcf-48h-curfew-v1' or row.get('state') not in ('reported','ongoing'):continue
        try:end=dt.datetime.fromisoformat(meta['providerEnd'].replace('Z','+00:00'))
        except (KeyError,ValueError,TypeError):continue
        if end<=current:
            row['state']='expired';changed.append(row['id'])
    return changed

class SameHost(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        old,new=urllib.parse.urlsplit(request.full_url),urllib.parse.urlsplit(newurl)
        if new.scheme!='https' or new.hostname!=old.hostname or new.username or new.password:
            raise ValueError('Archive redirect changed source identity')
        return super().redirect_request(request,fp,code,msg,headers,newurl)

def collect_wordpress(source,after,before,output,max_pages=100):
    """Persist each complete representation privately; all-or-fail pagination metadata."""
    output.mkdir(parents=True,exist_ok=True);rows=[];evidence=[];total=None;pages=None
    opener=urllib.request.build_opener(SameHost())
    for page in range(1,max_pages+1):
        query=urllib.parse.urlencode(dict(after=after,before=before,per_page=100,page=page,orderby='date',order='desc'))
        url=source['archiveApi']+'?'+query
        with opener.open(urllib.request.Request(url,headers={'User-Agent':'PublicPolicyHazardMonitor/1.0 (public source archive)'}),timeout=45) as response:
            raw=response.read();headers=dict(response.headers)
            declared_total=int(response.headers['X-WP-Total']);declared_pages=int(response.headers['X-WP-TotalPages'])
        if len(raw)>MAX_BYTES:raise ValueError('Complete response too large; not parsed')
        if total is None:total,pages=declared_total,declared_pages
        if (total,pages)!=(declared_total,declared_pages):raise ValueError('Archive changed during pagination; retain saved pages and retry complete crawl')
        name=f'{source["id"]}-{page:03d}.response';(output/name).write_bytes(raw)
        batch=parse_wordpress(raw,source);rows.extend(batch)
        evidence.append(dict(url=url,file=name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),count=len(batch),retrievedAt=stamp(dt.datetime.now(UTC))))
        if page>=pages:break
    if pages is None or pages>max_pages or len(rows)!=total or len({r['guid'] for r in rows})!=total:
        raise ValueError('Incomplete archive pagination; no completeness claim permitted')
    result=dict(sourceId=source['id'],after=after,before=before,complete=True,articleCount=len(rows),pages=evidence)
    (output/'archive-audit.json').write_bytes(encode(result));(output/'accepted-items.json').write_bytes(encode(rows))
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--after',default='2025-10-05T00:00:00');parser.add_argument('--before',default='2026-10-05T23:59:59')
    args=parser.parse_args();print(json.dumps(collect_wordpress(json.loads(args.source.read_bytes()),args.after,args.before,args.output)))
