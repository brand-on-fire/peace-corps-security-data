"""Complete synthetic municipal HTML, modeled on captured public documents."""
import copy
import html
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import public_infrastructure as m

NOW='2026-10-15T06:00:00Z'
COUNTRIES={'KG':dict(code='KG',name='Kyrgyz Republic',region='Central Asia',status='active')}
SOURCE=dict(id=m.SOURCE_ID,countryCodes=['KG'],language='ru')
POLICY=m.prepare_infrastructure_policy(dict(sourceId=m.SOURCE_ID,adapter='kg-bishkek-water',historyFrom='2025-10-16'),{},NOW)
TITLE='Внимание! В части Бишкека временно не будет воды'
BODY=('15 октября 2026 года будет прекращена подача питьевой воды с 10:00 до 20:00 в жилые дома и школы.\n'
      'Район отключения ограничен улицами: ул. Примерная — ул. Первая — западная сторона проспекта Второго.\n'
      'Временное отключение водоснабжения связано с ремонтными работами.\n'
      'МП «Бишкекводоканал» приносит извинения и просит жителей заранее запастись питьевой водой.')


def entry(identity=1,title=TITLE,date='2026-10-12'):
    day=m.dt.date.fromisoformat(date)
    return dict(id=identity,title=title,publishedDate=date,
                published=m.iso(m.dt.datetime.combine(day,m.dt.time(),m.ZONE)),url=m.HOST+'/ru/post/'+str(identity))


def listing(rows,search=False,other=''):
    cards=[]
    for row in rows:
        day=m.dt.date.fromisoformat(row['publishedDate']);month=list(m.MONTHS)[2*(day.month-1)].title()
        if search:
            cards.append('<a class="media" href="/ky/post/'+str(row['id'])+'"><h5>'+html.escape(row['title'])
                         +'</h5><span>'+str(day.day)+'-'+month+' '+str(day.year)+'-ж.</span></a>')
        else:
            cards.append('<div class="card"><div class="card-body-date">'+str(day.day)+' '+month+' '+str(day.year)
                +'</div><h6 class="card-title"><a href="/ru/post/'+str(row['id'])+'">'+html.escape(row['title'])+'</a></h6></div>')
    return ('<!doctype html><html><body>'+other+
        ('<div class="col-sm-6"><div class="section-title-in"><h5>Посты</h5></div>'+''.join(cards)+'</div>' if search
         else '<div class="newservice-news">'+''.join(cards)+'</div>')+'</body></html>').encode()


def document(identity=1,body=BODY,title=TITLE,canonical=None):
    return ('<!doctype html><html><head><meta property="og:url" content="'+(canonical or 'http://www.bishkek.gov.kg/ru/post/'+str(identity))
        +'"></head><body><section class="article post"><div class="article-info-date">3 дня назад</div>'
        +'<div class="article-title"><h5>'+html.escape(title)+'</h5></div><article id="lightgallery">'
        +''.join('<p>'+html.escape(line)+'</p>' for line in body.split('\n'))+'</article></section></body></html>').encode()


class Fetch:
    def __init__(self,rows,documents=None,overrides=None):
        self.rows=rows;self.documents=documents or {};self.overrides=overrides or {};self.calls=[]

    def __call__(self,policy,*args):
        url=policy['url'];self.calls.append(url)
        if url in self.overrides:
            raw=self.overrides[url]
            return (dict(httpStatus=503),None) if raw is None else (dict(httpStatus=200),raw)
        if '/post/' in url:
            identity=int(url.rsplit('/',1)[1]);value=self.documents.get(identity,document(identity))
            if value is None:return dict(httpStatus=503),None
            if value is False:return dict(httpStatus=404),b'<html><body>Not found</body></html>'
            return dict(httpStatus=200),value
        q=m.urllib.parse.parse_qs(m.urllib.parse.urlsplit(url).query);number=int(q.get('page',['1'])[0]);size=m.PAGE_SIZE
        return dict(httpStatus=200),listing(self.rows[(number-1)*size:number*size],search='q' in q)


def collect(rows=None,documents=None,now=NOW,records=None,previous=None,fetch=None,countries=COUNTRIES):
    rows=[entry()] if rows is None else rows
    fetch=fetch or Fetch(rows,documents)
    _,raw=fetch(POLICY)
    return m.collect_infrastructure(SOURCE,POLICY,raw,now,countries,previous or {},records or {},fetch,
                                   fixtures=True,history_from='2025-10-16')


