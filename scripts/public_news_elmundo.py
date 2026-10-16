#!/usr/bin/env python3
"""Reviewed El Mundo Spanish crime intake; complete public HTML, no model/network.

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
# Reviewed names, not a geocoder. Law 762 establishes the administrative
# relationships; it does not certify these provider-simplified legacy polygons.
# The shortened provider name "Opico" remains deliberately unassigned at ADM2.
ADMIN_REVIEW = 'sv-elmundo-named-areas-2026-10-16-v1'
ADMIN_INDEX_SHA256 = '248aae25962b4c530c900ea2d87a2f802bd9c48d9273c1d88c650dfda46190a1'
ADMIN_LAYERS = {
    'ADM1': ('SLV-ADM1-98794003', '2021', 'f9244692cbce900c1c23b69be7371ee7ea7a99413ef1b76fabc920be774f9f9e'),
    'ADM2': ('SLV-ADM2-79315794', '2007', '625800b49cb893b399d788d4b971d6af6974413c9cf1f5554df605ecd99cac5b'),
}
ADMIN_PLACES = {
    'Ciudad Barrios': ('gb:98794003B50968784291710', 'Departamento de San Miguel', 'gb:79315794B38727167793446', 14),
    'San Juan Opico': ('gb:98794003B3671241590115', 'La Libertad', None, 6),
    'Colón': ('gb:98794003B3671241590115', 'La Libertad', 'gb:79315794B90706400022034', 6),
    'Acajutla': ('gb:98794003B56835012218806', 'Departamento de Sonsonate', 'gb:79315794B43866171766073', 12),
    'Sensuntepeque': ('gb:98794003B16890138661932', 'Departamento de Cabañas', 'gb:79315794B14613066597096', 9),
    'Santa Tecla': ('gb:98794003B3671241590115', 'La Libertad', 'gb:79315794B65816888820880', 7),
    'Zacatecoluca': ('gb:98794003B62092818645437', 'Departamento de La Paz', 'gb:79315794B51596434492977', 10),
}
ADMIN_FIELDS = ('admin1Code','admin1Name','admin1Id','admin2Code','admin2Name','admin2Id','adminAssignment')
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


def administrative_matches(areas=(), boundary_layers=None):
    """Fail closed unless the complete reviewed SV index and layers match.

    Hash canonical JSON, including geometry, rather than trusting an externally
    supplied hash or a layer's claimed date. Other countries are out of scope.
    """
    def fingerprint(value):
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    try:
        fields=('id','name','countryCode','level','parentId')
        sv=[{k:a[k] for k in fields} for a in areas if a['countryCode']=='SV']
        if fingerprint(sorted(sv,key=lambda a:a['id']))!=ADMIN_INDEX_SHA256:return {}
        for level,(layer_id,year,sha) in ADMIN_LAYERS.items():
            layer=boundary_layers[level]
            if (fingerprint(layer)!=sha or layer['source']['layerId']!=layer_id or
                    layer['source']['boundaryYearRepresented']!=year):return {}
        result={}
        for place,(parent_id,parent_name,child_id,page) in ADMIN_PLACES.items():
            parent=[a for a in sv if a['level']=='ADM1' and a['name']==parent_name]
            child=[a for a in sv if a['level']=='ADM2' and a['name']==place and a['parentId']==parent_id]
            if len(parent)!=1 or parent[0]['id']!=parent_id or parent[0]['parentId']!='country:SV':continue
            if child_id and (len(child)!=1 or child[0]['id']!=child_id):continue
            result[place]=dict(parent=parent[0],child=child[0] if child_id else None,lawPage=page)
        return result
    except (KeyError,TypeError,ValueError):
        return {}


def assign_administration(row, facts, matches):
    """A named source area supports filters, never a point or impact polygon."""
    for key in ADMIN_FIELDS:row.pop(key,None)
    row['sourceMetadata'].pop('administrativeAssociation',None)
    row['sourceMetadata']['administrativeMatchStatus']='unverified-dataset-or-place' if facts else 'source-place-requires-review'
    row['location']=dict(precision='country')
    if (not isinstance(facts,dict) or not isinstance(facts.get('place'),str) or
            PLACES.get(norm(facts['place']))!=(facts['place'],facts.get('municipality'))):return
    match=matches.get(facts['place'])
    if not match:return
    levels={}
    for level,area in (('ADM1',match['parent']),('ADM2',match['child'])):
        if area is None:continue
        prefix='admin'+level[-1]
        row[prefix+'Code']=row[prefix+'Id']=area['id'];row[prefix+'Name']=area['name']
        layer_id,year,sha=ADMIN_LAYERS[level]
        levels[level]=dict(id=area['id'],name=area['name'],parentId=area['parentId'],layerId=layer_id,
            boundaryYearRepresented=year,canonicalLayerSha256=sha)
    row['location']=dict(precision='area')
    row['sourceMetadata']['administrativeMatchStatus']='reviewed-name-association'
    row['adminAssignment']=dict(method='explicit_source_place_names',confidence='source_reported_area',pointInferred=False)
    row['sourceMetadata']['administrativeAssociation']=dict(review=ADMIN_REVIEW,reviewedAt='2026-10-16',
        reportedDistrict=facts['place'],reportedMunicipality=facts['municipality'],
        lawUrl='https://www.jurisprudencia.gob.sv/DocumentosBoveda/D/2/2020-2029/2023/06/F9625.PDF',
        lawSha256='41c096af17e53e37c87d120f1d509928d65b37c44ea07a008816626d08c96dfa',lawArticle=1,lawPdfPage=match['lawPage'],
        canonicalIndexSha256=ADMIN_INDEX_SHA256,levels=levels,geometryResolution='provider_simplified',
        currentBoundaryVerified=False,impactBoundary=False)


def administrative_change(before,row,now):
    def projection(record):
        return dict(location=record['location'],**{k:record[k] for k in ADMIN_FIELDS if k in record},
            association=record.get('sourceMetadata',{}).get('administrativeAssociation'),
            status=record.get('sourceMetadata',{}).get('administrativeMatchStatus'))
    previous=projection(before)
    if previous==projection(row):return False
    row['sourceMetadata']['administrativeHistory']=copy.deepcopy(before.get('sourceMetadata',{}).get('administrativeHistory',[]))+[
        dict(at=now,previous=previous)]
    row['sourceMetadata']['administrativeUpdatedAt']=now
    return True


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


def collect_elmundo(source,documents,now,countries,*,existing=None,reviews=(),areas=(),boundary_layers=None):
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
    geography=administrative_matches(areas,boundary_layers)
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
            # A later reviewed map crosswalk may improve (or invalidate) a
            # name association without fabricating a new source representation.
            row=copy.deepcopy(before);assign_administration(row,facts,geography)
            if administrative_change(before,row,now):
                row['updatedAt']=max(before['updatedAt'],now)
                known[iid]=row;changes[iid]=row
                decision.update(status='automatic-source-record' if facts else 'needs-review',incidentId=iid,
                    reason='administrative-association-rechecked')
                decisions.append(decision);continue
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
            row['sourceMetadata'].update(reviewRequired=True,reviewReason=decision['reason'])
        assign_administration(row,facts,geography)
        if before:
            for key in ('administrativeHistory','administrativeUpdatedAt'):
                if key in before['sourceMetadata']:row['sourceMetadata'][key]=copy.deepcopy(before['sourceMetadata'][key])
            administrative_change(before,row,now)
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
    # A validated listing batch can reproject retained source facts without
    # waiting for their rotating article recheck. Never revive a held account or
    # touch a reviewer-owned record. No discovery, source report or clock is made.
    for iid,before in known.items():
        meta=before.get('sourceMetadata',{})
        if (iid in changes or before.get('countryCode')!='SV' or before.get('sourceIds')!=[SOURCE_ID] or
                meta.get('parser')!=PARSER or any(r.get('url') in manual for r in before.get('reports',[]))):continue
        facts=meta.get('facts') if before.get('evidenceStatus')=='reported' and meta.get('reviewRequired') is False else None
        row=copy.deepcopy(before);assign_administration(row,facts,geography)
        if administrative_change(before,row,now):
            row['updatedAt']=max(before['updatedAt'],now);changes[iid]=row
    return dict(incidents=list(changes.values()),decisions=decisions)
