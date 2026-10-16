#!/usr/bin/env python3
"""Inactive El Mundo Spanish crime pilot; complete public HTML, no model/network.

The reviewed publisher's embedded police account supports the event claim; it is
not a second independent source. Unknown event dates use an explicitly labeled
publication anchor, never the arrest date. Only finite, replayed local patterns
qualify. Full source text is checked even when a background case is not projected.
"""
import copy
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import json
import re
import unicodedata
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from public_news_runtime import instant

SOURCE_ID = 'news-sv-diario-el-mundo'
PARSER = 'elmundo-es-police-event-v1'
HOST = 'diario.elmundo.sv'
MAX_BYTES = 2_000_000
ZONE = ZoneInfo('America/El_Salvador')
PLACES = {
    'ciudad barrios': ('Ciudad Barrios', 'San Miguel Norte'),
    'san juan opico': ('San Juan Opico', 'La Libertad Centro'),
    'colon': ('Colón', 'La Libertad Oeste'),
    'acajutla': ('Acajutla', 'Sonsonate Oeste'),
    'sensuntepeque': ('Sensuntepeque', 'Cabañas Este'),
    'santa tecla': ('Santa Tecla', 'La Libertad Sur'),
    'zacatecoluca': ('Zacatecoluca', 'La Paz Este'),
}
MONTHS = {v:i for i,v in enumerate(('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'),1)}
# Includes complete-body correction/denial guards, not a length-limited lead.
HOLD = (r'\b(?:simulacro|simulacion|ficcion|novela|hipotetico|rectificacion|retractacion|'
        r'desmentido|desmintio|noticia falsa|reporte falso|versiones contradictorias|ejemplo didactico|' 
        r'no (?:ocurrio|sucedio|hubo|se registro|es cierto|murio|fue asesinado|se trato de)|ocurrira|sucedera|'
        r'ataque (?:simulado|planeado|previsto)|robo (?:simulado|planeado))\b')
CONFLICT = r'\b(?:conflicto armado|combates? entre|ataque (?:militar|insurgente)|guerrilla|bombardeo|enfrentamientos? armados?)\b'
COURT = r'\b(?:condena(?:n|do|da|dos|das)?|sentencian|prision provisional|seguira(?:n)? detenido|seguira en prision|juicio|orden(?:es)? judicial(?:es)?|estadisticas|homicidios: pnc|registro seis homicidios)\b'
VOID = {'br','img','meta','link','input','hr','source','area','embed','wbr'}


def norm(value):
    return ''.join(c for c in unicodedata.normalize('NFD', value.casefold()) if not unicodedata.combining(c))


def tight(value):
    return re.sub(r'\s+', '', html.unescape(html.unescape(value)))


def canonical(url):
    p=urlsplit(url)
    if p.scheme!='https' or p.netloc!=HOST or p.query or p.fragment or not re.fullmatch(r'/nacionales/[a-z0-9-]+',p.path):
        raise ValueError('Unreviewed El Mundo article URL')
    return url


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[]; self.jsonld=[]; self.script=None; self.meta={}; self.canon=[]
        self.body_count=0; self.body=[]; self.quotes=[]; self.quote=None; self.quote_depth=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta':
            key=a.get('property',a.get('name'))
            if key in ('article:section','article:published_time','article:modified_time','og:url','og:title'):
                if key in self.meta and self.meta[key]!=a.get('content'): raise ValueError('Conflicting article metadata')
                self.meta[key]=a.get('content')
        if tag=='link' and a.get('rel')=='canonical':self.canon.append(a.get('href'))
        inbody=any(x[1] for x in self.stack)
        ownbody='article-body' in a.get('class','').split()
        if ownbody and not inbody:self.body_count+=1
        ignored=tag in ('script','style','iframe') or any(x[2] for x in self.stack)
        if tag=='script' and a.get('type')=='application/ld+json':self.script=[]
        if inbody and tag=='blockquote':
            if self.quote is not None: raise ValueError('Nested source quote')
            self.quote={'text':[],'links':[]};self.quote_depth=len(self.stack)
        if inbody and self.quote is not None and tag=='a':self.quote['links'].append(a.get('href',''))
        if tag not in VOID:self.stack.append((tag,ownbody or inbody,ignored))
        elif inbody and not ignored:self.body.append('\n')
    def handle_endtag(self,tag):
        if tag=='script' and self.script is not None:
            self.jsonld.append(''.join(self.script));self.script=None
        if tag=='blockquote' and self.quote is not None:
            self.quotes.append({'text':' '.join(''.join(self.quote['text']).split()),'links':self.quote['links']});self.quote=None
        if any(x[1] for x in self.stack) and tag in ('p','div','blockquote','li'):self.body.append('\n')
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:self.stack=self.stack[:i];break
    def handle_data(self,data):
        if self.script is not None:self.script.append(data)
        if any(x[1] for x in self.stack) and not any(x[2] for x in self.stack):
            self.body.append(data)
            if self.quote is not None:self.quote['text'].append(data)


