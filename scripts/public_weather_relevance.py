"""Versioned security-feed relevance; retained observations never mean safe.

Only three reviewed CAP issuer templates are interpreted. Other severe CAP
warnings retain the existing eligibility rule. GDACS low-alert fire/cyclone/drought
observations require reviewed, cited human exposure/impact before publication.
"""
import copy
import datetime as dt
import re
import unicodedata
import xml.etree.ElementTree as ET

SCOPE = 'security-weather-v2'
CAP = 'CAP 1.2'
REASONS = frozenset({'routine_cap_weather', 'cap_severity_narrative_conflict',
                     'gdacs_human_impact_unverified'})
REVIEWED_HASHES = {
    'cap-bj-meteo-fr': '8c1fe4fa6cf3a17fd7d6eaea8f4f7e6d7727cab829aabdf29c0428b7df703d1b',
    'cap-gn-dnm-fr': 'f1074e7e0412ff7661499776df42e90442360aaf93a2d8006b6c2a7e26ce8c28',
    'cap-cm-meteo-en': '0916e68fae9c1915ed5fb937f3dec3f0430a2eae2275393c9c20c8f7ef4912ba',
}
PROFILES = {
    'cap-bj-meteo-fr': r'\bvigilance jaune\b',
    'cap-gn-dnm-fr': r'\brisques? moderes? de (?:foudre|inondation)',
    'cap-cm-meteo-en': r'\bmoderate risk of flooding\b',
}
IMPACT_KINDS = frozenset({'population_exposed', 'evacuation', 'injury', 'fatality',
                         'occupied_buildings_damaged', 'essential_access_disrupted'})


def normalized(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                           if not unicodedata.combining(c)).split())


def source_hash(row, source_id):
    """Current assertion's source hash, not any old hash in its lifecycle chain."""
    reports = [r for r in row.get('reports', []) if r.get('sourceId') == source_id]
    meta = row.get('sourceMetadata', {})
    if meta.get('provider') == CAP and meta.get('sent'):
        reports = [r for r in reports if r.get('publishedAt') == meta['sent']]
    for report in reports:
        value = report.get('contentHash', '')
        if re.fullmatch('[0-9a-f]{64}', value):
            return value
    return None


def cap_blocks(row):
    blocks = row.get('sourceMetadata', {}).get('infoBlocks')
    if not isinstance(blocks, list) or not blocks:
        return []
    result = []
    for value in blocks:
        if not isinstance(value, str) or '<!DOCTYPE' in value.upper() or '<!ENTITY' in value.upper():
            raise ValueError('CAP relevance requires complete safe information blocks')
        node = ET.fromstring(value)
        if node.tag != '{urn:oasis:names:tc:emergency:cap:1.2}info':
            raise ValueError('CAP relevance information block is not CAP 1.2')
        result.append({
            key: node.findtext('{*}' + key, default='')
            for key in ('language', 'event', 'headline', 'description', 'instruction')
        } | {'parameters': [(p.findtext('{*}valueName', default=''), p.findtext('{*}value', default=''))
                            for p in node.findall('{*}parameter')]})
    return result


def explicit_severe_warning(blocks):
    """Positive override for the three profiles, never a global hazard classifier."""
    for block in blocks:
        for key, value in block['parameters']:
            if normalized(key) in ('awareness_level', 'vigilance', 'alert_level'):
                if re.search(r'\b(?:orange|red|rouge)\b', normalized(value)):
                    return True
        text = normalized(block['description'] + '\n' + block['instruction'])
        # Do not interpret ordinary precautionary advice as an observed impact.
        # These explicit destructive/high-impact statements also admit forecasts.
        for sentence in re.split(r'[.!?;\n]', text):
            if re.search(r'\b(?:no|not|without|ne .{0,30} pas|aucun\w*|sans)\b', sentence):
                continue
            if re.search(r'\b(?:damaging|destructive|violent) (?:wind|gust)|'
                         r'\b(?:winds?|gusts?)\b.{0,60}\b(?:damag\w*|destroy\w*)\b|'
                         r'\bflash flood|\b(?:major|severe) flooding\b|'
                         r'\b(?:homes?|houses?|buildings?|roads?) (?:are |were |have been )?flooded\b|'
                         r'\bevacuations? (?:ordered|required|underway)\b|'
                         r'\b(?:vents?|rafales?) (?:violents?|destructeurs?|destructrices?)\b|'
                         r'\b(?:crues? soudaines?|inondations? (?:majeures?|importantes?))\b|'
                         r'\b(?:habitations?|maisons?|routes?) (?:sont |seront )?inondees?\b|'
                         r'\b(?:ordre d.evacuation|evacuation obligatoire|degats importants)\b', sentence):
                return True
    return False


