"""Complete small factual fixtures; original complete warnings stay in the audit.

Provenance: artifacts/weather-relevance-20261015/all-weather-source-text.json.
Descriptions below are curated factual transformations, not article copies or
claimed translations. Exact-hash tests exercise the three reviewed originals.
"""
import copy
import datetime as dt
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_cap as cap
import public_data_pipeline as pipeline
import public_weather_relevance as weather

NOW = '2026-10-15T23:30:00Z'
COUNTRIES = {cc: dict(code=cc, name=name, region='Test region', status='active', reviewDue='2026-11-15')
             for cc, name in [('BJ', 'Benin'), ('GN', 'Guinea'), ('CM', 'Cameroon'), ('UG', 'Uganda'),
                              ('EC', 'Ecuador'), ('PY', 'Paraguay'), ('ME', 'Montenegro'),
                              ('MX', 'Mexico'), ('MW', 'Malawi'), ('CO', 'Colombia')]}
PROFILE_CASES = [
    ('cap-bj-meteo-fr', 'BJ', 'fr', 'Pluie/période humide',
     'Des pluies modérées avec des vents forts sont prévues. La vigilance jaune est maintenue.'),
    ('cap-gn-dnm-fr', 'GN', 'fr', 'Pluie/période humide',
     "Des averses sont prévues. Il y a des risques modérés de foudre et d'inondation."),
    ('cap-cm-meteo-en', 'CM', 'en', 'thunderstorm',
     'Moderate rain is expected to intensify, with a moderate risk of flooding in low-lying areas.'),
]


def message(source_id='cap-bj-meteo-fr', code='BJ', language='fr', event='Pluie/période humide',
            description='Des pluies modérées sont prévues. La vigilance jaune est maintenue.',
            instruction='Remain vigilant. Avoid flooded roads.', severity='Severe', identifier='m1',
            kind='Alert', reference='', sent='2026-10-15T21:00:00Z', extra=''):
    def tag(k, v): return '<' + k + '>' + escape(v) + '</' + k + '>'
    fields = ''.join(tag(k, v) for k, v in [('language', language), ('category', 'Met'), ('event', event),
        ('severity', severity), ('urgency', 'Expected'), ('certainty', 'Likely'),
        ('expires', '2026-10-16T06:00:00Z'), ('headline', event), ('description', description),
        ('instruction', instruction)])
    raw = ('<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">' +
        ''.join(tag(k, v) for k, v in [('identifier', identifier), ('sender', 'issuer.example'),
            ('sent', sent), ('status', 'Actual'), ('scope', 'Public'), ('msgType', kind),
            ('references', reference)]) + '<info>' + fields + '</info>' + extra + '</alert>').encode()
    return cap.parse_alert(dict(id=source_id, countryCodes=[code]), raw,
        'https://issuer.example/' + identifier, NOW, COUNTRIES, [])


def gdacs(alert='Green', event='WF', hash_suffix='original'):
    report_hash = hashlib.sha256(hash_suffix.encode()).hexdigest()
    return dict(id='gdacs-' + event + '-12345-MX', countryCode='MX', title='Provider event',
        summary='The provider lists an event; local human exposure is not established.',
        category='weather', severity={'Orange': 'moderate', 'Red': 'severe'}.get(alert, 'unknown'),
        state='reported', occurredAt='2026-10-15T00:00:00Z', updatedAt='2026-10-15T21:00:00Z',
        firstSeenAt=NOW, historical=False, sourceIds=['gdacs'], location={'precision': 'country'},
        reports=[dict(id='report-' + hash_suffix, sourceId='gdacs',
            incidentId='gdacs-' + event + '-12345-MX', contentHash=report_hash, excerpt='')],
        sourceMetadata=dict(eventType=event, eventId='12345', providerAlert=alert))