def parse_article(raw,url,now):
    canonical(url)
    if not isinstance(raw,bytes) or not raw or len(raw)>MAX_BYTES:raise ValueError('Complete article missing or oversized')
    p=Document();p.feed(raw.decode('utf-8',errors='strict'));p.close()
    documents=[json.loads(x) for x in p.jsonld]
    articles=[x for x in documents if isinstance(x,dict) and x.get('@type')=='NewsArticle']
    if len(articles)!=1 or p.body_count!=1 or p.canon!=[url]:raise ValueError('Article identity/body contract changed')
    d=articles[0]
    if (d.get('url')!=url or d.get('mainEntityOfPage',{}).get('@id')!=url or d.get('inLanguage')!='es' or
        d.get('publisher',{}).get('name')!='Diario El Mundo' or d.get('publisher',{}).get('url')!=f'https://{HOST}/' or
        p.meta.get('article:section')!='Nacionales' or p.meta.get('og:url')!=url or
        html.unescape(html.unescape(p.meta.get('og:title','')))!=html.unescape(html.unescape(d.get('headline','')))):
        raise ValueError('Article source identity changed')
    body=d.get('articleBody');title=d.get('headline')
    if not isinstance(body,str) or not body.strip() or not isinstance(title,str) or not title.strip():raise ValueError('Empty complete article')
    # Both representations must contain the same entire accepted narrative.
    if tight(body)!=tight(''.join(p.body)):raise ValueError('Visible/structured complete article disagree')
    dates=[]
    for key,meta in (('datePublished','article:published_time'),('dateModified','article:modified_time')):
        if d.get(key)!=p.meta.get(meta):raise ValueError('Article clocks disagree')
        t=instant(d[key])
        if t>instant(now)+dt.timedelta(minutes=5):raise ValueError('Future article clock')
        dates.append(t.astimezone(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'))
    quotes=[]
    for q in p.quotes:
        links=[]
        for link in q['links']:
            u=urlsplit(link)
            if u.scheme=='https' and u.netloc in ('x.com','twitter.com') and re.fullmatch(r'/PNCSV/status/\d+',u.path):
                links.append('https://x.com'+u.path)
        if links:quotes.append({'text':q['text'],'url':links[0]} if len(set(links))==1 else {'text':q['text'],'url':None})
    return dict(url=url,title=html.unescape(html.unescape(title)).strip(),description=body,publishedAt=dates[0],modifiedAt=dates[1],
        policeQuotes=quotes,representationFormat='elmundo-complete-html-jsonld-v1')


def classify(item):
    full=norm(item['title']+' '+item['description']);title=norm(item['title'])
    if re.search(HOLD,full):return None,'denied-corrected-planned-or-simulated'
    if re.search(CONFLICT,full):return None,'organized-conflict-requires-review'
    if re.search(COURT,title):return None,'court-history-or-statistics-not-new-event'
    if len(item['policeQuotes'])!=1 or not item['policeQuotes'][0]['url']:return None,'one-complete-attributed-police-account-required'
    q=norm(item['policeQuotes'][0]['text'])
    primary=norm(next((p.strip() for p in item['description'].splitlines() if p.strip()),''))
    if re.search(r'\bno\s+(?:habia robado|golpeo|ataco|protagonizaron|amenazo)\b',q):
        return None,'negated-event-claim'
    if re.search(r'\b(?:si se confirmara|si fuera cierto|de confirmarse|podria tratarse|sin relacion|otro caso|caso de (?:19|20)\d{2}|relato historico|anos anteriores)\b',q):
        return None,'conditional-historical-or-unrelated-police-account'
    if re.search(r'\b(?:supuestamente|al parecer)\b.{0,45}\b(?:habia robado|golpeo|ataco|protagonizaron|amenazo)\b',q):
        return None,'uncertain-event-occurrence'
    if re.search(r'\b(?:robo|ataque|agresion|homicidio)\b.{0,35}\b(?:ocurrio|sucedio|cometido)\b',full):
        return None,'unsupported-explicit-event-date'
    # Exact reviewed district + municipality pairs. Nationality is not location.
    matches=[(k,v) for k,v in PLACES.items() if re.search(r'(?<!\w)'+re.escape(k)+r'(?!\w)',title) and
             re.search(re.escape(k)+r',\s*'+re.escape(norm(v[1])),q)]
    if len(matches)!=1:return None,'primary-event-place-pair-required'
    key,(place,municipality)=matches[0]
    if any(k!=key and re.search(re.escape(k)+r',\s*'+re.escape(norm(v[1])),q) for k,v in PLACES.items()):
        return None,'multiple-police-event-places'
    if re.search(r'\b(?:ocurrio|sucedio|hechos|ataque|robo)\b.{0,50}\ben (?:guatemala|honduras|nicaragua|costa rica|mexico|estados unidos)\b',full):
        return None,'foreign-event-context'
    # Source-specific event clauses in the quoted account, plus headline cause.
    kind=None;fact=None
    if re.search(r'\b(?:matar|asesin|homicidio)',title) and re.search(r'\breportamos un homicidio en '+re.escape(key)+r',',q) and re.search(r'\bataco con arma blanca\b',q) and re.search(r'\bdurante el ataque asesino\b',q) and re.search(r'\bprovoco un incendio en la vivienda\b',q):
        kind='fatal-attack';fact='a fatal knife attack and a fire at a home'
    elif re.search(r'\brobar a punta de pistola\b',title) and re.search(r'\bhabia robado a un ciudadano a punta de pistola\b',q) and re.search(r'\bdurante un patrullaje preventivo en '+re.escape(key)+r',',q):
        # The patrol/arrest locality alone is not an event location. Require
        # the publisher's separate primary sentence to locate the robbery.
        local_robbery=(r'\brobar [“\"]?a punta de pistola[”\"]?,? a (?:otro |un )?ciudadano en el distrito de '+re.escape(key)+r' en '+re.escape(norm(municipality))+r'\b')
        if not re.search(local_robbery,primary):return None,'primary-robbery-location-not-established'
        kind='armed-robbery';fact='a reported armed robbery'
    elif re.search(r'\bgolpear\b',title) and re.search(r'\bgolpeo (?:a su companera de vida en la cara con un taladro|con los punos el rostro de otro hombre)\b',q) and re.search(r'\b(?:victima fue trasladada al hospital|victima resulto con lesiones.{0,100}fue llevado a un hospital)\b',q):
        kind='physical-assault';fact='a reported physical assault'
    elif re.search(r'\bamenaza\w*\b',title) and re.search(r'\bamenazando con un corvo a unas personas frente a su vivienda\b',q) and re.search(r'\blos hechos ocurrieron\b',q):
        kind='armed-threat';fact='reported threats with a machete outside a home'
    elif re.search(r'\b(?:pelea|rina)\b',title) and re.search(r'\b(?:protagonizaron una pelea al interior de un bus|protagonizaron una rina callejera, agrediendose con punos)\b',q):
        if 'interior de un bus' in q and not re.search(r'\bpelea a golpes en el interior de una unidad.{0,80}en el distrito de '+re.escape(key)+r',\s*'+re.escape(norm(municipality)),primary):
            return None,'primary-bus-fight-location-not-established'
        kind='physical-assault';fact='a reported physical fight on a bus' if 'interior de un bus' in q else 'a reported street fight'
    if not kind:return None,'outside-reviewed-crime-account'
    # Reject negated/conditional core claims; accusations about an alleged
    # perpetrator do not become an assertion that the person committed a crime.
    if re.search(r'\b(?:supuestamente|al parecer|habria|presunto|presunta)\s+(?:ocurrido|ataque|robo|agresion|homicidio)\b',q):
        return None,'uncertain-event-occurrence'
    if kind=='physical-assault' and not re.search(r'\b(?:en|sobre|sucedio afuera|lugar de los hechos)\b.{0,180}'+re.escape(key)+r'\b',q):
        return None,'event-place-not-established'
    published=instant(item['publishedAt']).astimezone(ZONE).date();day=None
    date_match=re.search(r'\blos hechos ocurrieron el\s+(\d{1,2})\s+(?:de\s+)?('+'|'.join(MONTHS)+r')(?:\s+de\s+(20\d{2}))?\b',q)
    if date_match:
        try:day=dt.date(int(date_match[3] or published.year),MONTHS[date_match[2]],int(date_match[1]))
        except ValueError:return None,'invalid-event-date'
        if day>published or (published-day).days>366:return None,'future-or-old-event-date'
    # All dated onset phrases must be parsed rather than silently falling back.
    elif re.search(r'\b(?:hechos ocurrieron|ocurrio el|sucedio el|desde (?:19|20)\d{2})\b',q):
        return None,'unsupported-explicit-event-date'
    return dict(kind=kind,fact=fact,place=place,municipality=municipality,eventDay=day.isoformat() if day else None,
        policeAccount=item['policeQuotes'][0]['url']),'reviewed-police-event-account'


def collect_elmundo(source,documents,now,countries,*,existing=None,reviews=()):
    """documents=[{'url': canonical, 'raw': complete bytes}]; validate atomically.

    No requests or automatic activation. A malformed representation raises before
    returning any changes. Caller must retain failed/absent-source records and
    refresh earlier accepted URLs; a listing is discovery, not a correction log.
    """
    from public_data_pipeline import digest,make_incident
    if (source.get('id')!=SOURCE_ID or source.get('url')!=f'https://{HOST}/' or source.get('language')!='es' or
        source.get('tier')!='local-news' or source.get('countryCodes')!=['SV']):raise ValueError('Unreviewed source scope')
    if not isinstance(documents,list) or len(documents)>100:raise ValueError('Unbounded article batch')
    items=[parse_article(d['raw'],d['url'],now) for d in documents]
    if len({x['url'] for x in items})!=len(items):raise ValueError('Duplicate article URL')
    if countries.get('SV',{}).get('status')!='active':return dict(incidents=[],decisions=[])
    known=copy.deepcopy(existing or {});changes={};decisions=[]
    manual={r['url'] for r in reviews if r['sourceId']==SOURCE_ID}
    for item in items:
        url=item['url'];before=next((r for r in known.values() if any(x.get('url')==url for x in r.get('reports',[]))),None)
        facts,reason=classify(item);representation=digest(item)
        decision=dict(url=url,title=item['title'],publishedAt=item['publishedAt'],contentHash=representation,status='needs-review',reason=reason)
        if instant(item['publishedAt'])<instant(now)-dt.timedelta(days=366):facts=None;decision['reason']='outside-one-year-pilot-window'
        if url in manual or before and before.get('sourceMetadata',{}).get('parser')!=PARSER:
            decision.update(status='review-owned',reason='existing-review-preserved');decisions.append(decision);continue
        if facts:
            iid='news-'+hashlib.sha256((SOURCE_ID+'|'+facts['policeAccount']).encode()).hexdigest()[:20]
            if before and before['id']!=iid:facts=None;decision['reason']='primary-police-account-changed'
            elif not before:before=known.get(iid)
            if before and before.get('sourceMetadata',{}).get('parser')!=PARSER:
                decision.update(status='review-owned',reason='existing-review-preserved');decisions.append(decision);continue
            if facts and before and before.get('sourceMetadata',{}).get('facts') and before['sourceMetadata']['facts']!=facts:
                facts=None;decision['reason']='changed-event-facts-require-review'
        if not facts and not before:decisions.append(decision);continue
        iid=before['id'] if before else iid
        latest=before.get('sourceMetadata',{}).get('latestRepresentations',{}) if before else {}
        if latest.get(url)==representation:
            decision.update(status='unchanged',incidentId=iid);decisions.append(decision);continue
        report=dict(id=iid+'-'+representation[:12],incidentId=iid,sourceId=SOURCE_ID,title=item['title'],url=url,
            language='es',publishedAt=item['publishedAt'],retrievedAt=now,excerpt='',contentHash=representation,
            independentGroup=source.get('independentGroup',SOURCE_ID),sourceMetadata=dict(
                representationFormat=item['representationFormat'],completeAcceptedTextSha256=hashlib.sha256(item['description'].encode()).hexdigest()))
        if facts:
            anchor=min(item['publishedAt'],before['occurredAt']) if before and not facts['eventDay'] else item['publishedAt']
            occurrence=facts['eventDay']+'T00:00:00Z' if facts['eventDay'] else anchor
            prefix=f"On {facts['eventDay']}" if facts['eventDay'] else f"Reported {instant(anchor).astimezone(ZONE).date().isoformat()}"
            summary=f"{prefix}: Diario El Mundo, citing El Salvador's police, describes {facts['fact']} in {facts['place']}."
            if facts['fact']=='a reported physical assault':summary+=' The victim was taken to hospital.'
            if not facts['eventDay']:summary+=' The event date was not specified.'
            provider=dict(parser=PARSER,facts=facts,provider='Diario El Mundo',policeAccount=facts['policeAccount'],
                eventTimeBasis='explicit-source-event-day' if facts['eventDay'] else 'publication',eventDateKnown=bool(facts['eventDay']),
                eventDatePrecision='day' if facts['eventDay'] else 'unknown',reportedLocalTimeZone='America/El_Salvador',
                reportedPlace=facts['place'],reportedMunicipality=facts['municipality'],
                summaryMethod='deterministic-reviewed-fact-template',summaryLanguage='en',originalLanguage='es',
                evidenceScope='One publisher quotes a police account; no independent corroboration',reviewRequired=False)
            row=make_incident(source,iid,item['title'],summary,countries['SV'],'crime',occurrence,
                max(item['publishedAt'],item['modifiedAt']),now,dict(precision='country'),url,provider,item,published=item['publishedAt'])
            row.update(evidenceStatus='reported',region=facts['place'])
            decision.update(status='automatic-source-record',incidentId=iid)
        else:
            row=copy.deepcopy(before);row.update(title=item['title'],summary='The changed source account requires review.',
                severity='unknown',state='expired',evidenceStatus='unverified',location=dict(precision='country'))
            for k in ('admin1Code','admin1Name','admin1Id','admin2Code','admin2Name','admin2Id','adminAssignment'):row.pop(k,None)
            row['sourceMetadata'].update(reviewRequired=True,reviewReason=decision['reason'])
        row['sourceMetadata'].update(latestRepresentations={**latest,url:representation},providerPublishedAt=item['publishedAt'],providerModifiedAt=item['modifiedAt'])
        if before:row['sourceMetadata']['changeObservedAt']=now
        row['updatedAt']=max(item['publishedAt'],item['modifiedAt'],before['updatedAt'] if before else '',now if before else '')
        row['firstSeenAt']=before['firstSeenAt'] if before else now
        row['historical']=instant(row['occurredAt'])<instant(now)-dt.timedelta(days=30)
        old_reports=before.get('reports',[]) if before else []
        old_version=next((r for r in old_reports if r['id']==report['id']),report)
        row['reports']=[copy.deepcopy(old_version)]+[r for r in old_reports if r['id']!=report['id']]
        if before and before.get('revisions'):row['revisions']=copy.deepcopy(before['revisions'])
        known[iid]=row;changes[iid]=row;decisions.append(decision)
    return dict(incidents=list(changes.values()),decisions=decisions)
