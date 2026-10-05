#!/usr/bin/env python3
"""Bounded free official collection adapters; no network except injected fetcher.

Every response is accepted whole or the source fails atomically. This module does
not write files, mutate the archive, enable a source, or call a model/service SDK.
"""
import copy,datetime as dt,hashlib,json,math,re,urllib.parse
UTC=dt.timezone.utc
WHO='https://www.who.int/api/news/diseaseoutbreaknews'
GDACS='https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH'
USGS_WEEK='https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_week.geojson'
CATALOG='https://earthquake.usgs.gov/fdsnws/event/1/'
MAX_PAGES=30;MAX_REQUESTS=40;MAX_TOTAL_BYTES=60_000_000
ADAPTERS={'who-don','gdacs-api','usgs-week'}
def iso(v):
 if isinstance(v,str):v=dt.datetime.fromisoformat(v.replace('Z','+00:00'))
 if not v.tzinfo:raise ValueError('Official timestamp needs explicit UTC/timezone')
 return v.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00','Z')
def moment(v):return dt.datetime.fromisoformat(v.replace('Z','+00:00'))
def provider_time(v):
 # GDACS API dates without a suffix are documented UTC, not machine local time.
 return iso(v+'Z' if isinstance(v,str) and not re.search(r'(Z|[+-]\d\d:\d\d)$',v) else v)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def digest(obj):return sha(json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
def url(base,params):return base+'?'+urllib.parse.urlencode(params)
def prepare_official_policy(policy,previous,now):
 """Call before the initial fetch. Clear conditional headers if its URL changed."""
 p=dict(policy);now=moment(now) if isinstance(now,str) else now;adapter=p.get('adapter')
 if adapter not in ADAPTERS:return p
 last=moment(previous['lastSuccessAt']) if previous.get('lastSuccessAt') else None
 if adapter=='who-don':
  since=max((last-dt.timedelta(days=2)) if last else now-dt.timedelta(days=30),now-dt.timedelta(days=366))
  p['url']=url(WHO,{'$filter':f'LastModified ge {iso(since)} and LastModified le {iso(now)}','$orderby':'LastModified asc,Id asc','$count':'true','$expand':'EmergencyEvent'})
 elif adapter=='gdacs-api':
  since=min(now-dt.timedelta(days=14),(last-dt.timedelta(days=2)) if last else now)
  since=max(since,now-dt.timedelta(days=60))
  p['url']=url(GDACS,{'eventlist':'TC;FL;VO;DR;WF','fromdate':since.date().isoformat(),'todate':now.date().isoformat(),'alertlevel':'red;orange;green','pagenumber':1,'pagesize':100})
 else:p['url']=USGS_WEEK
 return p

def request_state(policy,previous):
 return previous if previous.get('url')==policy['url'] else {k:v for k,v in previous.items() if k not in ('etag','lastModified')}

class Requests:
 def __init__(self,policy,fetcher,now,fixtures):self.policy=policy;self.fetcher=fetcher;self.now=now;self.fixtures=fixtures;self.receipts=[];self.total=0
 def accept(self,raw,address):
  if not isinstance(raw,bytes):raise ValueError('Complete raw response missing')
  self.total+=len(raw)
  if len(raw)>20_000_000 or self.total>MAX_TOTAL_BYTES:raise ValueError('Official response budget exceeded; no partial source admission')
  self.receipts.append({'url':address,'retrievedAt':self.now,'bytes':len(raw),'sha256':sha(raw)})
  return json.loads(raw)
 def get(self,address,suffix):
  if len(self.receipts)>=MAX_REQUESTS:raise ValueError('Official request ceiling reached; no partial source admission')
  original=urllib.parse.urlsplit(self.policy['url']);parsed=urllib.parse.urlsplit(address)
  if parsed.scheme!='https' or parsed.hostname!=original.hostname or parsed.username or parsed.password:raise ValueError('Official pagination left reviewed anonymous HTTPS host')
  p={**self.policy,'sourceId':self.policy['sourceId']+'-'+suffix,'url':address}
  state,raw=self.fetcher(p,{},self.now,self.fixtures)
  if raw is None:raise ValueError('Official child response unavailable: '+state.get('message','missing full body'))
  if state.get('httpStatus')==204:
   if raw:raise ValueError('Unexpected nonempty HTTP204 body')
   self.receipts.append({'url':address,'retrievedAt':self.now,'bytes':0,'sha256':sha(raw),'httpStatus':204})
   return {'type':'FeatureCollection','features':[],'_empty204':True}
  return self.accept(raw,address)

def base(source,iid,code,title,summary,when,updated,now,category,location,metadata):
 return dict(id=iid,title=title,summary=summary,countryCode=code,region=source['_countries'][code]['region'],category=category,severity='unknown',evidenceStatus='official',state='reported',occurredAt=when,updatedAt=updated,firstSeenAt=now,location=location,sourceIds=[source['id']],reports=[],historical=moment(when)<moment(now)-dt.timedelta(days=30),aiStatus='not-requested',sourceMetadata=metadata)
def evidence(source,row,title,address,published,now,summary,obj,receipt,external):
 p=urllib.parse.urlsplit(address)
 if p.scheme!='https' or p.username or p.password:raise ValueError('Official citation must be anonymous HTTPS')
 return dict(id=row['id']+'-report-'+digest([external,digest(obj)])[:16],sourceId=source['id'],incidentId=row['id'],title=title,url=address,language='en',publishedAt=published,retrievedAt=now,excerpt=summary,contentHash=digest(obj),independentGroup=source['id'],sourceMetadata=dict(externalId=external,rawResponseSha256=receipt['sha256'],rawResponseUrl=receipt['url']))

def gdacs_rows(source,features,now,receipt):
 result=[];seen=set()
 for f in features:
  if f.get('type')!='Feature' or not isinstance(f.get('properties'),dict):raise ValueError('GDACS invalid feature')
  p=f['properties'];typ=p.get('eventtype');eid=str(p.get('eventid',''));episode=str(p.get('episodeid',''))
  if typ not in {'TC','FL','VO','DR','WF'} or not eid.isdigit() or not episode.isdigit():raise ValueError('GDACS invalid identity/type')
  key=(typ,eid);start=provider_time(p['fromdate']);end=provider_time(p['todate']);updated=provider_time(p['datemodified'])
  if end<start:raise ValueError('GDACS end precedes start')
  if start>now:continue
  if not isinstance(p.get('affectedcountries'),list):raise ValueError('GDACS affected-country evidence missing')
  codes=sorted({x.get('iso2') for x in p['affectedcountries'] if x.get('iso2') in source['_countries']})
  kind={'FL':'flood','TC':'tropical cyclone','WF':'wildfire','DR':'drought','VO':'volcanic activity'}[typ]
  address=p.get('url',{}).get('report','')
  if not address.startswith('https://www.gdacs.org/'):raise ValueError('GDACS report URL left official host')
  for code in codes:
   identity=(typ,eid,code)
   if identity in seen:raise ValueError('GDACS unmerged duplicate event-country')
   seen.add(identity);iid=f'gdacs-{typ}-{eid}-{code}';country=source['_countries'][code]['name'];title=f'{p["eventname"]} — {country}' if p.get('eventname') else f'{kind.capitalize()} — {country}';color=str(p.get('alertlevel','unknown'));forecast=p.get('source')=='GLOFAS' or updated<start
   summary=f'GDACS lists {country} among the affected countries for this {kind} event. Provider event window: {start[:10]} to {end[:10]}; alert level: {color}. '+('Dates may describe modeled or forecast conditions; occurrence and local effects require confirmation. ' if forecast else '')+'The provider map point is a centroid; no precise impact location is asserted.'
   row=base(source,iid,code,title,summary,start,updated,now,{'FL':'flood','TC':'weather','WF':'weather','DR':'weather','VO':'volcano'}[typ],{'precision':'country'},dict(eventType=typ,eventId=eid,episodeId=episode,providerAlert=color,providerStart=start,providerEnd=end,providerIsCurrent=p.get('iscurrent'),providerSource=p.get('source'),providerClass=p.get('Class'),providerPointLabel=p.get('polygonlabel'),providerAffectedCountryCodes=codes,forecastOrInconsistentTimes=forecast,locationMeaning='provider_centroid_not_impact_location',archiveOrderingLimitation='GDACS end-date pagination lacks stable secondary sort'))
   row['severity']={'red':'severe','orange':'moderate'}.get(color.lower(),'unknown');row['reports']=[evidence(source,row,title,address,updated,now,summary,f,receipt,f'{typ}:{eid}')];result.append(row)
 return result

def title_countries(title,countries):
 """Exact country labels only in the final geographic title segment; no body NER."""
 parts=re.split(r'\s*[–—]\s*|\s+-\s*|(?<=\w)-\s+|,\s*',title)
 if len(parts)<2:return []
 segment=parts[-1].strip();labels=re.split(r'\s+(?:and|&)\s+|;',segment,flags=re.I)
 aliases={c['name'].casefold():c['code'] for c in countries.values()}
 aliases.update({'vietnam':'VN','viet nam':'VN','the gambia':'GM','gambia':'GM','kyrgyzstan':'KG','kyrgyz republic':'KG','swaziland':'SZ','east timor':'TL','saint lucia':'LC','saint vincent and the grenadines':'VC'})
 return sorted({aliases[x.strip().casefold()] for x in labels if x.strip().casefold() in aliases and aliases[x.strip().casefold()] in countries})

def who_rows(source,articles,now,receipt,records,article_receipts=None,event_aliases=None):
 incoming={};discoveries=[];country_map=source['_countries'];known_article={};known_event={}
 for alias in event_aliases or []:
  iid=alias['incidentId'];code=alias['countryCode']
  if iid in records and records[iid]['countryCode']==code and source['id'] in records[iid]['sourceIds']:
   known_event[(alias['eventId'],code)]=iid
 for old in records.values():
  if source['id'] not in old.get('sourceIds',[]):continue
  md=old.get('sourceMetadata',{})
  for aid in md.get('recordIds',[]):known_article[(aid,old['countryCode'])]=old['id']
  if md.get('whoEmergencyEventId'):known_event[(md['whoEmergencyEventId'],old['countryCode'])]=old['id']
 # Infer only equivalence of source-linked events to reviewed existing article IDs.
 # This is identity matching, never affected-country or onset inference.
 for article in articles:
  ev=article.get('EmergencyEvent') or {};ev_id=ev.get('Id')
  if ev_id:
   for (aid,code),iid in known_article.items():
    if aid==article['Id']:known_event[(ev_id,code)]=iid
 for article in sorted(articles,key=lambda a:(a['PublicationDateAndTime'],a['Id'])):
  aid=article.get('Id');receipt=(article_receipts or {}).get(aid,receipt);title=article.get('Title');pub=iso(article['PublicationDateAndTime']);modified=iso(article['LastModified'])
  if not isinstance(aid,str) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',aid) or not isinstance(title,str) or not title.strip():raise ValueError('WHO article identity/title missing')
  if pub>now or modified>now:continue
  path=article.get('ItemDefaultUrl','')
  if not re.fullmatch(r'/[A-Za-z0-9-]+',path):raise ValueError('WHO article URL path invalid')
  address='https://www.who.int/emergencies/disease-outbreak-news/item'+path;codes=title_countries(title,country_map);ev=article.get('EmergencyEvent') or {};ev_id=ev.get('Id');core={k:v for k,v in article.items() if k!='EmergencyEvent'}
  # Preserve reviewed source representations verbatim; expanded relation metadata
  # does not itself supersede their factual summaries or explicit resolved state.
  known=[records[i] for (a,c),i in known_article.items() if a==aid]
  unchanged=bool(known) and all(any(r.get('contentHash')==digest(core) or r.get('contentHash')==digest(article) for r in old['reports']) for old in known)
  for old in known:
   if any(r.get('contentHash')==digest(core) for r in old['reports']):codes=[c for c in codes if c!=old['countryCode']]
  if not codes:
   if not unchanged:
    discoveries.append(dict(id='who-discovery-'+aid,sourceId=source['id'],title=title,url=address,publishedAt=pub,retrievedAt=now,firstSeenAt=now,language='en',sourceCountryCodes=source['countryCodes'],mentionedCountryCodes=[],categoryCandidates=['health'],status='needs-review',locationStatus='unverified',contentHash=digest(article),note='WHO report requires affected-country review; body mentions are not automatically geocoded.',sourceMetadata=dict(recordId=aid,donId=article.get('DonId'),rawResponseSha256=receipt['sha256'],providerLastModified=modified)))
   continue
  for code in codes:
   iid=known_article.get((aid,code)) or known_event.get((ev_id,code)) or ('who-event-'+ev_id+'-'+code if ev_id else 'who-report-'+aid+'-'+code)
   old=incoming.get(iid) or records.get(iid);latest=old.get('sourceMetadata',{}).get('latestPublicationDate',old['occurredAt']) if old else None
   summary=f'WHO published “{title}” on {pub[:10]}. The title explicitly includes {country_map[code]["name"]}. This is an official health report; current transmission, case counts, subnational effects and outbreak status have not been automatically inferred.'
   metadata=dict(provider='WHO',whoEmergencyEventId=ev_id,recordIds=[aid],donIds=[article.get('DonId')],latestPublicationDate=pub,eventTimeBasis='report_publication_not_onset',exactEventTimeUnknown=True,countryEvidence='exact_country_label_in_geographic_title_segment',statusReviewRequired=True)
   row=base(source,iid,code,title+' — '+country_map[code]['name'],summary,pub,max(pub,modified),now,'health',{'precision':'country'},metadata)
   report=evidence(source,row,title,address,pub,now,summary,article,receipt,aid)
   row['reports']=[report]
   if old:
    row=copy.deepcopy(old);row['reports']=[r for r in old['reports'] if r.get('sourceMetadata',{}).get('externalId')!=aid]+[report]
    md=row.setdefault('sourceMetadata',{});md['whoEmergencyEventId']=ev_id;md['recordIds']=sorted(set(md.get('recordIds',[])+[aid]));md['donIds']=sorted(set(md.get('donIds',[])+[article.get('DonId')]));row['updatedAt']=max(old['updatedAt'],pub,modified)
    if not latest or pub>=latest:
     row['summary']=summary;md['latestPublicationDate']=pub;md['statusReviewRequired']=True
     # A generic bulletin cannot reopen or cancel a previously reviewed outbreak.
     if row['state'] not in ('resolved','cancelled'):row['state']='reported'
   incoming[iid]=row
 return list(incoming.values()),discoveries

def collect_official(source,policy,raw,now,countries,geo,previous,records,fetcher,fixtures=None,initial_http_status=200):
 """Return {incidents, discoveries, state, evidence}; caller commits atomically."""
 from public_data_pipeline import official_incidents
 source={**source,'_countries':countries};adapter=policy['adapter'];requests=Requests(policy,fetcher,now,fixtures);state={};discoveries=[]
 if initial_http_status==204 and adapter=='gdacs-api' and raw==b'':
  requests.receipts.append({'url':policy['url'],'retrievedAt':now,'bytes':0,'sha256':sha(raw),'httpStatus':204});document={'type':'FeatureCollection','features':[]}
 else:document=requests.accept(raw,policy['url'])
 if adapter=='gdacs-api':
  features=[];seen_pages=set();overlaps=0;unique={};previous_end=None
  for page in range(1,MAX_PAGES+1):
   if document.get('type')!='FeatureCollection' or not isinstance(document.get('features'),list):raise ValueError('GDACS FeatureCollection missing')
   current=document['features'];assert len(current)<=100
   h=digest(document)
   if current and h in seen_pages:raise ValueError('GDACS repeated pagination response')
   seen_pages.add(h)
   for f in current:
    p=f['properties'];end=p['todate']
    if previous_end is not None and end>previous_end:raise ValueError('GDACS end-date order changed during pagination')
    previous_end=end;key=(p['eventtype'],str(p['eventid']))
    if key in unique:overlaps+=1
    if key not in unique or (int(p['episodeid']),p['datemodified'])>(int(unique[key][0]['properties']['episodeid']),unique[key][0]['properties']['datemodified']):unique[key]=(f,requests.receipts[-1])
   if len(current)<100:break
   params=urllib.parse.parse_qs(urllib.parse.urlsplit(policy['url']).query);params={k:v[0] for k,v in params.items()};params['pagenumber']=page+1
   document=requests.get(url(GDACS,params),f'page-{page+1:03d}')
  else:raise ValueError('GDACS page ceiling reached; source rejected atomically')
  incoming=[]
  for f,receipt in unique.values():incoming.extend(gdacs_rows(source,[f],now,receipt))
  state.update(pageBoundaryOverlaps=overlaps,exhaustiveArchiveGuarantee=False,collectionWindow=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(policy['url']).query)))
 elif adapter=='who-don':
  articles=[];article_receipts={};seen=set();declared=document.get('@odata.count');order=[]
  if not isinstance(declared,int) or declared<0:raise ValueError('WHO declared count missing')
  for page in range(1,MAX_PAGES+1):
   if not isinstance(document.get('value'),list) or document.get('@odata.count',declared)!=declared:raise ValueError('WHO page/count mismatch')
   for a in document['value']:
    if a['Id'] in seen:raise ValueError('WHO duplicate article across pages')
    seen.add(a['Id']);articles.append(a);article_receipts[a['Id']]=requests.receipts[-1];order.append((a['LastModified'],a['Id']))
   next_url=document.get('@odata.nextLink')
   if not next_url:break
   p=urllib.parse.urlsplit(next_url)
   if p.hostname!='www.who.int' or p.path!='/api/news/diseaseoutbreaknews' or p.scheme not in ('http','https') or p.username:raise ValueError('WHO nextLink left documented API')
   document=requests.get(urllib.parse.urlunsplit(p._replace(scheme='https')),f'page-{page+1:03d}')
  else:raise ValueError('WHO page ceiling reached; source rejected atomically')
  if len(articles)!=declared or order!=sorted(order):raise ValueError('WHO complete count/order validation failed')
  incoming,discoveries=who_rows(source,articles,now,requests.receipts[0],records,article_receipts,policy.get('eventAliases',[]))
  state.update(declaredCount=declared,completeCountVerified=True,lastModifiedCursor=now,collectionWindow=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(policy['url']).query)))
 elif adapter=='usgs-week':
  if len({f['id'] for f in document.get('features',[])})!=len(document.get('features',[])):raise ValueError('USGS duplicate provider IDs')
  incoming=official_incidents(source,raw,now,countries,geo,{})
  last=moment(previous['lastSuccessAt']) if previous.get('lastSuccessAt') else None;current=moment(now);week=current-dt.timedelta(days=7)
  if last and last<week:
   start=max(last-dt.timedelta(days=1),current-dt.timedelta(days=30));end=week;state['catchupWindow']={'from':iso(start),'through':iso(end)}
   if start>last:state['coverageGap']={'from':iso(last),'through':iso(start),'reason':'Outage exceeds bounded 30-day catalog catch-up; historical review required'}
   left=start;counter=0
   while left<end:
    right=min(left+dt.timedelta(days=7),end);params={'format':'geojson','starttime':iso(left),'endtime':iso(right),'eventtype':'earthquake'};counter+=1
    count=requests.get(url(CATALOG+'count',params),f'count-{counter:03d}')
    n=count.get('count')
    if not isinstance(n,int) or not 0<=n<=20000:raise ValueError('USGS catch-up count exceeds complete-query limit')
    if n:
     params['orderby']='time-asc';page=requests.get(url(CATALOG+'query',params),f'catalog-{counter:03d}')
     if page.get('metadata',{}).get('count')!=n or len(page.get('features',[]))!=n:raise ValueError('USGS catch-up count/query mismatch')
     incoming.extend(official_incidents(source,json.dumps(page).encode(),now,countries,geo,{}))
    left=right
  unique={}
  for r in incoming:
   if r['id'] not in unique or r['updatedAt']>unique[r['id']]['updatedAt']:unique[r['id']]=r
  incoming=list(unique.values())
  if any(r['occurredAt']>now for r in incoming):raise ValueError('USGS future occurrence in accepted response')
  state['feedWindowDays']=7;state['minimumMagnitude']=None
 else:raise ValueError('Unknown official adapter')
 state.update(responseCount=len(requests.receipts),completeResponseBytes=requests.total,responseHashes=[x['sha256'] for x in requests.receipts])
 return {'incidents':incoming,'discoveries':discoveries,'state':state,'evidence':requests.receipts}