def cap_conflict(row, source_id, content_hash):
    if content_hash and REVIEWED_HASHES.get(source_id) == content_hash:
        return True
    if source_id not in PROFILES:
        return False
    blocks = cap_blocks(row)
    if not blocks:
        # Legacy compact projections have no complete narrative. They are not
        # new evidence for a template match; exact reviewed hashes still apply.
        return False
    wet_weather = re.compile(r'\b(?:pluie|orage|thunderstorm|rain|squall)')
    applicable = [b for b in blocks if wet_weather.search(normalized(b['event']))]
    conflict = any(re.search(PROFILES[source_id], normalized(b['description'])) for b in applicable)
    return conflict and not explicit_severe_warning(blocks)


def human_impact_supported(row):
    """Require a reviewed explicit statement linked to retained source evidence.

    No collector currently creates these reviews automatically. Provider country
    matches, burnt hectares, centroids and generic alert scores do not qualify.
    """
    meta = row.get('sourceMetadata', {})
    reports = {r['id']: r for r in row.get('reports', [])}
    evidence = meta.get('humanImpactEvidence', [])
    if not isinstance(evidence, list):
        return False
    for item in evidence:
        if not isinstance(item, dict):
            continue
        report = reports.get(item.get('reportId'))
        quote = item.get('quote')
        if (item.get('reviewStatus') == 'reviewed' and item.get('kind') in IMPACT_KINDS
                and item.get('countryCode') == row.get('countryCode')
                and str(item.get('eventId', '')) == str(meta.get('eventId', ''))
                and report and item.get('contentHash') == report.get('contentHash')
                and isinstance(quote, str) and quote.strip()
                and quote in report.get('excerpt', '')):
            return True
    return False


def decision(row):
    meta = row.get('sourceMetadata', {})
    source_ids = row.get('sourceIds', [])
    if row.get('category') != 'weather':
        return dict(public=True)
    is_cap = meta.get('provider') == CAP or row.get('id', '').startswith('cap-')
    if is_cap:
        source_id = next((sid for sid in source_ids if sid.startswith('cap-')), source_ids[0] if source_ids else '')
        content_hash = source_hash(row, source_id)
        reason = ('routine_cap_weather' if row.get('severity') != 'severe' else
                  'cap_severity_narrative_conflict' if cap_conflict(row, source_id, content_hash) else None)
        if reason:
            return dict(public=False, reason=reason, sourceId=source_id, contentHash=content_hash,
                        provider=CAP, eventType=None, alertLevel=None)
    if 'gdacs' in source_ids and meta.get('eventType') in ('WF', 'TC', 'DR'):
        alert = str(meta.get('providerAlert') or '').lower().strip()
        if alert in ('green', 'unknown', '') and not human_impact_supported(row):
            return dict(public=False, reason='gdacs_human_impact_unverified', sourceId='gdacs',
                        contentHash=source_hash(row, 'gdacs'), provider='GDACS',
                        eventType=meta['eventType'], alertLevel=alert or None)
    return dict(public=True)


