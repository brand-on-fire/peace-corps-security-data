"""Synthetic factual transformations; complete copyrighted source replay is local.

Provenance: El Mundo /nacionales articles captured2026-10-16, identifiers and
complete hashes in artifacts/mvp-live-20261015/spanish-crime/selected-corpus.json
and *.receipt.json. Fixtures omit names, images and complete published articles.
"""
import copy
import html
import json
from pathlib import Path
import unittest

from public_news_elmundo import SOURCE_ID,PARSER,ADMIN_PLACES,collect_elmundo,parse_article

SOURCE=dict(id=SOURCE_ID,name='Diario El Mundo',url='https://diario.elmundo.sv/',language='es',tier='local-news',countryCodes=['SV'])
COUNTRIES={'SV':dict(code='SV',name='El Salvador',region='Americas',status='active')}
NOW='2026-10-16T01:06:00Z'
URL='https://diario.elmundo.sv/nacionales/fixture-crime'
DATE='2026-10-08T10:00:00-06:00'
CASE={
 'robbery':('Señalan a sospechoso de robar a punta de pistola en Colón',
    'Durante un patrullaje preventivo en Colón, La Libertad Oeste, un equipo recibió un aviso: un hombre había robado a un ciudadano a punta de pistola.'),
 'fatal':('Detienen a sospechoso de matar a una persona en Ciudad Barrios',
    'Reportamos un homicidio en Ciudad Barrios, San Miguel Norte. Un hombre atacó con arma blanca a familiares y provocó un incendio en la vivienda. Durante el ataque asesinó a una persona.'),
 'assault':('Detienen a sospechoso de golpear a una persona en Acajutla',
    'Un hombre golpeó con los puños el rostro de otro hombre en Acajutla, Sonsonate Oeste. La víctima resultó con lesiones y fue llevado a un hospital.'),
 'domestic':('Detienen a sospechoso de golpear con taladro a su pareja en San Juan Opico',
    'Un hombre golpeó a su compañera de vida en la cara con un taladro. Acudimos al lugar de los hechos, en Ciudad Marsella, San Juan Opico, La Libertad Centro. La víctima fue trasladada al hospital.'),
 'threat':('PNC captura a persona que amenaza con un corvo en Sensuntepeque',
    'Fue grabado amenazando con un corvo a unas personas frente a su vivienda. Los hechos ocurrieron el 03 octubre, en Sensuntepeque, Cabañas Este.'),
 'bus':('Arrestan a dos personas tras pelea en Santa Tecla',
    'Dos personas protagonizaron una pelea al interior de un bus. Fuimos alertados e intervenimos sobre la carretera Panamericana, en Santa Tecla, La Libertad Sur.'),
 'street':('Detienen a personas tras riña en Zacatecoluca',
    'Varios sujetos protagonizaron una riña callejera, agrediéndose con puños. Sucedió afuera de un bar en Zacatecoluca, La Paz Este.'),
}


def article(case='robbery',*,title=None,quote=None,extra='',url=URL,status='2110000000000000001',published=DATE,modified=DATE,visible_extra='',ld_extra='',police=True,lead=None):
    base_title,base_quote=CASE[case];title=title or base_title;quote=base_quote if quote is None else quote
    if lead is None:
        lead=('La Policía Nacional Civil informó de un sospechoso señalado de robar “a punta de pistola”, a otro ciudadano en el distrito de Colón en La Libertad Oeste.' if case=='robbery' else
              'La Policía Nacional Civil informó de una pelea a golpes en el interior de una unidad de la ruta 205 en el distrito de Santa Tecla, La Libertad Sur.' if case=='bus' else
              'Diario El Mundo publica un informe de la Policía Nacional Civil.')
    lead+='\n'
    body=lead+quote+'\n'+extra
    d=dict(**{'@type':'NewsArticle'},url=url,mainEntityOfPage={'@id':url},headline=title,
        inLanguage='es',publisher={'name':'Diario El Mundo','url':'https://diario.elmundo.sv/'},
        datePublished=published,dateModified=modified,articleBody=body+ld_extra)
    link=f'<a href="https://x.com/{"PNCSV" if police else "other"}/status/{status}"></a>'
    source=(f'<link rel="canonical" href="{url}"><meta property="og:url" content="{url}">'
        f'<meta property="og:title" content="{html.escape(title,quote=True)}"><meta property="article:section" content="Nacionales">'
        f'<meta property="article:published_time" content="{published}"><meta property="article:modified_time" content="{modified}">'
        f'<script type="application/ld+json">{json.dumps(d,ensure_ascii=False)}</script><div class="article-body">'
        f'<p>{html.escape(lead)}</p><blockquote>{html.escape(quote)}{link}</blockquote><p>{html.escape(extra)}</p>{visible_extra}</div>')
    return dict(url=url,raw=source.encode())


