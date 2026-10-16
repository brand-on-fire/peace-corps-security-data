"""Factual synthetic fixtures; complete copyrighted provider corpus stays ignored."""
import copy
import json
from pathlib import Path
import unittest
from public_news_caribe import collect_caribe, parse_posts, classify, article_text, PARSER, SOURCE_ID

NOW='2026-10-16T04:30:00Z'
SOURCE=dict(id=SOURCE_ID,name='Caribe Actual',url='https://caribeactual.com/',tier='local-news',language='es',countryCodes=['CR'])
COUNTRIES={'CR':dict(code='CR',name='Costa Rica',region='Central America',status='active')}

def post(identifier=81001,title='Hombre murió asesinado a balazos en Matina este martes',body=None,day='2026-10-13',modified=None):
    body=body or ('<p>Un hombre murió este martes tras ser atacado a balazos en Matina. '
        'El Organismo de Investigación Judicial (OIJ) investiga el ataque.</p>'
        '<p>Según el informe preliminar, los agentes recolectaron indicios balísticos. La identidad de los responsables está bajo investigación.</p>')
    return dict(id=identifier,type='post',status='publish',link=f'https://caribeactual.com/{day.replace("-","/")}/noticia-{identifier}/',
        date=day+'T12:00:00',date_gmt=day+'T18:00:00',modified=modified or day+'T12:00:00',modified_gmt=(modified or day+'T12:00:00').replace('T12:','T18:'),
        title={'rendered':title},content={'rendered':body,'protected':False})

def run(rows,existing=None,now=NOW,reviews=(),countries=None):
    return collect_caribe(SOURCE,json.dumps(rows,ensure_ascii=False).encode(),now,countries or COUNTRIES,existing=existing,reviews=reviews)