def prior_cap_occurrences(row, verdict, evidence, prior_hashes, since):
    """Pair retained warning times with their own evidence, never cancellation times."""
    meta = row.get('sourceMetadata', {})
    if (verdict.get('reason') != 'routine_cap_weather' or row.get('severity') == 'severe'
            or meta.get('provider') != CAP or meta.get('eventTimeBasis') != 'provider-warning-issued'
            or meta.get('sent') != row.get('occurredAt')):
        return []
    def timestamp(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed if parsed.tzinfo is not None else None
        except ValueError:
            return None
    current = timestamp(row['occurredAt'])
    earliest = timestamp(since + 'T00:00:00Z' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', since) else since)
    if current is None or earliest is None:
        return []
    hashes, pairs = set(prior_hashes), set()
    legacy_id = re.compile('cap-' + re.escape(verdict['sourceId']) + r'-[a-f0-9]{24}-report')
    for report in evidence:
        if (report.get('sourceId') != verdict['sourceId'] or report.get('incidentId') != row['id']
                or report.get('contentHash') not in hashes):
            continue
        identity, published = report.get('id', ''), report.get('publishedAt')
        details = report.get('sourceMetadata', {})
        if not isinstance(identity, str) or '-cancel-' in identity or not isinstance(details, dict):
            continue
        modern = details.get('messageType') in ('Alert', 'Update') and details.get('sent') == published
        legacy = not details and legacy_id.fullmatch(identity) is not None
        previous = timestamp(published)
        if (modern or legacy) and previous is not None and earliest <= previous < current:
            pairs.add((published, report['contentHash']))
    if len(pairs) > 2_000:
        raise ValueError('CAP prior occurrence evidence exceeds bounded ledger')
    return [dict(occurredAt=at, contentHash=content_hash) for at, content_hash in sorted(pairs)]


def exclusion_index(records, country_codes, since):
    result = []
    for row in sorted(records.values(), key=lambda r: r['id']):
        if row['countryCode'] not in country_codes or row['occurredAt'] < since:
            continue
        verdict = decision(row)
        if verdict['public']:
            continue
        if not verdict.get('contentHash'):
            raise ValueError('Weather exclusion has no current source representation hash: ' + row['id'])
        evidence = list(row.get('reports', []))
        for revision in row.get('revisions', []):
            evidence.extend(revision.get('previousReports', []))
        prior_hashes = sorted({report['contentHash'] for report in evidence
            if report.get('sourceId') == verdict['sourceId']
            and re.fullmatch('[0-9a-f]{64}', report.get('contentHash', ''))
            and report['contentHash'] != verdict['contentHash']})
        entry = {key: value for key, value in verdict.items() if key != 'public'} | dict(
            id=row['id'], countryCode=row['countryCode'], occurredAt=row['occurredAt'],
            priorSeverity=row['severity'], priorContentHashes=prior_hashes)
        occurrences = prior_cap_occurrences(row, verdict, evidence, prior_hashes, since)
        if occurrences:
            entry['priorOccurrences'] = occurrences
        result.append(entry)
    return result


def annotate(row):
    verdict = decision(row)
    meta = row.get('sourceMetadata', {})
    if not verdict['public']:
        row.setdefault('sourceMetadata', {})['weatherRelevance'] = dict(policy=SCOPE, **verdict)
    elif 'weatherRelevance' in meta:
        del meta['weatherRelevance']


def preserve_significant_history(row, old):
    """End a previously qualifying hazard when its provider lowers relevance.

    Keep the original significant hazard and footprint visible as expired, with
    the complete newer report and metadata attached. A later qualifying update
    can replace it normally. A correction/retraction is not a routine downgrade.
    """
    if (not old or row.get('category') != 'weather' or old.get('category') != 'weather'
            or not decision(old)['public'] or decision(row)['public']
            or row.get('state') in ('cancelled', 'resolved')
            or row.get('sourceMetadata', {}).get('messageType') == 'Cancel'):
        return row
    meta = row.get('sourceMetadata', {})
    prior_meta = old.get('sourceMetadata', {})
    # Only provider warning downgrades, never arbitrary news/impact corrections.
    if not (meta.get('provider') == CAP and prior_meta.get('provider') == CAP
            or 'gdacs' in row.get('sourceIds', []) and 'gdacs' in old.get('sourceIds', [])
            and meta.get('eventType') in ('WF', 'TC', 'DR') and meta.get('eventType') == prior_meta.get('eventType')):
        return row
    result = copy.deepcopy(old)
    result.update(state='expired', updatedAt=row['updatedAt'])
    reports = {}
    for report in row['reports'] + old['reports']:
        reports.setdefault((report['id'], report['contentHash']), copy.deepcopy(report))
    result['reports'] = list(reports.values())
    retained_meta = copy.deepcopy(meta)
    retained_meta.pop('weatherRelevance', None)
    result.setdefault('sourceMetadata', {})['lowerRelevanceUpdate'] = dict(
        at=row['updatedAt'], severity=row['severity'], title=row['title'], summary=row['summary'],
        location=copy.deepcopy(row['location']), sourceMetadata=retained_meta,
        reason=decision(row)['reason'])
    if meta.get('capMessageKeys'):
        result['sourceMetadata']['capMessageKeys'] = list(dict.fromkeys(
            prior_meta.get('capMessageKeys', []) + meta['capMessageKeys']))
    return result