def routine_warning_history():
    initial = message(severity='Moderate', sent='2026-10-14T21:00:00Z')
    update = message(severity='Moderate', identifier='m2', kind='Update',
        reference='issuer.example,m1,2026-10-14T21:00:00Z')
    old = initial['record']
    return cap.apply_lifecycle([update], {old['id']: old}, NOW)[0]


class WeatherRelevanceTests(unittest.TestCase):
    def test_exact_reviewed_representations_require_matching_source_and_current_hash(self):
        for sid, cc, language, event, description in PROFILE_CASES:
            row = message(sid, cc, language, event, 'Legacy compact representation')['record']
            row['sourceMetadata'].pop('infoBlocksReportId')
            row['reports'][0]['sourceMetadata'].pop('infoBlocks')
            row['reports'][0]['contentHash'] = weather.REVIEWED_HASHES[sid]
            self.assertEqual(weather.decision(row)['reason'], 'cap_severity_narrative_conflict')
            row['reports'][0]['contentHash'] = 'a' * 64
            self.assertTrue(weather.decision(row)['public'])
            row['reports'].append(dict(row['reports'][0], contentHash=weather.REVIEWED_HASHES[sid],
                                      publishedAt='2026-10-14T21:00:00Z'))
            self.assertTrue(weather.decision(row)['public'], 'an old hash cannot suppress a later assertion')

    def test_future_equivalent_issuer_templates_are_held_from_complete_narratives(self):
        for sid, cc, language, event, description in PROFILE_CASES:
            row = message(sid, cc, language, event, description)['record']
            before = copy.deepcopy(row)
            self.assertEqual(weather.decision(row)['reason'], 'cap_severity_narrative_conflict')
            self.assertEqual(row, before)
            row['sourceIds'] = ['cap-other-issuer']
            self.assertTrue(weather.decision(row)['public'])

    def test_complete_other_language_block_can_establish_severe_forecast(self):
        extra = '<info><language>en</language><category>Met</category><event>Thunderstorm</event><severity>Severe</severity><description>Destructive winds could damage roofs and trees.</description></info>'
        row = message(extra=extra)['record']
        self.assertEqual(row['category'], 'weather')
        self.assertTrue(weather.decision(row)['public'])
        row = message(instruction='Des vents destructeurs sont attendus.')['record']
        self.assertTrue(weather.decision(row)['public'])
        row = message(instruction='No damaging winds are forecast. Avoid flooded roads.')['record']
        self.assertFalse(weather.decision(row)['public'])
        row = message(extra='<info><language>en</language><category>Met</category><event>Thunderstorm</event><severity>Severe</severity><parameter><valueName>awareness_level</valueName><value>3; orange; Severe</value></parameter></info>')['record']
        self.assertTrue(weather.decision(row)['public'])

    def test_unreviewed_template_cannot_remove_genuine_wind_flood_fire_or_heat(self):
        controls = [
            ('cap-ug-dms-en', 'UG', 'Thunderstorm Warning', 'Gusts can damage roofs and threaten fishermen.'),
            ('cap-ec-inamhi-es', 'EC', 'lluvia tormenta eléctrica', 'Moderate to heavy rain is forecast.'),
            ('cap-py-dmh-es', 'PY', 'tormenta eléctrica', 'Wind gusts around 90 km/h with hail are forecast.'),
            ('cap-me-meteo-xx', 'ME', 'Wind', 'Wind gusts exceed 17 m/s. Orange warning.'),
            ('cap-mx-smn-es', 'MX', 'Aviso de lluvias', 'A cold front may bring 75 to 150 mm of rain.'),
            ('cap-mw-met-en', 'MW', 'Strong winds', 'Strong lake winds threaten boat users.'),
            ('cap-co-ideam-es', 'CO', 'Forest fire warning', 'Moderate probability of forest fire.'),
        ]
        for sid, cc, event, text in controls:
            self.assertTrue(weather.decision(message(sid, cc, 'en', event, text,
                instruction='Floodwater may enter homes and affect roads.')['record'])['public'])
        self.assertTrue(weather.decision(message(event='Landslide warning',
            description='Moderate probability of landslide.')['record'])['public'])
        self.assertTrue(weather.decision(message(event='Dangerous heat',
            description='Extreme heat threatens all residents.')['record'])['public'])
        self.assertTrue(weather.decision(message(event='Flood warning', severity='Moderate')['record'])['public'])

    def test_invalid_complete_profile_xml_fails_instead_of_using_a_prefix(self):
        row = message()['record']; cap.info_blocks(row).append('<info>broken')
        with self.assertRaises(Exception): weather.decision(row)

    def test_report_backed_narratives_and_legacy_inline_blocks_give_same_decision(self):
        row = message()['record']; before = copy.deepcopy(row)
        self.assertEqual(weather.decision(row)['reason'], 'cap_severity_narrative_conflict')
        legacy = copy.deepcopy(row)
        legacy['sourceMetadata']['infoBlocks'] = copy.deepcopy(cap.info_blocks(row))
        legacy['sourceMetadata'].pop('infoBlocksReportId')
        self.assertEqual(weather.cap_blocks(legacy), weather.cap_blocks(row))
        self.assertEqual(weather.decision(legacy), weather.decision(row))
        self.assertEqual(row, before)

    def test_missing_malformed_or_conflicting_report_reference_fails_closed(self):
        original = message()['record']
        changes = [
            lambda r: r['sourceMetadata'].pop('infoBlocksReportId'),
            lambda r: r['sourceMetadata'].update(infoBlocksReportId='missing'),
            lambda r: r['sourceMetadata'].update(infoBlocksReportId=[]),
            lambda r: r['reports'].append(copy.deepcopy(r['reports'][0])),
            lambda r: r['reports'][0].update(sourceId='other-issuer'),
            lambda r: r['reports'][0].update(incidentId='other-incident'),
            lambda r: r['reports'][0].update(publishedAt='2026-10-14T21:00:00Z'),
            lambda r: r['reports'][0]['sourceMetadata'].update(sent='2026-10-14T21:00:00Z'),
            lambda r: r['reports'][0]['sourceMetadata'].update(recordId='other-warning'),
            lambda r: r['reports'][0]['sourceMetadata'].update(messageType='Cancel'),
            lambda r: r['reports'][0]['sourceMetadata'].pop('infoBlocks'),
            lambda r: r['sourceMetadata'].update(infoBlocks=['<info>different</info>']),
        ]
        for change in changes:
            row = copy.deepcopy(original); change(row)
            with self.assertRaisesRegex(ValueError, 'CAP infoBlocks reference'):
                weather.decision(row)

    def test_gdacs_low_fire_and_cyclone_need_impact_while_orange_red_remain(self):
        for event in ('WF', 'TC', 'DR'):
            for alert in ('Green', 'green', 'Unknown', None):
                self.assertEqual(weather.decision(gdacs(alert, event))['reason'], 'gdacs_human_impact_unverified')
            for alert in ('Orange', 'Red'):
                self.assertTrue(weather.decision(gdacs(alert, event))['public'])
        for event in ('FL', 'VO'):
            self.assertTrue(weather.decision(gdacs('Green', event))['public'])
        row = gdacs(); row['summary'] = 'A wildfire occurred near people. No houses were damaged.'
        self.assertFalse(weather.decision(row)['public'])

    def test_human_impact_requires_review_and_exact_source_quote_identity(self):
        row = gdacs(); report = row['reports'][0]
        report['excerpt'] = 'Officials ordered evacuation of the settlement because of this wildfire.'
        item = dict(reviewStatus='reviewed', kind='evacuation', countryCode='MX', eventId='12345',
                    reportId=report['id'], contentHash=report['contentHash'], quote=report['excerpt'])
        row['sourceMetadata']['humanImpactEvidence'] = [item]
        self.assertTrue(weather.decision(row)['public'])
        for key, value in [('reviewStatus', 'unreviewed'), ('countryCode', 'UG'), ('eventId', '54321'),
                           ('contentHash', 'a' * 64), ('reportId', 'missing'), ('quote', 'Invented impacts'),
                           ('kind', 'burnt_hectares')]:
            bad = copy.deepcopy(row); bad['sourceMetadata']['humanImpactEvidence'][0][key] = value
            self.assertFalse(weather.decision(bad)['public'], key)

    def test_retention_ledger_has_full_window_closed_schema_and_current_hash(self):
        old = gdacs(); old['occurredAt'] = '2026-01-01T00:00:00Z'; old['historical'] = True
        expired = copy.deepcopy(old); expired['id'] += '-outside'; expired['occurredAt'] = '2024-01-01T00:00:00Z'
        other = copy.deepcopy(old); other['id'] += '-inactive'; other['countryCode'] = 'XX'
        ledger = weather.exclusion_index({x['id']: x for x in (old, expired, other)}, {'MX'}, '2025-10-15T00:00:00Z')
        self.assertEqual(len(ledger), 1)
        self.assertEqual(set(ledger[0]), {'id', 'countryCode', 'occurredAt', 'reason', 'sourceId', 'contentHash',
                                         'provider', 'eventType', 'alertLevel', 'priorSeverity', 'priorContentHashes'})
        self.assertEqual(ledger[0]['alertLevel'], 'green')
        self.assertEqual(ledger[0]['contentHash'], old['reports'][0]['contentHash'])
        self.assertEqual(ledger[0]['priorContentHashes'], [])

    def test_skipped_publication_hashes_come_only_from_same_source_retained_evidence(self):
        row = gdacs(); current = row['reports'][0]['contentHash']
        old_report = dict(row['reports'][0], id='prior-report', contentHash='a' * 64)
        row['reports'].append(old_report)
        row['revisions'] = [{'previousReports': [old_report,
            dict(old_report, contentHash='b' * 64),
            dict(old_report, sourceId='unrelated-source', contentHash='c' * 64),
            dict(old_report, contentHash='not-a-hash')]}]
        entry = weather.exclusion_index({row['id']: row}, {'MX'}, '2025-10-15T00:00:00Z')[0]
        self.assertEqual(entry['contentHash'], current)
        self.assertEqual(entry['priorContentHashes'], ['a' * 64, 'b' * 64])

    def test_prior_occurrences_pair_modern_and_legacy_warning_times_with_exact_hashes(self):
        row = routine_warning_history()
        prior = row['reports'][1]
        expected = [dict(occurredAt=prior['publishedAt'], contentHash=prior['contentHash'])]
        def entry(value):
            return weather.exclusion_index({value['id']: value}, {'BJ'}, '2025-10-15T00:00:00Z')[0]
        before = copy.deepcopy(row)
        self.assertEqual(entry(row)['priorOccurrences'], expected)
        self.assertEqual(row, before, 'ledger construction must not modify retained evidence')
        # Legacy records predate per-report CAP metadata. Their exact warning ID
        # shape and retained report identity provide the narrower migration path.
        legacy = copy.deepcopy(prior)
        legacy.pop('sourceMetadata')
        legacy['id'] = legacy['id'].rsplit('-', 1)[0]
        row['reports'] = row['reports'][:1]
        row['revisions'] = [dict(previousReports=[legacy, copy.deepcopy(legacy)])]
        self.assertEqual(entry(row)['priorOccurrences'], expected)
        self.assertIn(expected[0]['contentHash'], entry(row)['priorContentHashes'])

    def test_prior_occurrences_reject_unrelated_cancelled_and_unproven_representations(self):
        original = routine_warning_history()
        cases = [
            ('foreign source', lambda row, report: report.update(sourceId='cap-other')),
            ('wrong incident', lambda row, report: report.update(incidentId='other-incident')),
            ('invalid hash', lambda row, report: report.update(contentHash='not-a-hash')),
            ('current hash', lambda row, report: report.update(contentHash=row['reports'][0]['contentHash'])),
            ('cancel control', lambda row, report: report['sourceMetadata'].update(messageType='Cancel')),
            ('cancel identity', lambda row, report: report.update(id=row['id']+'-cancel-123456789abc')),
            ('unsupported message', lambda row, report: report['sourceMetadata'].update(messageType='Ack')),
            ('sent mismatch', lambda row, report: report['sourceMetadata'].update(sent='2026-10-14T20:00:00Z')),
            ('unsupported time basis', lambda row, report: row['sourceMetadata'].update(eventTimeBasis='provider-impact-onset')),
            ('wrong provider', lambda row, report: row['sourceMetadata'].update(provider='another provider')),
            ('current occurrence mismatch', lambda row, report: row.update(occurredAt='2026-10-15T20:00:00Z')),
            ('severe narrative conflict', lambda row, report: row.update(severity='severe')),
        ]
        for name, change in cases:
            with self.subTest(name=name):
                row = copy.deepcopy(original)
                change(row, row['reports'][1])
                entries = weather.exclusion_index({row['id']: row}, {'BJ'}, '2025-10-15T00:00:00Z')
                self.assertTrue(entries)
                self.assertNotIn('priorOccurrences', entries[0])
        for identity in ('arbitrary-report', original['id']+'-cancel-123456789abc',
                         original['id']+'-report-unverified'):
            with self.subTest(legacy_id=identity):
                row = copy.deepcopy(original)
                row['reports'][1].pop('sourceMetadata')
                row['reports'][1]['id'] = identity
                entry = weather.exclusion_index({row['id']: row}, {'BJ'}, '2025-10-15T00:00:00Z')[0]
                self.assertNotIn('priorOccurrences', entry)

    def test_prior_occurrence_dates_stay_inside_history_and_precede_current_warning(self):
        for at in ('2025-10-14T23:59:59Z', '2026-10-15T21:00:00Z', '2026-10-15T22:00:00Z',
                   '2026-10-14T21:00:00', 'invalid-time'):
            with self.subTest(at=at):
                row = routine_warning_history()
                row['reports'][1]['publishedAt'] = at
                row['reports'][1]['sourceMetadata']['sent'] = at
                entry = weather.exclusion_index({row['id']: row}, {'BJ'}, '2025-10-15T00:00:00Z')[0]
                self.assertNotIn('priorOccurrences', entry)
        row = routine_warning_history()
        row['reports'][1]['publishedAt'] = '2025-10-15T00:00:00Z'
        row['reports'][1]['sourceMetadata']['sent'] = '2025-10-15T00:00:00Z'
        self.assertEqual(weather.exclusion_index({row['id']: row}, {'BJ'}, '2025-10-15')[0]
                         ['priorOccurrences'][0]['occurredAt'], '2025-10-15T00:00:00Z')

    def test_orange_to_green_retains_real_hazard_history_and_complete_update(self):
        old = gdacs('Orange'); new = gdacs('Green', hash_suffix='update')
        new['updatedAt'] = '2026-10-15T22:00:00Z'
        kept = weather.preserve_significant_history(new, old)
        self.assertTrue(weather.decision(kept)['public']); self.assertEqual(kept['state'], 'expired')
        self.assertEqual(kept['severity'], 'moderate')
        self.assertEqual(kept['sourceMetadata']['lowerRelevanceUpdate']['sourceMetadata']['providerAlert'], 'Green')
        self.assertEqual({r['contentHash'] for r in kept['reports']}, {old['reports'][0]['contentHash'], new['reports'][0]['contentHash']})
        replay = weather.preserve_significant_history(new, kept)
        self.assertEqual(replay, kept)

    def test_new_held_warning_retained_then_promoted_and_cancelled_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); data = root / 'docs/data'
            source = dict(id='cap-bj-meteo-fr', kind='cap', language='fr', countryCodes=['BJ'])
            for path, value in {
                'config/countries.json': [COUNTRIES['BJ']], 'config/sources.json': [source],
                'config/collection-policy.json': {'sources': [dict(sourceId=source['id'], mode='official', cadenceMinutes=15, url='https://issuer.example/feed')]},
                'config/country-iso3.json': {}, 'config/geo/index.json': [],
                'docs/data/snapshot.json': dict(coverage=[], history={'from': '2025-10-15T00:00:00Z'}),
            }.items(): pipeline.save(root / path, value)
            def run(messages, minutes):
                with patch.object(pipeline, 'fetch', return_value=({'sourceId': source['id'], 'httpStatus': 200}, b'complete fixture')), patch.object(cap, 'collect_cap', return_value=messages):
                    pipeline.run(root, dt.datetime.fromisoformat(NOW.replace('Z', '+00:00')) + dt.timedelta(minutes=minutes))
            initial = message(); run([initial], 0)
            iid = initial['record']['id']
            self.assertEqual(pipeline.load(data / 'snapshot.json')['incidents'], [])
            retained = pipeline.read_archive(data, COUNTRIES)[iid]
            self.assertEqual(retained['reports'], initial['record']['reports'])
            self.assertEqual(len(pipeline.load(data / 'retained-weather/BJ.json')), 1)
            snapshot = pipeline.load(data / 'snapshot.json')
            self.assertEqual(snapshot['collector']['incidentScope'], weather.SCOPE)
            self.assertEqual(snapshot['collector']['weatherExclusions'][0]['contentHash'], initial['control']['contentHash'])
            update = message(identifier='m2', kind='Update', reference='issuer.example,m1,2026-10-15T21:00:00Z',
                sent='2026-10-15T22:00:00Z', description='Des vents destructeurs sont attendus.')
            run([update], 20)
            self.assertEqual(len(pipeline.load(data / 'snapshot.json')['incidents']), 1)
            self.assertEqual(pipeline.load(data / 'retained-weather/BJ.json'), [])
            cancel = message(identifier='m3', kind='Cancel', reference='issuer.example,m2,2026-10-15T22:00:00Z', sent='2026-10-15T23:00:00Z')
            run([cancel], 40)
            current = pipeline.read_archive(data, COUNTRIES)[iid]
            self.assertEqual(current['state'], 'cancelled'); self.assertEqual(current['firstSeenAt'], NOW)
            self.assertEqual(len(current['reports']), 3)
            self.assertEqual(len(pipeline.load(data / 'history.json')), 1)
            self.assertEqual(pipeline.load(data / 'snapshot.json')['collector']['weatherExclusions'], [])

    def test_severe_to_same_severity_routine_update_preserves_public_history(self):
        severe = message(description='Des vents destructeurs sont attendus.')
        old = severe['record']
        update = message(identifier='m2', kind='Update', reference='issuer.example,m1,2026-10-15T21:00:00Z', sent='2026-10-15T22:00:00Z')
        new = cap.apply_lifecycle([update], {old['id']: old}, NOW)[0]
        self.assertFalse(weather.decision(new)['public'])
        kept = weather.preserve_significant_history(new, old)
        self.assertTrue(weather.decision(kept)['public'])
        self.assertEqual(kept['state'], 'expired')
        self.assertEqual(len(kept['sourceMetadata']['capMessageKeys']), 2)
        cancel = message(identifier='m3', kind='Cancel', reference='issuer.example,m2,2026-10-15T22:00:00Z', sent='2026-10-15T23:00:00Z')
        cancelled = cap.apply_lifecycle([cancel], {old['id']: kept}, NOW)[0]
        self.assertTrue(weather.decision(cancelled)['public'])
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(len(cancelled['reports']), 3)


if __name__ == '__main__':
    unittest.main()