class CaribeTests(unittest.TestCase):
    def test_reusable_shooting_template_not_fixed_article_title_or_hash(self):
        rows=[post(),post(81002,title='Mujer herida tras ataque en Guácimo este martes',body='<p>Una mujer resultó herida tras ser atacada con arma de fuego este martes en Guácimo. El OIJ investiga las causas.</p>')]
        result=run(rows);self.assertEqual(len(result['incidents']),2)
        for row in result['incidents']:
            self.assertEqual(row['category'],'crime');self.assertEqual(row['evidenceStatus'],'reported');self.assertEqual(row['severity'],'unknown')
            self.assertEqual(row['location'],{'precision':'country'});self.assertNotIn('admin1Id',row);self.assertEqual(row['reports'][0]['language'],'es')
            self.assertIn('Caribe Actual reports',row['summary']);self.assertEqual(row['sourceMetadata']['summaryLanguage'],'en')
            self.assertEqual(row['occurredAt'],'2026-10-13T00:00:00Z')

    def test_origin_office_unknown_and_competing_locations_are_held(self):
        for text in [
            'Un hombre de Matina murió tras ser atacado a balazos en San José.',
            'Un hombre murió tras ser atacado a balazos en Matina y en San José.',
            'Un hombre murió tras ser atacado a balazos en Matina y San José.',
            'Un hombre murió tras ser atacado a balazos en Matina, San José.',
            'Un hombre murió tras ser atacado a balazos en Matina y Guácimo.',
            'Un hombre murió tras ser atacado a balazos en una localidad desconocida; policías de Matina informaron el caso.',
        ]:
            with self.subTest(text=text):
                row=post(body='<p>'+text+' El OIJ investiga el ataque este martes.</p>')
                self.assertFalse(run([row])['incidents'])

    def test_death_and_unrelated_gunfire_are_not_causally_joined(self):
        for text in [
            'Un hombre murió de un infarto este martes en Matina. Los vecinos escucharon disparos en Guácimo.',
            'Un hombre murió en una colisión este martes en Matina. Se reportaron disparos cerca del lugar.',
            'Un hombre murió este martes en Matina. La policía encontró balas en su vehículo.',
        ]:
            with self.subTest(text=text):self.assertFalse(run([post(body='<p>'+text+' El OIJ investiga.</p>')])['incidents'])

    def test_uncertain_title_and_same_weekday_pasado_require_review(self):
        for title in ['Hombre habría muerto en Matina', 'Posible homicidio en Matina']:
            self.assertFalse(run([post(title=title)])['incidents'])
        row=post();row['content']['rendered']=row['content']['rendered'].replace('este martes','el pasado martes')
        self.assertFalse(run([row])['incidents'])

    def test_independent_full_article_semantic_counterexamples(self):
        root=Path(__file__).resolve().parents[1]/'artifacts/caribe-crime-20261016'
        if not (root/'posts.response').exists():self.skipTest('Complete provider evidence intentionally local')
        original=next(r for r in json.loads((root/'posts.response').read_bytes()) if r['id']==73989)
        variants=[]
        for addition in ['El OIJ retiró el comunicado que sustentaba esta noticia.', 'El relato describe una recreación teatral.']:
            changed=copy.deepcopy(original);changed['content']['rendered']+='<p>'+addition+'</p>';variants.append(changed)
        changed=copy.deepcopy(original);changed['content']['rendered']=changed['content']['rendered'].replace('en el sector de Goli, en Batán','en San José, junto a la oficina comercial de Batán');variants.append(changed)
        changed=copy.deepcopy(original)
        for key in ('title','content'):changed[key]['rendered']=changed[key]['rendered'].replace('este martes','el martes de hace dos semanas')
        variants.append(changed)
        for row in variants:
            with self.subTest(title=row['title']['rendered']):self.assertFalse(run([row])['incidents'])

    def test_withdrawal_recreation_mixed_office_and_elapsed_week_synthetic(self):
        # Portable regressions for the same independent-review failures.
        bodies=[
            '<p>Un hombre murió este martes tras ser atacado a balazos en San José, junto a la oficina comercial de Batán. El OIJ investiga.</p>',
            '<p>Un hombre murió el martes de hace dos semanas tras ser atacado a balazos en Matina. El OIJ investiga.</p>',
        ]
        for body in bodies:self.assertFalse(run([post(body=body)])['incidents'])
        for addition in ['El OIJ retiró el comunicado que sustentaba esta noticia.','El relato describe una recreación teatral.']:
            row=post();row['content']['rendered']+='<p>'+addition+'</p>';self.assertFalse(run([row])['incidents'])

    def test_source_local_midnight_and_actual_day_not_utc_publication_day(self):
        row=post(day='2026-10-13');row.update(date='2026-10-13T21:00:00',date_gmt='2026-10-14T03:00:00',modified='2026-10-13T21:00:00',modified_gmt='2026-10-14T03:00:00')
        self.assertEqual(run([row])['incidents'][0]['occurredAt'],'2026-10-13T00:00:00Z')

    def test_explicit_historical_robbery_not_new_police_request(self):
        row=post(title='OIJ busca sospechosos de asalto en Guácimo',body='<p>La Delegación Regional de Pococí y Guácimo del OIJ solicita la colaboración para identificar sospechosos de robo agravado. Los hechos ocurrieron el pasado 12 de diciembre de 2025 en Guácimo, donde los sujetos sustrajeron artículos tras amenazar al dependiente.</p>')
        out=run([row])['incidents'][0];self.assertEqual(out['occurredAt'],'2025-12-12T00:00:00Z');self.assertTrue(out['historical'])
        self.assertIn('reported robbery',out['summary']);self.assertNotIn('armed robbery',out['summary'])

    def test_office_place_alone_cannot_locate_robbery(self):
        row=post(title='OIJ busca sospechosos de asalto',body='<p>La Delegación Regional de Pococí y Guácimo del OIJ solicita ayuda por robo agravado. Los hechos ocurrieron el pasado 12 de diciembre de 2025 en una localidad sin identificar, donde los sujetos sustrajeron artículos tras amenazar al dependiente.</p>')
        self.assertFalse(run([row])['incidents'])

    def test_full_narrative_negative_and_contradiction_variants(self):
        additions=['El OIJ aclaró que no ocurrió el ataque.','El hecho era falso.','La narración es un ejemplo hipotético.',
            'Este es un simulacro.','Los informes originales fueron errores.','La víctima no recibió disparos.',
            'El hecho sucedió en Panamá.','Se trata de otro ataque.', 'El ataque ocurrió el 13 de octubre de 2010.', 'El ataque ocurrió el 13 de octubre de 1998.',
            'El ataque ocurrió el pasado lunes.', 'El ataque ocurrirá el próximo viernes.',
            'El evento citado es de otra noticia.']
        for text in additions:
            with self.subTest(text=text):
                row=post();row['content']['rendered']+='<p>'+text+'</p>'
                self.assertFalse(run([row])['incidents'])

    def test_primary_uncertainty_and_noncrime_never_become_fatal_shooting(self):
        for title,body in [
            ('Capturan sospechoso de homicidio en Matina','El OIJ detuvo este martes a un sospechoso en Matina por un homicidio a balazos.'),
            ('Hallan hombre muerto en Matina','Un hombre fue localizado sin vida este martes en Matina. El OIJ investiga el homicidio con disparos.'),
            ('Hombre podría haber sido asesinado en Matina','Un hombre habría muerto tras disparos este martes en Matina. El OIJ investiga.'),
            ('Colisión fatal en Matina','Un hombre murió tras una colisión este martes en Matina. El OIJ investiga.'),
            ('Aumentan homicidios en Matina','El OIJ contabiliza diez homicidios a balazos este martes en Matina.'),
            ('Ataque terrorista en Matina','Un hombre murió tras disparos este martes en Matina. El OIJ investiga un ataque terrorista.'),
        ]:
            with self.subTest(title=title): self.assertFalse(run([post(title=title,body='<p>'+body+'</p>')])['incidents'])

    def test_actual_headline_body_weekday_conflict_is_held(self):
        row=post(title='Hombre herido en Guácimo este domingo',day='2026-09-28',body='<p>Un hombre resultó herido con arma de fuego este lunes en Guácimo. El OIJ investiga el ataque.</p>')
        self.assertFalse(run([row])['incidents'])

    def test_date_missing_future_or_wrong_weekday_is_not_invented(self):
        for body in ['Un hombre murió tras disparos en Matina. El OIJ investiga.',
            'Un hombre murió tras disparos el martes 14 de octubre de 2026 en Matina. El OIJ investiga.',
            'Un hombre murió tras disparos el 20 de octubre de 2026 en Matina. El OIJ investiga.']:
            self.assertFalse(run([post(title='Fatal ataque en Matina',body='<p>'+body+'</p>')])['incidents'])

    def test_known_related_cards_are_excluded_but_trailing_correction_is_read(self):
        row=post();row['content']['rendered']+='<div class="wp-block-newspack-blocks-homepage-articles"><article><p>Otra noticia falsa: el asesinato no ocurrió.</p></article></div>'
        self.assertEqual(len(run([row])['incidents']),1)
        row['content']['rendered']+='<p>El ataque no ocurrió.</p>'
        self.assertFalse(run([row])['incidents'])

    def test_arbitrary_class_cannot_hide_a_correction(self):
        row=post();row['content']['rendered']+='<div class="ignore-me"><p>El ataque no ocurrió.</p></div>'
        self.assertFalse(run([row])['incidents'])

    def test_unclosed_narrative_is_held_without_parsing_prefix(self):
        row=post();row['content']['rendered']+='<p>Incomplete'
        self.assertFalse(run([row])['incidents'])

    def test_malformed_batch_is_atomic_and_no_truncation(self):
        for mutate in (lambda row:row['content'].update(protected=True),lambda row:row.update(link='https://other.test/'),lambda row:row.update(date_gmt='2026-10-13T19:00:00')):
            bad=post(81002);mutate(bad)
            with self.assertRaises(ValueError):run([post(),bad])
        with self.assertRaises(ValueError):parse_posts(b' '*8_000_001,NOW)
        with self.assertRaises(ValueError):run([post(),post()])

    def test_reusable_update_retains_complete_representation_evidence(self):
        original=post();before=run([original])['incidents'][0];updated=copy.deepcopy(original)
        updated['content']['rendered']+='<p>Los agentes continúan la investigación del mismo ataque.</p>'
        out=run([updated],{before['id']:before})['incidents'][0]
        self.assertEqual(len(out['reports']),2);self.assertEqual(out['firstSeenAt'],before['firstSeenAt']);self.assertEqual(out['occurredAt'],before['occurredAt'])
        self.assertEqual(out['updatedAt'],NOW);self.assertEqual(out['reports'][0]['publishedAt'],before['reports'][0]['publishedAt'])
        self.assertFalse(run([updated],{out['id']:out})['incidents'])

    def test_same_or_regressing_cms_correction_withholds_without_losing_evidence(self):
        for modified in [None,'2026-10-12T12:00:00']:
            before=run([post()])['incidents'][0];updated=post(modified=modified);updated['content']['rendered']+='<p>El informe anterior era falso.</p>'
            out=run([updated],{before['id']:before})['incidents'][0]
            self.assertEqual((out['state'],out['evidenceStatus']),('expired','unverified'));self.assertEqual(len(out['reports']),2)
            self.assertEqual(out['updatedAt'],NOW);self.assertEqual(out['occurredAt'],before['occurredAt'])
            again=run([post()],{out['id']:out})
            self.assertTrue(not again['incidents'] or again['incidents'][0]['evidenceStatus']=='unverified')

    def test_changed_facts_and_same_id_url_require_review(self):
        before=run([post()])['incidents'][0]
        for mutate in (lambda r:r['content'].update(rendered=r['content']['rendered'].replace('Matina','Guácimo')),lambda r:r.update(link=r['link'].replace('noticia','otra'))):
            row=post();mutate(row);out=run([row],{before['id']:before})['incidents'][0]
            self.assertEqual(out['evidenceStatus'],'unverified')

    def test_absence_inactive_country_and_manual_review_preserve_existing(self):
        row=post();before=run([row])['incidents'][0];known={before['id']:before}
        self.assertFalse(run([],known)['incidents']);self.assertEqual(known[before['id']],before)
        self.assertFalse(run([row],reviews=[dict(sourceId=SOURCE_ID,url=row['link'])])['incidents'])
        self.assertFalse(run([row],countries={'CR':{'status':'suspended'}})['incidents'])
        review=copy.deepcopy(before);review['sourceMetadata']['parser']='reviewer'
        self.assertFalse(run([row],{review['id']:review})['incidents'])

    def test_actual_complete_provider_replay_when_local_capture_exists(self):
        root=Path(__file__).resolve().parents[1]/'artifacts/caribe-crime-20261016'
        if not (root/'posts.response').exists():self.skipTest('Complete provider evidence intentionally local')
        seen={};accepted={};reasons={}
        for name in ['posts','robberies','abductions']:
            for item in parse_posts((root/(name+'.response')).read_bytes(),NOW):seen[item['guid']]=item
        for iid,item in seen.items():
            facts,reason=classify(item);reasons[iid]=reason
            if facts:accepted[iid]=facts
        self.assertEqual(len(seen),115)
        self.assertGreaterEqual(len(accepted),10)
        self.assertEqual(accepted['73989']['eventDay'],'2026-10-13')
        self.assertEqual(accepted['73460']['kind'],'shooting with reported injuries')
        self.assertEqual(accepted['68542']['eventDay'],'2025-12-12')
        for iid in ['73658','73948','73763','73450','73519','73688']:
            self.assertNotIn(iid,accepted)

if __name__=='__main__':unittest.main()
