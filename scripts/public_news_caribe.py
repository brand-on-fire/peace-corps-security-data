#!/usr/bin/env python3
"""Inactive, source-specific Caribe Actual Spanish crime pilot. No network calls."""
from html.parser import HTMLParser
import re


def tight(value):
    return re.sub(r'\s+', ' ', value).strip()


class ArticleText(HTMLParser):
    """Read all narrative text; remove only identified rendered navigation/media.

    Newspack embeds older article cards inside WP content.rendered. These are
    navigation, not statements from the current article. Never take an arbitrary
    prefix of article text. The complete response is hashed by parse_posts.
    """
    VOID = {'img', 'br', 'hr', 'input', 'meta', 'link', 'source', 'wbr', 'embed', 'area', 'base', 'col', 'param', 'track'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []; self.parts = []; self.excluded = []; self.media = False
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        excluded = tag in {'script','style'} or 'wp-block-newspack-blocks-homepage-articles' in attrs.get('class','').split()
        if tag in {'iframe','video','audio'}: self.media = True
        inherited = self.stack[-1][1] if self.stack else False
        if excluded and not inherited: self.excluded.append('newspack-related-articles' if tag == 'div' else tag)
        if tag not in self.VOID: self.stack.append((tag, inherited or excluded))
        if not (inherited or excluded) and tag in {'p','h1','h2','h3','h4','li','blockquote','br'}: self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in self.VOID: return
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError('Unbalanced article HTML; complete narrative requires review')
        _, excluded = self.stack.pop()
        if not excluded and tag in {'p','h1','h2','h3','h4','li','blockquote'}: self.parts.append('\n')
    def handle_data(self, text):
        if not self.stack or not self.stack[-1][1]: self.parts.append(text)
    def result(self):
        if self.stack: raise ValueError('Unclosed article HTML')
        return [tight(line) for line in ''.join(self.parts).splitlines() if tight(line)]


def article_text(html):
    parser = ArticleText(); parser.feed(html); parser.close()
    return parser.result(), parser.excluded, parser.media

import copy
import datetime as dt
import hashlib
import json
import unicodedata
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SOURCE_ID = 'news-cr-caribe-actual'
HOST = 'caribeactual.com'
PARSER = 'caribe-spanish-crime-v1'
ZONE = ZoneInfo('America/Costa_Rica')
WEEKDAYS = dict(zip(('lunes','martes','miercoles','jueves','viernes','sabado','domingo'),range(7)))
MONTHS = dict(zip(('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'),range(1,13)))
MONTHS['setiembre'] = 9
# Source-named Caribbean localities; these are NOT polygon or coordinate claims.
# The selected source consistently identifies these within Costa Rican OIJ reports.
PLACES = ('Pococí','Guácimo','Matina','Siquirres','Limón','Batán','Guápiles')
DISQUALIFY_TITLE = r'\b(?:captur\w*|detien\w*|deten\w*|arrest\w*|conden\w*|juicio|sentencia|estadistic\w*|aumento|balance|hallan|hallado|desaparecid\w*|simulacro|opinion)\b'
UNCERTAIN = r'\b(?:habria\w*|podria\w*|supuest\w*|presuntamente|aparentemente|posible|al parecer|en apariencia)\b'
REVIEW = r'\b(?:desmient\w*|desmint\w*|niega\w*|nego|negado|rectific\w*|correg\w*|correccion|retract\w*|fals[oa]s?|errone\w*|errores?|rumor\w*|simul\w*|ficcion|hipotetic\w*|condicional|ejemplo|aniversario|historico|antiguo|recuerda|recordo|background|archivo|aclaracion|en realidad|nunca|jamas|desmentid\w*|descart\w*|retiro|retirad\w*|retira\w*|recreacion|teatral|pelicula|dramatiz\w*)\b'
FOREIGN = r'\b(?:panama|nicaragua|honduras|el salvador|guatemala|mexico|colombia|venezuela|ecuador|peru|estados unidos|espana|ucrania|rusia|gaza|israel)\b'
SHOOT = r'\b(?:balazos|dispar\w*|proyectil\w*|bal[ae]s?|balead\w*|arma[s]? de fuego|ataque armado|tiroteo)\b'
FATAL = r'\b(?:murio|murieron|fallecio|fallecieron|perdio la vida|perdieron la vida|fue asesinado|fueron asesinados|homicidio)\b'
HURT = r'\bresult(?:o|aron) (?:gravemente )?herid[oa]s?\b'


def norm(text):
    return ''.join(c for c in unicodedata.normalize('NFD',text.lower()) if unicodedata.category(c)!='Mn')


def instant(text):
    value=dt.datetime.fromisoformat(text.replace('Z','+00:00'))
    if value.tzinfo is None: raise ValueError('Clock has no timezone')
    return value.astimezone(dt.timezone.utc)


def sha(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def parse_posts(raw, now):
    """Validate the complete bounded WP response before returning any decisions."""
    if not isinstance(raw,bytes) or len(raw)>8_000_000: raise ValueError('Complete response exceeds pilot byte budget')
    rows=json.loads(raw)
    if not isinstance(rows,list) or len(rows)>100: raise ValueError('Expected at most100 complete WordPress posts')
    result=[];ids=set();urls=set()
    for row in rows:
        if (not isinstance(row,dict) or type(row.get('id')) is not int or row['id']<=0 or row['id'] in ids
                or row.get('type')!='post' or row.get('status')!='publish'):
            raise ValueError('Unexpected or duplicate public post identity')
        ids.add(row['id']);u=urlsplit(row.get('link',''))
        if (u.scheme!='https' or u.netloc!=HOST or u.query or u.fragment or
                not re.fullmatch(r'/20\d\d/\d\d/\d\d/[a-z0-9-]+/',u.path) or row['link'] in urls):
            raise ValueError('Article URL is outside reviewed source')
        urls.add(row['link'])
        for key in ('title','content'):
            if not isinstance(row.get(key),dict) or not isinstance(row[key].get('rendered'),str):
                raise ValueError('Complete title and body required')
        if row['content'].get('protected') is not False: raise ValueError('Protected/unknown article body')
        clocks={}
        for field in ('date','modified'):
            utc=instant(row[field+'_gmt']+'Z');local=dt.datetime.fromisoformat(row[field]).replace(tzinfo=ZONE)
            if utc!=local.astimezone(dt.timezone.utc) or utc>instant(now)+dt.timedelta(minutes=5):
                raise ValueError('Invalid source clock or timezone')
            clocks['publishedAt' if field=='date' else 'modifiedAt']=utc.isoformat(timespec='seconds').replace('+00:00','Z')
        if u.path.split('/')[1:4] != row['date'][:10].split('-'):raise ValueError('URL/publication date conflict')
        title_parts,_,_=article_text(row['title']['rendered']); title=tight(' '.join(title_parts))
        if not title:raise ValueError('Empty source headline')
        error=None
        try: paragraphs,excluded,media=article_text(row['content']['rendered'])
        except ValueError as exc: paragraphs=[];excluded=[];media=False;error=str(exc)
        result.append(dict(guid=str(row['id']),url=row['link'],title=title,paragraphs=paragraphs,
            description='\n'.join(paragraphs),parseError=error,excludedBlocks=excluded,embeddedMedia=media,
            representationFormat='caribe-complete-wordpress-v1',contentHash=sha(row),**clocks))
    return result


def date_evidence(text,published):
    """Resolve explicitly written dates only, using the source's local day.

    Weekday and numeric dates must agree. The output is a day, never an inferred
    incident time. A publication date is never silently used as the event date.
    """
    value=norm(text);base=instant(published).astimezone(ZONE).date();dates=set()
    if re.search(r'\b(?:hace|semanas?|meses?|antepasad[oa]|anterior)\b',re.sub(r'\b(?:el )?pasado (?=(?:'+ '|'.join(WEEKDAYS)+r'|\d))','',value)):
        raise ValueError('Unsupported historical interval')
    for m in re.finditer(r'\b(\d{1,2}) de ('+'|'.join(MONTHS)+r')(?: (?:de|del) (\d{4}))?\b',value):
        year=int(m[3]) if m[3] else base.year
        day=dt.date(year,MONTHS[m[2]],int(m[1]))
        if not m[3] and day>base:raise ValueError('Ambiguous year/future event date')
        if day>base:raise ValueError('Future event date')
        dates.add(day)
    # Unsupported explicit year/date representations may not disappear behind a
    # relative weekday. Future tense cannot be rolled back to last week's day.
    if re.search(r'\b(?:'+'|'.join(WEEKDAYS)+r') pasado\b',value):raise ValueError('Ambiguous previous weekday')
    years={int(x) for x in re.findall(r'\b(?:19|20|21)\d{2}\b',value)}
    if years-{d.year for d in dates}: raise ValueError('Unresolved explicit event year')
    weekday_matches=list(re.finditer(r'\b(?:este|el pasado|pasado|del|el) ('+'|'.join(WEEKDAYS)+r')\b',value))
    for m in weekday_matches:
        weekday=WEEKDAYS[m[1]]
        if not dates and 'pasado' in m[0] and weekday==base.weekday():
            raise ValueError('Same-weekday pasado reference is ambiguous')
        if dates:
            if any(d.weekday()!=weekday for d in dates):raise ValueError('Numeric date/weekday conflict')
        else:dates.add(base-dt.timedelta(days=(base.weekday()-weekday)%7))
    if re.search(r'\bayer\b',value):dates.add(base-dt.timedelta(days=1))
    if re.search(r'\b(?:hoy|esta manana|este dia)\b',value):dates.add(base)
    if len(dates)>1:raise ValueError('Conflicting event days')
    return next(iter(dates)) if dates else None


def suspicious_text(text):
    value=norm(text)
    if re.search(REVIEW,value):return 'correction-contradiction-or-non-event-context'
    if re.search(r'\b(?:otro hecho|otro caso|otro ataque|un segundo ataque|en otra fecha|en una fecha distinta|de otra noticia|cita de otro)\b',value):return 'multiple-event-context'
    if re.search(FOREIGN,value):return 'foreign-or-cross-border-context'
    if re.search(r'\b(?:manana|proxim[oa]|ocurrira|ocurriria|ocurririan|ocurrio en el futuro|sucedera|seria|serian|si ocurriera)\b',value):
        # "la mañana" is a day part, "mañana" alone is tomorrow.
        cleaned=re.sub(r'\b(?:la|esta) manana\b','',value)
        if re.search(r'\b(?:manana|proxim[oa]|ocurrira|ocurriria|ocurririan|sucedera|seria|serian|si ocurriera)\b',cleaned):return 'future-or-conditional-account'
    # Only these documented negative descriptions are compatible with the
    # minimal shooting assertion. Every other negation requires human review.
    cleaned=re.sub(r'\b(?:sin vida|sin mediar palabra|sin que mediara palabra alguna|no presentaba[n]? signos vitales|no se localizaron indicios balisticos|sin signos vitales)\b','',value)
    if re.search(r'\b(?:no|ni|tampoco|sin|ningun[oa]?)\b',cleaned):return 'unresolved-negative-account'
    return None


def event_place(statement):
    """Require grammatical event-location text after the event predicate.

    A victim's origin and the police-office prefix are not event locations.
    Unsupported/competing location clauses remain unassigned, not geocoded.
    """
    value=norm(statement)
    # A report/transfer/other-event clause cannot extend the location evidence.
    if re.search(r'\b(?:segun|informo|reporto|desde|otro hecho|en otro lugar|natural|infarto|colision|accidente)\b',value):return None
    value=re.split(r'\b(?:donde|cuando|tras|luego|para|mientras)\b',value,maxsplit=1)[0]
        # Source's recurring named-place clauses, including qualified neighbourhoods
    # ("Moín de Limón") and a named district/canton. Unknown proper-name clauses
    # cannot inherit a victim's origin elsewhere in the sentence.
    clauses=list(re.finditer(r'\ben\s+([^.;:!?]+)',value))
    if not clauses:return None
    tail=value[clauses[0].start():]
    fragments=re.split(r'\ben\s+',tail)[1:]
    names=[]
    for fragment in fragments:
        fragment=re.sub(r',?\s+lugar$','',fragment.strip(' ,'))
        matches=[name for name in PLACES if re.search(r'\b'+re.escape(norm(name))+r'\b',fragment)]
        if not matches:
            # A generic scene or qualified sublocality may be followed by a
            # second explicit named locality, as in "en Goli, en Batán".
            if not fragment.endswith(',') and not re.fullmatch(r'(?:(?:el sector de |la localidad de |el sector conocido como ).+|la via publica|(?:un|una) (?:local comercial|establecimiento comercial|minisuper|almacen|carniceria) ubicad[oa])',fragment):
                return None
            continue
        if re.search(r'\b(?:policia|agentes|victima|procedente|originario|residente|vecino|oriundo)\b',fragment):return None
        located=sorted((m.start(),m.end()) for name in matches for m in re.finditer(r'\b'+re.escape(norm(name))+r'\b',fragment))
        prefix=fragment[:located[0][0]].strip();suffix=fragment[located[-1][1]:].strip(' ,')
        # Only reviewed geographical qualifiers may lead to a known locality.
        # "San José, junto a la oficina comercial de Batán" cannot become Batán.
        qualifier=r'(?:(?:el|la) (?:sector|localidad|canton|distrito|provincia|comunidad|barrio)(?: central)? de )?'
        local_prefix=r'(?:moin|pocora(?: sur)?|roxana|germania|la rita|pueblo nuevo) de'
        if suffix or prefix and not (re.fullmatch(r'(?:el|la) (?:sector|localidad|canton|distrito|provincia|comunidad|barrio)(?: central)? de',prefix) or re.fullmatch(qualifier+local_prefix,prefix)):
            return None
        names.extend(matches)
    names=list(dict.fromkeys(names))
    if not names:return None
    # Limón is also the province; two distinct canton names are not one event
    # place. These explicit source pairs are retained only as reported text.
    leaves=set(names)-{'Limón'}
    if len(leaves)>1 and leaves not in ({'Batán','Matina'},{'Guápiles','Pococí'}):return None
    return ' / '.join(names)


def primary_sentence(text):
    # Decimal clock abbreviations are source syntax, not sentence boundaries.
    masked=re.sub(r'\b([ap])\.\s*m\.',lambda m:m[1]+'__CLOCK__',text,flags=re.I)
    return re.split(r'[.!?](?:\s|$)',masked,maxsplit=1)[0]


def shooting_event(lead):
    """Return supported outcome and its event clause, never a later gunfire story."""
    statement=primary_sentence(lead)
    investigation=re.match(r'agentes\b',statement) and re.search(r'\binvestigan\b.+\bhomicidio\b.+\bocurrido\b',statement)
    if investigation:
        return 'homicide under investigation',statement[re.search(r'\bocurrido\b',statement).start():]
    if not re.match(r'(?:un|una|dos|tres)\s+(?:hombre|mujer|menor|joven|adulto|nino|ataque)\b',statement):return None,None
    if re.search(r'\b(?:natural|infarto|colision|accidente|atropell|otro hecho|otro ataque)\w*',statement):return None,None
    outcome=re.search(r'\b(?:murio asesinado|fue asesinad[oa]|fueron asesinados)\b',statement)
    if outcome:
        # Do not infer firearm cause from a separate paragraph or headline.
        kind='homicide under investigation'
    else:
        outcome=re.search(r'\b(?:murio|murieron|fallecio|fallecieron|perdio la vida|perdieron la vida|resulto (?:gravemente )?herid[oa])\b',statement)
        if not outcome:return None,None
        tail=statement[outcome.end():]
        if not re.search(r'\b(?:tras (?:ser (?:atacad[oa]s?|victima[s]?) (?:a|con|de)|recibir)|(?:por|con) (?:un[oa]? |multiples )?(?:arma|proyectil|dispar|impacto))',tail):return None,None
        if not re.search(SHOOT,tail):return None,None
        kind='shooting with reported injuries' if 'herid' in outcome[0] else 'fatal shooting'
    # The first "en" after an outcome may follow the causal phrase. Only
    # grammatical location fragments from there are considered, never origins.
    tail=statement[outcome.end():]
    at=re.search(r'\ben\b',tail)
    return (kind,tail[at.start():]) if at else (None,None)


def classify(item):
    if item['parseError']:return None,'complete-narrative-parse-failed'
    paragraphs=item['paragraphs']
    if not paragraphs:return None,'empty-narrative'
    title=norm(item['title']);body=' '.join(paragraphs);lead=norm(paragraphs[0])
    if re.search(UNCERTAIN,title):return None,'headline-event-uncertainty'
    # The original report remains untrusted data; do not execute embedded media.
    problem=suspicious_text(item['title']+' '+body)
    if problem:return None,problem
    if not re.search(r'\b(?:organismo de investigacion judicial|oij)\b',norm(body)):
        return None,'explicit-police-attribution-required'
    if re.search(r'\b(?:terroris\w*|insurgent\w*|guerra|guerrilla|conflicto armado)\b',norm(body)):
        return None,'organized-conflict-requires-separate-review'
    # Repeated OIJ wanted-report structure: the request date is NOT the event.
    wanted=bool(re.match(r'la (?:sub)?delegacion regional\b',lead) and
        re.search(r'\b(?:solicita|solicito|requiere)\b',lead) and re.search(r'\b(?:robo agravado|asalto)\b',lead))
    if wanted:
        match=re.search(r'\b(?:los hechos ocurrieron|el suceso ocurrio)\b(.+)',lead)
        if not match:return None,'wanted-report-event-clause-required'
        event=match.group(0)
        if not re.search(r'\bdonde los (?:sujetos|individuos) sustrajeron\b',event):return None,'affirmative-robbery-event-required'
        if not re.search(r'\b(?:amenazar|agredir|bajo amenaza)\b',event+' '+norm(body)):return None,'robbery-threat-or-assault-evidence-required'
        kind='reported robbery';basis='explicit-source-event-day'
        at=re.search(r'\ben\b',event)
        place=event_place(event[at.start():]) if at else None
        date_text=event
    else:
        if re.search(DISQUALIFY_TITLE,title):return None,'not-a-primary-current-event-headline'
        if not re.search(SHOOT+'|'+FATAL+'|'+HURT+r'|\b(?:asesin\w*|herid[oa]s?)\b',title):return None,'headline-event-agreement-required'
        if re.search(UNCERTAIN,lead):return None,'primary-event-uncertainty'
        if re.search(r'\b(?:hallazgo|localizad[oa]|encontrad[oa]|fue hallad[oa])\b',lead):return None,'discovery-is-not-occurrence'
        kind,event=shooting_event(lead)
        if not event:return None,'affirmative-primary-event-cause-required'
        place=event_place(event);date_text=lead+' '+title;basis='explicit-source-event-day'
    if not place:return None,'explicit-local-event-place-required'
    try:
        day=date_evidence(date_text,item['publishedAt'])
        if not wanted and date_evidence(item['title']+' '+body,item['publishedAt'])!=day:
            raise ValueError('Other narrative weekday/date')
        # Numeric dates anywhere in the complete article cannot contradict the
        # selected event date (wanted-report publication weekdays are not onset).
        full_dates=re.findall(r'\b\d{1,2} de (?:'+'|'.join(MONTHS)+r')(?: (?:de|del) \d{4})?\b',norm(body))
        for written in full_dates:
            if date_evidence(written,item['publishedAt'])!=day:raise ValueError('Other narrative event date')
    except ValueError:return None,'conflicting-or-unsupported-event-date'
    if day is None:return None,'explicit-event-day-required'
    return dict(kind=kind,place=place,eventDay=day.isoformat(),eventTimeBasis=basis,
        attribution='Caribe Actual reports a police account',originalLanguage='es'),None


def collect_caribe(source,raw,now,countries,*,existing=None,reviews=()):
    """Inactive pure collector; caller must recheck retained URLs and commit atomically.

    A latest100 feed is not a correction log or a complete historical baseline.
    Disappearance does not resolve an incident. Explicit reviews own their URLs.
    """
    from public_data_pipeline import make_incident
    if (source.get('id')!=SOURCE_ID or source.get('url')!='https://'+HOST+'/' or source.get('tier')!='local-news'
            or source.get('language')!='es' or source.get('countryCodes')!=['CR']):raise ValueError('Unreviewed source identity')
    items=parse_posts(raw,now)
    if countries.get('CR',{}).get('status')!='active':return dict(incidents=[],decisions=[])
    known=existing or {};changes=[];decisions=[];manual={r['url'] for r in reviews if r['sourceId']==SOURCE_ID}
    for item in items:
        iid='news-'+hashlib.sha256((SOURCE_ID+'|'+item['guid']).encode()).hexdigest()[:20];before=known.get(iid)
        if item['url'] in manual or before and before.get('sourceMetadata',{}).get('parser')!=PARSER:
            decisions.append(dict(url=item['url'],status='review-owned'));continue
        facts,reason=classify(item)
        if (facts and instant(facts['eventDay']+'T00:00:00Z')<instant(now)-dt.timedelta(days=366)):
            facts=None;reason='outside-one-year-pilot-window'
        decision=dict(url=item['url'],guid=item['guid'],status='needs-review',reason=reason,contentHash=item['contentHash'])
        decisions.append(decision)
        meta=before.get('sourceMetadata',{}) if before else {}
        if meta.get('latestRepresentationHash')==item['contentHash']:
            decision.update(status='unchanged',incidentId=iid);continue
        if facts and before:
            if meta.get('facts')!=facts or any(r['url']!=item['url'] for r in before.get('reports',[])):
                facts=None;reason=decision['reason']='changed-event-identity-requires-review'
            elif instant(item['modifiedAt'])<instant(meta['providerModifiedAt']):
                decision['reason']='stale-positive-representation';continue
            elif meta.get('reviewRequired'):
                facts=None;reason=decision['reason']='held-account-needs-human-reinstatement'
        if not facts and not before:continue
        report=dict(id=iid+'-'+item['contentHash'][:12],incidentId=iid,sourceId=SOURCE_ID,title=item['title'],url=item['url'],
            language='es',publishedAt=item['publishedAt'],retrievedAt=now,excerpt='',contentHash=item['contentHash'],
            independentGroup=source.get('independentGroup',SOURCE_ID),sourceMetadata=dict(
                providerModifiedAt=item['modifiedAt'],representationFormat=item['representationFormat'],
                completeAcceptedTextSha256=hashlib.sha256(item['description'].encode()).hexdigest()))
        if facts:
            summary=f"{facts['eventDay']}: Caribe Actual reports a {facts['kind']} in {facts['place']}, citing Costa Rican judicial police."
            provider=dict(parser=PARSER,facts=facts,provider='Caribe Actual',recordId=item['guid'],reviewRequired=False,
                eventTimeBasis=facts['eventTimeBasis'],eventDateKnown=True,eventDatePrecision='day',reportedLocalTimeZone='America/Costa_Rica',
                reportedPlace=facts['place'],summaryMethod='deterministic-source-facts-not-full-translation',summaryLanguage='en',originalLanguage='es',
                evidenceScope='Single publisher account; police attribution is reported by the publisher, not independently corroborated')
            row=make_incident(source,iid,item['title'],summary,countries['CR'],'crime',facts['eventDay']+'T00:00:00Z',
                max(item['publishedAt'],item['modifiedAt']),now,dict(precision='country'),item['url'],provider,item,published=item['publishedAt'])
            row.update(region=facts['place'],evidenceStatus='reported',severity='unknown')
        else:
            row=copy.deepcopy(before);row.update(title=item['title'],summary='The changed source account requires review.',
                evidenceStatus='unverified',state='expired',severity='unknown',location=dict(precision='country'))
            row['sourceMetadata'].update(reviewRequired=True,reviewReason=reason)
        row['sourceMetadata'].update(latestRepresentationHash=item['contentHash'],providerModifiedAt=item['modifiedAt'],providerPublishedAt=item['publishedAt'])
        row['updatedAt']=max(item['publishedAt'],item['modifiedAt'],before['updatedAt'] if before else '',now if before else '')
        row['firstSeenAt']=before['firstSeenAt'] if before else now
        row['historical']=instant(row['occurredAt'])<instant(now)-dt.timedelta(days=30)
        reports=before.get('reports',[]) if before else []
        same=next((r for r in reports if r['id']==report['id']),report)
        row['reports']=[copy.deepcopy(same)]+[copy.deepcopy(r) for r in reports if r['id']!=report['id']]
        if before and before.get('revisions'):row['revisions']=copy.deepcopy(before['revisions'])
        changes.append(row);decision.update(status='automatic-source-record' if facts else 'needs-review',incidentId=iid)
    return dict(incidents=changes,decisions=decisions)