class InfrastructureTests(unittest.TestCase):
    def test_request_ceiling_rejects_before_101st_fetch_and_retains_cached_response(self):
        calls=[]
        def fetch(policy,*args):
            calls.append(policy['url'])
            return dict(httpStatus=200),b'Complete synthetic response'
        req=m.Requests(POLICY,fetch,NOW,None)
        for identity in range(1,101):req.get(m.HOST+'/ru/post/'+str(identity))
        self.assertEqual(len(calls),100)
        req.get(m.HOST+'/ru/post/1')
        with self.assertRaisesRegex(ValueError,'budget exceeded'):req.get(m.HOST+'/ru/post/101')
        self.assertEqual(len(calls),100)

    def correction_document(self,identity=2,deferred=False,area=None):
        prefix=('МП «Бишкекводоканал» сообщает, что ранее запланированное отключение питьевой воды на 15 октября 2026 года '
                'в связи с ремонтными работами переносится на неопределенный срок.' if deferred else
                'МП «Бишкекводоканал» сообщает о переносе планового отключения питьевой воды с 15 на 16 октября 2026 года.')
        area=area or 'ул. Примерная — ул. Первая — западная сторона проспекта Второго.'
        lines=[prefix]
        if not deferred:lines.append('16 октября с 10:00 до 20:00 будет временно прекращена подача питьевой воды в жилые дома.')
        lines.extend(['Запланированный район отключения: '+area,'МП «Бишкекводоканал» приносит извинения.'])
        return document(identity,body='\n'.join(lines),title='Плановое отключение воды переносится')

    def test_actual_template_calendar_timezone_area_source_headline(self):
        row=collect()['incidents'][0]
        self.assertEqual(row['occurredAt'],'2026-10-15T04:00:00Z')
        self.assertEqual(row['sourceMetadata']['effectiveUntil'],'2026-10-15T14:00:00Z')
        self.assertEqual(row['reports'][0]['publishedAt'],'2026-10-11T18:00:00Z')
        self.assertEqual(row['reports'][0]['title'],TITLE)
        self.assertEqual(row['reports'][0]['language'],'ru')
        self.assertEqual(row['sourceMetadata']['publicationDatePrecision'],'day')
        self.assertIn('ул. Примерная',row['summary'])
        self.assertEqual((row['category'],row['severity'],row['state']),('infrastructure','unknown','reported'))
        self.assertEqual(row['location'],{'precision':'country'})
        self.assertFalse('admin1Code' in row or 'admin2Code' in row)

    def test_future_notice_is_pending_then_rechecked_at_start(self):
        first=collect(now='2026-10-14T06:00:00Z');self.assertEqual(first['incidents'],[])
        self.assertEqual([r['id'] for r in first['state']['infrastructurePending']],[1])
        result=collect([],previous=first['state'])
        self.assertEqual(result['incidents'][0]['state'],'reported')

    def test_interval_expiry_is_not_confirmed_restoration(self):
        old=collect()['incidents'][0]
        row=collect(records={old['id']:old},now='2026-10-15T15:00:00Z')['incidents'][0]
        self.assertEqual(row['state'],'expired');self.assertNotIn('restoredAt',row['sourceMetadata'])
        self.assertEqual(row['occurredAt'],old['occurredAt'])

    def test_explicit_midnight_is_next_day_but_unspecified_overnight_is_held(self):
        row=collect(documents={1:document(body=BODY.replace('20:00','00:00'))})['incidents'][0]
        self.assertEqual(row['sourceMetadata']['effectiveUntil'],'2026-10-15T18:00:00Z')
        self.assertFalse(collect(documents={1:document(body=BODY.replace('20:00','02:00'))})['incidents'])

    def test_explicit_2400_is_end_of_day_but_2401_is_invalid(self):
        row=collect(documents={1:document(body=BODY.replace('20:00','24:00'))})['incidents'][0]
        self.assertEqual(row['sourceMetadata']['effectiveUntil'],'2026-10-15T18:00:00Z')
        self.assertEqual(row['sourceMetadata']['sourceEndDate'],'2026-10-16')
        self.assertFalse(collect(documents={1:document(body=BODY.replace('20:00','24:01'))})['incidents'])

    def test_complete_two_date_and_shared_year_intervals(self):
        texts=[
            'С 10:00 15 октября 2026 года до 08:00 16 октября 2026 года будет прекращена подача питьевой воды.',
            '15 октября с 10:00 до 16 октября 08:00 2026 года будет прекращена подача питьевой воды.',
            '15 октября 2026 года с 10:00 до 08:00 16 октября будет прекращена подача питьевой воды.']
        for text in texts:
            row=collect(documents={1:document(body=text+'\n'+'\n'.join(BODY.splitlines()[1:]))})['incidents'][0]
            self.assertEqual(row['sourceMetadata']['effectiveUntil'],'2026-10-16T02:00:00Z')

    def test_impossible_dates_times_multiple_years_and_long_intervals_held(self):
        variants=[BODY.replace('15 октября','32 октября'),BODY.replace('10:00','25:00'),BODY.replace('20:00','20:99'),
                  BODY.replace('2026 года','года'),BODY+ '\n'+BODY.splitlines()[0],
                  BODY.replace('15 октября 2026 года','15 октября 2026 года и 16 октября 2026 года')]
        for body in variants:
            with self.subTest(body=body):self.assertFalse(collect(documents={1:document(body=body)})['incidents'])

    def test_area_marker_comma_inline_and_named_neighborhood(self):
        for body in [BODY.replace('Район отключения ограничен','Район отключения, ограниченный'),
                     BODY.replace('школы.\nРайон','школы.Район'),
                     BODY.replace(BODY.splitlines()[1],'Отключение водоснабжения затронет жилой массив Примерный.')]:
            row=collect(documents={1:document(body=body)})['incidents'][0]
            self.assertNotIn('приносит извинения',row['sourceMetadata']['serviceAreaOriginal'])

    def test_complete_multiline_area_and_exclusions_preserved(self):
        body=BODY.replace(BODY.splitlines()[1],'Район отключения:\n- ул. Примерная;\n- северная часть квартала;\n- 5-й микрорайон — частично.')
        area=collect(documents={1:document(body=body)})['incidents'][0]['sourceMetadata']['serviceAreaOriginal']
        self.assertIn('северная',area);self.assertIn('частично',area)

    def test_routine_hot_irrigation_possible_hypothetical_and_cancelled_held(self):
        variants=[(TITLE,BODY.replace('питьевой воды','горячей воды')),
                  ('Фестиваль воды',BODY),(TITLE,BODY.replace('будет прекращена','не будет прекращена')),
                  (TITLE,BODY.replace('будет прекращена','возможно будет прекращена')),
                  (TITLE,BODY+'\nЭто отключение отменено.'),(TITLE,BODY+'\nОтключение переносится.'),
                  (TITLE,BODY+'\nОтключение произойдет при условии согласования.'),
                  (TITLE,BODY.replace('Бишкекводоканал','Другая организация'))]
        for title,body in variants:
            self.assertFalse(collect(documents={1:document(body=body,title=title)})['incidents'])

    def test_tail_cancellation_is_not_truncated(self):
        body=BODY+'\n'+'контекст '*20000+'\nОтключение отменено.'
        self.assertFalse(collect(documents={1:document(body=body)})['incidents'])

    def test_missing_area_empty_article_image_only_and_incomplete_html(self):
        self.assertFalse(collect(documents={1:document(body=BODY.replace(BODY.splitlines()[1],''))})['incidents'])
        self.assertFalse(collect(documents={1:document(body='Подробности на изображении.')})['incidents'])
        with self.assertRaisesRegex(ValueError,'incomplete'):
            collect(documents={1:document().replace(b'</article>',b'')})

    def test_changed_same_post_end_preserves_original_evidence(self):
        old=collect()['incidents'][0]
        row=collect(documents={1:document(body=BODY.replace('20:00','21:00'))},records={old['id']:old},now='2026-10-15T07:00:00Z')['incidents'][0]
        self.assertEqual(row['reports'][1],old['reports'][0]);self.assertEqual(row['firstSeenAt'],old['firstSeenAt'])
        self.assertEqual(row['occurredAt'],old['occurredAt']);self.assertEqual(row['sourceMetadata']['effectiveUntil'],'2026-10-15T15:00:00Z')

    def test_changed_time_area_cancelled_or_removed_details_withhold_prior(self):
        old=collect()['incidents'][0]
        for body in [BODY.replace('Примерная','Другая'),BODY.replace('10:00','11:00'),BODY+'\nОтключение отменено.','Объявление удалено.']:
            row=collect(documents={1:document(body=body)},records={old['id']:old},now='2026-10-15T07:00:00Z')['incidents'][0]
            self.assertEqual((row['state'],row['evidenceStatus']),('expired','unverified'))
            self.assertEqual(row['reports'][1],old['reports'][0]);self.assertEqual(row['occurredAt'],old['occurredAt'])

    def test_a_b_a_and_stable_repeat_current_hash(self):
        old=collect()['incidents'][0]
        changed=collect(documents={1:document(body='Отменено.')},records={old['id']:old},now='2026-10-15T07:00:00Z')['incidents'][0]
        returned=collect(records={old['id']:changed},now='2026-10-15T08:00:00Z')['incidents'][0]
        self.assertEqual(returned['reports'][0]['contentHash'],old['reports'][0]['contentHash'])
        self.assertEqual((returned['state'],returned['evidenceStatus']),('reported','official'))
        self.assertEqual(collect(records={old['id']:returned},now='2026-10-15T09:00:00Z')['incidents'][0],returned)

    def test_exact_404_withholds_and_503_fails_without_mutating_archive(self):
        old=collect()['incidents'][0];snapshot=copy.deepcopy(old)
        row=collect(documents={1:False},records={old['id']:old})['incidents'][0]
        self.assertEqual((row['state'],row['evidenceStatus']),('expired','unverified'));self.assertEqual(row['reports'][1],old['reports'][0])
        with self.assertRaisesRegex(ValueError,'unavailable'):collect(documents={1:None},records={old['id']:old})
        self.assertEqual(old,snapshot)

    def test_original_publication_cannot_shift_on_correction(self):
        old=collect()['incidents'][0]
        with self.assertRaisesRegex(ValueError,'publication date changed'):
            collect([entry(date='2026-10-13')],records={old['id']:old})

    def test_general_unique_area_date_postponement_retains_evidence(self):
        old=collect()['incidents'][0]
        result=collect([entry(2,title='Отключение воды переносится',date='2026-10-13')],
                       documents={2:self.correction_document(deferred=True)},records={old['id']:old})
        row=result['incidents'][0]
        self.assertEqual((row['state'],row['evidenceStatus']),('cancelled','official'))
        self.assertEqual(row['reports'][1],old['reports'][0]);self.assertEqual(row['occurredAt'],old['occurredAt'])
        self.assertEqual(result['state']['infrastructureUnresolvedCorrectionIds'],[])
        again=collect([],documents={2:self.correction_document(deferred=True)},records={row['id']:row},previous=result['state'])
        self.assertEqual(again['incidents'][0],row)

    def test_negated_postponement_never_cancels_original_schedule(self):
        old=collect()['incidents'][0]
        negated=self.correction_document(deferred=True).replace('переносится на'.encode(),'не переносится на'.encode())
        result=collect([entry(2,title='Отключение воды переносится')],documents={2:negated},records={old['id']:old})
        self.assertEqual(result['incidents'],[old])
        self.assertEqual(result['state']['infrastructureUnresolvedCorrectionIds'],[2])

    def test_conditional_rescheduling_never_cancels_original_or_creates_replacement(self):
        old=collect()['incidents'][0]
        for conditional in [self.correction_document().replace('сообщает о'.encode(),'при условии согласования сообщает о'.encode()),
                            self.correction_document().replace(b'</article>','<p>Перенос возможен, если работы будут согласованы.</p></article>'.encode())]:
            result=collect([entry(2,title='Отключение воды переносится')],documents={2:conditional},records={old['id']:old})
            self.assertEqual(result['incidents'],[old]);self.assertEqual(result['state']['infrastructurePending'],[])
            self.assertEqual(result['state']['infrastructureUnresolvedCorrectionIds'],[2])

    def test_complete_body_revocation_or_denial_withholds_postponement(self):
        old=collect()['incidents'][0]
        for tail in ['Перенос отменен.','Сообщение о переносе опровергнуто.',
                     'Сообщение о переносе не соответствует действительности.']:
            for deferred in [True,False]:
                revoked=self.correction_document(deferred=deferred).replace(
                    b'</article>',('<p>'+tail+'</p></article>').encode())
                result=collect([entry(2,title='Отключение воды переносится')],documents={2:revoked},records={old['id']:old})
                self.assertEqual(result['incidents'],[old])
                self.assertEqual(result['state']['infrastructurePending'],[])
                self.assertEqual(result['state']['infrastructureUnresolvedCorrectionIds'],[2])

    def test_upcoming_reschedule_rechecks_both_until_original_and_replacement_dates(self):
        rows=[entry(2,title='Отключение воды переносится',date='2026-10-13'),entry()]
        docs={2:self.correction_document()}
        first=collect(rows,documents=docs,now='2026-10-14T06:00:00Z')
        self.assertFalse(first['incidents']);self.assertEqual(len(first['state']['infrastructurePendingCorrections']),1)
        second=collect([],documents=docs,previous=first['state'])
        row=second['incidents'][0];self.assertEqual(row['state'],'cancelled')
        self.assertEqual([x['id'] for x in second['state']['infrastructurePending']],[2])
        third=collect([],documents=docs,previous=second['state'],records={row['id']:row},now='2026-10-16T06:00:00Z')
        records={r['sourceMetadata']['recordId']:r for r in third['incidents']}
        self.assertEqual(records[1]['state'],'cancelled');self.assertEqual(records[2]['state'],'reported')
        self.assertEqual(records[2]['occurredAt'],'2026-10-16T04:00:00Z')

    def test_changed_side_or_ambiguous_pair_stays_unresolved_and_retried(self):
        old=collect()['incidents'][0]
        wrong=self.correction_document(deferred=True,area='ул. Примерная — ул. Первая — восточная сторона проспекта Второго.')
        result=collect([entry(2,title='Отключение воды переносится')],documents={2:wrong},records={old['id']:old})
        self.assertEqual(result['state']['infrastructureUnresolvedCorrectionIds'],[2])
        fetch=Fetch([],documents={2:wrong})
        again=collect([],records={old['id']:old},previous=result['state'],fetch=fetch)
        self.assertIn(m.HOST+'/ru/post/2',fetch.calls)
        self.assertEqual(again['state']['infrastructureUnresolvedCorrectionIds'],[2])
        duplicate=copy.deepcopy(old);duplicate['id']='another';duplicate['sourceMetadata']['recordId']=3
        duplicate['sourceMetadata']['listing']=entry(3)
        ambiguous=collect([entry(2,title='Отключение воды переносится')],documents={2:self.correction_document(deferred=True)},records={old['id']:old,'another':duplicate})
        self.assertEqual(ambiguous['state']['infrastructureUnresolvedCorrectionIds'],[2])

    def test_removed_or_changed_correction_does_not_certify_previous_cancellation(self):
        old=collect()['incidents'][0]
        result=collect([entry(2,title='Отключение воды переносится')],documents={2:self.correction_document(deferred=True)},records={old['id']:old})
        row=result['incidents'][0]
        changed=collect([],documents={2:document(2,body='Объявление удалено.')},records={row['id']:row},previous=result['state'])
        revised=changed['incidents'][0]
        self.assertEqual((revised['state'],revised['evidenceStatus']),('expired','unverified'))
        self.assertTrue({r['contentHash'] for r in row['reports']} <= {r['contentHash'] for r in revised['reports']})
        self.assertEqual(changed['state']['infrastructureUnresolvedCorrectionIds'],[2])

    def test_accepted_correction_404_retains_evidence_and_recovers_without_own_incident(self):
        old=collect()['incidents'][0]
        result=collect([entry(2,title='Отключение воды переносится')],documents={2:self.correction_document(deferred=True)},records={old['id']:old})
        row=result['incidents'][0]
        missing=collect([],documents={2:False},records={row['id']:row},previous=result['state'])
        withheld=missing['incidents'][0]
        self.assertEqual((withheld['state'],withheld['evidenceStatus']),('expired','unverified'))
        self.assertEqual(missing['state']['infrastructureUnresolvedCorrectionIds'],[2])
        self.assertEqual(missing['state']['infrastructureCorrectionQueue'],[entry(2,title='Отключение воды переносится')])
        receipt=next(r for r in missing['evidence'] if r['url']==m.HOST+'/ru/post/2')
        self.assertEqual(receipt['httpStatus'],404)
        self.assertEqual(withheld['reports'][0]['sourceMetadata']['rawResponseSha256'],receipt['sha256'])
        self.assertTrue({r['contentHash'] for r in row['reports']} <= {r['contentHash'] for r in withheld['reports']})
        restored=collect([],documents={2:self.correction_document(deferred=True)},records={withheld['id']:withheld},previous=missing['state'])
        recovered=restored['incidents'][0]
        self.assertEqual((recovered['state'],recovered['evidenceStatus']),('cancelled','official'))
        self.assertNotIn('automaticEvidenceState',recovered['sourceMetadata'])
        self.assertEqual(restored['state']['infrastructureUnresolvedCorrectionIds'],[])
        self.assertTrue({r['contentHash'] for r in withheld['reports']} <= {r['contentHash'] for r in recovered['reports']})

    def test_restored_correction_cannot_recertify_independently_changed_original(self):
        old=collect()['incidents'][0]
        result=collect([entry(2,title='Отключение воды переносится')],documents={2:self.correction_document(deferred=True)},records={old['id']:old})
        row=result['incidents'][0]
        changed=document(body='Оригинальное объявление удалено.')
        missing=collect([],documents={1:changed,2:False},records={row['id']:row},previous=result['state'])
        withheld=missing['incidents'][0]
        self.assertEqual(withheld['sourceMetadata']['automaticEvidenceState'],'original_notice_changed_after_rescheduling_requires_review')
        self.assertEqual(len(withheld['reports']),4)
        restored=collect([],documents={1:changed,2:self.correction_document(deferred=True)},records={withheld['id']:withheld},previous=missing['state'])
        revised=restored['incidents'][0]
        self.assertEqual((revised['state'],revised['evidenceStatus']),('expired','unverified'))
        self.assertEqual(restored['state']['infrastructureUnresolvedCorrectionIds'],[2])
        self.assertTrue({r['contentHash'] for r in withheld['reports']} <= {r['contentHash'] for r in revised['reports']})

    def test_removed_replacement_notice_withholds_both_old_association_and_replacement(self):
        result=collect([entry(2,title='Отключение воды переносится'),entry()],documents={2:self.correction_document()},now='2026-10-16T06:00:00Z')
        records={row['id']:row for row in result['incidents']}
        self.assertEqual(len(records),2)
        changed=collect([],documents={2:False},records=records,previous=result['state'],now='2026-10-16T07:00:00Z')
        self.assertEqual({(row['state'],row['evidenceStatus']) for row in changed['incidents']},{('expired','unverified')})
        for row in changed['incidents']:
            self.assertTrue({r['contentHash'] for r in records[row['id']]['reports']} <= {r['contentHash'] for r in row['reports']})
        self.assertEqual(changed['state']['infrastructureUnresolvedCorrectionIds'],[2])

    def test_retargeted_correction_cannot_certify_its_previous_association(self):
        old=collect()['incidents'][0]
        result=collect([entry(2,title='Отключение воды переносится')],documents={2:self.correction_document(deferred=True)},records={old['id']:old})
        first=result['incidents'][0]
        third_doc=document(3,body=BODY.replace('15 октября','14 октября'))
        third=collect([entry(3)],documents={3:third_doc})['incidents'][0]
        revised=self.correction_document(deferred=True).replace('15 октября'.encode(),'14 октября'.encode())
        changed=collect([],documents={2:revised,3:third_doc},records={first['id']:first,third['id']:third},previous=result['state'])
        rows={row['sourceMetadata']['recordId']:row for row in changed['incidents']}
        self.assertEqual((rows[1]['state'],rows[1]['evidenceStatus']),('expired','unverified'))
        self.assertEqual((rows[3]['state'],rows[3]['evidenceStatus']),('cancelled','official'))
        self.assertEqual(changed['state']['infrastructureUnresolvedCorrectionIds'],[2])

    def test_explicit_original_link_allows_changed_area_only_with_correct_original_date(self):
        old=collect()['incidents'][0]
        other='ул. Примерная — ул. Первая — восточная сторона проспекта Второго.'
        linked=self.correction_document(deferred=True,area=other).replace(b'</article>',b'<p><a href="/ru/post/1">Original notice</a></p></article>')
        result=collect([entry(2,title='Отключение воды переносится')],documents={2:linked},records={old['id']:old})
        self.assertEqual(result['incidents'][0]['state'],'cancelled')
        wrong_date=linked.replace('15 октября'.encode(),'14 октября'.encode())
        held=collect([entry(2,title='Отключение воды переносится')],documents={2:wrong_date},records={old['id']:old})
        self.assertEqual(held['state']['infrastructureUnresolvedCorrectionIds'],[2])
        self.assertEqual(held['incidents'][0],old)

    def test_pending_and_reconciliation_ceilings_fail_without_eviction(self):
        rows=[entry(1),entry(2)]
        with patch.object(m,'EXACT_LIMIT',1),self.assertRaisesRegex(ValueError,'upcoming interruption limit'):
            collect(rows,now='2026-10-14T06:00:00Z')
        first=collect(rows,now='2026-10-14T06:00:00Z')
        snapshot=copy.deepcopy(first['state'])
        with patch.object(m,'EXACT_LIMIT',1),self.assertRaisesRegex(ValueError,'reconciliation limit'):
            collect([],previous=first['state'])
        self.assertEqual(first['state'],snapshot)

    def test_unknown_host_identity_and_wrong_locale_fail(self):
        for canonical in ['https://evil.example/ru/post/1',m.HOST+'/ru/post/2',m.HOST+'/ky/post/1']:
            with self.assertRaisesRegex(ValueError,'identity or language'):collect(documents={1:document(canonical=canonical)})

    def test_no_private_article_body_or_contact_copy(self):
        row=collect(documents={1:document(body=BODY+'\nUNRELATED-CONTACT-SENTINEL')})['incidents'][0]
        self.assertNotIn('UNRELATED-CONTACT-SENTINEL',json.dumps(row))
        self.assertIn('rawResponseSha256',row['reports'][0]['sourceMetadata'])

    def test_search_uses_only_russian_column(self):
        other='<div class="col-sm-6"><div class="section-title-in"><h5>Посттор</h5></div><a class="media" href="/ky/post/9"><h5>воды</h5><span>1-Январь 2020-ж.</span></a></div>'
        self.assertEqual([r['id'] for r in m.listing(listing([entry()],True,other),NOW,'воды')],[1])

    def test_listing_missing_absolute_date_wrong_order_duplicate_fails(self):
        for raw in [listing([entry()]).replace('12 Октябрь 2026'.encode(),'3 дня назад'.encode()),
                    listing([entry(),entry()]),listing([entry(1,date='2026-10-11'),entry(2,date='2026-10-12')])]:
            with self.assertRaises(ValueError):m.listing(raw,NOW)

    def test_baseline_resumes_and_verifies_complete_ordered_anchor(self):
        rows=[entry(3,date='2026-10-13'),entry(2),entry(1,date='2026-10-11')]
        with patch.object(m,'PAGE_SIZE',2):
            a=collect(rows);self.assertEqual(a['state']['infrastructureBaseline']['nextPage'],2)
            b=collect(rows,previous=a['state']);self.assertEqual(b['state']['infrastructureBaseline']['searchIndex'],1)
            changed=collect([entry(4,date='2026-10-14')]+rows,previous=a['state'])
            self.assertTrue(changed['state']['infrastructureBaseline']['resetForChangedAnchor'])
            self.assertFalse(changed['state']['infrastructureBaseline']['complete'])

    def test_backlog_failed_page_or_oversize_is_atomic(self):
        with patch.object(m,'PAGE_SIZE',1),patch.object(m,'LIVE_PAGES',1),self.assertRaisesRegex(ValueError,'backlog'):
            collect([entry(),entry(2)])
        with patch.object(m,'MAX_RESPONSE',20),self.assertRaisesRegex(ValueError,'budget'):collect()
        fetch=Fetch([entry()],overrides={m.endpoint(1,'воды'):None})
        with self.assertRaisesRegex(ValueError,'unavailable'):collect(fetch=fetch)

    def test_inactive_country_and_out_of_history_are_not_public(self):
        self.assertFalse(collect(countries={})['incidents'])
        old=BODY.replace('15 октября 2026 года','15 октября 2025 года')
        self.assertFalse(collect(documents={1:document(body=old)})['incidents'])


if __name__=='__main__':unittest.main()