def collect(documents=None,**kw):return collect_elmundo(SOURCE,[article()] if documents is None else documents,NOW,COUNTRIES,**kw)


def reviewed_geography():
    """The same complete versioned layers shipped to the public collector."""
    from public_data_pipeline import load_boundary_layer
    root=Path(__file__).resolve().parents[1]
    if (root/'config/admin-places.json').exists():
        areas=json.loads((root/'config/admin-places.json').read_text());folder=root/'config/map-areas'
    else:
        areas=json.loads((root/'data/admin-areas.json').read_text())['areas'];folder=root/'data/map-areas'
    return dict(areas=areas,boundary_layers={level:load_boundary_layer(folder/f'SV-{level}.geojson') for level in ('ADM1','ADM2')})


class ElMundoTests(unittest.TestCase):
    def test_seven_actual_narrative_shapes_preserve_attribution_and_original_language(self):
        for case in CASE:
            with self.subTest(case=case):
                row=collect([article(case)])['incidents'][0]
                self.assertEqual((row['category'],row['severity'],row['evidenceStatus']),('crime','unknown','reported'))
                self.assertEqual(row['title'],CASE[case][0]);self.assertEqual(row['reports'][0]['language'],'es')
                self.assertIn('citing El Salvador',row['summary']);self.assertEqual(row['reports'][0]['excerpt'],'')
                self.assertEqual(row['location'],{'precision':'country'});self.assertNotIn('adminAssignment',row)
                self.assertEqual(row['sourceMetadata']['summaryMethod'],'deterministic-reviewed-fact-template')
                if case in ('assault','domestic'):
                    self.assertIn('The victim was taken to hospital.',row['summary'])
                    self.assertNotIn('hospital in ',row['summary'])
                if case!='threat':
                    self.assertEqual(row['occurredAt'],'2026-10-08T16:00:00Z')
                    self.assertEqual(row['sourceMetadata']['eventTimeBasis'],'publication')
                    self.assertFalse(row['sourceMetadata']['eventDateKnown']);self.assertIn('event date was not specified',row['summary'])
                else:
                    self.assertEqual(row['occurredAt'],'2026-10-03T00:00:00Z')
                    self.assertTrue(row['sourceMetadata']['eventDateKnown'])

    def test_bus_fight_is_crime_and_gang_labels_do_not_invent_armed_conflict(self):
        self.assertEqual(collect([article('bus')])['incidents'][0]['category'],'crime')
        row=collect([article(extra='El acusado pertenece presuntamente a la MS13 y será procesado por organizaciones terroristas.')])['incidents'][0]
        self.assertEqual(row['category'],'crime');self.assertNotIn('terrorist',row['summary'])
        self.assertEqual(collect([article(extra='El hecho fue un ataque militar durante un conflicto armado.')])['incidents'],[])

    def test_arrest_time_does_not_become_event_time_or_guilt(self):
        row=collect([article(extra='El sospechoso fue detenido el lunes 5 de octubre; no ha sido condenado.')])['incidents'][0]
        self.assertEqual(row['occurredAt'],'2026-10-08T16:00:00Z');self.assertIn('reported armed robbery',row['summary'])
        self.assertNotIn('guilty',row['summary']);self.assertNotIn('arrested',row['summary'])

    def test_known_event_day_invalid_future_older_or_unsupported_is_held(self):
        for value in ['31 febrero','20 octubre','03 octubre de 2024','ayer','el año pasado']:
            q=CASE['threat'][1].replace('03 octubre',value)
            with self.subTest(value=value):self.assertEqual(collect([article('threat',quote=q)])['incidents'],[])
        q=CASE['robbery'][1]+' El robo ocurrió en 2020.'
        self.assertEqual(collect([article(quote=q)])['incidents'],[])
        self.assertEqual(collect([article(extra='El robo ocurrió en 2010.')])['incidents'],[])

    def test_whole_text_denial_simulation_foreign_and_conditional_claims_hold(self):
        for suffix in ['No ocurrió este hecho.','Fue una simulación.','El reporte falso fue retirado.',
            'La noticia es una rectificación.','El ataque ocurrió en Guatemala.','Es un ejemplo didáctico.',
            'El ataque no fue asesinado.','Se trató de un conflicto armado.']:
            with self.subTest(suffix=suffix):self.assertEqual(collect([article(extra='Contexto neutral. '*9000+suffix)])['incidents'],[])
        for q in [CASE['robbery'][1].replace('había robado','no había robado'),CASE['assault'][1].replace('golpeó','no golpeó'),CASE['robbery'][1].replace('había robado','supuestamente había robado')]:
            self.assertEqual(collect([article(quote=q)])['incidents'],[])

    def test_independent_review_conditional_old_foreign_and_unrelated_quotes_are_held(self):
        for prefix in ['Si se confirmara que ', 'Este relato corresponde a un caso de 2010. ',
            'El robo sucedió en Panamá. ', 'La cita siguiente corresponde a otro caso sin relación con la detención. ']:
            with self.subTest(prefix=prefix):
                self.assertEqual(collect([article(quote=prefix+CASE['robbery'][1])])['incidents'],[])

    def test_patrol_arrest_or_reporting_location_is_not_the_crime_location(self):
        for case,lead in [('robbery','La PNC detuvo en Colón a un sospechoso de un robo; el lugar del robo no fue informado.'),
            ('robbery','La oficina de policía en Colón informó de un robo ocurrido en otra localidad.'),
            ('bus','La PNC interceptó en Santa Tecla a dos sospechosos de una pelea en un autobús.')]:
            with self.subTest(case=case,lead=lead):
                self.assertEqual(collect([article(case,lead=lead)])['incidents'],[])

    def test_no_police_account_no_event_clause_no_unique_local_place_no_admission(self):
        for doc in [article(police=False),article(quote='PNC anunció una captura en Colón, La Libertad Oeste.'),
            article(quote=CASE['robbery'][1].replace('La Libertad Oeste','La Libertad Este')),
            article(quote=CASE['robbery'][1]+' Otro hecho ocurrió en Acajutla, Sonsonate Oeste.'),
            article(title='Sentencian a una persona por robar a punta de pistola en Colón'),
            article(title='Arrestan a una persona con orden judicial por robar a punta de pistola en Colón')]:
            with self.subTest(doc=doc['raw']):self.assertEqual(collect([doc])['incidents'],[])

    def test_whole_batch_fails_before_any_output_for_incomplete_changed_identity_or_bad_clock(self):
        for bad in [article(url='https://other.example/nacionales/fake'),article(visible_extra='Texto ausente del JSON'),
            article(ld_extra='Texto ausente del cuerpo'),article(published='2026-10-18T00:00:00-06:00'),
            dict(url=URL+'-broken',raw=b'<html>Loading article</html>')]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):collect([article(url=URL+'-first'),bad])
        with self.assertRaises(ValueError):collect([article(),article()])

    def test_html_and_jsonld_double_escaped_space_equivalence_is_lossless(self):
        doc=article(extra='Un espacio &nbsp; sin contenido oculto.')
        self.assertIsNotNone(parse_article(doc['raw'],doc['url'],NOW))

    def test_actual_cms_nested_body_and_encoded_headline_keep_complete_identity(self):
        title=CASE['robbery'][0]+' "Informe"'
        doc=article(title=title)
        raw=doc['raw'].decode().replace('"headline": '+json.dumps(title,ensure_ascii=False),
            '"headline": '+json.dumps(html.escape(title,quote=True),ensure_ascii=False))
        raw=raw.replace('<div class="article-body">','<div class="article-body"><div class="article-body">')+'</div>'
        parsed=parse_article(raw.encode(),URL,NOW);self.assertEqual(parsed['title'],title)
        self.assertEqual(parsed['description'],parse_article(doc['raw'],URL,NOW)['description'])
        with self.assertRaisesRegex(ValueError,'identity/body'):
            parse_article(doc['raw']+b'<div class="article-body">Different additional body</div>',URL,NOW)

    def test_same_police_account_republication_merges_reports_but_not_different_events(self):
        rows=collect([article(),article(url=URL+'-update',published='2026-10-09T10:00:00-06:00',modified='2026-10-09T10:00:00-06:00')])['incidents']
        self.assertEqual(len(rows),1);self.assertEqual(len(rows[0]['reports']),2)
        self.assertEqual(rows[0]['occurredAt'],'2026-10-08T16:00:00Z')
        self.assertEqual(len(collect([article(),article(url=URL+'-another',status='2110000000000000002')])['incidents']),2)
        conflicting=collect([article(),article('assault',url=URL+'-conflict')])['incidents'][0]
        self.assertEqual(conflicting['evidenceStatus'],'unverified')

    def test_same_url_changed_primary_account_is_withheld_with_history(self):
        first=collect()['incidents'][0]
        changed=collect([article(status='2110000000000000002')],existing={first['id']:first})['incidents'][0]
        self.assertEqual(changed['id'],first['id']);self.assertEqual(changed['state'],'expired')
        self.assertEqual(len(changed['reports']),2);self.assertEqual(changed['sourceMetadata']['reviewReason'],'primary-police-account-changed')

    def test_correction_and_restore_dedup_reports_even_with_unchanged_or_rolled_back_clock(self):
        first=collect()['incidents'][0];known={first['id']:first};original=copy.deepcopy(first['reports'][0])
        for suffix in [' No ocurrió este hecho.',' Fue una simulación.',' No ocurrió este hecho.']:
            changed=collect([article(extra=suffix,modified='2026-10-07T10:00:00-06:00')],existing=known)['incidents'][0]
            self.assertEqual((changed['state'],changed['evidenceStatus']),('expired','unverified'))
            self.assertEqual(changed['updatedAt'],NOW);self.assertEqual(changed['firstSeenAt'],first['firstSeenAt'])
            known={changed['id']:changed}
        self.assertEqual(len(changed['reports']),3);self.assertEqual(len({r['id'] for r in changed['reports']}),3)
        restored=collect(existing=known)['incidents'][0]
        self.assertEqual(restored['state'],'reported');self.assertEqual(restored['reports'][0],original)
        self.assertEqual(collect(existing={restored['id']:restored})['incidents'],[])

    def test_other_review_ownership_inactive_country_and_absent_article_preserved(self):
        first=collect()['incidents'][0]
        self.assertEqual(collect(existing={first['id']:first},reviews=[dict(sourceId=SOURCE_ID,url=URL)])['incidents'],[])
        first['sourceMetadata']['parser']='human-review'
        self.assertEqual(collect(existing={first['id']:first})['incidents'],[])
        self.assertEqual(collect([])['incidents'],[])
        self.assertEqual(collect_elmundo(SOURCE,[article()],NOW,{'SV':dict(COUNTRIES['SV'],status='suspended')})['incidents'],[])

    def test_reviewed_seven_place_pairs_use_area_names_without_coordinates_or_impact_claim(self):
        geography=reviewed_geography();rows=[]
        for case in CASE:
            row=collect([article(case)],**geography)['incidents'][0];rows.append(row)
            parent,name,child,_=ADMIN_PLACES[row['region']]
            self.assertEqual((row['admin1Code'],row['admin1Name']),(parent,name))
            self.assertEqual(row.get('admin2Code'),child)
            self.assertEqual(row['location'],{'precision':'area'})
            self.assertEqual(row['adminAssignment'],dict(method='explicit_source_place_names',confidence='source_reported_area',pointInferred=False))
            provenance=row['sourceMetadata']['administrativeAssociation']
            self.assertFalse(provenance['currentBoundaryVerified']);self.assertFalse(provenance['impactBoundary'])
            self.assertEqual(provenance['levels']['ADM1']['boundaryYearRepresented'],'2021')
            if child:self.assertEqual(provenance['levels']['ADM2']['boundaryYearRepresented'],'2007')
            else:self.assertNotIn('ADM2',provenance['levels'])
        self.assertEqual(sum('admin2Code' in r for r in rows),6)

    def test_missing_changed_or_ambiguous_complete_geography_fails_closed(self):
        base=reviewed_geography();sv=[a for a in base['areas'] if a['countryCode']=='SV']
        changes=[]
        duplicate=copy.deepcopy(base);colon=next(a for a in duplicate['areas'] if a['name']=='Colón' and a['countryCode']=='SV')
        duplicate['areas'].append(dict(colon,id='duplicate-same-department'));changes.append(duplicate)
        renamed=copy.deepcopy(base);next(a for a in renamed['areas'] if a['id']==colon['id'])['name']='Colón changed';changes.append(renamed)
        reparented=copy.deepcopy(base);next(a for a in reparented['areas'] if a['id']==colon['id'])['parentId']='country:SV';changes.append(reparented)
        changed_geometry=copy.deepcopy(base);changed_geometry['boundary_layers']['ADM2']['features'][0]['geometry']['coordinates']=[];changes.append(changed_geometry)
        changes.extend([dict(areas=sv,boundary_layers={}),dict(areas=[],boundary_layers=base['boundary_layers']),
            dict(areas=[None],boundary_layers=base['boundary_layers']),dict(areas=sv,boundary_layers={'ADM1':None,'ADM2':None})])
        for changed in changes:
            with self.subTest(changed=changes.index(changed)):
                row=collect(**changed)['incidents'][0]
                self.assertEqual(row['location'],{'precision':'country'});self.assertNotIn('adminAssignment',row)
                self.assertNotIn('administrativeAssociation',row['sourceMetadata'])
                self.assertEqual(row['sourceMetadata']['administrativeMatchStatus'],'unverified-dataset-or-place')

    def test_geography_reprojection_preserves_source_evidence_dates_and_is_idempotent(self):
        geography=reviewed_geography();first=collect()['incidents'][0]
        now='2026-10-16T02:06:00Z'
        result=collect_elmundo(SOURCE,[article()],now,COUNTRIES,existing={first['id']:first},**geography)
        improved=result['incidents'][0]
        self.assertEqual(improved['reports'],first['reports']);self.assertEqual(improved['occurredAt'],first['occurredAt'])
        self.assertEqual(improved['firstSeenAt'],first['firstSeenAt']);self.assertEqual(improved['updatedAt'],now)
        for key in ('providerPublishedAt','providerModifiedAt','latestRepresentations'):
            self.assertEqual(improved['sourceMetadata'][key],first['sourceMetadata'][key])
        self.assertNotIn('changeObservedAt',improved['sourceMetadata'])
        self.assertEqual(collect(existing={improved['id']:improved},**geography)['incidents'],[])
        unavailable=collect(existing={improved['id']:improved})['incidents'][0]
        self.assertEqual(unavailable['location'],{'precision':'country'});self.assertNotIn('admin2Code',unavailable)
        self.assertEqual(unavailable['reports'],first['reports'])
        prior=unavailable['sourceMetadata']['administrativeHistory'][-1]['previous']
        self.assertEqual(prior['admin2Code'],improved['admin2Code'])
        self.assertEqual(collect(existing={unavailable['id']:unavailable})['incidents'],[])

    def test_geographic_correction_clears_old_assignment_keeps_evidence_and_restores_once(self):
        geography=reviewed_geography();first=collect(**geography)['incidents'][0]
        first['revisions']=[dict(at=NOW,note='Prior complete source evidence.',previousReports=copy.deepcopy(first['reports']))]
        held=collect([article(extra='No ocurrió este hecho.')],existing={first['id']:first},**geography)['incidents'][0]
        self.assertEqual(held['location'],{'precision':'country'});self.assertNotIn('admin1Code',held)
        self.assertNotIn('administrativeAssociation',held['sourceMetadata'])
        self.assertEqual(held['revisions'],first['revisions']);self.assertIn(first['reports'][0],held['reports'])
        self.assertEqual(held['sourceMetadata']['administrativeHistory'][-1]['previous']['admin2Code'],first['admin2Code'])
        restored=collect(existing={held['id']:held},**geography)['incidents'][0]
        self.assertEqual(restored['admin2Code'],first['admin2Code']);self.assertEqual(len(restored['reports']),2)
        self.assertEqual(collect(existing={restored['id']:restored},**geography)['incidents'],[])

    def test_unselected_retained_rows_reproject_without_new_reports_or_source_dates(self):
        geography=reviewed_geography()
        original=collect([article(),article('assault',url=URL+'-second',status='2110000000000000002')])['incidents']
        known={r['id']:r for r in original}
        for kwargs,precision in [(geography,'area'),({},'country'),(geography,'area')]:
            result=collect([],existing=known,**kwargs)
            self.assertEqual(result['decisions'],[]);self.assertEqual(len(result['incidents']),2)
            for row in result['incidents']:
                before=known[row['id']]
                self.assertEqual(row['location'],{'precision':precision})
                for key in ('reports','occurredAt','firstSeenAt'):self.assertEqual(row[key],before[key])
                for key in ('providerPublishedAt','providerModifiedAt','latestRepresentations'):
                    self.assertEqual(row['sourceMetadata'][key],before['sourceMetadata'][key])
            known={r['id']:r for r in result['incidents']}
            self.assertEqual(collect([],existing=known,**kwargs)['incidents'],[])


if __name__=='__main__':unittest.main()
